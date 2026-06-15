# §12A Bootstrap-Reorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Design rationale, rejected alternatives, and the probe findings live in `LOCAL-DEV-DEPLOYMENT.md` §12A — this doc is the execution decomposition only.**

**Goal:** Re-sequence the local-dev genesis so `make local-bootstrap` brings up the secure foundation (OpenBao → dns → step-ca → caddy → authentik) **directly** (Mac-direct, not through Semaphore), and Semaphore comes up **last, already OIDC-secured at boot**; `make local-up` then deploys only Tier-3 (o11y, opa, erpnext, netbox, n8n) through Semaphore.

**Architecture:** `bootstrap-local-dev.yml` gains a foundation-deploy stage (between OpenBao and Semaphore) that shells out to each existing `deploy-<svc>.yml` un-forked — Mac-direct (`connection: local`), with the bootstrap's own BAO AppRole creds in the environment, `COMPOSE_CMD` forced to podman-compose, and a genesis monorepo dir under `$HOME` (auto-mounted into the podman VM at the same path so compose bind-mounts resolve). Semaphore's start moves after the foundation and gains fail-safe OIDC env (jq-validated `SEMAPHORE_OIDC_PROVIDERS`, `SEMAPHORE_WEB_ROOT`, step-ca trust bundle via `SSL_CERT_FILE`).

**Tech Stack:** Ansible (localhost/`connection: local`), podman + podman-compose, OpenBao AppRole, Semaphore native OIDC, Authentik (issuer), step-ca (trust bundle), make + bash wrapper, BATS (static).

---

## Pre-flight context (read once)

- **Branch:** continue on `feat/local-dev-phase0` (no new branch).
- **Live stack is up** (OpenBao, Semaphore, dns, step-ca, caddy, authentik, o11y, opa, erpnext, n8n, netbox all running). This means **idempotent re-run validation is non-destructive** and is the primary in-loop test (§12A requirement #1). A cold `make local-clean && make local-bootstrap` wipes the live vault and forces re-deploy of stateful services — treat as an explicit, user-opted heavier test, NOT the default loop.
- **Two inventories exist and that is intentional, not a fork:**
  - `platform/inventory/local-dev.yml` (Mac-side; `connection: local`) — used by the wrapper guard, host lookups (resolver/https/tls), and **now the genesis Mac-direct foundation deploys**.
  - The static INI baked into `bootstrap-local-dev.yml` `_inv_ini` and stored *inside* Semaphore — used by **Tier-3 deploys through Semaphore** (paths under `/var/lib/agent-cloud-deploy`). Service-specific vars (zone, ports, routes, stepca_*, authentik_*) are duplicated between the two; keeping them in sync is a pre-existing hazard noted in the example inventory — do **not** attempt to de-dupe it in this plan.
- **assert-orchestrated.yml ships UNWIRED (Phase 0A)** and is not wired into deploy playbooks. The genesis Mac-direct deploys carry `BAO_ROLE_ID`/`BAO_SECRET_ID` in the environment, which is exactly the fallback marker `assert-orchestrated` accepts — so when it *is* wired later, the genesis path is already orchestration-valid with **no** per-playbook `_bootstrap_play` flag needed. No code change to assert-orchestrated in this plan.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `platform/playbooks/deploy-{dns,step-ca,caddy,authentik}.yml` + all others | Composable service deploys | Fix #2 (lazy `ansible_user`) globally; Fix #1 (`COMPOSE_CMD` passthrough) on the 4 foundation deploys' Phase-2 environment |
| `platform/inventory/local-dev.yml.example` | Mac-side committed inventory | Add `local_workspace_dir` to `all.vars`; repoint `local_monorepo_dir` at the genesis dir |
| `scripts/local-dev.sh` | Bootstrap wrapper | `init` substitutes the genesis dir; nothing else |
| `platform/playbooks/bootstrap-local-dev.yml` | Genesis orchestration | Insert foundation-deploy stage; move Semaphore start last; add fail-safe OIDC env |
| `Makefile` | Entry points | `local-up` drops the foundation targets (now in bootstrap) |
| `platform/tests/test_bootstrap_12a.bats` | Static structural guard | New — assert ordering/env/fixes are present |
| `plan/architecture/ACCESS-BOUNDARIES.md`, `plan/development/AUTH-SSO-DEPLOYMENT.md`, `docs/LOCAL-DEV.md`, `LOCAL-DEV-README.md`, `CLAUDE.md` | Docs | Reflect the reorder + Semaphore-OIDC done |

---

## Task 1: Fix #2 — lazy `ansible_user` default (mechanism fix, all playbooks)

**Why:** `_monorepo_dir: "{{ local_monorepo_dir | default('/home/' ~ ansible_user ~ '/agent-cloud') }}"` evaluates the `default(...)` argument **eagerly** (Jinja), so an undefined `ansible_user` errors even when `local_monorepo_dir` is set. The genesis `connection: local` hosts have no `ansible_user`. Fix the mechanism everywhere (every caller benefits; prod always defines `ansible_user`, so prod render is byte-identical).

**Files:**
- Modify: all 30 occurrences of the literal `'/home/' ~ ansible_user ~ '/agent-cloud'` under `platform/playbooks/`

- [ ] **Step 1: Write the failing static test**

Create `platform/tests/test_bootstrap_12a.bats` with:

```bash
#!/usr/bin/env bats
# §12A bootstrap-reorder structural guards (static — no live calls).
REPO="${BATS_TEST_DIRNAME}/../.."
PB="${REPO}/platform/playbooks"

@test "no deploy playbook uses an eager ansible_user default (fix #2)" {
  run grep -rn "~ ansible_user ~" "${PB}"
  [ "$status" -eq 1 ]   # grep finds nothing -> rc 1
}

@test "ansible_user default is lazy where _monorepo_dir is computed (fix #2)" {
  run grep -rn "ansible_user | default('deploy')" "${PB}/deploy-dns.yml"
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bats platform/tests/test_bootstrap_12a.bats -f "fix #2"`
Expected: both FAIL (eager form still present; lazy form absent).

- [ ] **Step 3: Apply the mechanism fix**

Run (single global, exact-substring — safe because the substring is uniform):

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
grep -rl "'/home/' ~ ansible_user ~ '/agent-cloud'" platform/playbooks \
  | xargs sed -i '' "s/'\/home\/' ~ ansible_user ~ '\/agent-cloud'/'\/home\/' ~ (ansible_user | default('deploy')) ~ '\/agent-cloud'/g"
```

- [ ] **Step 4: Verify the tests pass + nothing else moved**

Run: `bats platform/tests/test_bootstrap_12a.bats -f "fix #2"` → PASS.
Run: `git diff --stat platform/playbooks` → only the expected files changed; spot-check `deploy-dns.yml:20`.

- [ ] **Step 5: Commit**

```bash
git add platform/playbooks platform/tests/test_bootstrap_12a.bats
git commit -m "fix(local-dev): lazy ansible_user default so genesis local hosts render _monorepo_dir (§12A fix #2)"
```

---

## Task 2: Fix #1 — `COMPOSE_CMD` passthrough on foundation deploys

**Why:** The local stack is built with **podman-compose** (in the Semaphore container). On the Mac-direct genesis path, `detect_runtime` falls back to `podman compose` (→ docker-compose) when brew's `podman-compose` isn't on the ansible shell-task PATH, and docker-compose rejects the podman-compose-built network on a label mismatch. Let the deploy playbooks accept a `local_compose_cmd` var and inject it as `COMPOSE_CMD` (which `common.sh detect_runtime` already honors when preset). Semaphore path is unaffected (var unset → `omit` → existing derivation).

**Files:**
- Modify Phase-2 `environment:` in `platform/playbooks/deploy-dns.yml`, `deploy-step-ca.yml`, `deploy-caddy.yml`, `deploy-authentik.yml`

- [ ] **Step 1: Add a structural test**

Append to `platform/tests/test_bootstrap_12a.bats`:

```bash
@test "foundation deploys pass COMPOSE_CMD through from local_compose_cmd (fix #1)" {
  for svc in dns step-ca caddy authentik; do
    run grep -q "local_compose_cmd" "${PB}/deploy-${svc}.yml"
    [ "$status" -eq 0 ] || { echo "deploy-${svc}.yml missing local_compose_cmd"; return 1; }
  done
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bats platform/tests/test_bootstrap_12a.bats -f "fix #1"` → FAIL.

- [ ] **Step 3: Add the passthrough to each foundation deploy's Phase-2 environment**

In each of the four playbooks, the Phase-2 "Run deploy.sh" task `environment:` block currently has `CONTAINER_ENGINE` and `LOCAL_MODE`. Add one line:

```yaml
      environment:
        CONTAINER_ENGINE: "{{ container_engine | default('podman') }}"
        LOCAL_MODE: "{{ 'true' if (local_mode | default(false) | bool) else '' }}"
        COMPOSE_CMD: "{{ local_compose_cmd | default(omit) }}"
```

(For `deploy-caddy.yml`/`deploy-step-ca.yml` confirm the Phase-2 env block — if a deploy renders a cert in a separate become/cert task, only the `deploy.sh` task needs the var.)

- [ ] **Step 4: Verify the test passes**

Run: `bats platform/tests/test_bootstrap_12a.bats -f "fix #1"` → PASS.
Run: `ansible-playbook --syntax-check platform/playbooks/deploy-dns.yml` (and the other three) → no error.

- [ ] **Step 5: Commit**

```bash
git add platform/playbooks/deploy-dns.yml platform/playbooks/deploy-step-ca.yml \
        platform/playbooks/deploy-caddy.yml platform/playbooks/deploy-authentik.yml \
        platform/tests/test_bootstrap_12a.bats
git commit -m "fix(local-dev): foundation deploys honor local_compose_cmd as COMPOSE_CMD (§12A fix #1)"
```

---

## Task 3: Inventory — genesis monorepo dir + workspace dir

**Why:** Mac-direct genesis runs `place-monorepo` in local mode: rsync `local_workspace_dir` (the repo) → `local_monorepo_dir`, then deploy.sh runs from `local_monorepo_dir/...`. They must differ — the genesis dir must be a separate, writable copy under `$HOME` (auto-mounted into the VM at the same path), NOT the working tree (which deploys would otherwise mutate with rendered `.env`/certs).

**Files:**
- Modify: `platform/inventory/local-dev.yml.example` (`all.vars`)
- Modify: `scripts/local-dev.sh` (`init` sed substitutions)

- [ ] **Step 1: Update `all.vars` in the example inventory**

Replace the current `all.vars` block:

```yaml
  vars:
    local_mode: true
    monorepo_repo: "https://github.com/uhstray-io/agent-cloud.git"
    openbao_addr: "http://127.0.0.1:8200"
    # Genesis (Mac-direct) deploys rsync the working tree (local_workspace_dir)
    # into a SEPARATE writable copy under $HOME (local_monorepo_dir). $HOME
    # auto-mounts into the podman VM at the same absolute path, so deploy.sh's
    # compose bind-mounts (./config, ./certs) resolve on the VM engine. The two
    # MUST differ — never point local_monorepo_dir at the working tree (deploys
    # render .env/certs into it). The Semaphore (Tier-3) path uses its own paths
    # baked into bootstrap-local-dev.yml _inv_ini.
    local_workspace_dir: "__REPO_DIR__"
    local_monorepo_dir: "__GENESIS_DIR__"
    local_home_dir: "__GENESIS_DIR__"
    ansible_python_interpreter: "{{ ansible_playbook_python }}"
```

- [ ] **Step 2: Teach `init` the genesis-dir substitution**

In `scripts/local-dev.sh` `init()`, the heredoc sed currently substitutes `__REPO_DIR__` and `__HOME_DIR__`. Add `__GENESIS_DIR__` and drop the now-unused `__HOME_DIR__`:

```bash
    sed -e "s|__REPO_DIR__|${REPO_ROOT}|g" -e "s|__GENESIS_DIR__|${HOME}/.agent-cloud-genesis|g" \
      "$EXAMPLE" > "$INV"
```

- [ ] **Step 3: Verify the guard still passes**

Run: `REFRESH=1 make local-init` then confirm `[local-dev] guard OK`.
Run: `ansible-inventory -i platform/inventory/local-dev.yml --host dns-local` → shows `local_monorepo_dir` ending in `.agent-cloud-genesis`, `local_workspace_dir` = repo.

- [ ] **Step 4: Commit**

```bash
git add platform/inventory/local-dev.yml.example scripts/local-dev.sh
git commit -m "feat(local-dev): genesis monorepo dir under \$HOME for Mac-direct foundation deploys (§12A)"
```

---

## Task 4: Genesis foundation-deploy stage in `bootstrap-local-dev.yml`

**Why:** §12A — the foundation comes up directly during bootstrap, in dependency order, before Semaphore. Reuse each `deploy-<svc>.yml` un-forked via `ansible-playbook` shell-out (same pattern the play already uses for `setup-templates.yml`), carrying the bootstrap's BAO AppRole creds + `local_compose_cmd`.

**Files:**
- Modify: `platform/playbooks/bootstrap-local-dev.yml` (insert a stage after the AppRole secret-id task at line ~298, before "Stage 2: Semaphore")

- [ ] **Step 1: Add the genesis-deploy stage**

Insert after "Generate AppRole secret-id" (and before the current Stage 2 Semaphore tasks). Use a loop so it's ordered and idempotent; each foundation deploy is itself idempotent.

```yaml
    # ── Stage 1.5: genesis foundation deploys (Mac-direct, BEFORE Semaphore) ──
    # §12A: the secure foundation comes up directly here — dns → step-ca →
    # caddy → authentik — so Semaphore can boot LAST already OIDC-secured.
    # Each runs its EXISTING composable deploy-<svc>.yml un-forked, on localhost
    # (connection: local), with the bootstrap's BAO AppRole creds in env (which
    # is also the orchestration marker assert-orchestrated accepts) and
    # COMPOSE_CMD forced to podman-compose (§12A fix #1). The genesis monorepo
    # dir / workspace dir come from the local-dev inventory (Task 3).
    - name: "Discover podman-compose on the Mac (forces the compose provider)"
      ansible.builtin.command: command -v podman-compose
      register: _pc
      changed_when: false
      failed_when: _pc.rc != 0   # genesis REQUIRES podman-compose; fail loud, not silent fallback

    - name: "Genesis-deploy the secure foundation in dependency order"
      ansible.builtin.command:
        cmd: >-
          ansible-playbook -i {{ _inv_file }}
          {{ playbook_dir }}/deploy-{{ item }}.yml
          -e local_compose_cmd={{ _pc.stdout | trim }}
        chdir: "{{ playbook_dir }}/.."
      environment:
        BAO_ADDR: "{{ _bao_url_host }}"
        BAO_ROLE_ID: "{{ _role_id.json.data.role_id }}"
        BAO_SECRET_ID: "{{ _secret_id.json.data.secret_id }}"
        CONTAINER_ENGINE: podman
      loop: [dns, step-ca, caddy, authentik]
      register: _genesis
      changed_when: true
      no_log: false   # deploys/verification must stay diagnosable (CLAUDE.md no_log scope)
```

Add to the play `vars:` (near `_state_dir`):

```yaml
    _inv_file: "{{ playbook_dir }}/../inventory/local-dev.yml"
```

> NOTE: the foundation deploys target their inventory groups (`dns_svc` etc.) which already exist in `local-dev.yml` with `connection: local`. The BAO creds reach OpenBao at `127.0.0.1:8200` (the Mac-side `openbao_addr` in the inventory), NOT the container address — correct for Mac-direct.

- [ ] **Step 2: Add a structural test**

Append to `platform/tests/test_bootstrap_12a.bats`:

```bash
@test "bootstrap genesis-deploys the foundation before Semaphore (§12A order)" {
  bp="${PB}/bootstrap-local-dev.yml"
  foundation=$(grep -n "Genesis-deploy the secure foundation" "$bp" | cut -d: -f1)
  semaphore=$(grep -n "Start local Semaphore" "$bp" | cut -d: -f1)
  [ -n "$foundation" ] && [ -n "$semaphore" ]
  [ "$foundation" -lt "$semaphore" ]
}

@test "genesis loop covers dns step-ca caddy authentik in order" {
  run grep -E "loop: \[dns, step-ca, caddy, authentik\]" "${PB}/bootstrap-local-dev.yml"
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 3: Run the tests**

Run: `bats platform/tests/test_bootstrap_12a.bats` → all PASS (order test will currently fail if Semaphore tasks precede — confirm the loop sits above Stage 2).
Run: `ansible-playbook --syntax-check -i platform/inventory/local-dev.yml platform/playbooks/bootstrap-local-dev.yml` → OK.

- [ ] **Step 4: Commit**

```bash
git add platform/playbooks/bootstrap-local-dev.yml platform/tests/test_bootstrap_12a.bats
git commit -m "feat(local-dev): genesis-deploy secure foundation before Semaphore (§12A)"
```

---

## Task 5: Semaphore last + fail-safe OIDC env

**Why:** §12A — Semaphore starts last with OIDC already present (deps up): `SEMAPHORE_OIDC_PROVIDERS` (jq-validated; malformed value panics startup), `SEMAPHORE_WEB_ROOT`, step-ca trust bundle via `SSL_CERT_FILE`. Fail-safe: if step-ca/authentik aren't up (first pass mid-build), Semaphore still starts without OIDC — never leave the control plane unbootable. Local admin login retained as fallback.

**Files:**
- Modify: `platform/playbooks/bootstrap-local-dev.yml` (Stage 2 Semaphore start + recreate-detection)

- [ ] **Step 1: Compute the OIDC env (conditional + jq-validated)**

Before the "Start local Semaphore" task, add:

```yaml
    # OIDC is added only when its deps exist (step-ca trust + authentik issuer).
    # Absent on a first mid-build pass -> Semaphore still boots (fail-safe).
    - name: "Detect OIDC dependencies (step-ca + authentik RUNNING)"
      ansible.builtin.shell: |
        # `container exists` is true for stopped containers too; the downstream
        # `podman exec step-ca …` needs them actually RUNNING, so check State.
        running() { [ "$(podman inspect -f '{{ "{{" }}.State.Running{{ "}}" }}' "$1" 2>/dev/null)" = "true" ]; }
        running step-ca && running authentik-server && echo ready || echo notready
      register: _oidc_deps
      changed_when: false

    - name: "Read the Semaphore OIDC client secret from OpenBao"
      ansible.builtin.uri:
        url: "{{ _bao_url_host }}/v1/secret/data/services/authentik"
        headers: *bao_root_hdr
        status_code: [200, 404]
      register: _ak_secret
      when: _oidc_deps.stdout | trim == 'ready'
      no_log: true

    - name: "Build + jq-validate SEMAPHORE_OIDC_PROVIDERS"
      vars:
        _zone: "{{ dev_zone | default('agent-cloud.test') }}"
        _oidc_map:
          authentik:
            display_name: Authentik
            provider_url: "https://auth.{{ _zone }}:8443/application/o/semaphore/"
            client_id: semaphore
            client_secret: "{{ _ak_secret.json.data.data.semaphore_oidc_client_secret }}"
            redirect_url: "https://semaphore.{{ _zone }}:8443/api/auth/oidc/authentik/redirect"
            username_claim: preferred_username
            scopes: [openid, profile, email]
      ansible.builtin.set_fact:
        _sem_oidc_json: "{{ _oidc_map | to_json }}"
      when:
        - _oidc_deps.stdout | trim == 'ready'
        - _ak_secret.json.data.data.semaphore_oidc_client_secret is defined
      no_log: true

    - name: "jq-validate the OIDC JSON (a malformed value panics Semaphore)"
      ansible.builtin.command: "jq -e ."
      args:
        stdin: "{{ _sem_oidc_json }}"
      register: _jq
      changed_when: false
      when: _sem_oidc_json is defined
      no_log: true
```

- [ ] **Step 2: Mount the step-ca trust bundle + add OIDC env to the run command**

The Semaphore `podman run` must add (only meaningful when OIDC is present, but harmless otherwise):
- `-v {{ _bao_config_dir }}/../step-ca-bundle.crt:/etc/ssl/certs/step-ca-bundle.crt:ro` (write the bundle first — see sub-step) and `-e SSL_CERT_FILE=/etc/ssl/certs/step-ca-bundle.crt`
- `-e SEMAPHORE_WEB_ROOT=https://semaphore.{{ _dev_zone }}:8443`
- `-e SEMAPHORE_OIDC_PROVIDERS={{ _sem_oidc_json }}` (only when defined)

Write the bundle (step-ca root) to the state dir before the run, when deps are ready:

```yaml
    - name: "Export the step-ca root bundle for Semaphore TLS trust"
      ansible.builtin.shell: |
        podman exec step-ca cat /home/step/certs/root_ca.crt > {{ _state_dir }}/step-ca-bundle.crt
      register: _bundle
      changed_when: true
      when: _oidc_deps.stdout | trim == 'ready'
```

Then parameterize the run. Because the run is a `shell:` heredoc, build the OIDC/TLS flags as a fact and interpolate:

```yaml
    - name: "Compose Semaphore OIDC/TLS run flags"
      ansible.builtin.set_fact:
        _sem_oidc_flags: >-
          {{ ('-v ' ~ _state_dir ~ '/step-ca-bundle.crt:/etc/ssl/certs/step-ca-bundle.crt:ro '
              ~ '-e SSL_CERT_FILE=/etc/ssl/certs/step-ca-bundle.crt '
              ~ '-e SEMAPHORE_WEB_ROOT=https://semaphore.' ~ (dev_zone | default('agent-cloud.test')) ~ ':8443 ')
             if (_oidc_deps.stdout | trim == 'ready') else '' }}
      # SEMAPHORE_OIDC_PROVIDERS is added via the env-file path below (NOT inline)
      # so the JSON's quotes/spaces never break the shell command line.
```

> **Decision (record in commit):** pass `SEMAPHORE_OIDC_PROVIDERS` via `--env-file {{ _state_dir }}/semaphore-oidc.env` (written 0600 when `_sem_oidc_json is defined`) rather than inline `-e`, because the JSON contains spaces/quotes that would corrupt the `shell:` command line. Mount/flags for TLS+web-root can be inline (no special chars).

- [ ] **Step 3: Recreate Semaphore when the OIDC env changed**

Extend the existing "Recreate Semaphore if any required mount or label=disable is missing" check so a warm run that newly gained OIDC deps recreates Semaphore to pick up the env (compare presence of `SSL_CERT_FILE` in the running container's env vs `_oidc_deps`):

```yaml
        # ...existing mount/secopt checks, plus:
        OIDC=$(podman inspect {{ _sem_name }} --format '{{ "{{" }}range .Config.Env{{ "}}" }}{{ "{{" }}.{{ "}}" }} {{ "{{" }}end{{ "}}" }}' | grep -c SSL_CERT_FILE || true)
        # recreate when deps are ready but the container has no OIDC env yet
```

(Implement as: if `_oidc_deps` ready AND running container lacks `SSL_CERT_FILE` → `podman rm -f` → recreate.)

- [ ] **Step 4: Tests**

Append to BATS:

```bash
@test "Semaphore run is fail-safe: OIDC env only when deps ready" {
  bp="${PB}/bootstrap-local-dev.yml"
  run grep -q "SEMAPHORE_OIDC_PROVIDERS" "$bp"; [ "$status" -eq 0 ]
  run grep -q "jq-validate the OIDC JSON" "$bp"; [ "$status" -eq 0 ]
  run grep -q "SSL_CERT_FILE" "$bp"; [ "$status" -eq 0 ]
}
```

Run: `bats platform/tests/test_bootstrap_12a.bats` → PASS.
Run: `ansible-playbook --syntax-check -i platform/inventory/local-dev.yml platform/playbooks/bootstrap-local-dev.yml` → OK.

- [ ] **Step 5: Commit**

```bash
git add platform/playbooks/bootstrap-local-dev.yml platform/tests/test_bootstrap_12a.bats
git commit -m "feat(local-dev): Semaphore boots last, OIDC-secured + step-ca trust (fail-safe; §12A)"
```

---

## Task 6: Makefile — `local-bootstrap` = genesis; `local-up` = bootstrap + Tier-3

**Why:** §12A — `make local-bootstrap` now stands up the whole foundation + OIDC Semaphore; `make local-up` adds only Tier-3 through Semaphore. The foundation targets must leave `local-up` (they're in bootstrap now) to keep it idempotent and avoid double-deploy.

**Files:**
- Modify: `Makefile` (`local-up` recipe + the help text on `local-bootstrap`)

- [ ] **Step 1: Edit `local-up`**

```make
local-bootstrap: ## Genesis: OpenBao + secure foundation (dns,step-ca,caddy,authentik) + OIDC-secured Semaphore (idempotent)
	@$(LOCAL_DEV) bootstrap

local-up: ## Full stack: genesis (bootstrap) then Tier-3 services through Semaphore (idempotent)
	@$(MAKE) --no-print-directory local-bootstrap
	@$(MAKE) --no-print-directory local-deploy-o11y
	@$(MAKE) --no-print-directory local-deploy-opa
	@$(MAKE) --no-print-directory local-deploy-erpnext
	@$(MAKE) --no-print-directory local-netbox
	-@$(MAKE) --no-print-directory local-deploy-n8n
	@echo "[local-up] full stack up: foundation via genesis, Tier-3 via Semaphore."
```

Update the Tier comment block above `local-up` to reflect that Tier 0–2 are now genesis.

- [ ] **Step 2: Test**

Run: `make help` → shows the new descriptions; `make -n local-up` → expands to bootstrap + only the Tier-3 targets (no dns/step-ca/caddy/authentik).

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat(local-dev): local-up deploys only Tier-3; foundation moves into genesis bootstrap (§12A)"
```

---

## Task 7: Live idempotent re-run validation (non-destructive)

**Why:** §12A requirement #1 + validation. The stack is already up, so re-running bootstrap must converge — not duplicate/error — and must add OIDC to Semaphore since step-ca+authentik are up.

- [ ] **Step 1: Re-run genesis (warm)**

Run: `make local-bootstrap`
Expected: foundation deploys report healthy (idempotent); Semaphore recreated once to gain OIDC env (deps ready); play ends "Local control plane up".

- [ ] **Step 2: Control plane survived + OIDC present**

Run: `curl -sf http://127.0.0.1:3000/api/ping` → `pong`.
Run: `podman inspect local-semaphore --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'SSL_CERT_FILE|SEMAPHORE_WEB_ROOT'` → present.
Run: `podman inspect local-semaphore --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c SEMAPHORE_OIDC_PROVIDERS` → 1 (via env-file: confirm the login page instead — next step).

- [ ] **Step 3: Login page offers the OIDC button + discovery TLS-verifies**

Run: `curl -sk https://127.0.0.1:8443/api/auth/oidc/authentik/login -o /dev/null -w '%{http_code}\n'` (via caddy) → 30x redirect to Authentik (not 500/panic).
Run (TLS trust from inside the container): `podman exec local-semaphore sh -c 'SSL_CERT_FILE=/etc/ssl/certs/step-ca-bundle.crt wget -qO- https://auth.agent-cloud.test:8443/application/o/semaphore/.well-known/openid-configuration | head -c 80'` → JSON issuer (proves the bundle verifies the IdP).

- [ ] **Step 4: `platform-user` is denied at the IdP**

Confirm the §P1 binding: the `semaphore` application has the `platform-member` policy bound (it does via `zz-sso-bindings.yaml`). Record as a browser check (manual) — note in the validation log.

- [ ] **Step 5: Run the full static suite + lint**

Run: `bats platform/tests/` → green.
Run: `yamllint -c .yamllint.yml platform/playbooks platform/semaphore platform/inventory` and `shellcheck -S warning scripts/*.sh` → clean.

- [ ] **Step 6: (Optional, user-opted) cold test** — `make local-clean && make local-bootstrap` from an empty vault, then re-deploy stateful Tier-3. DESTRUCTIVE; only on explicit go-ahead.

---

## Task 8: Docs + revision history

**Files:**
- Modify: `plan/architecture/ACCESS-BOUNDARIES.md` (confirm/extend the bootstrap exemption to the foundation set)
- Modify: `plan/development/AUTH-SSO-DEPLOYMENT.md` (Semaphore OIDC control-plane side: PENDING → DONE)
- Modify: `docs/LOCAL-DEV.md`, `LOCAL-DEV-README.md` (bootstrap now = full foundation; local-up = Tier-3)
- Modify: `CLAUDE.md` (bootstrap-local-dev.yml description; Independent Workflows note if needed)
- Modify: `plan/development/LOCAL-DEV-DEPLOYMENT.md` §12A probe-findings → mark implemented; add a revision-history row

- [ ] **Step 1:** Update each doc to reflect the reorder; flip AUTH-SSO Semaphore row to implemented with the issuer/redirect specifics.
- [ ] **Step 2:** Add a §14 revision-history row dated 2026-06-15: "§12A implemented: foundation genesis-deployed before Semaphore; Semaphore boots OIDC-secured; fixes #1/#2 landed."
- [ ] **Step 3: Commit**

```bash
git add plan docs LOCAL-DEV-README.md CLAUDE.md
git commit -m "docs(local-dev): §12A bootstrap reorder implemented; Semaphore OIDC control-plane side done"
```

---

## Self-Review notes (author)

- **Spec coverage:** §12A requirements map → idempotent/re-runnable (Task 7), no-forks (Task 4 reuses deploy-<svc>.yml), fail-safe OIDC (Task 5), foundation set + Tier-3 split (Tasks 4/6), make targets (Task 6), fix #1 (Task 2), fix #2 (Task 1), validation (Task 7). ✓
- **Open risk to confirm at execution:** exact `SEMAPHORE_OIDC_PROVIDERS` field names for Semaphore v2.18 (`provider_url` vs `issuer`, `username_claim`) — verify against the running image's docs before trusting Task 5 Step 1; the jq-validate guard prevents a panic but not a wrong-field silent no-login. The Authentik side (issuer/discovery) is already validated per AUTH-SSO.
- **Env-file vs inline OIDC:** chosen env-file to avoid shell-quoting corruption of the JSON.
