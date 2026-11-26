#!/usr/bin/env bats

@test "Ensure NetBox Business Domains is Dev-bound, has no survey, and sits beside Ensure NetBox Prefix" {
  python3 - "$BATS_TEST_DIRNAME/../semaphore/templates.yml" "$BATS_TEST_DIRNAME/../playbooks/ensure-netbox-business-domains.yml" <<'PY'
import sys
import yaml

templates = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]
names = [item["name"] for item in templates]
entry, = (item for item in templates if item["name"] == "Ensure NetBox Business Domains")
assert entry["dev_variant"] is True
assert "survey_vars" not in entry, "the domain list is inventory, never a launch-time value"
assert entry["playbook"] == "platform/playbooks/ensure-netbox-business-domains.yml"
assert names.index("Ensure NetBox Business Domains") == names.index("Ensure NetBox Prefix") + 1
plays = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
assert plays[1]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
assert plays[1]["vars"] == {"preflight_group": "netbox_svc", "preflight_group_expected": "netbox_svc"}
assert plays[2]["hosts"] == "netbox_svc"
PY
}

@test "business-domain playbook never deletes a tag and never reads a credential" {
  python3 - "$BATS_TEST_DIRNAME/../playbooks/ensure-netbox-business-domains.yml" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
body = plays[2]["vars"]["_ensure_body"]
assert ".delete(" not in body, "an undeclared domain tag is reported by name, never deleted"
dumped = yaml.dump(plays[2]).lower()
for word in ("x-vault-token", "secret/", "approle", "api_token", "/v1/"):
    assert word not in dumped, word
PY
}
