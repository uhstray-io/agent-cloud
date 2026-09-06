#!/usr/bin/env python3
"""Stage declared Postiz provider inputs in Semaphore; never source an env file.

Run without --apply for a provider-presence report. With --apply, read the
existing operator token from stdin, stage encrypted inputs, run the existing
seed template, then remove only the created inputs after a terminal result.
Use a dedicated seed environment and reserve it: Semaphore v2.17 has no configuration CAS.
"""

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_PLAYBOOK = "platform/playbooks/seed-postiz-secrets.yml"
# v2.17.31 pkg/task_logger/task_logger.go, IsFinished (rejected is not finished).
TERMINAL = {"success", "error", "stopped"}


class Refusal(Exception):
    """Safe, value-free error text."""


def provider_fields():
    """Derive the allow-list from the actual seed declaration and app template."""
    seed = (ROOT / SEED_PLAYBOOK).read_text()
    declaration = seed.split("    _seedable:\n", 1)[1].split("\n  tasks:", 1)[0]
    names = set(re.findall(r"^      - ([a-z_]+)$", declaration, re.M))
    template = (ROOT / "platform/services/postiz/deployment/templates/postiz.env.j2").read_text()
    fields = re.findall(r"^([A-Z_]+)=\{\{ secrets\.postiz_([a-z_]+)", template, re.M)
    return {env: "SEED_" + name.upper() for env, name in fields if name in names}


def parse_inputs(text, fields):
    selected = {}
    seen = set()
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match[1] not in fields:
            continue
        name, value = match.groups()
        if name in seen:
            raise Refusal(f"Duplicate selected field: {name}")
        seen.add(name)
        if value.startswith(("'", '"')):
            quote = value[0]
            # Deliberately literal; ambiguous escaped/multiline forms are refused.
            end = value.find(quote, 1)
            tail = value[end + 1:].strip()
            if end < 0 or "\\" in value[1:end] or (tail and not tail.startswith("#")):
                raise Refusal(f"Unsupported quoting in selected field: {name}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value or "\r" in value:
            raise Refusal(f"Unsupported control character in selected field: {name}")
        if value:
            selected[fields[name]] = value
    groups = {}
    for name, target in fields.items():
        groups.setdefault(name.split("_")[0], []).append(target)
    for provider, keys in groups.items():
        if any(key in selected for key in keys):
            missing = [key.removeprefix("SEED_") for key in keys if key not in selected]
            if missing:
                raise Refusal(f"Incomplete {provider} fields: {', '.join(missing)}")
    if not selected:
        raise Refusal("No complete provider credentials supplied")
    return selected


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, url, project, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise Refusal("Semaphore URL must be a plain HTTPS origin")
        if not token:
            raise Refusal("An existing authorized operator token is required on stdin")
        self.url = url.rstrip("/") + f"/api/project/{project}"
        self.token = token
        self.open = urllib.request.build_opener(NoRedirect).open

    def __call__(self, path, body=None):
        req = urllib.request.Request(self.url + path, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        }, data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "PUT" if path.startswith("/environment/") else "POST")
        try:
            with self.open(req, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise Refusal(f"Semaphore HTTP {error.code}; response body suppressed") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Refusal("Semaphore request outcome unavailable; do not retry a write blindly") from None


def environment_body(env, operations):
    # Preserve ALL fields returned by the server, replacing only operation data.
    return {**env, "secrets": operations}


def stage_and_seed(api, project, template_id, expected_env, values, timeout=600):
    template = api(f"/templates/{template_id}")
    if (template.get("playbook") != SEED_PLAYBOOK
            or template.get("name") not in {"Seed Postiz Secrets", "Seed Postiz Secrets (Dev)"}
            or template.get("app") != "ansible"
            or template.get("arguments") not in (None, "[]", [])
            or template.get("environment_id") != expected_env):
        raise Refusal("Template name, playbook or expected environment binding differs")
    env_path = f"/environment/{expected_env}"
    before = api(env_path)
    if before.get("project_id") != project or before.get("id") != expected_env:
        raise Refusal("Environment identity differs")
    secrets = before.get("secrets")
    if not isinstance(secrets, list):
        raise Refusal("Environment response has no secrets array; absence cannot be established")
    # Refuse every seed input, including providers absent from this source file.
    if any(item.get("name", "").startswith("SEED_") for item in secrets):
        raise Refusal("Existing encrypted SEED_ inputs require reconciliation before staging")
    for field in ("json", "env"):
        existing = json.loads(before.get(field) or "{}")
        if any(key.startswith(("SEED_", "seed_")) for key in existing):
            raise Refusal("Existing plaintext seed inputs require reconciliation before staging")
    templates = {item["id"]: item for item in api("/templates")}
    if any(item.get("environment_id") == expected_env and item.get("id") != template_id
           for item in templates.values()):
        raise Refusal("Seed environment is bound to other templates; provision a dedicated environment through code")
    for task in api("/tasks/last"):
        if task.get("status") not in TERMINAL:
            owner = templates.get(task.get("template_id"), {})
            if owner.get("environment_id") == expected_env:
                raise Refusal("Another task is using the seed environment")
    # A fresh equality check catches changes made during preflight. It is not CAS.
    if api(env_path) != before:
        raise Refusal("Environment changed during preflight")
    operations = [{"name": name, "type": "env", "secret": value, "operation": "create"}
                  for name, value in values.items()]
    # Print recovery metadata BEFORE the first write. The server may accept a
    # request whose response is lost, so absence of an acknowledgement proves nothing.
    print(f"Reconciliation scope: project {project}, environment {expected_env}; "
          f"inputs {', '.join(sorted(values))}", flush=True)
    api(env_path, environment_body(before, operations))
    staged = api(env_path)
    if {k: v for k, v in staged.items() if k != "secrets"} != {
            k: v for k, v in before.items() if k != "secrets"}:
        raise Refusal("Environment configuration changed; retain inputs for reconciliation")
    created = [item for item in staged.get("secrets", []) if item.get("name") in values]
    if (len(created) != len(values) or {item["name"] for item in created} != set(values)
            or any(item.get("type") != "env" or not isinstance(item.get("id"), int) for item in created)):
        raise Refusal("Encrypted input read-back differs; retain inputs for reconciliation")
    print("Created encrypted input IDs: " + ", ".join(str(item["id"]) for item in created), flush=True)
    now = api(f"/templates/{template_id}")
    bindings = ("id", "name", "playbook", "environment_id", "repository_id", "inventory_id", "arguments", "app")
    if any(now.get(key) != template.get(key) for key in bindings):
        raise Refusal("Seed template changed; retain inputs for reconciliation")
    # No retry/finally around submission: an uncertain response can mean a live
    # task still needs these inputs. Leave them encrypted and reconcile by task ID.
    task = api("/tasks", {"project_id": project, "template_id": template_id,
                          "message": "Seed declared Postiz provider credentials via encrypted inputs"})
    task_id = task.get("id")
    if not isinstance(task_id, int):
        raise Refusal("Task identity unavailable; retain inputs and reconcile before another submission")
    print(f"Seed task {task_id} submitted", flush=True)
    deadline = time.monotonic() + timeout
    while task.get("status") not in TERMINAL:
        if time.monotonic() > deadline:
            raise Refusal(f"Task {task_id} still nonterminal; encrypted inputs retained for reconciliation")
        time.sleep(5)
        task = api(f"/tasks/{task_id}")
    current = api(env_path)
    current_secrets = current.get("secrets", [])
    for item in created:
        if not any(all(entry.get(key) == item.get(key) for key in ("id", "name", "type"))
                   for entry in current_secrets):
            raise Refusal(f"Task {task_id} terminal but input identity changed; cleanup refused")
    deletions = [{"id": item["id"], "name": item["name"], "type": "env", "operation": "delete"}
                 for item in created]
    api(env_path, environment_body(current, deletions))
    remaining = api(env_path)
    if any(item.get("id") in {entry["id"] for entry in created} for item in remaining.get("secrets", [])):
        raise Refusal(f"Task {task_id} terminal but encrypted input cleanup incomplete")
    print(f"Seed task {task_id}: {task['status']}; temporary encrypted inputs removed", flush=True)
    if task["status"] != "success":
        raise Refusal("Seed failed; inspect credential-redacted task output before retrying")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--project", type=int)
    parser.add_argument("--template", type=int)
    parser.add_argument("--environment", type=int)
    args = parser.parse_args()
    try:
        values = parse_inputs(args.env_file.read_text(), provider_fields())
        providers = sorted({name.removeprefix("SEED_").split("_")[0] for name in values})
        print("Configured provider fields: " + ", ".join(providers))
        if args.apply:
            if not all((args.url, args.project, args.template, args.environment)):
                raise Refusal("Apply requires URL, project, template and expected environment")
            api = API(args.url, args.project, sys.stdin.read().strip())
            stage_and_seed(api, args.project, args.template, args.environment, values)
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Input or API shape refused; details suppressed to protect credentials", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
