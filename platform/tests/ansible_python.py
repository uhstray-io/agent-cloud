"""The Python interpreter of the `ansible-playbook` on PATH, shared by every test that needs it.

Several tests run a snippet in the interpreter ansible-core is installed in (the test
interpreter need not have it) and gate on `ansible-playbook` being installed at all. Each kept
its own copy that read line 1 of the script as the interpreter. That breaks when the
environment's path is too long for a kernel shebang (127 characters on Linux): pip and uv then
write a `/bin/sh` trampoline, so line 1 is `/bin/sh` and the real interpreter is on line 2:

    #!/bin/sh
    '''exec' '/very/long/venv/bin/python' "$0" "$@"
    ' '''

Reading line 1 there runs the snippet under sh, which fails nine different ways at once. This
module resolves the interpreter from either shape and proves the answer by importing ansible
with it, so a wrong interpreter fails here, with its name, instead of inside a test.

Imported as `from ansible_python import ansible_python, needs_ansible`, like playbook_yaml:
pytest puts this directory on sys.path because it has no __init__.py.
"""

import functools
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_TRAMPOLINE_MARKER = "'''exec' "


def _skip_marker() -> pytest.MarkDecorator:
    return pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


needs_ansible = _skip_marker()


def interpreter_from_script(text: str) -> str | None:
    """The interpreter a script's first lines name, or None when they name none.

    A plain shebang names it on line 1 (`#!/venv/bin/python`, with optional arguments, or
    `#!/usr/bin/env python3`). pip's and uv's trampoline names `/bin/sh` on line 1 and the real
    interpreter as the first word after `'''exec'` on line 2, quoted or not."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#!"):
        return None
    shebang = shlex.split(lines[0][2:])
    if not shebang:
        return None
    if os.path.basename(shebang[0]) in ("sh", "bash", "dash"):
        if len(lines) < 2 or not lines[1].startswith(_TRAMPOLINE_MARKER):
            return None
        words = shlex.split(lines[1][len(_TRAMPOLINE_MARKER):])
        return words[0] if words else None
    if os.path.basename(shebang[0]) == "env":
        words = [w for w in shebang[1:] if not w.startswith("-")]
        return shutil.which(words[0]) if words else None
    return shebang[0]


@functools.cache
def _verified(script: str) -> str:
    python = interpreter_from_script(Path(script).read_text(errors="replace"))
    if python is None:
        raise RuntimeError(f"cannot find the interpreter {script} runs under; its first lines are neither "
                           "a python shebang nor a pip/uv `/bin/sh` trampoline")
    proc = subprocess.run([python, "-c", "import ansible"], capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{python} (read from {script}) cannot import ansible: {proc.stderr.strip()[-300:]}")
    return python


def ansible_python() -> str:
    """The interpreter `ansible-playbook` runs under; skips the test when it is not installed."""
    script = shutil.which("ansible-playbook")
    if script is None:
        pytest.skip("ansible-playbook not installed")
    return _verified(script)
