"""A ratchet on raw `local_mode` reads in the playbooks and services (AC-726, plan 24).

Every playbook, shared task and template takes the local-dev mode from the inventory's identity,
`(groups['all'] | local_dev_inventory)`. A raw read of `local_mode` lets an extra var decide what
runs: an extra var outranks the inventory and renders afresh at each read (docs/MISTAKES.md 1.15).
local_mode_reads_allowlist.txt lists the files that still read it raw. The list only shrinks: a
file leaves it in the PR that migrates the file, and a new reader fails here.

A read is found by parsing, not by grep, so prose that names the variable is not one:
- YAML: the values of when, that, failed_when, changed_when and until are bare Jinja, so the whole
  string is searched. Every other string contributes only its {{ }} and {% %} blocks. Mapping keys
  never count, so inventory text that assigns local_mode (`local_mode: true`, or `local_mode=true`
  inside a block scalar) is not a read.
- .j2 templates: only their {{ }} and {% %} blocks.
- Jinja comments ({# #}) never count.

pytest puts this directory on sys.path, so playbook_yaml is imported by name.
"""

import functools
import os
import re
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
ROOTS = ("platform/playbooks", "platform/services")
SUFFIXES = (".yml", ".yaml", ".j2")
ALLOWLIST = Path(__file__).with_name("local_mode_reads_allowlist.txt")

IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])local_mode(?![A-Za-z0-9_])")
BARE_KEYS = frozenset({"when", "that", "failed_when", "changed_when", "until"})
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)

# The one reader that stays: it compares the declared local_mode with the inventory's identity.
EXEMPT = {
    "platform/playbooks/tasks/assert-local-mode-identity.yml":
        "the one permanent reader: it compares the declaration with the inventory's identity",
}

# YAML files that are Jinja templates and do not parse as YAML: scanned as .j2, with the reason.
PARSE_AS_TEMPLATE: dict[str, str] = {}


def reads_in_text(text: str, bare: bool) -> bool:
    """One string's read. `bare` searches the whole string (a condition); otherwise only its Jinja blocks."""
    text = JINJA_COMMENT.sub("", text)
    if bare:
        return IDENTIFIER.search(text) is not None
    return any(IDENTIFIER.search(block) for block in JINJA_BLOCK.findall(text))


def _yaml_reads(node, bare: bool = False) -> bool:
    if isinstance(node, str):
        return reads_in_text(node, bare)
    if isinstance(node, dict):
        return any(_yaml_reads(value, key in BARE_KEYS) for key, value in node.items())
    if isinstance(node, list):
        return any(_yaml_reads(item, bare) for item in node)
    return False


def reads_in_yaml(text: str) -> bool:
    """Every document of a YAML file (a multi-document file reads if any document does). Raises on YAML
    that does not parse, so a file is never passed over silently."""
    return any(_yaml_reads(document) for document in yaml.load_all(text, Loader=playbook_yaml.Loader))


def reads_in_template(text: str) -> bool:
    return reads_in_text(text, bare=False)


def reads(rel: str) -> bool:
    text = (REPO / rel).read_text()
    if rel.endswith(".j2") or rel in PARSE_AS_TEMPLATE:
        return reads_in_template(text)
    return reads_in_yaml(text)


def tracked() -> list[str]:
    """Git-tracked files under ROOTS with a template or YAML suffix. The git environment is cleared of
    GIT_DIR and its relatives: a hook exports them, and this listing must come from THIS checkout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "--", *ROOTS], env=env, text=True,
                            capture_output=True, check=True, timeout=120).stdout
    return sorted(p for p in listed.splitlines() if p.endswith(SUFFIXES))


@functools.cache
def scan() -> tuple[frozenset[str], dict[str, str]]:
    """The files that read local_mode, and the YAML files that do not parse, with their first error line."""
    readers: set[str] = set()
    broken: dict[str, str] = {}
    for rel in tracked():
        try:
            if reads(rel):
                readers.add(rel)
        except yaml.YAMLError as err:
            broken[rel] = (str(err).splitlines() or [type(err).__name__])[0]
    return frozenset(readers), broken


def allowlisted() -> list[str]:
    return [line for line in ALLOWLIST.read_text().splitlines() if line and not line.startswith("#")]


def test_no_file_reads_local_mode_raw_outside_the_allowlist():
    readers, broken = scan()
    assert not broken, (
        "YAML that does not parse, so its reads cannot be checked (add it to PARSE_AS_TEMPLATE, with the "
        "reason, if it is a Jinja template):\n  " + "\n  ".join(f"{p}: {e}" for p, e in sorted(broken.items())))
    offenders = sorted(readers - set(EXEMPT) - set(allowlisted()))
    assert not offenders, (
        "These files read local_mode raw and are not on the migration allowlist:\n  " + "\n  ".join(offenders)
        + "\nRead the mode as (groups['all'] | local_dev_inventory) instead (plan 24, "
        "plan/development/24-local-mode-from-inventory.md). local_mode is an inventory declaration.")


def test_every_allowlisted_file_still_reads_local_mode():
    readers, _ = scan()
    stale = [p for p in allowlisted() if p not in readers]
    assert not stale, (
        "These allowlisted files no longer read local_mode, or no longer exist. Remove them from "
        "platform/tests/local_mode_reads_allowlist.txt: the list only shrinks.\n  " + "\n  ".join(stale))


def test_the_allowlist_is_sorted_unique_scoped_and_lists_no_exemption():
    lines = allowlisted()
    assert lines == sorted(lines), "the allowlist is sorted by path"
    assert len(lines) == len(set(lines)), "a path appears once"
    assert all(p.startswith(ROOTS) and p.endswith(SUFFIXES) for p in lines), "only scanned files are listed"
    assert not set(lines) & set(EXEMPT), "an exempt file is not on the allowlist"
    readers, _ = scan()
    assert all(p in readers for p in EXEMPT), (
        f"an exemption that no longer reads local_mode is stale: {sorted(set(EXEMPT) - readers)}")


def test_prose_that_names_the_variable_is_not_a_read():
    assert not reads_in_yaml('fail_msg: "local_mode is {{ x }}"\n')
    assert not reads_in_yaml('name: "Refuse a local_mode that disagrees with the inventory\'s identity"\n')
    assert not reads_in_yaml("fail_msg: local_mode is set\n")


def test_a_jinja_block_naming_the_variable_is_a_read():
    assert reads_in_yaml('fail_msg: "{{ local_mode }}"\n')
    assert reads_in_yaml('msg: "x {% if local_mode %}y{% endif %}"\n')


def test_a_bare_condition_is_read_whole():
    assert reads_in_yaml("when: local_mode | bool\n")
    assert reads_in_yaml("when:\n  - x\n  - local_mode | bool\n")
    assert reads_in_yaml("that: [not (local_mode | default(false) | bool)]\n")
    assert reads_in_yaml("that:\n  - not (local_mode | default(false) | bool)\n")
    for key in sorted(BARE_KEYS):
        assert reads_in_yaml(f"{key}: local_mode\n"), key


def test_mapping_keys_and_text_outside_jinja_are_not_reads():
    assert not reads_in_yaml("local_mode: true\n")
    assert not reads_in_yaml("_inv_ini: |\n  localhost ansible_connection=local local_mode=true\n")
    assert not reads_in_yaml("_launch_gate_local_mode: \"{{ groups['all'] | local_dev_inventory }}\"\n")


def test_jinja_comments_and_longer_names_are_not_reads():
    assert not reads_in_template("{# local_mode #}\n")
    assert not reads_in_yaml('fail_msg: "{# {{ local_mode }} #}"\n')
    assert not reads_in_yaml('fail_msg: "{{ my_local_mode_x }}"\n')
    assert not reads_in_template("{{ my_local_mode_x }} {% if local_mode_2 %}\n")
    assert reads_in_template("{% if local_mode %}x{% endif %}\n")


def test_every_document_of_a_multi_document_file_is_read():
    assert reads_in_yaml("name: a\n---\nwhen: local_mode\n")
    assert not reads_in_yaml("name: a\n---\nname: b\n")


def test_yaml_that_does_not_parse_raises_instead_of_passing():
    with pytest.raises(yaml.YAMLError):
        reads_in_yaml("{{ bad: [\n")
