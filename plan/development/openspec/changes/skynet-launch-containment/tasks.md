## 1. Prerequisites

- [x] 1.1 Production OPA refuses policy and data writes and serves only decisions and health (#805 and #810 merged and deployed; Deploy OPA's verify proves `PUT /v1/data` and `GET /v1/data` are refused)
- [x] 1.2 Confirm the current `agentcloud` rego ignores an added `settings` input field: `opa test /policies` green with a decision test that sends `settings`
- [x] 1.3 svc-skynet is `guest` in production (agent-cloud and site-config demotion PRs merged, Provision skynet Semaphore Access (Dev) re-run, launch probe 403)
- [x] 1.4 Validation gate: scenario "skynet's own token cannot launch" holds in production (403 on `POST /api/project/{p}/tasks` with skynet's token)

## 2. Gate service (agent-cloud, `platform/services/launch-gate/`)

- [x] 2.1 Request handling: bearer auth per caller (hmac compare), caller→agent map, JSON body limit, 401/403/400 refusals before any outbound call
- [x] 2.2 Validation: allowlist from rendered config, survey declarations read from Semaphore, literal-string rule (no `{{`, `{%`, `{#`; 4096-character cap), declared defaults filled, required fields enforced
- [x] 2.3 OPA call: skynet's decision input shape plus `settings`, launch only on `allowed: true`; deny → 403 with reason, error/unreachable/no `allowed` → 503
- [x] 2.4 Launch: task body built from scratch (`template_id`, `environment`, `message`, `params`), dedup against any running task of the template (409, check and create under one lock), task tag `<caller> <workflow_id> <step_id>`, check mode refused outside the verified Semaphore versions
- [x] 2.5 HTTPS listener that reloads its certificate when the file changes; structured decision/launch log lines without secrets
- [x] 2.6 Unit tests covering every refusal, the OPA verdict paths, the exact task body, dedup and certificate reload, with fake OPA and Semaphore servers
- [x] 2.7 Validation gate: scenarios "Undeclared key is refused", "Templated value is refused", "Template off the allowlist", "OPA denies", "OPA unreachable", "The created task holds only validated values" and "Task message for skynet" pass in the gate's unit tests

## 3. Deployment as code (agent-cloud)

- [x] 3.1 Compose (digest-pinned Python image, source read-only, certs mount, published on the declared LAN bind), deploy.sh (lifecycle only), env template (the digest pin is enforced by the refusal in `deploy-launch-gate.yml`)
- [x] 3.2 `deploy-launch-gate.yml`: input refusals, place repo, secrets (`caller_<name>_token` generated, `semaphore_api_token` existing), allowlist rendered from OPA's catalog excluding destructive templates, leaf issuance, deploy, verify (TLS health, 401 without token, 400 for an undeclared key, 403 for a non-allowlisted template, plain HTTP refused). Structure and refusals are tested in `platform/tests/test_launch_gate_playbooks.bats`; first live deploy: local-dev, task 3.4
- [x] 3.3 `clean-deploy-launch-gate.yml`, `provision-launch-gate-semaphore-access.yml` (shared identity task, `task_runner`, launch probe 404). First live run: local-dev, task 3.4
- [x] 3.4 Templates (base with Dev variant, Local) in `templates.yml` / `templates-local.yml`; local-dev inventory and bootstrap entries (local-dev bootstrapped from dev 47d902fb on 2026-11-04; local Semaphore tasks 11594 Provision launch-gate Semaphore Access (Local), 11596 Deploy launch-gate (Local), every verify step ok)
- [x] 3.5 Renew Internal Certs: generalise the OPA server-leaf proof into a declared-listener proof and admit `launch-gate-server`; tests for both leaves
- [x] 3.6 deploy-skynet: shared-read `caller_skynet_token`, render `SKYNET_LAUNCH_GATE_URL` and the gate's name mapping (its own overlay, `compose.launch-gate.yml`, loaded only when the gate is declared: in `compose.prod.yml` its required variables would stop every production deploy without a gate), verify skynet's container reaches the gate (401 without token) (code merged in #869. Local-dev, 2026-11-07, task 13411: Phase 1, which holds the gate-token shared read and the `.env` render, completed, and the run stopped at the GHCR login because the site-config PAT backup seeded for it (`github-ai-automation`) answers 401 at GitHub. The in-container 401/400 probes have not run; they run at 5.3.) Proven in production 2026-11-09: Deploy skynet (Dev) task 14142 (check-mode run 14141 first), from the inventory synced after site-config #164: the in-container probes answered 401 without a token and 400 bad_request with skynet's caller token, so the container reaches the gate over TLS through the name mapping. Local-dev not yet re-run.
- [x] 3.7 Docs: service CLAUDE.md, AGENTS.md rows (secrets layout, workflows, sub-directory docs), playbooks README
- [x] 3.8 Validation gate: scenarios "Missing token" and "Caller claims another identity" pass against the locally deployed gate (2026-11-04: no token -> 401 unauthorized, logged with no caller; skynet's token naming an agent outside its set -> 403 agent_not_permitted, logged with no OPA reason and no task; only skynet is a local caller, so the second scenario used an agent outside skynet's set)

## 4. skynet (skynet repo)

- [x] 4.1 Semaphore client: `launch()` calls the gate when `SKYNET_LAUNCH_GATE_URL` is set, refuses to launch otherwise; reads (`task`, `wait`, `output`) keep using skynet's guest token (skynet#14, merged 2026-11-09 as `73bcbafc`; the call site forwards the feeding step's proposal and context so the gate's OPA input carries them)
- [x] 4.2 Tests with a fake gate: request shape, error mapping (403/409/400/503 recorded as step `fail`), no direct-launch fallback (skynet#14: `tests/test_semaphore.py` and a graph-level test in `tests/orchestrator/test_service_onboarding.py`)
- [x] 4.3 Publish the image, record its digest (publish-tier2 run 42554401870 on skynet `main` `73bcbafc`: `ghcr.io/uhstray-io/skynet-tier2@sha256:7bcd4153bb1c22462bfefad0bcaec740ea51c18249d0fc6f8a94f5d01884578a`, pinned in production by site-config #164)
- [x] 4.4 Validation gate: scenario "Task message for skynet" holds end to end: skynet's client, launching through the locally deployed gate, creates a task tagged `skynet <workflow_id> <step_id>`
  - **Done 2026-11-09, local-dev**, with `Probe skynet Launch (Local)` (#952): from inside skynet's container it starts one check-mode `service_onboarding` run bounded to registry step `service-validate`, then requires exactly one Semaphore task tagged `skynet <run_id> service-validate`. Local stack: OPA task 14129, launch gate 14131, skynet 14133 (image from skynet#14, pinned locally by #939).
    - First run, local task 14161: skynet's request reached the local gate intact, and OPA refused it with "an agent task may run only from main or dev", because local-dev's working-tree repository record declares `HEAD`. Fixed by #951: in local mode only, the gate reads `HEAD` as `dev` (design decision 12).
    - After the local gate was redeployed with that fix (local task 14201), local task 14202 passed. OPA answered "allowed by agent policy", and the gate created task 14203, the one task tagged `skynet service_onboarding:62a20720-7001-4217-8ae8-aefc08645d8b service-validate`. Task 14203 itself errored on a missing local `opa-local` health URL (#950, fixed by #945), which does not bear on the launch path.

## 5. Production rollout

- [x] 5.1 site-config: `launch_gate_svc` host and VM declaration (NetBox IP reserved first), `internal_leaves` entry, firewall (API port from `skynet_svc` and `console_svc`), OPA `firewall_allow_groups` gains `launch_gate_svc`, `semaphore_project_members` gains `svc-launch-gate: task_runner`; inventory synced
  - **Done 2026-11-08/09.** Address reserved by `Allocate NetBox IP (Dev)` task 13901 (outside the pfSense DHCP range). site-config #158 declared the host, VM, firewall and OPA's admission; #162 declared the leaf and the team member once the VM and the account existed (declared earlier, the daily renewal and Manage Semaphore Access would have failed on them). Inventory synced after each with `scripts/semaphore-sync-inventory.py`. Placement (Joe, 2026-11-08): crabnebula, vmid 226, beside OPA and skynet.
- [x] 5.2 Workflow steps in order, check mode first: lookup, validate address, provision, keys + backup, harden, firewall, grow root if needed
  - **Done 2026-11-08/09**, each step check mode first:
    - Lookup Service Inventory (Dev) task 13916; Validate Address Free (Dev) task 13918 (no ARP answer; NetBox VM record created).
    - Provision VM (Dev): task 13921 timed out waiting for the template clone on its source node (the wait is 60 x 10 s, `provision-vm.yml`); the re-run task 14075 resumed, migrated the VM to crabnebula and passed (key login, agent, cloud-init done).
    - Generate Service SSH Key (Dev) 14080; Distribute SSH Keys (Dev) 14082; Back Up Service SSH Key (Dev) 14084 (site-config #161).
    - Verify Host Access (Dev) 14086, plus a key-only login from an operator workstation; Harden SSH (Dev) 14090.
    - Apply Firewall (Dev) on `launch_gate_svc` 14092 (8443 from skynet and the console) and on `opa_svc` 14094 (OPA admits the gate on 8281).
    - No root grow: the disk is declared at the template's 20G, as for OPA.
- [x] 5.3 Provision launch-gate Semaphore Access (Dev), Deploy launch-gate (Dev); skynet image pin bumped and Deploy skynet (Dev); Deploy skynet's verify runs task 3.6's in-container gate probes (401 without a token, 400 bad_request with skynet's)
  - **Gate half done 2026-11-09:** templates published by `Publish Semaphore Template Surveys (Dev)` (tasks 14097, 14100, 14103); Provision launch-gate Semaphore Access (Dev) task 14105 (account created at `task_runner`, launch probe 404, token stored); Deploy step-ca (Dev) task 14110 (name policy gains the gate's SAN; root fingerprint unchanged); Deploy launch-gate (Dev) task 14113 (leaf issued; `/healthz` 200 over TLS, plain HTTP refused, 401/403/400 refusals, and OPA's `/health` 200 from inside the container through the name map).
  - **Skynet half done 2026-11-09:** site-config #164 bumped `skynet_image` to the skynet#14 build (`sha256:7bcd4153…`) and declared `skynet_launch_gate_url` and `skynet_launch_gate_ip`; inventory synced with `scripts/semaphore-sync-inventory.py` (apply verified, re-check matched); Deploy skynet (Dev) check-mode task 14141, then task 14142: the in-container probes answered 401 without a token and 400 `bad_request` with skynet's caller token.
- [x] 5.4 Back up the gate's credentials to site-config (caller tokens; not the launch token)
  - **Done 2026-11-09:** `Back Up Credentials to site-config (Dev)` task 14115 with `credential_fields=caller_skynet_token` (site-config #163).
- [x] 5.5 Validation gate: in production, scenarios "OPA allows" (a check-mode launch of a read-only snapshot template through the gate as `service-agent`), "Duplicate launch while running", "Dry run", "Plain HTTP" and "Another LAN host" hold, and "Declared team matches" passes in Manage Semaphore Access check mode (allow path: `Probe launch-gate (Dev)`)
  - **Done 2026-11-09:**
    - OPA allows, Duplicate launch, Dry run: `Probe launch-gate (Dev)` task 14117. The first launch answered 201 and created task 14118 (`Snapshot Service Assessment (Dev)`, `params.dry_run` true, message `skynet probe-1794232620 service-assess`, launched by the gate's account); the immediate repeat answered 409 `already_running`.
    - Plain HTTP: Deploy launch-gate (Dev) task 14113's verify.
    - Another LAN host: `Probe Reachability (Dev)` 8443 from `dns_svc` closed (task 14127); from `skynet_svc` (14123) and `console_svc` (14125) open.
    - Declared team matches: `Manage Semaphore Access (Dev)` check mode task 14132 (nothing to add, re-role or remove).
    - Also: `Verify Service Health (Dev)` task 14134 (200).
