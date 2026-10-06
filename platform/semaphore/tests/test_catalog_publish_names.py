"""Every template the catalog publishes has its own name.

setup-templates.yml finds the live template by name and PUTs over it. Two declarations
publishing the same name — for instance a directly Dev-bound "X (Dev)" next to a base "X"
with `dev_variant: true`, whose generated twin is also "X (Dev)" — would both write the one
live record, and whichever ran last would win, silently.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parents[1] / "templates.yml"


def _published_names() -> list[str]:
    templates = yaml.safe_load(CATALOG.read_text())["templates"]
    return [t["name"] for t in templates] + [
        t["name"] + " (Dev)" for t in templates if t.get("dev_variant")]


def test_no_two_declarations_publish_the_same_name():
    duplicates = sorted(n for n, c in Counter(_published_names()).items() if c > 1)
    assert not duplicates, f"published more than once: {duplicates}"


def test_twinned_templates_publish_both_names():
    names = set(_published_names())
    for base in ("Deploy o11y", "Clean Deploy o11y", "Snapshot VM", "Create VM Template"):
        assert {base, base + " (Dev)"} <= names, base
