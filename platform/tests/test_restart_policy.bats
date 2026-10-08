#!/usr/bin/env bats
# Containers must come back on their own after a host reboot.
#
# Podman has no daemon. At boot, podman-restart.service (system unit for rootful,
# user unit for rootless) runs `podman start --all --filter restart-policy=always`
# on podman 4.9.3, so it starts ONLY `restart: always` containers. A container
# declared `unless-stopped` stays down: production OpenBao sat stopped for three
# days after the 2026-09-19 reboot this way (docs/MISTAKES.md 10.15). Docker honours
# `always` too, so one policy is correct on both engines.
#
# Run: bats platform/tests/test_restart_policy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  LINGER="$REPO_ROOT/platform/playbooks/tasks/enable-linger.yml"
  PREAMBLE="$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml"
}

# Every compose file the platform deploys (not the archived plans).
compose_files() {
  git -C "$REPO_ROOT" ls-files -- 'platform/**/*compose*.yml' 'platform/**/*compose*.yaml' \
    'agents/**/*compose*.yml' 'agents/**/*compose*.yaml' | sed "s#^#$REPO_ROOT/#"
}

@test "restart policy: there are compose files to check" {
  [ "$(compose_files | wc -l)" -gt 10 ]
}

@test "restart policy: every service's effective policy is always or \"no\"" {
  # Parsed, not grepped: a service with NO restart key is Compose's default "no"
  # restart — invisible to a line match — and anchors/merge keys and overlay files
  # change what a service ends up with. Per directory, overlays (compose.*.yml,
  # docker-compose.*.yml) apply over the base file, so a policy set only in the
  # base still counts and a service an overlay adds must carry its own.
  # "no" is for one-shot init containers, which must not restart at all, and each one must
  # declare itself with the label agent-cloud.one-shot: "true": the persistence check
  # (verify-service-persistence.yml) accepts "no" only from a labelled container that exited 0,
  # since a long-running "no" container can exit 0 too.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$REPO_ROOT" <<'PY'
import os, subprocess, sys, yaml
root = sys.argv[1]
class Loader(yaml.SafeLoader):
    pass
def any_tag(loader, suffix, node):  # compose tags such as !override / !reset
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)
Loader.add_multi_constructor('!', any_tag)
files = subprocess.check_output(
    ['git', '-C', root, 'ls-files', '--', 'platform/**/*compose*.yml', 'platform/**/*compose*.yaml',
     'agents/**/*compose*.yml', 'agents/**/*compose*.yaml'], text=True).split()
dirs = {}
for f in files:
    doc = yaml.load(open(os.path.join(root, f)), Loader=Loader)
    if not isinstance(doc, dict) or not isinstance(doc.get('services'), dict):
        continue  # not a compose file (e.g. an Ansible task list with "compose" in its name)
    base = os.path.basename(f) in ('compose.yml', 'compose.yaml', 'docker-compose.yml', 'docker-compose.yaml')
    dirs.setdefault(os.path.dirname(f), []).append((not base, f, doc['services']))
bad = []
for d, docs in sorted(dirs.items()):
    effective, labels = {}, {}
    for _, f, services in sorted(docs):  # base first, then overlays
        for name, svc in services.items():
            svc = svc or {}
            if 'restart' in svc or name not in effective:
                effective[name] = (svc.get('restart'), f)
            if 'labels' in svc:
                raw = svc['labels'] or {}
                # Compose merges labels by key across overlays; so does this (PR 253 CodeRabbit review).
                labels.setdefault(name, {}).update(raw if isinstance(raw, dict) else dict(x.split('=', 1) for x in raw))
    for name, (policy, f) in sorted(effective.items()):
        if policy not in ('always', 'no'):
            bad.append(f"{f}: service {name}: restart={policy!r}")
        elif policy == 'no' and str(labels.get(name, {}).get('agent-cloud.one-shot')) != 'true':
            bad.append(f"{f}: service {name}: restart 'no' without label agent-cloud.one-shot: \"true\"")
print('\n'.join(bad))
sys.exit(1 if bad else 0)
PY
  [ "$status" -eq 0 ] || { echo "services podman will not start at boot:"; echo "$output"; false; }
}

@test "restart policy: no container is started with --restart unless-stopped" {
  local bad
  # This file names the flag in a test title, so it excludes itself.
  bad=$(git -C "$REPO_ROOT" grep -nE -- '--restart[= ]+unless-stopped' -- platform agents scripts \
    ':!platform/tests/test_restart_policy.bats' || true)
  [ -z "$bad" ] || { echo "$bad"; false; }
}

@test "restart policy: the shared preamble enables linger for every composable deploy" {
  assert_grep -qF 'include_tasks: enable-linger.yml' "$PREAMBLE"
}

@test "restart policy: linger task enables podman's user boot unit, not just linger" {
  # Linger alone starts an empty user manager: the unit ships disabled.
  assert_grep -qF 'src: /usr/lib/systemd/user/podman-restart.service' "$LINGER"
  assert_grep -qF '.config/systemd/user/default.target.wants' "$LINGER"
  assert_grep -qF 'dest: "{{ _unit_wants_dir }}/podman-restart.service"' "$LINGER"
  # Owned by the container user, not root, so their systemd reads it.
  assert_grep -qF 'become_user: "{{ linger_user | default(ansible_user) }}"' "$LINGER"
}
