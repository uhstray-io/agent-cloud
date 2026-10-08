"""Every Clean Deploy refuses a launch that does not name the host it destroys.

A Clean Deploy wipes a service's containers, volumes and clone. Only o11y and step-ca asked
the launch to confirm which host; a main-bound `Clean Deploy n8n` (and every other template)
destroyed production data from a single click. The confirmation is now enforced once, in
tasks/clean-service.yml (via tasks/assert-reset-confirmed.yml), which every clean-deploy-*.yml
includes: `-e confirm_reset=<host>` must equal the play's real host list, read from
ansible_play_hosts_all, never from inventory_hostname (an extra var can set that one).

The per-includer cases run each real clean-deploy play (its Fresh deploy import removed, so
nothing renders or deploys) in check mode against a local-dev style host, so a refusal is
proved before any teardown task runs.
"""

import json
import re
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml
from test_executor_internal_overrides import (
    PLAYBOOKS,
    REPO,
    _clean_only,
    _fake_engines,
    _prod_inventory,
    _run,
    needs_ansible,
)

CLEAN_SERVICE = PLAYBOOKS / "tasks/clean-service.yml"
CATALOGS = [REPO / "platform/semaphore/templates.yml", REPO / "platform/semaphore/templates-local.yml"]
TEARDOWN = "Stop and remove containers + volumes"
REFUSAL = "Refusing: pass -e confirm_reset="


def _includes_clean_service(path: Path) -> bool:
    return any(str(t.get("ansible.builtin.include_tasks", "")).endswith("clean-service.yml")
               for play in playbook_yaml.load(path) for t in play.get("tasks", []) or [])


INCLUDERS = [p for p in sorted(PLAYBOOKS.glob("clean-deploy-*.yml")) if _includes_clean_service(p)]
IDS = [p.stem.removeprefix("clean-deploy-") for p in INCLUDERS]


def test_every_clean_deploy_playbook_composes_clean_service():
    assert len(INCLUDERS) == len(list(PLAYBOOKS.glob("clean-deploy-*.yml"))) >= 16, INCLUDERS


def test_the_confirmation_is_the_first_thing_clean_service_does_after_its_inputs_exist():
    tasks = yaml.safe_load(CLEAN_SERVICE.read_text())
    assert tasks[0]["name"].startswith("Assert required variables")
    gate = tasks[1]
    assert gate["ansible.builtin.include_tasks"] == "assert-reset-confirmed.yml", gate
    # Nothing that changes state runs before it.
    for task in tasks[:2]:
        assert not set(task) & {"ansible.builtin.shell", "ansible.builtin.file", "ansible.builtin.command"}


def _service(path: Path) -> str:
    return path.stem.removeprefix("clean-deploy-")


def _deploy_path(service: str) -> str:
    for base in ("platform/services", "agents"):
        if (REPO / base / service / "deployment").is_dir():
            return f"{base}/{service}/deployment"
    return f"platform/services/{service}/deployment"


def _destroy_only(path: Path, tmp_path: Path) -> tuple[Path, str, str]:
    """The includer's own plays with its imports (the guard and the Fresh deploy) dropped and
    the task-library paths made absolute, for a playbook that lives elsewhere. Returns the
    playbook, the first play's inventory group and the service's inventory line."""
    plays = [p for p in playbook_yaml.load(path) if "hosts" in p]
    text = yaml.safe_dump(plays, width=10**6)
    text = re.sub(r"(?<![\w/.-])tasks/([\w-]+\.yml)", lambda m: f"{PLAYBOOKS}/tasks/{m[1]}", text)
    out = tmp_path / "destroy-only.yml"
    out.write_text(text)
    service = _service(path)
    return out, plays[0]["hosts"], f"service_name={service} monorepo_deploy_path={_deploy_path(service)}"


def _local_inventory(tmp_path: Path, group: str, service_vars: str, *hosts: str) -> str:
    inv = tmp_path / "inv.ini"
    inv.write_text(f"[{group}]\n" + "".join(
        f"{h} ansible_connection=local local_mode=true {service_vars}\n" for h in hosts))
    return str(inv)


def _launch(path: Path, tmp_path: Path, *extra: str, hosts: tuple[str, ...] = ()):
    playbook, group, service_vars = _destroy_only(path, tmp_path)
    host = f"{_service(path)}-local"
    inv = _local_inventory(tmp_path, group, service_vars, *(hosts or (host,)))
    return host, _run(playbook, tmp_path, "--check", *extra, inventory=inv)


@needs_ansible
@pytest.mark.parametrize("path", INCLUDERS, ids=IDS)
@pytest.mark.parametrize("extra", [{}, {"confirm_reset": ""}, {"confirm_reset": "some-other-host"}],
                         ids=["no-confirm", "empty-confirm", "wrong-confirm"])
def test_every_clean_deploy_refuses_an_unconfirmed_launch_before_any_teardown(path, extra, tmp_path):
    host, proc = _launch(path, tmp_path, "-e", json.dumps(extra))
    assert proc.returncode != 0, proc.stdout
    assert f"{REFUSAL}{host}" in proc.stdout, proc.stdout
    assert TEARDOWN not in proc.stdout and "Remove agent-cloud clone" not in proc.stdout, proc.stdout
    assert "TASK [Verify clean]" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("path", INCLUDERS, ids=IDS)
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("inventory_hostname", "honest-host", "forged-host"))
def test_a_forged_inventory_hostname_cannot_satisfy_the_confirmation(forge, path, tmp_path):
    # inventory_hostname CAN be set by an extra var, so a gate that compared the confirmation to
    # it would pass `-e inventory_hostname=x -e confirm_reset=x`. The play's real hosts are read
    # from ansible_play_hosts_all, which cannot be set from outside the run.
    host, proc = _launch(path, tmp_path, "-e", forge(tmp_path), "-e", json.dumps({"confirm_reset": "forged-host"}))
    assert proc.returncode != 0, proc.stdout
    assert f"{REFUSAL}{host}" in proc.stdout, proc.stdout
    assert TEARDOWN not in proc.stdout, proc.stdout


@needs_ansible
@pytest.mark.parametrize("path", INCLUDERS, ids=IDS)
def test_a_launch_naming_the_host_gets_past_the_confirmation(path, tmp_path):
    host, proc = _launch(path, tmp_path, "-e", json.dumps({"confirm_reset": f"{_service(path)}-local"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REFUSAL not in proc.stdout, proc.stdout
    assert "TASK [Verify clean]" in proc.stdout, proc.stdout


def _two_host_inventory(tmp_path: Path) -> str:
    service_vars = "service_name=o11y monorepo_deploy_path=platform/services/o11y/deployment"
    return _local_inventory(tmp_path, "o11y_svc", service_vars, "b-local", "a-local")


@needs_ansible
@pytest.mark.parametrize("confirm, ok", [("a-local", False), ("b-local", False), ("b-local,a-local", False),
                                        ("a-local,b-local", True)],
                         ids=["first-only", "second-only", "unsorted", "sorted-joined"])
def test_a_play_on_several_hosts_needs_every_host_named(confirm, ok, tmp_path):
    proc = _run(_clean_only(tmp_path), tmp_path, "--check", "-e", json.dumps({"confirm_reset": confirm}),
                inventory=_two_host_inventory(tmp_path))
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    if not ok:
        assert f"{REFUSAL}a-local,b-local" in proc.stdout, proc.stdout
        assert TEARDOWN not in proc.stdout


def _prod_launch(tmp_path: Path, *extra: str, limit: bool = False):
    args = ["--limit", "obs"] if limit else []
    proc = _run(_clean_only(tmp_path), tmp_path, *args, "-e", json.dumps({"ansible_become": False}), *extra,
                inventory=_prod_inventory(tmp_path), path_prepend=_fake_engines(tmp_path))
    log = tmp_path / "engine-args"
    return proc, (log.read_text() if log.exists() else "")


@needs_ansible
@pytest.mark.parametrize("extra", [{}, {"confirm_reset": "other"}], ids=["no-confirm", "wrong-confirm"])
def test_a_remote_host_is_not_torn_down_without_its_name(extra, tmp_path):
    proc, log = _prod_launch(tmp_path, "-e", json.dumps(extra))
    assert proc.returncode != 0, proc.stdout
    assert f"{REFUSAL}obs" in proc.stdout, proc.stdout
    assert "compose" not in log and "Stop and remove containers" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("inventory_hostname", "obs", "forged-host"))
def test_a_forged_inventory_hostname_cannot_pass_a_remote_host(forge, tmp_path):
    # --limit skips the run-start guard's check, so the forged name reaches the gate.
    proc, log = _prod_launch(tmp_path, "-e", forge(tmp_path), "-e", json.dumps({"confirm_reset": "forged-host"}),
                             limit=True)
    assert proc.returncode != 0, proc.stdout
    assert f"{REFUSAL}obs" in proc.stdout, proc.stdout
    assert "compose" not in log and "Stop and remove containers" not in proc.stdout


@needs_ansible
def test_the_remote_host_named_by_the_launch_is_torn_down(tmp_path):
    proc, log = _prod_launch(tmp_path, "-e", json.dumps({"confirm_reset": "obs"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ps -a" in log


def _catalog_templates():
    includers = {f"platform/playbooks/{p.name}" for p in INCLUDERS}
    for catalog in CATALOGS:
        for template in yaml.safe_load(catalog.read_text())["templates"]:
            if template["playbook"] in includers:
                yield pytest.param(template, id=f"{catalog.stem}:{template['name']}")


@pytest.mark.parametrize("template", list(_catalog_templates()))
def test_every_clean_deploy_template_asks_for_a_required_confirm_reset(template):
    survey = {v["name"]: v for v in template.get("survey_vars", [])}
    assert "confirm_reset" in survey, f"{template['name']} has no confirm_reset survey variable"
    field = survey["confirm_reset"]
    assert field["required"] is True
    # No default: a pre-filled confirmation is not a confirmation.
    assert not field.get("default_value") and "default" not in field
    assert not {"confirm_o11y_reset", "confirm_ca_reset"} & set(survey)


def test_every_clean_deploy_playbook_has_a_template_in_a_catalog():
    # The survey test above only covers playbooks some catalog launches.
    covered = {t.values[0]["playbook"] for t in _catalog_templates()}
    assert covered == {f"platform/playbooks/{p.name}" for p in INCLUDERS}
