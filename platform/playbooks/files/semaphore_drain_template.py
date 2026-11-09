#!/usr/bin/env python3
"""Drain one Semaphore template's task history, a page at a time, before the template is deleted.

Deleting a template also removes its task history. On a template with thousands of runs that
DELETE outlived every timeout tried (Semaphore tasks 14206, 14211, 14223), so
retire-semaphore-templates.yml drains the history first and deletes the now-small template.

Stdlib only. The token comes from a file, never argv (ps shows argv). It is never printed.

Order, and why:
  1. The destination must be a loopback origin, http://127.0.0.1:<port>, with no path. Refused
     before any request, so the token is never sent anywhere else.
  2. The token file is read; an empty token is refused.
  3. Every template's newest page is read first. A running or queued task on any of them
     refuses the whole run before a single delete.
  4. Unless --dry-run: each template is drained. Read the newest page, delete its tasks, repeat
     until a page comes back empty. A task that starts running mid-drain stops the drain.

Output: progress lines on stderr (counts and ids only). The final JSON object on stdout.

Exit codes: 0 done or dry run; 2 refused input; 3 a task is running or queued; 4 a task DELETE
returned an unexpected status; 5 --max-pages reached with history left; 6 an unexpected GET
status, a malformed reply, or a transport failure.
"""

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.request

PAGE = 200
GET_TIMEOUT = 60
DELETE_TIMEOUT = 120
# Upstream TaskStatus.IsFinished (pkg/task_logger/task_logger.go): only these are finished. Any other
# status, including waiting_confirmation, confirmed and one this file has never seen, counts as
# not finished and refuses.
FINISHED = frozenset({"success", "error", "stopped"})
LOOPBACK = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")
POSITIVE = re.compile(r"[1-9][0-9]*")


class Stop(Exception):
    """A refusal or failure: the exit code and the one line that names it."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the token must not follow it to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# No environment proxy: a proxy set in the environment would receive the token.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def positive(text):
    if not POSITIVE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return int(text)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Drain a Semaphore template's task history.")
    parser.add_argument("--url", required=True, help="loopback origin, http://127.0.0.1:<port>")
    parser.add_argument("--project", required=True, type=positive)
    parser.add_argument("--template-id", dest="template_ids", action="append", type=positive, required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--dry-run", action="store_true", help="read the first pages and delete nothing")
    parser.add_argument("--max-pages", type=positive, default=500)
    args = parser.parse_args(argv)
    if len(set(args.template_ids)) != len(args.template_ids):
        parser.error("--template-id was given twice")
    return args


def check_destination(url):
    match = LOOPBACK.fullmatch(url)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise Stop(2, "refused: --url must be http://127.0.0.1:<port>, a loopback origin with no path")
    return url


def read_token(path):
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as error:
        raise Stop(2, f"cannot read --token-file ({error.strerror})") from None
    if not token:
        raise Stop(2, "--token-file is empty")
    return token


def call(method, url, token, timeout):
    request = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        raise Stop(6, f"{method} failed before a reply ({type(error).__name__})") from None


def history_page(base, project, template_id, token):
    url = f"{base}/api/project/{project}/templates/{template_id}/tasks/last?limit={PAGE}"
    status, body = call("GET", url, token, GET_TIMEOUT)
    if status != 200:
        raise Stop(6, f"template {template_id}: reading the newest history page returned {status}")
    try:
        tasks = json.loads(body)
    except ValueError:
        raise Stop(6, f"template {template_id}: history page is not JSON") from None
    if not isinstance(tasks, list) or not all(isinstance(t, dict) and isinstance(t.get("id"), int) for t in tasks):
        raise Stop(6, f"template {template_id}: history page has an unexpected shape")
    return tasks


def running_ids(tasks):
    return [task["id"] for task in tasks if task.get("status") not in FINISHED]


def drain_template(base, project, template_id, token, max_pages, progress):
    pages = deleted = 0
    while True:
        tasks = history_page(base, project, template_id, token)
        running = running_ids(tasks)
        if running:
            ids = ", ".join(str(task_id) for task_id in running)
            raise Stop(
                3,
                f"stopped: template {template_id} has running or queued task ids {ids} "
                f"after {deleted} deleted; the rest of its history was left in place",
            )
        if not tasks:
            return {"deleted": deleted, "pages": pages}
        pages += 1
        if pages > max_pages:
            raise Stop(
                5, f"template {template_id}: --max-pages {max_pages} reached with history left ({deleted} deleted)"
            )
        for task in tasks:
            task_id = task["id"]
            status, _ = call("DELETE", f"{base}/api/project/{project}/tasks/{task_id}", token, DELETE_TIMEOUT)
            if status == 204:
                deleted += 1
            elif status == 404:
                continue  # already gone
            elif status == 400 and task_id not in {t["id"] for t in history_page(base, project, template_id, token)}:
                # Semaphore answers 400 for a missing task and for a database error alike
                # (plan/development/18-semaphore-task-retention.md, "Why a presence check"). A
                # task the list no longer carries is gone; one it still carries is a failure.
                continue
            else:
                raise Stop(
                    4, f"template {template_id}: DELETE task {task_id} returned {status} ({deleted} deleted so far)"
                )
        progress(f"template {template_id}: page {pages} handled, {len(tasks)} listed; {deleted} deleted so far")


def run(args, progress):
    base = check_destination(args.url)
    token = read_token(args.token_file)
    first = {}
    found = {}
    for template_id in args.template_ids:
        first[template_id] = history_page(base, args.project, template_id, token)
        ids = running_ids(first[template_id])
        if ids:
            found[template_id] = ids
    if found:
        detail = "; ".join(
            f"template {template_id}: task ids {', '.join(str(task_id) for task_id in ids)}"
            for template_id, ids in found.items()
        )
        raise Stop(3, f"refused, nothing deleted: running or queued tasks on {detail}")
    if args.dry_run:
        return {
            "dry_run": True,
            "templates": {
                str(template_id): {
                    "first_page": len(first[template_id]),
                    "capped": len(first[template_id]) == PAGE,
                    "deleted": 0,
                    "pages": 0,
                }
                for template_id in args.template_ids
            },
        }
    templates = {
        str(template_id): drain_template(base, args.project, template_id, token, args.max_pages, progress)
        for template_id in args.template_ids
    }
    return {"dry_run": False, "templates": templates}


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    def progress(line):
        print(line, file=sys.stderr, flush=True)

    try:
        result = run(args, progress)
    except Stop as stop:
        print(f"semaphore_drain_template: {stop}", file=sys.stderr)
        return stop.code
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
