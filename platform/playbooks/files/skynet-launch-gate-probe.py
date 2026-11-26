"""The launch-gate probe that deploy-skynet.yml's verify play runs inside skynet's container.

    python -I -c <this file> anon|caller

One POST to <SKYNET_LAUNCH_GATE_URL>/v1/launch with an empty JSON body, in one of two modes:
    anon    no Authorization header. Expected: 401 unauthorized. This mode never reads
            SKYNET_LAUNCH_GATE_TOKEN, so the request that proves the 401 cannot carry the token.
    caller  Authorization: Bearer <SKYNET_LAUNCH_GATE_TOKEN>. Expected: 400 bad_request. The gate
            refuses the empty body after authenticating, so no launch starts.

Output, one line:
    <status> <error>   the gate's HTTP status and the error field of its JSON body
    <status>           the gate's HTTP status alone, when the body carries no error field
    env-missing        the URL is unset, or caller mode runs without the token (exit 0, nothing sent)
    bad-mode           the mode is neither anon nor caller (exit 2, nothing sent)

The error field is printed only when it is a lowercase word (a-z and _, up to 64 characters). Anything
else prints as '?', so the gate's reply cannot put a second line into the output. A redirect is not
followed: its status is printed and the caller fails on it, so the Authorization header never reaches
the address a redirect names.

The URL and the token come from the container's environment. Neither is printed.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

# The gate's error codes are lowercase words. Any other text is printed as '?'.
ERROR_CODE = re.compile(r"[a-z_]{1,64}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Returns no request for a redirect, so the 3xx surfaces as an HTTPError carrying its status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
# A redirect would carry the Authorization header to the address it names, so none is followed.
opener = urllib.request.build_opener(NoRedirect)
try:
    response = opener.open(request, timeout=10)
    status, body = response.status, response.read()
except urllib.error.HTTPError as error:
    status, body = error.code, error.read()
try:
    code = json.loads(body.decode("utf-8")).get("error", "")
except (ValueError, AttributeError):
    code = ""
if not isinstance(code, str) or (code and not ERROR_CODE.fullmatch(code)):
    code = "?"
print(f"{status} {code}" if code else str(status))
