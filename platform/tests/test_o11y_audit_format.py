"""Keep Go container formats out of Ansible's recursively rendered loop items."""

import ast
from pathlib import Path

import yaml
from jinja2 import Environment


def test_grafana_audit_formats_render_once():
    playbook = Path(__file__).resolve().parents[2] / "platform/playbooks/audit-o11y-containers.yml"
    task = next(
        task for task in yaml.safe_load(playbook.read_text())[0]["tasks"]
        if task["name"] == "List all containers on the existing Grafana host"
    )
    for item in task["loop"]:
        assert "--format" not in item["argv"]
        rendered = Environment().from_string(task["ansible.builtin.command"]["argv"]).render(item=item)
        assert ast.literal_eval(rendered)[-2:] == ["--format", "{{.Names}} {{.Image}} {{.Status}}"]
