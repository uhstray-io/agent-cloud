"""The signed-in HTTP session every Plane configuration script runs on (shared, not run alone).

deploy-plane.yml and reconcile-plane-members.yml run `<engine> exec -i plane-api python -c` on
THIS file followed by the script that needs it, with one JSON document on stdin. It reads that
document into `cfg` (base, host, origin), and provides:

  fail(message)            print one JSON {"error"} line, never a credential, and exit 1
  call(method, path, ...)  one request with Plane's CSRF and cookies handled; returns
                           (status, Location, parsed JSON or None)
  form_post(path, fields)  a form POST that Plane answers with a redirect (sign-up, sign-in)

Cookies are kept by hand: with an https public URL Plane marks them Secure, and a cookie jar
would never send a Secure cookie to http://localhost:8000 (TLS ends at the platform edge).
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

cfg = json.loads(sys.stdin.readline())
BASE, HOST, ORIGIN = cfg["base"], cfg["host"], cfg["origin"]


def fail(message, **extra):
    """One JSON line, never a credential: the playbook shows it while the task stays no_log."""
    print(json.dumps({"error": message, **extra}))
    sys.exit(1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the sign-up/sign-in result is in the redirect itself


opener = urllib.request.build_opener(NoRedirect)
cookies = {}


def call(method, path, data=None, form=False):
    headers = {"Host": HOST, "Origin": ORIGIN, "Referer": ORIGIN + "/god-mode/"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if "csrftoken" in cookies:
        headers["X-CSRFToken"] = cookies["csrftoken"]
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    for header in resp.headers.get_all("Set-Cookie") or []:
        jar = SimpleCookie()
        jar.load(header)
        cookies.update({k: m.value for k, m in jar.items()})
    raw = resp.read()
    if b"CSRF Verification Failed" in raw:  # Plane renders this page with status 200
        fail(f"{method} {path}: CSRF verification failed")
    return resp.status, resp.headers.get("Location", ""), (json.loads(raw) if raw[:1] in (b"{", b"[") else None)


def form_post(path, fields):
    _, _, tok = call("GET", "/auth/get-csrf-token/")
    status, location, _ = call("POST", path, dict(fields, csrfmiddlewaretoken=tok["csrf_token"]), form=True)
    if status != 302 or "error_code" in location:
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("error_message", ["?"])[0]
        fail(f"{path}: refused ({status}, {code})")
