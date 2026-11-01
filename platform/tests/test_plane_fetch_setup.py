"""fetch_setup.py writes Plane's setup files only when each matches its pinned sha256."""

import hashlib
import importlib.util
import io
import pathlib
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fetch_setup", ROOT / "platform/services/plane/deployment/fetch_setup.py")
fetch_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_setup)

COMPOSE, VARS = b"services: {}\n", b"WEB_URL=http://localhost\n"


def zip_of(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def pins(compose=COMPOSE, variables=VARS):
    return {"docker-compose.yml": hashlib.sha256(compose).hexdigest(),
            "variables.env": hashlib.sha256(variables).hexdigest()}


def test_matching_release_returns_both_members():
    got = fetch_setup.verified_members(zip_of({"docker-compose.yml": COMPOSE, "variables.env": VARS}), pins())
    assert got == {"docker-compose.yml": COMPOSE, "variables.env": VARS}


def test_a_changed_file_is_refused():
    blob = zip_of({"docker-compose.yml": COMPOSE + b"# tampered\n", "variables.env": VARS})
    with pytest.raises(ValueError, match="docker-compose.yml"):
        fetch_setup.verified_members(blob, pins())


def test_a_missing_member_is_refused():
    with pytest.raises(KeyError):
        fetch_setup.verified_members(zip_of({"docker-compose.yml": COMPOSE}), pins())


def test_extra_members_are_ignored_not_extracted():
    blob = zip_of({"docker-compose.yml": COMPOSE, "variables.env": VARS, "../escape.sh": b"x"})
    assert set(fetch_setup.verified_members(blob, pins())) == {"docker-compose.yml", "variables.env"}


@pytest.mark.parametrize("given, qualified", [
    ("makeplane/backend-commercial:v3.3.3", "docker.io/makeplane/backend-commercial:v3.3.3"),
    ("valkey/valkey:7.2.11-alpine", "docker.io/valkey/valkey:7.2.11-alpine"),
    ("postgres:15.7-alpine", "docker.io/library/postgres:15.7-alpine"),
    ("rabbitmq:3.13.6-management-alpine", "docker.io/library/rabbitmq:3.13.6-management-alpine"),
    ("docker.io/pgsty/minio:RELEASE.2026-08-04T00-00-00Z", "docker.io/pgsty/minio:RELEASE.2026-08-04T00-00-00Z"),
    ("ghcr.io/org/app:1", "ghcr.io/org/app:1"),
    ("localhost/app:dev", "localhost/app:dev"),
    ("registry.local:5000/app:1", "registry.local:5000/app:1"),
    ("${PLANE_IMAGE}", "${PLANE_IMAGE}"),
])
def test_short_image_names_get_their_registry(given, qualified):
    assert fetch_setup.qualify(given) == qualified


def test_only_image_lines_are_rewritten_and_indentation_is_kept():
    compose = ("services:\n  api:\n    image: makeplane/backend-commercial:v3.3.3\n"
               "    command: postgres:15 is not an image line\n  db:\n    image: postgres:15.7-alpine\n")
    assert fetch_setup.qualify_images(compose) == (
        "services:\n  api:\n    image: docker.io/makeplane/backend-commercial:v3.3.3\n"
        "    command: postgres:15 is not an image line\n  db:\n    image: docker.io/library/postgres:15.7-alpine\n")
