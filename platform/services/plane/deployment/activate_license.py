"""Activate the workspace's Plane licence, once, through the route Plane's own Billing page calls.

Run by deploy-plane.yml INSIDE the plane-api container, after plane_session.py, as a credential task
(no_log), with one JSON document on stdin:

  {base, host, origin,                plane_session.py
   email, password,                   the automation account (the workspace's creator and owner)
   workspace_slug,
   license_key,                       from secret/services/plane:plane_license_key
   plan}                              the plan the key carries, e.g. PRO

Plane documents licence activation only as a UI step (Workspace settings, Billing and plans;
design.md row 0.4). The web app calls POST /api/payments/workspaces/{slug}/licenses/ with
{"license_key"}, which needs billing-manage permission (owner or Admin; before the licence the
Admin role does not exist, so the owner is the one account that has it): the automation account,
which created the workspace, activates, and no person needs Admin first. In the v3.3.3 image
(plane/payment/views/license_activate.py) the route answers 200 once Plane's payment server has
accepted the key, and 400 when it refuses it, with a body that can echo the key.

  1. Sign in as the automation account.
  2. Read the plan (GET .../current-plan/, field "product": FREE, PRO, ...). This read is not
     side-effect free: Plane resyncs its local licence row with its payment server. Already the
     wanted plan: nothing is sent, so once the plan reads as licensed the key is never resubmitted.
     The plan must match exactly: a workspace on a higher plan than plane_license_plan would be
     sent the key again.
  3. Otherwise POST the key, then read the plan back; anything but the wanted plan is a refusal.
     A non-200 POST is checked against the plan too, because Plane answers 400 when the payment
     server accepted the key but its own resync failed (license_activate.py), and 500 when the
     accepted response could not be decoded.

The key binds to the instance's machine signature (plane_machine_signature, generated once and
never rotated). Whether the licence server accepts the same signature after a rebuild is still
open (design.md row 0.2).

Prints ONE JSON line {"changed", "product"}. A refusal prints {"error"} with the HTTP status only,
never a response body, and exits 1.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the playbook, so its
# names are defined only in the joined source.
# ruff: noqa: F821

ws = cfg["workspace_slug"]
plan_path = f"/api/payments/workspaces/{ws}/current-plan/"


def current_plan():
    status, _, body = call("GET", plan_path)
    if status != 200 or not isinstance(body, dict):
        fail(f"reading the plan of {ws} failed ({status})")
    return body.get("product")


form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

product = current_plan()
if product == cfg["plan"]:
    print(json.dumps({"changed": False, "product": product}))
    sys.exit(0)

status, _, _ = call("POST", f"/api/payments/workspaces/{ws}/licenses/", {"license_key": cfg["license_key"]})
product = current_plan()
if status != 200 and product != cfg["plan"]:
    fail(f"licence activation for {ws} refused ({status})")
if product != cfg["plan"]:
    fail(f"licence activated but {ws} reads plan {product}, not {cfg['plan']}")
print(json.dumps({"changed": True, "product": product}))
