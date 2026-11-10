"""Create Plane's marketplace applications (GitHub, Slack, GitLab and the rest) when they are missing.

Run by deploy-plane.yml INSIDE plane-api through `python manage.py shell -c`, with no input and
no credential. These Plane-owned OAuth applications are what Workspace settings > Integrations
lists, and Plane creates them in only three places (v3.3.3): migration silo 0002, which skips when
no instance admin exists yet (a fresh database), the add-admin endpoint, and the setup_instance
command. The first admin of an agent-cloud deploy signs up through /api/instances/admins/sign-up/
(configure_instance.py), which takes none of them, so without this the page lists no GitHub.

Plane's own create_applications skips every application whose secret already exists, so a re-run
changes nothing. reset_marketplace_app_secrets is deliberately NOT used: it deletes the secrets
that connected integrations depend on.

Prints one JSON line: {"created": [...], "present": [...], "missing": [...]} or {"error": ...}.
create_applications generates client secrets and the playbook task is visible, so its stdout and
stderr are discarded and an exception is reported by type only: a database error can quote the
row it refused, secret included.
"""

import contextlib
import io
import json

from plane.license.models import InstanceAdmin
from plane.silo.models.application_secret import ApplicationSecret
from plane.silo.services.generate_application import create_applications
from plane.silo.utils.constants import APPLICATIONS

admin = InstanceAdmin.objects.first()
if admin is None:
    print(json.dumps({"error": "no instance admin yet; first-run setup has not completed"}))
else:
    before = {k for k in APPLICATIONS if ApplicationSecret.objects.filter(key=f"x-{k}-id").exists()}
    error = None
    if before != set(APPLICATIONS):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                create_applications(admin.user.id)
        except Exception as exc:  # noqa: BLE001 - reported by type only, never its message
            error = f"create_applications raised {type(exc).__name__}"
    after = {k for k in APPLICATIONS if ApplicationSecret.objects.filter(key=f"x-{k}-id").exists()}
    print(
        json.dumps(
            {
                **({"error": error} if error else {}),
                "created": sorted(after - before),
                "present": sorted(before),
                "missing": sorted(set(APPLICATIONS) - after),
            }
        )
    )
