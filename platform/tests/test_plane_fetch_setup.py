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
