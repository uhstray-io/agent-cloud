"""Every committed inventory under platform/inventory/ opens with its not-production banner.

docs/MISTAKES.md 1.14: a fact about production was read twice from this repo's public template
inventory, which shares variable names and value shapes with the private one but not its group
layout. The banner puts the warning where that read happens; this test keeps it there.

The banner is the first comment block after the `---` document start: the contiguous `#` lines
that follow it. Joined and whitespace-collapsed, it must contain `TEMPLATE`, `not production`
and the path of the real production inventory, `site-config/inventory/production.yml`.

Only tracked files are checked: `make local-init` writes a gitignored working copy next to the
template, and an old copy made before the banner existed is not this repo's to police.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REQUIRED = ("TEMPLATE", "not production", "site-config/inventory/production.yml")


def banner(text: str) -> str:
    """The first comment block after the `---` line, joined and whitespace-collapsed."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ""
    block: list[str] = []
    for line in lines[1:]:
        if not line.startswith("#"):
            break
        block.append(line.lstrip("#").strip())
    return re.sub(r"\s+", " ", " ".join(block)).strip()


def missing_from_banner(text: str) -> list[str]:
    found = banner(text)
    return [phrase for phrase in REQUIRED if phrase not in found]


def _tracked_inventories() -> list[str]:
    # A hook exports GIT_DIR and friends; a read must not inherit another checkout's.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", "platform/inventory/"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.split()
    return sorted(p for p in out if re.search(r"\.ya?ml(\.example)?$", p))


INVENTORIES = _tracked_inventories()


def test_there_are_inventories_to_check():
    assert "platform/inventory/production.yml" in INVENTORIES, INVENTORIES


@pytest.mark.parametrize("path", INVENTORIES)
def test_inventory_opens_with_the_not_production_banner(path):
    missing = missing_from_banner((REPO / path).read_text())
    assert not missing, f"{path}: first comment block after '---' lacks {missing} (docs/MISTAKES.md 1.14)"


def test_the_banner_check_catches_a_missing_or_displaced_banner():
    good = "---\n# TEMPLATE, not production. Production is\n# site-config/inventory/production.yml\nall: {}\n"
    assert missing_from_banner(good) == []
    assert missing_from_banner("---\nall: {}\n") == list(REQUIRED)
    # A banner below the first key is not at the place the wrong read happens.
    below = "---\n# Some other header\nall: {}\n# TEMPLATE, not production. site-config/inventory/production.yml\n"
    assert missing_from_banner(below) == list(REQUIRED)
    # Without a document start the file has no defined first block.
    assert missing_from_banner(good.removeprefix("---\n")) == list(REQUIRED)
    weakened = good.replace("not production", "production-like")
    assert missing_from_banner(weakened) == ["not production"]
