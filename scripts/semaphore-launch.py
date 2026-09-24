#!/usr/bin/env python3
"""Launch one Semaphore template through the API, with check mode enforced BEFORE launch.

Semaphore starts a task the moment it is created and has no "create paused", so a
check-mode run cannot be made safe by reading the task back and stopping it: the stop can
arrive after the task has written secrets or started a deploy (docs/MISTAKES.md 3.8, review
of PR #220). The gate is therefore this launcher, before the POST:

  1. the check/diff flags are placed where the server reads them - inside `params`
     (Semaphore v2.17 db/Task.go AnsibleTaskParams); a top-level `dry_run` is ignored;
  2. a dry run is refused on any server version whose `params` shape has not been
     verified (VERIFIED_DRY_RUN_VERSIONS);
  3. survey values must be the template's own declared survey fields.

After the POST, the recorded task is read back as a tripwire: if the server did not
record check mode the task is stopped and the launch exits non-zero. That catches a
server change; it is not what makes the launch safe.

  scripts/semaphore-launch.py --template "Deploy agentgateway (Dev)" --set service_branch=dev \\
      --dry-run --url https://semaphore.uhstray.io < <token-file>

The operator token arrives on stdin (platform/semaphore/README.md, API path).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Exact server releases whose task body was read from source and whose check-mode flag was
# confirmed recorded (task params.dry_run). Exact, not a series: another patch release may
# read the flag differently, and a mis-read flag means a real run (review of PR #220).
VERIFIED_DRY_RUN_VERSIONS = {"v2.17.31"}
TERMINAL = {"success", "error", "stopped"}


class Refusal(Exception):
    """A reason to stop, with no credential in it."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would copy the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, url, project, token):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise Refusal("Semaphore URL must be a plain HTTPS origin")
        if not token:
            raise Refusal("An operator token is required on stdin")
        self.base, self.project, self.token = url.rstrip("/"), project, token
        self.open = urllib.request.build_opener(NoRedirect).open

    def __call__(self, path, body=None, project_scoped=True):
        prefix = f"/api/project/{self.project}" if project_scoped else "/api"
        req = urllib.request.Request(self.base + prefix + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with self.open(req, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise Refusal(f"Semaphore HTTP {error.code} on {path}; body suppressed") from None


def parse_settings(pairs):
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise Refusal("Each --set must be KEY=VALUE")
        settings[key] = value
    return settings


def build_task(template, project, settings, dry_run, message):
    """The exact POST body. Check mode lives in `params`, never at the top level."""
    declared = {v.get("name") for v in template.get("survey_vars") or []}
    unknown = sorted(set(settings) - declared)
    if unknown:
        raise Refusal(f"Not survey fields of {template['name']!r}: {', '.join(unknown)}")
    body = {"project_id": project, "template_id": template["id"], "message": message}
    if settings:
        body["environment"] = json.dumps(settings)
    if dry_run:
        body["params"] = {"dry_run": True, "diff": True}
    return body


def require_dry_run_support(version):
    # Semaphore reports e.g. "v2.17.31-02309ba-1774450250": the release is the first field.
    if str(version).split("-")[0] not in VERIFIED_DRY_RUN_VERSIONS:
        raise Refusal(f"Check mode is unverified for Semaphore {version}; confirm where it reads the "
                      "flag, then add the version to VERIFIED_DRY_RUN_VERSIONS")


def launch(api, template_name, settings, dry_run, wait=True, timeout=1800):
    matches = [t for t in api("/templates") if t.get("name") == template_name]
    if len(matches) != 1:
        raise Refusal(f"Expected one template named {template_name!r}")
    template = api(f"/templates/{matches[0]['id']}")
    if dry_run:
        require_dry_run_support((api("/info", project_scoped=False) or {}).get("version", "unknown"))
    busy = [t["id"] for t in api("/tasks/last")
            if t.get("template_id") == template["id"] and t.get("status") not in TERMINAL]
    if busy:
        raise Refusal(f"{template_name!r} already has a running task: {busy}")
    body = build_task(template, api.project, settings, dry_run,
                      f"{'Check-mode run' if dry_run else 'Run'} via semaphore-launch.py")
    task = api("/tasks", body)
    task_id = task.get("id")
    if not isinstance(task_id, int):
        raise Refusal("Task identity unavailable; check Semaphore before relaunching")
    # Printed at once: from here the task may be running, and this id is the only handle on it.
    print(f"Task {task_id} launched ({'check mode' if dry_run else 'real run'})", flush=True)
    if dry_run:
        try:
            recorded = (api(f"/tasks/{task_id}").get("params") or {}).get("dry_run") is True
        except Refusal:
            raise Refusal(f"Task {task_id}: launched, but its check mode could not be read back. "
                          f"Treat it as running for real until task {task_id} is inspected") from None
        if not recorded:
            api(f"/tasks/{task_id}/stop", {})
            raise Refusal(f"Task {task_id}: the server did not record check mode; stop requested. "
                          "The server's task format changed - update this launcher before relaunching")
    if not wait:
        return task_id, None
    deadline = time.monotonic() + timeout
    while task.get("status") not in TERMINAL:
        if time.monotonic() > deadline:
            raise Refusal(f"Task {task_id} still running after {timeout}s")
        time.sleep(10)
        task = api(f"/tasks/{task_id}")
    return task_id, task["status"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", required=True, help="exact live template name")
    parser.add_argument("--set", action="append", default=[], help="KEY=VALUE survey value")
    parser.add_argument("--dry-run", action="store_true", help="Ansible check + diff mode")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", type=int, default=1)
    args = parser.parse_args()
    try:
        api = API(args.url, args.project, sys.stdin.read().strip())
        task_id, status = launch(api, args.template, parse_settings(args.set), args.dry_run, not args.no_wait)
        if status is None:
            return 0
        print(f"Task {task_id}: {status}")
        keep = ("TASK [", "fatal", "FAILED", "PLAY RECAP", '"msg"', "msg:", "WARNING")
        for line in (o.get("output", "") for o in api(f"/tasks/{task_id}/output") or []):
            if any(k in line for k in keep):
                print("  " + line[:300])
        return 0 if status == "success" else 1
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
