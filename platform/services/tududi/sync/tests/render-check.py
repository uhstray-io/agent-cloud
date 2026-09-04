#!/usr/bin/env python3
"""render-check.py — task 2.4's schema check for the sync workflow template.

Renders tududi-github-sync.workflow.json.j2 exactly the way the provisioning
playbook will (Jinja2, the real committed mapping, the real embedded JS), then
asserts the result is valid JSON with the reviewed structure and that no
credential VALUE appears anywhere — credentials ride by name/id only.

Run: python3 platform/services/tududi/sync/tests/render-check.py
"""

import json
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

SYNC = Path(__file__).resolve().parent.parent


def main() -> int:
    mapping = yaml.safe_load((SYNC / "github-mapping.yml").read_text())
    all_pairs = mapping["sync_pairs"]
    assert len(all_pairs) == 9, "the reviewed declaration is nine pairs"
    enabled = [p for p in all_pairs if p["enabled"]]
    # Task 2.4's gate: the FULL nine-pair declaration renders to valid JSON,
    # and so does the enabled subset the provisioning actually deploys.
    for pairs in (all_pairs, enabled):
        run_render(pairs)
    print(f"RENDER CHECK PASS — 10 nodes; full nine-pair AND enabled-subset renders valid; no credential values")
    return 0


def run_render(pairs):
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["to_json"] = json.dumps
    template = env.from_string(
        (SYNC / "templates" / "tududi-github-sync.workflow.json.j2").read_text()
    )
    rendered = template.render(
        workflow_name="tududi-github-sync: cycle",
        cadence_minutes=5,
        tududi_base_url="https://todo.example.test",
        sync_tag="gh-sync",
        sync_login="example-sync-login",
        write_cap=20,
        sync_pairs=pairs,
        sync_core_js=(SYNC / "lib" / "sync-core.js").read_text(),
        cycle_glue_js=(SYNC / "lib" / "cycle-glue.js").read_text(),
        tududi_credential_id="cred-t-id",
        tududi_credential_name="tududi-sync-api",
        github_credential_id="cred-g-id",
        github_credential_name="github-sync-api",
    )

    wf = json.loads(rendered)  # must be valid JSON — the schema check's floor

    names = [n["name"] for n in wf["nodes"]]
    assert len(wf["nodes"]) == 10, f"expected 10 nodes, got {len(wf['nodes'])}"
    assert wf["name"] == "tududi-github-sync: cycle"

    # Every node connection target exists.
    for src, conn in wf["connections"].items():
        assert src in names, f"connection source {src!r} is not a node"
        for branch in conn["main"]:
            for hop in branch:
                assert hop["node"] in names, f"connection target {hop['node']!r} missing"

    # The compute node embeds the real engine and the config constants.
    compute = next(n for n in wf["nodes"] if n["id"] == "compute")
    code = compute["parameters"]["jsCode"]
    for needle in ("function computeOps", "SYNC_CONFIG", "write_cap: 20", "pull_request"):
        assert needle in code, f"compute node missing {needle!r}"

    # Credentials are referenced by name/id ONLY — no header, token or value.
    for node in wf["nodes"]:
        creds = node.get("credentials", {})
        for c in creds.values():
            assert set(c) == {"id", "name"}, f"credential carries more than id/name: {c}"
    assert not re.search(r"tt_[0-9a-f]{8}|ghp_|github_pat_|Authorization", rendered), (
        "a credential-shaped value leaked into the rendered workflow"
    )

    # The mapping that rendered is exactly the enabled declaration.
    fan = next(n for n in wf["nodes"] if n["id"] == "fan-out")
    embedded = json.loads(
        re.search(r"const PAIRS = (\[.*?\]);", fan["parameters"]["jsCode"], re.S).group(1)
    )
    assert embedded == pairs, "embedded pairs differ from the enabled mapping entries"

    # GitHub-origin creation: the tududi executor can POST a create, and the
    # link node hands the minted uid back to the issue through item linking.
    exec_t = next(n for n in wf["nodes"] if n["id"] == "exec-tududi")
    assert "create_task" in exec_t["parameters"]["method"], "tududi executor cannot create"
    link = next(n for n in wf["nodes"] if n["id"] == "link-created")
    assert "itemMatching" in link["parameters"]["jsCode"] and "__UID__" in link["parameters"]["jsCode"]
    assert wf["connections"]["Execute tududi op"]["main"][0][0]["node"] == link["name"]
    assert wf["connections"][link["name"]]["main"][0][0]["node"] == "Execute GitHub op"

    # No delete operation anywhere in the rendered artifact (spec invariant).
    assert "DELETE" not in rendered, "a DELETE method appeared in the rendered workflow"


if __name__ == "__main__":
    sys.exit(main())
