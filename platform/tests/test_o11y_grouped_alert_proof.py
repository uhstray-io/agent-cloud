import json
import re
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
INSTANCES = ["127.0.0.1:65534", "127.0.0.1:65535"]
SERVICE = "o11y-active-alert-012345abcdef"


def _tasks(path):
    return yaml.safe_load((ROOT / path).read_text())


def _task(tasks, name):
    pending = list(tasks)
    while pending:
        task = pending.pop()
        if not isinstance(task, dict):
            continue
        if task.get("name") == name:
            return task
        for key in ("block", "rescue", "always", "tasks"):
            pending.extend(task.get(key, []))
    raise AssertionError(f"task not found: {name}")


def _expression(value, context):
    env = Environment(undefined=StrictUndefined)
    env.filters["from_json"] = json.loads
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = bool
    env.tests["match"] = lambda value, pattern: re.search(pattern, value) is not None
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    return env.compile_expression(value)(context)


def _up(instance, value="0", cluster="prod-a", environment="prod"):
    return {
        "metric": {
            "instance": instance,
            "cluster": cluster,
            "environment": environment,
        },
        "value": ["0", value],
    }


def test_grouped_scrape_predicate_accepts_both_down_targets_with_shared_labels():
    probe = _tasks("platform/playbooks/tasks/o11y-alert-probe.yml")
    task = _task(probe, "Wait for the declared failed scrape")
    context = {
        "_probe_scrapes": {
            "rc": 0,
            "stdout": json.dumps({"data": {"result": [_up(i) for i in INSTANCES]}}),
        },
        "_probe_expected_instances": INSTANCES,
        "_drill_cluster": "prod-a",
        "_drill_environment": "prod",
        "expected_instance": INSTANCES[-1],
        "o11y_grouped_probe": True,
    }

    assert _expression(task["until"], context)

    for rejected in (
        [_up(INSTANCES[0])],
        [_up(INSTANCES[0]), _up(INSTANCES[1], value="1")],
        [_up(INSTANCES[0]), _up(INSTANCES[1], cluster="other")],
        [_up(INSTANCES[0]), _up(INSTANCES[1], environment="dev")],
        [_up(INSTANCES[0]), _up(INSTANCES[1]), _up("127.0.0.1:65533")],
        [_up(INSTANCES[0]), _up(INSTANCES[1]), _up("127.0.0.1:65533", value="1")],
    ):
        context["_probe_scrapes"]["stdout"] = json.dumps({"data": {"result": rejected}})
        assert not _expression(task["until"], context)


def test_grouped_firing_predicate_requires_exact_instance_alerts():
    probe = _tasks("platform/playbooks/tasks/o11y-alert-probe.yml")
    task = _task(probe, "Wait for Grafana's service-down rule to fire for the probe")

    def alert(instance, state="Alerting", cluster="prod-a", environment="prod", rule_uid="o11y_service_down"):
        return {
            "labels": {
                "service": SERVICE,
                "__alert_rule_uid__": rule_uid,
                "instance": instance,
                "cluster": cluster,
                "environment": environment,
            },
            "state": state,
        }

    context = {
        "_firing_alerts": {
            "rc": 0,
            "stdout": json.dumps({"data": {"alerts": [alert(i) for i in INSTANCES]}}),
        },
        "_probe_expected_instances": INSTANCES,
        "_drill_cluster": "prod-a",
        "_drill_environment": "prod",
        "expected_service": SERVICE,
        "o11y_grouped_probe": True,
    }
    assert _expression(task["until"], context)

    for rejected in (
        [alert(i, rule_uid="unrelated_rule") for i in INSTANCES],
        [alert(INSTANCES[0])],
        [alert(INSTANCES[0]), alert(INSTANCES[1], state="Normal")],
        [alert(INSTANCES[0]), alert(INSTANCES[1], cluster="other")],
        [alert(INSTANCES[0]), alert(INSTANCES[1]), alert("127.0.0.1:65533")],
    ):
        context["_firing_alerts"]["stdout"] = json.dumps({"data": {"alerts": rejected}})
        assert not _expression(task["until"], context)


def test_discord_receipt_requires_one_owned_message_with_both_details_and_dashboard():
    probe = _tasks("platform/playbooks/tasks/o11y-alert-probe.yml")
    task = _task(probe, "Count the matching Discord receipt without retaining its content")
    facts = task["ansible.builtin.set_fact"]["_discord_receipt_summary"]
    marker = f"o11y-delivery-status=firing service={SERVICE}"
    content = (
        f"{marker}\n{marker}\n**Firing**\nLabels:\n"
        f" - instance = {INSTANCES[0]}\n - instance = {INSTANCES[1]}\n"
        f"Annotations:\n - dashboard_url = https://grafana.test/d/service-overview?var-service={SERVICE}"
    )
    context = {
        "_discord_messages": {"json": [{"webhook_id": "owned", "content": content}]},
        "_webhook_id": "owned",
        "_delivery_marker": marker,
        "_probe_expected_instances": INSTANCES,
        "expected_service": SERVICE,
        "o11y_grouped_probe": True,
    }
    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["bool"] = bool
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    env.tests["succeeded"] = lambda result: not result.get("failed", False)
    rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
    assert rendered["grouped_message_count"].strip() == "1"
    assert rendered["grouped_details_present"] == "True"

    context["_discord_messages"]["json"] = [
        {"webhook_id": "owned", "content": content.replace(marker + "\n", "", 1)}
    ]
    rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
    assert rendered["grouped_details_present"] == "False"

    context["_discord_messages"]["json"] = [{"webhook_id": "owned", "content": content}]
    context["_discord_messages"]["json"].append({"webhook_id": "owned", "content": content})
    rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
    assert rendered["grouped_message_count"].strip() == "2"
    assert rendered["grouped_details_present"] == "False"

    context["_discord_messages"]["json"] = [
        {"webhook_id": "owned", "content": content.replace(INSTANCES[1], "")}
    ]
    rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
    assert rendered["grouped_details_present"] == "False"

    context["_discord_messages"]["json"] = [
        {"webhook_id": "owned", "content": content.replace(INSTANCES[1], "")},
        {"webhook_id": "owned", "content": INSTANCES[1]},
    ]
    rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
    assert rendered["grouped_details_present"] == "False"

    for bad in (
        content.replace(marker, marker + "-other", 1),
        content.replace(INSTANCES[0], INSTANCES[0] + "0", 1),
        content.replace(INSTANCES[1], INSTANCES[1] + "0", 1),
        content.replace(f"var-service={SERVICE}", f"var-service={SERVICE}-other"),
    ):
        context["_discord_messages"]["json"] = [{"webhook_id": "owned", "content": bad}]
        rendered = {name: env.from_string(value).render(context) for name, value in facts.items()}
        assert rendered["grouped_details_present"] == "False", bad


def test_grouped_discord_wait_retries_until_the_single_complete_owned_message_exists():
    probe = _tasks("platform/playbooks/tasks/o11y-alert-probe.yml")
    task = _task(probe, "Wait for the matching Discord webhook message")
    marker = f"o11y-delivery-status=firing service={SERVICE}"
    complete = (
        f"{marker}\n{marker}\n**Firing**\nLabels:\n"
        f" - instance = {INSTANCES[0]}\n - instance = {INSTANCES[1]}\n"
        f"Annotations:\n - dashboard_url = https://grafana.test/d/service-overview?var-service={SERVICE}"
    )
    context = {
        "_discord_messages": {"json": []},
        "_webhook_id": "owned",
        "_delivery_marker": marker,
        "_probe_expected_instances": INSTANCES,
        "expected_service": SERVICE,
        "o11y_grouped_probe": True,
    }
    assert not _expression(task["until"], context)

    context["_discord_messages"]["json"] = [{"webhook_id": "other", "content": complete}]
    assert not _expression(task["until"], context)

    context["_discord_messages"]["json"] = [{"webhook_id": "owned", "content": complete}]
    assert _expression(task["until"], context)

    context["_discord_messages"]["json"] = [
        {"webhook_id": "owned", "content": complete.replace(marker + "\n", "", 1)}
    ]
    assert not _expression(task["until"], context)

    for bad in (
        complete.replace(marker, marker + "-other", 1),
        complete.replace(INSTANCES[0], INSTANCES[0] + "0", 1),
        complete.replace(INSTANCES[1], INSTANCES[1] + "0", 1),
        complete.replace(f"var-service={SERVICE}", f"var-service={SERVICE}-other"),
    ):
        context["_discord_messages"]["json"] = [{"webhook_id": "owned", "content": bad}]
        assert not _expression(task["until"], context), bad

    context["_discord_messages"]["json"] = [
        {"webhook_id": "owned", "content": complete.replace(INSTANCES[1], "")},
        {"webhook_id": "owned", "content": INSTANCES[1]},
    ]
    assert not _expression(task["until"], context)

    context["_discord_messages"]["json"] = [{"webhook_id": "owned", "content": complete}]
    context["_discord_messages"]["json"].append({"webhook_id": "owned", "content": complete})
    assert not _expression(task["until"], context)


def test_failed_grouped_drill_always_recovers_before_clearing_marker():
    drill = _tasks("platform/playbooks/drill-o11y-active-alert-delivery.yml")
    play = next(
        item for item in drill
        if item.get("name") == "Prove production alert delivery while preserving active state"
    )
    flight = next(item for item in play["tasks"] if "block" in item)
    assert flight["always"][0]["ansible.builtin.include_tasks"] == "tasks/o11y-recover-active-alert-drill.yml"

    recovery = _tasks("platform/playbooks/tasks/o11y-recover-active-alert-drill.yml")
    names = [item["name"] for item in recovery]
    clear = names.index("Clear the active-drill marker after recovery checks pass")
    for required in (
        "Require the drill scrape job to be absent",
        "Require the production service-down rule to remain active",
        "Require the provisioned alert pause baseline after recovery",
        "Require the production Discord contact point to remain present once",
    ):
        assert names.index(required) < clear
    assert recovery[clear]["ansible.builtin.file"]["state"] == "absent"

    absent = _task(recovery, "Require the drill scrape job to be absent")
    context = {
        "_active_drill_targets": {
            "stdout": json.dumps({"data": {"activeTargets": []}}),
        }
    }
    assert _expression(absent["ansible.builtin.assert"]["that"], context)
    context["_active_drill_targets"]["stdout"] = json.dumps(
        {"data": {"activeTargets": [{"scrapePool": "o11y-production-active-alert-drill"}]}}
    )
    assert not _expression(absent["ansible.builtin.assert"]["that"], context)


def test_preexisting_marker_or_scrape_file_refuses_before_claim_or_injection():
    drill = _tasks("platform/playbooks/drill-o11y-active-alert-delivery.yml")
    play = next(
        item for item in drill
        if item.get("name") == "Prove production alert delivery while preserving active state"
    )
    tasks = play["tasks"]
    names = [item["name"] for item in tasks]
    claim = names.index("Atomically claim the active-drill interruption marker")
    inject = next(index for index, item in enumerate(tasks) if item.get("block"))

    checks = (
        ("Require no active drill to be in progress", "_active_drill_marker"),
        ("Require the active-drill scrape slot to be unused", "_active_drill_scrape"),
    )
    for name, register_name in checks:
        task = _task(tasks, name)
        expression = task["ansible.builtin.assert"]["that"]
        assert _expression(expression, {register_name: {"stat": {"exists": False}}})
        assert not _expression(expression, {register_name: {"stat": {"exists": True}}})
        assert names.index(name) < claim < inject
