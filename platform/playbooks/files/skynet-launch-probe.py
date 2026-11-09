"""The skynet launch probe, run inside skynet's container by probe-skynet-launch.yml.

    python -I -c <this file> <service> <step> <order> <timeout_s> [api_base]

One POST /skynet/runs for the service_onboarding graph, bounded to one registry step (from_order and to_order both
set to the step's order) in check mode. Then GET /skynet/runs/<run_id> every 5 s until the state is no longer
running or interrupted, or the timeout has passed. Then GET /api/project/<project>/tasks/last on Semaphore and count
the newest tasks whose message is "skynet <run_id> <step>", the tag skynet's launch gives a step's task.

The bearers and the Semaphore origin come from the container's own environment, never from argv:
    SKYNET_API_TOKEN                                    the bearer skynet checks on its own API
    SKYNET_SEMAPHORE_URL                                the Semaphore origin skynet's read identity uses
    SKYNET_SEMAPHORE_PROJECT, SKYNET_SEMAPHORE_TOKEN    the project, and svc-skynet's read-only token
The Semaphore certificate is verified against the default trust store, which the container's SSL_CERT_FILE
replaces. This script passes no CA file of its own, so it trusts what skynet trusts.

api_base defaults to skynet's listener inside the container; the test harness passes its own.

Output, exactly two lines, identifiers and words only, "-" where an answer carried no value:
    run <http_status> <error> <run_id> <state> <status>
    task <count> <error> <task_id> <task_status>
error is "-" unless the transport failed (the exception's class name), or, for the task list, the answer was not
200 ("http<code>"). The bearers, the headers, the response bodies and the run's failure text are never printed.
"""

import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse

SERVICE, STEP, ORDER, TIMEOUT = sys.argv[1:5]
API_BASE = sys.argv[5] if len(sys.argv) > 5 else "http://127.0.0.1:8080"
GRAPH = "service_onboarding"
POLL_SECONDS = 5
# While a run is in one of these it may still move: running (its thread is executing) or interrupted (no thread
# holds it; skynet resumes it at its next start). Every other state is final for this probe: finished, incomplete,
# failed, or waiting_on_human, which a person must act on. The probe reports the state and never decides on it.
MOVING = frozenset({"running", "interrupted"})


def _request(base, method, path, token, body=None):
    """One request, as (HTTP status, decoded JSON or None, error). The error is the transport exception's class name,
    and the status is None when it is set. The token and the raw body stay in this function."""
    parts = urllib.parse.urlsplit(base)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=10, context=ssl.create_default_context()
        )
    elif parts.scheme == "http":
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    else:
        raise SystemExit("the API base must be an http or https origin")
    headers = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, parts.path.rstrip("/") + path, body=data, headers=headers)
        response = connection.getresponse()
        status, raw = response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        return None, None, type(exc).__name__
    finally:
        connection.close()
    try:
        return status, json.loads(raw), None
    except ValueError:
        return status, None, None


def _word(value):
    """One token the output can carry. Anything absent, empty or containing whitespace is "-"."""
    text = "" if value is None else str(value)
    return text if text and not any(c.isspace() for c in text) else "-"


def _task_line(run_id):
    """Count the newest Semaphore tasks carrying this run's tag; when there is exactly one, name it."""
    base = os.environ["SKYNET_SEMAPHORE_URL"]
    project = os.environ["SKYNET_SEMAPHORE_PROJECT"]
    status, rows, error = _request(
        base, "GET", f"/api/project/{project}/tasks/last?count=200", os.environ["SKYNET_SEMAPHORE_TOKEN"]
    )
    if error is not None:
        return ["task", "-", _word(error), "-", "-"]
    if status != 200:
        return ["task", "-", _word(f"http{status}"), "-", "-"]
    if not isinstance(rows, list):
        return ["task", "-", "not-a-list", "-", "-"]
    tag = f"skynet {run_id} {STEP}"
    tagged = [row for row in rows if isinstance(row, dict) and row.get("message") == tag]
    if len(tagged) == 1:
        return ["task", "1", "-", _word(tagged[0].get("id")), _word(tagged[0].get("status"))]
    return ["task", str(len(tagged)), "-", "-", "-"]


def main():
    token = os.environ["SKYNET_API_TOKEN"]
    body = {
        "graph": GRAPH,
        "input": {"service": SERVICE, "mode": "check", "from_order": int(ORDER), "to_order": int(ORDER)},
    }
    post_status, posted, post_error = _request(API_BASE, "POST", "/skynet/runs", token, body)
    run_id = posted.get("run_id") if post_status == 202 and isinstance(posted, dict) else None
    if not isinstance(run_id, str):
        run_id = None
    state = status = None
    if run_id is not None:
        path = "/skynet/runs/" + urllib.parse.quote(run_id, safe=":")
        deadline = time.monotonic() + int(TIMEOUT)
        while True:
            # A 404 here is a run with no checkpoint yet, which is not an answer: keep polling until the deadline.
            code, seen, _ = _request(API_BASE, "GET", path, token)
            if code == 200 and isinstance(seen, dict) and isinstance(seen.get("state"), str):
                state, status = seen["state"], seen.get("status")
                if state not in MOVING:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(POLL_SECONDS, remaining))
    print("run", _word(post_status), _word(post_error), _word(run_id), _word(state), _word(status))
    task = _task_line(run_id) if run_id is not None else ["task", "-", "-", "-", "-"]
    print(*task)


main()
