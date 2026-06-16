# OpenBao KV Mount Parameterization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parameterize the OpenBao KV v2 **mount path** (hardcoded `secret/` today) behind a `bao_kv_mount` variable that defaults to `secret`, so multiple agent-cloud instances can each own a separately-named secret vault (e.g. `secret/`, `agent-cloud-b/`) with zero behavior change for existing instances.

**Architecture:** A single inventory/playbook variable `bao_kv_mount` (Ansible) / `BAO_KV_MOUNT` (bash) is threaded through every place the mount appears: the KV-enable step, all KV v2 API path construction, the OpenBao policy files (which become Jinja templates), the `hashi_vault` lookups, and the Semaphore environment. The KV-v2 `data/`/`metadata/` infixes are preserved. Defaulting to `secret` everywhere means omitting the variable reproduces today's layout exactly — no data migration.

**Tech stack:** Ansible (`ansible.builtin.uri`, `community.hashi_vault`), bash libs (`bao-client.sh`, `common.sh`), HCL policies, OpenBao KV v2 API, Semaphore template env.

**Scope decision (chosen by maintainer):** *Per-instance mount* isolation — each instance gets its own KV engine mount. This is **orthogonal** to the documented-but-unimplemented `vault_secret_prefix` (which parameterizes the path *prefix within* a mount, e.g. `secret/data/<prefix>/…`). This plan parameterizes the **mount only**; the `services` path segment stays literal. The two compose later if prefix-namespacing is ever implemented (`{{ bao_kv_mount }}/data/{{ vault_secret_prefix }}/…`).

---

## Background

Today the mount name `secret` is hardcoded in ~33 files across five categories (audited 2026-06-16):

| Category | Files | Nature |
|----------|-------|--------|
| KV v2 API paths | `tasks/manage-secrets.yml` (3 sites), ~15 playbooks, `lib/bao-client.sh`, service `deploy.sh`, `scripts/local-dev.sh` | `secret/data/services/*`, `secret/metadata/services/*` — safe string substitution |
| KV engine enable | `bootstrap-local-dev.yml` (`sys/mounts/secret`), `services/openbao/deployment/deploy.sh` (`secrets enable -path=secret`) | **migration-sensitive** — creates the mount |
| OpenBao policies | 9 `.hcl` files in `services/openbao/deployment/config/policies/` | `path "secret/data/services/*"` — must template |
| `hashi_vault` lookups | `distribute-ssh-keys.yml`, `harden-ssh.yml`, `proxmox-validate.yml`, `provision-vm.yml` | `secret=secret/data/…` lookup args |
| Documentation | `CLAUDE.md` (layout table) + several `plan/` docs | reference only |

**Why now:** prod will run multiple agent-cloud instances; per-instance vaults keep their secrets, policies, and AppRoles isolated under distinct mounts.

## Design decisions

1. **Variable + default.** `bao_kv_mount` (Ansible) and `BAO_KV_MOUNT` (bash), default `secret`. Reference it **inline with the default at every site** — `{{ bao_kv_mount | default('secret') }}` / `${BAO_KV_MOUNT:-secret}` — rather than relying on a single group_vars definition. This guarantees that any playbook run without the variable set behaves exactly as today, even outside the normal inventory.
2. **KV-v2 infix preserved.** API paths are `<mount>/data/<path>` (read/write) and `<mount>/metadata/<path>` (metadata/list). Only the leading `<mount>` token is parameterized; `data`/`metadata`/`services` are unchanged.
3. **Non-breaking, no migration.** Default `secret` keeps the existing mount. A *new* instance setting `bao_kv_mount: agent-cloud-b` gets a fresh empty mount created by the KV-enable step. **Renaming an existing instance's mount is out of scope** (would require `bao secrets move`); we only parameterize for new instances.
4. **Policies become templates.** The 9 static `.hcl` files become `.hcl.j2`, rendered with `bao_kv_mount` at apply time. `apply-openbao-policy.yml` switches `lookup('file', …)` → `lookup('template', …)`. Non-secret paths in the policies (`sys/policies/acl/*`, `auth/approle/*`) stay literal.
5. **Semaphore env.** Semaphore must export `BAO_KV_MOUNT` into the deploy environment (alongside `BAO_ROLE_ID`/`BAO_SECRET_ID`) so `bao-client.sh` and service `deploy.sh` resolve the same mount.

---

## Task 1: Define the variable + document the default

**Files:**
- Modify: `platform/inventory/local-dev.yml.example` (add commented `bao_kv_mount: secret` under the OpenBao/all vars, with explanatory comment)
- Reference (site-config, private): each prod instance's inventory sets `bao_kv_mount: <instance-mount>`

- [ ] **Step 1: Add the documented default to the inventory example**

```yaml
    # OpenBao KV v2 mount that holds this instance's secrets. Default 'secret'
    # reproduces the legacy layout (secret/data/services/<svc>). Set a distinct
    # value per agent-cloud instance to isolate vaults (e.g. agent-cloud-b).
    # Changing this on an EXISTING instance requires `bao secrets move` — only
    # set a non-default value when bootstrapping a fresh instance.
    bao_kv_mount: secret
```

- [ ] **Step 2: Verify** — `grep -n bao_kv_mount platform/inventory/local-dev.yml.example` shows the entry. No functional change yet.

---

## Task 2: Parameterize the KV engine enable (migration-sensitive)

**Files:**
- Modify: `platform/playbooks/bootstrap-local-dev.yml` (the "Enable KV v2 at secret/" task, ~L242)
- Modify: `platform/services/openbao/deployment/deploy.sh` (`enable_secrets_engines`, ~L99)

- [ ] **Step 1: bootstrap — parameterize the mount path in the enable URL**

```yaml
    - name: "Enable KV v2 at {{ bao_kv_mount | default('secret') }}/ (idempotent)"
      ansible.builtin.uri:
        url: "{{ _bao_url_host }}/v1/sys/mounts/{{ bao_kv_mount | default('secret') }}"
        method: POST
        headers:
          X-Vault-Token: "{{ _bao_root_token }}"
        body_format: json
        body: { type: kv, options: { version: 2 } }
        status_code: [200, 204, 400]   # 400 = already mounted
      register: _kv_enable
```

- [ ] **Step 2: openbao deploy.sh — parameterize the enable + the existence grep**

```bash
enable_secrets_engines() {
  local token="$1"
  local mount="${BAO_KV_MOUNT:-secret}"
  info "Step 4: Enabling secrets engines..."
  local enabled
  enabled=$(bao_auth "$token" secrets list -format=json 2>/dev/null | jq -r 'keys[]')

  echo "$enabled" | grep -q "^${mount}/$" || bao_auth "$token" secrets enable -path="${mount}" kv-v2
  echo "$enabled" | grep -q "^database/$" || bao_auth "$token" secrets enable database
  info "Secrets engines ready."
}
```

- [ ] **Step 3: Verify** — `shellcheck platform/services/openbao/deployment/deploy.sh` clean; warm re-run of `make local-bootstrap` returns 400 (already mounted at `secret/`) with no new mount created.

---

## Task 3: Parameterize the bash client (`bao-client.sh`)

**Files:**
- Modify: `platform/lib/bao-client.sh` (`bao_kv_get`, `bao_kv_get_field`, `bao_kv_put`, `bao_kv_patch`)

- [ ] **Step 1: Add a mount helper at the top of the KV section**

```bash
# KV v2 mount that holds this instance's secrets (per-instance isolation).
# Default 'secret' = legacy layout. Semaphore/Ansible export BAO_KV_MOUNT.
_bao_kv_mount() { printf '%s' "${BAO_KV_MOUNT:-secret}"; }
```

- [ ] **Step 2: Use it in all four helpers** (replace the literal `secret/`):

```bash
bao_kv_get() {
  local path="$1"
  _bao_api GET "/$(_bao_kv_mount)/data/${path}" | jq -r '.data.data'
}

bao_kv_get_field() {
  local path="$1" field="$2"
  _bao_api GET "/$(_bao_kv_mount)/data/${path}" | jq -r --arg f "$field" '.data.data[$f] // empty'
}

bao_kv_put() {
  local path="$1" json_data="$2"
  _bao_api POST "/$(_bao_kv_mount)/data/${path}" -d "$(jq -n --argjson data "$json_data" '{"data": $data}')"
}
# bao_kv_patch: replace the hardcoded
#   "${OPENBAO_ADDR}/v1/secret/data/${path}"
# with
#   "${OPENBAO_ADDR}/v1/$(_bao_kv_mount)/data/${path}"
```

- [ ] **Step 3: Verify** — `shellcheck platform/lib/bao-client.sh` clean; `grep -n 'secret/data' platform/lib/bao-client.sh` returns nothing.

---

## Task 4: Parameterize `manage-secrets.yml` (the reusable secrets task)

**Files:**
- Modify: `platform/playbooks/tasks/manage-secrets.yml` (fetch-existing, fetch-shared, store — 3 URL sites)

- [ ] **Step 1: Replace all three `/v1/secret/data/services/…` URLs**

```yaml
# fetch existing:
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ service_name }}"
# fetch shared (loop):
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ item.from_service }}"
# store:
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ service_name }}"
```

- [ ] **Step 2: Verify** — `grep -n 'v1/secret/data' platform/playbooks/tasks/manage-secrets.yml` returns nothing; `ansible-lint platform/playbooks/tasks/manage-secrets.yml` clean.

---

## Task 5: Parameterize the remaining playbooks

**Files (each contains one or more `/v1/secret/data/services/…` or `/secret/metadata/…` references — substitute the same pattern as Task 4):**
- `platform/playbooks/bootstrap-local-dev.yml` (the OpenBao OIDC client-secret read + any KV reads)
- `platform/playbooks/seed-n8n-secrets.yml`
- `platform/playbooks/sync-secrets-to-openbao.yml`
- `platform/playbooks/check-secrets.yml`, `validate-secrets.yml`
- `platform/playbooks/deploy-*.yml` and `clean-deploy-*.yml` that read/write KV directly (audit with the grep in Step 2)

- [ ] **Step 1: Find every remaining reference**

```bash
grep -rn "v1/secret/data\|v1/secret/metadata\|/secret/data/services\|/secret/metadata/services" platform/playbooks/
```

- [ ] **Step 2: Substitute** `secret` → `{{ bao_kv_mount | default('secret') }}` in each (preserving `data`/`metadata`/`services`).

- [ ] **Step 3: Verify** — the grep from Step 1 returns nothing (except the `| default('secret')` lines).

---

## Task 6: Parameterize the `hashi_vault` lookups

**Files:**
- `platform/playbooks/distribute-ssh-keys.yml`
- `platform/playbooks/harden-ssh.yml`
- `platform/playbooks/proxmox-validate.yml`
- `platform/playbooks/provision-vm.yml`

- [ ] **Step 1: Replace the embedded mount in each lookup** — e.g.

```yaml
# before: lookup('community.hashi_vault.hashi_vault', 'secret=secret/data/services/ssh:...')
# after:
"{{ lookup('community.hashi_vault.hashi_vault', 'secret=' ~ (bao_kv_mount | default('secret')) ~ '/data/services/ssh:...') }}"
```

- [ ] **Step 2: Verify** — `grep -rn "secret=secret/" platform/playbooks/` returns nothing.

---

## Task 7: Template the OpenBao policies

**Files:**
- Rename: 9 files `platform/services/openbao/deployment/config/policies/*.hcl` → `*.hcl.j2`
  (`inference-comfyui`, `inference-hunyuan3d`, `n8n-write`, `nemoclaw-read`, `nemoclaw-rotate`, `nocodb-write`, `orb-agent`, `semaphore-read`, `semaphore-write`)
- Modify: `platform/playbooks/tasks/apply-openbao-policy.yml` (file → template lookup)
- Modify: `platform/playbooks/apply-policy-*.yml` (7 files) + `apply-openbao-policies.yml` — update `_policy_file` extension to `.hcl.j2`

- [ ] **Step 1: In each policy, parameterize only the KV paths**

```hcl
path "{{ bao_kv_mount | default('secret') }}/data/services/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
}
path "{{ bao_kv_mount | default('secret') }}/metadata/services/*" {
  capabilities = ["read", "list"]
}
# sys/policies/acl/*, auth/approle/* paths stay literal (not KV mounts).
```

- [ ] **Step 2: Switch the apply task to render the template**

```yaml
- name: "Apply policy — {{ _policy_name }}"
  ansible.builtin.uri:
    url: "{{ _bao_url }}/v1/sys/policies/acl/{{ _policy_name }}"
    method: PUT
    headers:
      X-Vault-Token: "{{ _bao_auth.json.auth.client_token }}"
    body_format: json
    body:
      policy: "{{ lookup('template', _policy_file) }}"   # was lookup('file', _policy_file)
    status_code: [200, 204]
  delegate_to: localhost
```

- [ ] **Step 3: Update `_policy_file` paths** in `apply-policy-*.yml` + `apply-openbao-policies.yml` (e.g. `…/semaphore-read.hcl` → `…/semaphore-read.hcl.j2`).

- [ ] **Step 4: Verify** — `ansible-playbook --syntax-check` on an apply playbook; apply against live OpenBao (default mount) and confirm the rendered policy still reads `secret/data/services/*` (diff the policy via `bao policy read semaphore-read`). Update `config/policies/README.md` to note the `.j2` + `bao_kv_mount`.

---

## Task 8: Parameterize `scripts/local-dev.sh`

**Files:**
- Modify: `scripts/local-dev.sh` (the `creds()` reads of `secret/data/services/authentik`, `…/n8n`, etc.)

- [ ] **Step 1: Resolve the mount once + use it** — add near the top of `creds()` (and any other KV read):

```bash
local mount="${BAO_KV_MOUNT:-secret}"
# ... curl "${OPENBAO_ADDR}/v1/${mount}/data/services/authentik" ...
```

- [ ] **Step 2: Verify** — `shellcheck scripts/local-dev.sh` clean; `make local-creds` still prints the SSO logins (default mount).

---

## Task 9: Inject `BAO_KV_MOUNT` into the Semaphore environment

**Files:**
- Modify: `platform/semaphore/templates.yml` and/or `platform/semaphore/templates-local.yml` (the environment that already carries `BAO_ROLE_ID`/`BAO_SECRET_ID`/`OPENBAO_ADDR`)
- Modify: `platform/semaphore/setup-templates.yml` if the env is templated there

- [ ] **Step 1: Add `BAO_KV_MOUNT` to the Semaphore environment** sourced from `bao_kv_mount` so service `deploy.sh` + `bao-client.sh` resolve the instance mount. (Local default `secret`.)

- [ ] **Step 2: Verify** — re-run `setup-templates.yml`; a deploy task's env shows `BAO_KV_MOUNT=secret`.

---

## Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md` — the "OpenBao Secrets Layout" table: note paths are under `{{ bao_kv_mount }}/` (default `secret/`); add a one-liner under Secrets Management explaining per-instance mounts and the `bao_kv_mount` var.
- Modify: `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` — add a note that `bao_kv_mount` (mount-level) is **orthogonal** to `vault_secret_prefix` (path-prefix); they compose.
- Modify: `platform/services/openbao/deployment/config/policies/README.md` — `.hcl.j2` rendering + the variable.

- [ ] **Step 1:** Make the edits above. **Step 2:** `grep -n bao_kv_mount CLAUDE.md` shows the note.

---

## Task 11: Tests (bats guards)

**Files:**
- Modify: `platform/tests/test_common.bats` (or a new `test_bao_mount.bats`)

- [ ] **Step 1: Guard that the mount is parameterized, not hardcoded**

```bash
@test "bao: KV mount is parameterized (no hardcoded secret/data in code paths)" {
  # manage-secrets, bao-client, scripts must use bao_kv_mount / BAO_KV_MOUNT
  run grep -rn "v1/secret/data/services" "$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  [ "$status" -eq 1 ]
  run grep -q 'BAO_KV_MOUNT' "$REPO_ROOT/platform/lib/bao-client.sh"
  [ "$status" -eq 0 ]
}

@test "bao: default mount resolves to 'secret' (non-breaking)" {
  run grep -q "default('secret')" "$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  [ "$status" -eq 0 ]
  run grep -q 'BAO_KV_MOUNT:-secret' "$REPO_ROOT/platform/lib/bao-client.sh"
  [ "$status" -eq 0 ]
}

@test "bao: policies are templated with the mount" {
  run bash -c "ls $REPO_ROOT/platform/services/openbao/deployment/config/policies/*.hcl.j2 | wc -l"
  [ "$output" -ge 9 ]
  run grep -q "lookup('template'" "$REPO_ROOT/platform/playbooks/tasks/apply-openbao-policy.yml"
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 2: Verify** — `bats platform/tests/test_common.bats` (or the new file) passes.

---

## Task 12: Validation & rollout

- [ ] **Step 1: Warm re-run (default mount, regression).** `make local-bootstrap` + `make local-deploy-<svc>` for a couple of services with `bao_kv_mount` unset → KV-enable returns 400 (already at `secret/`), secrets read/write succeed, policies render identically. **No churn, no new mount.**
- [ ] **Step 2: Second-instance smoke (the actual capability).** In a throwaway inventory set `bao_kv_mount: agent-cloud-test`, bootstrap OpenBao only → confirm a new `agent-cloud-test/` mount is created and a service's secrets land under `agent-cloud-test/data/services/<svc>` (verify with `bao kv get agent-cloud-test/services/<svc>`), and the rendered policy scopes to `agent-cloud-test/data/services/*`.
- [ ] **Step 3:** `log()` / document that renaming a live instance's mount is **not** supported by this change (needs `bao secrets move`).

---

## Out of scope (record, don't implement here)

- **`vault_secret_prefix`** (path-prefix namespacing within a mount) — documented separately; composes with this if ever implemented.
- **OpenBao OIDC role group-binding** — the prod follow-up where `platform-developers` must NOT inherit the `platform-admin` policy (grounding/security finding 2026-06-16). Belongs in the prod OIDC hardening, not this mount refactor.
- **Migrating an existing mount's data** — `bao secrets move`; never triggered by defaulting to `secret`.

## Risks

| Risk | Mitigation |
|------|------------|
| A missed reference → a deploy reads/writes the wrong (or legacy) mount | Task 5 Step 1 grep is the completeness gate; Task 11 bats guard fails if `secret/data/services` literals remain in the core paths |
| Policy template not rendered (stale `lookup('file')`) → policy applied with literal `{{ }}` | Task 7 Step 4 reads back the live policy to confirm rendering |
| Semaphore env missing `BAO_KV_MOUNT` → bash libs default to `secret` while Ansible uses the instance mount (split-brain) | Task 9 makes the env explicit; second-instance smoke (Task 12 Step 2) would catch a split |
| KV-enable creates a new empty mount on a typo'd `bao_kv_mount` | Documented; per-instance value is set deliberately in site-config, reviewed |
