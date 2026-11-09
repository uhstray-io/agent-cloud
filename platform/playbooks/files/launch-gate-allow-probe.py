"""The launch gate's allow-path probe, run inside the gate's container by probe-launch-gate.yml.

    python -I -c <this file> <tls name> <caller> <agent> <template> <target_service> <workflow_id> <step>
        [address] [cafile]

Two POST /v1/launch requests, one right after the other, with the same body: a dry run of one template as one
agent. The caller token is read from the container's environment (LAUNCH_GATE_CALLER_TOKEN_<CALLER>, with the
caller name upper-cased and - written as _), never from argv or the output. The address defaults to the gate's
own listener and the cafile to the step-ca bundle the gate's container carries, which are the production values.
The test harness passes its own; nothing else differs under test.

Output, one line per request, with statuses and identifiers only:
    first <status> <error> <task_id> <message>
    repeat <status> <error> <task_id>
A field the answer did not carry is "-". A transport failure is status "-" with the exception's class name as the
error. The token, the headers and the body are never printed.
"""

import http.client
import json
import os
import socket
import ssl
import sys

NAME, CALLER, AGENT, TEMPLATE, TARGET, WORKFLOW_ID, STEP = sys.argv[1:8]
ADDRESS = sys.argv[8] if len(sys.argv) > 8 else "127.0.0.1:8443"
CAFILE = sys.argv[9] if len(sys.argv) > 9 else "/certs/step-ca-bundle.crt"
HOST, _, PORT = ADDRESS.rpartition(":")

CONTEXT = ssl.create_default_context(cafile=CAFILE)
CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2


class PinnedConnection(http.client.HTTPSConnection):
    """Dial the listener's address, and verify its certificate as the declared name."""

    def connect(self):
        raw = socket.create_connection((HOST, int(PORT)), timeout=10)
        self.sock = CONTEXT.wrap_socket(raw, server_hostname=NAME)


def _payload(data):
    try:
        payload = json.loads(data)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _word(value):
    """A single token the output can carry: an error code or a task id. Anything else is "-"."""
    return value if isinstance(value, str) and value and not any(c.isspace() for c in value) else "-"


def launch(token, workflow_id):
    """One request. Returns (status, error, task id, message), each a string, "-" when absent."""
    body = json.dumps(
        {
            "agent": AGENT,
            "template": TEMPLATE,
            "workflow_id": workflow_id,
            "step": STEP,
            "dry_run": True,
            "survey": {"target_service": TARGET},
        }
    )
    connection = PinnedConnection(NAME, int(PORT), timeout=10)
    try:
        connection.request(
            "POST",
            "/v1/launch",
            body=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        status, data = response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        return "-", type(exc).__name__, "-", "-"
    finally:
        connection.close()
    payload = _payload(data)
    task_id = payload.get("task_id") if status == 201 else None
    if not (isinstance(task_id, int) and not isinstance(task_id, bool)):
        task_id = None
    message = payload.get("message") if status == 201 else None
    return (
        str(status),
        _word(payload.get("error")),
        str(task_id) if task_id is not None else "-",
        message if isinstance(message, str) and message and "\n" not in message else "-",
    )


TOKEN = os.environ["LAUNCH_GATE_CALLER_TOKEN_" + CALLER.upper().replace("-", "_")]
FIRST = launch(TOKEN, WORKFLOW_ID)
print("first", *FIRST)
REPEAT = launch(TOKEN, WORKFLOW_ID)
print("repeat", *REPEAT[:3])
