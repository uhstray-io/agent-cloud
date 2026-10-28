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
  3. ENABLE_SIGNUP=0 — invitation-only; PATCHed only when it differs.
  4. The workspace, created only when its slug is absent.
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

cfg = json.loads(sys.stdin.readline())
BASE, HOST, ORIGIN = cfg["base"], cfg["host"], cfg["origin"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the sign-up/sign-in result is in the redirect itself


jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)


def call(method, path, data=None, form=False):
    headers = {"Host": HOST, "Origin": ORIGIN, "Referer": ORIGIN + "/god-mode/"}
    csrf = next((c.value for c in jar if c.name == "csrftoken"), None)
    if csrf:
        headers["X-CSRFToken"] = csrf
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
    raw = resp.read()
    return resp.status, resp.headers.get("Location", ""), (json.loads(raw) if raw[:1] in (b"{", b"[") else None)


def form_post(path, fields):
    _, _, tok = call("GET", "/auth/get-csrf-token/")
    status, location, _ = call("POST", path, dict(fields, csrfmiddlewaretoken=tok["csrf_token"]), form=True)
    if status != 302 or "error_code" in location:
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("error_code", ["?"])[0]
        sys.exit(f"{path}: refused ({status}, error {code})")


done = []
_, _, inst = call("GET", "/api/instances/")
if not inst["instance"]["is_setup_done"]:
    form_post("/api/instances/admins/sign-up/", {
        "email": cfg["email"], "password": cfg["password"], "first_name": cfg["first_name"],
        "last_name": cfg["last_name"], "company_name": cfg["company"], "is_telemetry_enabled": "False"})
    done.append("instance setup completed")
else:
    form_post("/api/instances/admins/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

_, _, configs = call("GET", "/api/instances/configurations/")
signup = next((c["value"] for c in configs if c["key"] == "ENABLE_SIGNUP"), None)
if signup != "0":
    status, _, _ = call("PATCH", "/api/instances/configurations/", {"ENABLE_SIGNUP": "0"})
    if status != 200:
        sys.exit(f"closing self sign-up failed ({status})")
    done.append("self sign-up closed")

_, _, spaces = call("GET", "/api/instances/workspaces/")
rows = spaces.get("results", []) if isinstance(spaces, dict) else (spaces or [])
if not any(w.get("slug") == cfg["workspace_slug"] for w in rows):
    status, _, _ = call("POST", "/api/instances/workspaces/", {
        "name": cfg["workspace_name"], "slug": cfg["workspace_slug"], "organization_size": "2-10"})
    if status != 201:
        sys.exit(f"workspace {cfg['workspace_slug']} could not be created ({status})")
    done.append(f"workspace {cfg['workspace_slug']} created")

print(json.dumps({"changed": bool(done), "actions": done}))
