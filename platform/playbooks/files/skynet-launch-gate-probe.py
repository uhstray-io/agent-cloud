"""The launch-gate probe that deploy-skynet.yml's verify play runs inside skynet's container.

    python -I -c <this file> anon|caller

One POST to <SKYNET_LAUNCH_GATE_URL>/v1/launch with an empty JSON body, in one of two modes:
    anon    no Authorization header. Expected: 401 unauthorized. This mode never reads
            SKYNET_LAUNCH_GATE_TOKEN, so the request that proves the 401 cannot carry the token.
    caller  Authorization: Bearer <SKYNET_LAUNCH_GATE_TOKEN>. Expected: 400 bad_request. The gate
            refuses the empty body after authenticating, so no launch starts.

Output, one line:
    <status> <error>   the gate's HTTP status and the error field of its JSON body ('' when absent)
    env-missing        the URL is unset, or caller mode runs without the token (exit 0, nothing sent)
    bad-mode           the mode is neither anon nor caller (exit 2, nothing sent)

The URL and the token come from the container's environment. Neither is printed.
"""

import json
import os
import sys
import urllib.error
import urllib.request

mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode not in ("anon", "caller"):
    print("bad-mode")
    raise SystemExit(2)

url = os.environ.get("SKYNET_LAUNCH_GATE_URL")
# Only caller mode reads the token; the conditional keeps the anonymous request from ever seeing it.
token = os.environ.get("SKYNET_LAUNCH_GATE_TOKEN") if mode == "caller" else None
if not url or (mode == "caller" and not token):
    print("env-missing")
    raise SystemExit(0)

headers = {"Content-Type": "application/json"}
if mode == "caller":
    headers["Authorization"] = "Bearer " + token
request = urllib.request.Request(
    url.rstrip("/") + "/v1/launch",
    data=b"{}",
    headers=headers,
    method="POST",
)
try:
    response = urllib.request.urlopen(request, timeout=10)
    status, body = response.status, response.read()
except urllib.error.HTTPError as error:
    status, body = error.code, error.read()
try:
    code = json.loads(body.decode("utf-8")).get("error", "")
except (ValueError, AttributeError):
    code = ""
print(status, code)
