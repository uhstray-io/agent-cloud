#!/usr/bin/env bats

@test "NetBox prefix onboarding is Dev-bound, exact, and default-off" {
  python3 - "$BATS_TEST_DIRNAME/../semaphore/templates.yml" "$BATS_TEST_DIRNAME/../playbooks/ensure-netbox-prefix.yml" <<'PY'
import sys
import yaml

templates = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]
entry, = (item for item in templates if item["name"] == "Ensure NetBox Prefix")
assert entry["dev_variant"] is True
assert entry["survey_vars"][1]["name"] == "prefix_apply"
assert entry["survey_vars"][1]["default_value"] == "false"
plays = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
assert plays[0]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
assert plays[1]["hosts"] == "netbox_svc"
tasks = plays[1]["tasks"]
script = tasks[1]["ansible.builtin.shell"]
for guard in ("strict=True", "len(records) > 1", "record.vrf_id is not None", "prefix_apply | default(false) | bool", "IPNetwork(str(network))", "record.full_clean()", "Prefix.objects.get(pk=record.pk)"):
    assert guard in script
assert "action == 'created'" in tasks[1]["changed_when"]
PY
}
