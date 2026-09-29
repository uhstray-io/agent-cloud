"""The repository's Ansible YAML, parsed once per content state and shared by every guard.

The check-mode contract (test_check_mode_contract.py), the loop and secret-read guards
(test_no_request_in_loop_items.py) and the SSH harnesses, which re-check the whole repository
before every ansible run, all read the same ~200 files. Parsing them was ~0.5 s per pass, and
there were three passes per session; a session now pays for one.

Imported as `import playbook_yaml`, like harness_sandbox: pytest puts this directory on
sys.path because it has no __init__.py.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
# Semaphore's own playbooks and shared tasks run under the same flags as the playbooks.
SCANNED = (REPO / "platform/playbooks", REPO / "platform/semaphore")


class Loader(yaml.SafeLoader):
    """Tolerates custom tags (for example `!unsafe`) the checkers do not need: a tagged value
    loads as None. On every file in the repository this parses identically to yaml.safe_load
    (2026-09-29, 214 files), which raised on a tag instead."""


Loader.add_multi_constructor("!", lambda loader, suffix, node: None)

_PARSED: dict[Path, tuple[tuple[int, int], object]] = {}


def files() -> list[Path]:
    return sorted(p for base in SCANNED for p in base.rglob("*.yml"))


def load(path: Path):
    """Keyed on mtime and size, so an edited file is read again. Callers treat the result as
    read-only (a test that edits a document deep-copies it first)."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(path)
    if hit is None or hit[0] != key:
        hit = _PARSED[path] = (key, yaml.load(path.read_text(), Loader=Loader))  # noqa: S506 - SafeLoader subclass
    return hit[1]


def loads(text: str):
    """A document from text, with the same loader: for the guards' own synthetic cases."""
    return yaml.load(text, Loader=Loader)  # noqa: S506 - SafeLoader subclass
