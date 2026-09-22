"""Local-only Semaphore templates cannot reach the production catalog.

Spec: change service-deployment-workflow, platform/semaphore-environments ("Environments are
separate controllers", scenario "Local template cannot reach production").
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SEMAPHORE = REPO / "platform/semaphore"
PLAYBOOKS = REPO / "platform/playbooks"
FLAG = "semaphore_include_local_templates"


def _names(path: Path) -> set[str]:
    return {t["name"] for t in yaml.safe_load(path.read_text())["templates"]}


def test_local_merge_defaults_off():
    text = (SEMAPHORE / "setup-templates.yml").read_text()
    assert f"{FLAG} | default(false) | bool" in text


def _code(path: Path) -> str:
    """The file without comment lines, so a comment naming the flag is not a setter."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_only_the_local_bootstrap_turns_it_on():
    candidates = [*PLAYBOOKS.rglob("*.yml"), *SEMAPHORE.rglob("*.yml"), REPO / "Makefile", *REPO.glob("scripts/*.sh")]
    setters = sorted(
        str(p.relative_to(REPO))
        for p in candidates
        if f"{FLAG}=true" in _code(p) or f"{FLAG}: true" in _code(p)
    )
    # The genesis bootstrap, and the runner's `templates` subcommand, which refuses any
    # controller not on loopback (test below). Nothing else may turn the flag on.
    assert setters == ["platform/playbooks/bootstrap-local-dev.yml", "scripts/local-dev.sh"]


def test_runner_publishes_local_templates_only_to_a_loopback_controller():
    text = (REPO / "scripts/local-dev.sh").read_text()
    body = text[text.index("templates() {"):text.index("\n}\n", text.index("templates() {"))]
    assert "http://127.0.0.1:*|http://localhost:*) ;;" in body
    assert body.index("is not the local controller") < body.index("semaphore_include_local_templates=true")


def test_no_local_template_name_is_in_the_shared_catalog():
    overlap = _names(SEMAPHORE / "templates-local.yml") & _names(SEMAPHORE / "templates.yml")
    assert not overlap, f"local-only templates also declared for production: {sorted(overlap)}"
