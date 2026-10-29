"""Converge a Plane instance's first-run configuration through its own session routes.

Run by deploy-plane.yml INSIDE the plane-api container (`<engine> exec -i plane-api python -c`),
with a JSON document on stdin, so it talks to the API on localhost:8000 and no credential is
ever on a command line. Prints one JSON line naming what it changed; never prints a
credential. Idempotent: a second run changes nothing.

Why it exists: until the first admin signs up, ANYONE who can reach /god-mode/ claims the
instance. deploy.sh therefore holds the proxy, the only way in, until the instance is set up,
and this script completes setup against the API directly. Plane offers no API key for
instance settings; these are the routes its own admin app uses (proven on a Commercial v3.3.3
instance, design.md "Runtime spike results", 0.3 and 0.5).

  1. Instance setup: admin sign-up (only while the instance is not set up).
  2. Admin sign-in (every run; the session drives 3 and 4).
  3. Instance settings: ENABLE_SIGNUP=0 (invitation-only) and the OIDC settings the playbook
     passes; PATCHed only where they differ, reporting setting NAMES only.
  4. The workspace, created only when its slug is absent.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

cfg = json.loads(sys.stdin.readline())
BASE, HOST, ORIGIN = cfg["base"], cfg["host"], cfg["origin"]


def fail(message):
    """One JSON line, never a credential: the playbook shows it while the task stays no_log."""
    print(json.dumps({"error": message}))
    sys.exit(1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the sign-up/sign-in result is in the redirect itself


opener = urllib.request.build_opener(NoRedirect)
# Cookies are kept by hand: with an https WEB_URL Plane marks them Secure, and a cookie jar
# would never send a Secure cookie to http://localhost:8000 (TLS ends at the platform edge).
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


done = []
_, _, inst = call("GET", "/api/instances/")
if not inst["instance"]["is_setup_done"]:
    form_post("/api/instances/admins/sign-up/", {
        "email": cfg["email"], "password": cfg["password"], "first_name": cfg["first_name"],
        "last_name": cfg["last_name"], "company_name": cfg["company"], "is_telemetry_enabled": "False"})
    done.append("instance setup completed")
else:
    form_post("/api/instances/admins/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

# Instance settings: invitation-only sign-up always, plus whatever the playbook passes (OIDC).
# The read-back returns encrypted settings decrypted, so a secret is compared like any value;
# only keys that differ are PATCHed, and only their NAMES are reported.
_, _, configs = call("GET", "/api/instances/configurations/")
current = {c["key"]: c["value"] for c in configs}
desired = dict(cfg.get("settings", {}), ENABLE_SIGNUP="0")
unknown = sorted(k for k in desired if k not in current)
if unknown:
    fail(f"this Plane release has no instance setting named {', '.join(unknown)}")
changed = {k: v for k, v in desired.items() if current.get(k) != v}
if changed:
    status, _, _ = call("PATCH", "/api/instances/configurations/", changed)
    if status != 200:
        fail(f"updating instance settings failed ({status})")
    done.append("instance settings updated: " + ", ".join(sorted(changed)))

_, _, spaces = call("GET", "/api/instances/workspaces/")
rows = spaces.get("results", []) if isinstance(spaces, dict) else (spaces or [])
if not any(w.get("slug") == cfg["workspace_slug"] for w in rows):
    status, _, _ = call("POST", "/api/instances/workspaces/", {
        "name": cfg["workspace_name"], "slug": cfg["workspace_slug"], "organization_size": "2-10"})
    if status != 201:
        fail(f"workspace {cfg['workspace_slug']} could not be created ({status})")
    done.append(f"workspace {cfg['workspace_slug']} created")

print(json.dumps({"changed": bool(done), "actions": done}))
