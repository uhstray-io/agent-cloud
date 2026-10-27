# Tasks: agentgateway as the inference edge gateway

## 0. Branch, decision record, host
- [x] 0.1 Feature branch from `dev`: `feat/inference-gateway-agentgateway`. Pull requests
      only when Joe asks for them (repo rule). 2026-09-17: the exact name was held by a
      stale codex worktree (one superseded proposal commit), so the work is on
      `feat/inference-gateway-agentgateway-impl`; the stale branch is Joe's to delete
- [x] 0.2 Draft the `plan/architecture/` record (agentgateway inference edge; skynet
      orchestrating gateway; distinct authorities; alternatives) as Proposed; Joe confirms
      the text before it is Accepted. Drafted 2026-09-17 as a Proposed section in
      `plan/architecture/05-platform-infra.md` (the repo's decision convention: numbered
      docs, no separate ADR directory)
- [x] 0.3 Allocate the gateway VM in site-config `proxmox/vm-specs.yml` (Infrastructure
      tier, Podman); provision through onboarding phases 1 to 2; AppRole
      `agentgateway` with read on `secret/services/agentgateway`
      2026-09-17 PARTIAL: declared on site-config branch `feat/agentgateway-host` —
      vm-specs entry on one hypervisor node (2c/4 GB/20G, podman; node and vmid in site-config) and the `agentgateway_svc`
      inventory group (upstream = the current Caddy inference upstream, four
      identities, firewall vars). Address chosen from the inventory's declared set
      because NetBox (IPAM) was unavailable; recorded as a future feature in
      `plan/architecture/02-service-onboarding.md` Known Gaps. NOT yet done, blocked
      off-LAN (prod Semaphore is 403 from outside): NetBox reserve, Provision VM,
      SSH key generate/distribute/verify/harden, Apply Firewall, AppRole.
      2026-09-18 (on-LAN, via the prod Semaphore API with the operator token): inventory
      synced to record `production` (sync-inventory.yml, operator-side); `vm_*` added to the
      host because the runner never sees vm-specs.yml. NetBox reservation NOT done: the
      IPAM read needs `secret/services/netbox:automation_api_token`, which is absent, and
      `Provision NetBox Automation Token` (task 1058) fails inside its no_log Django-shell
      mint — NetBox side, out of this change; the address keeps its provenance note.
      vmid moved to a free one: the first choice and its neighbour were the GitHub runners',
      absent from the ledger (adopted into vm-specs); provision-vm.yml gained a foreign-VM refusal guard (MISTAKES 3.5).
      Provisioning on that node then hit Proxmox's rule that a cross-node clone needs SHARED
      source storage (the template sits on another node's local LVM storage; task 1064) —
      fixed as clone-on-template-node + offline migrate in agent-cloud PR #188 (from dev,
      Joe's call); `Provision VM (Dev)` runs it once merged. SSH keypair minted (task 1061)
      and backed up to site-config branch `backup/ssh-agentgateway-20260918T115852Z-6a5828`
      (task 1063, dev-bound template — the playbook is not on main yet).
      Provision VM (Dev) with the clone-then-migrate path then created the VM
      (task 1068), but at an address that turned out to be gh-runner-01's: the runners
      were declared only on site-config's unmerged `feat/apply-firewall` (MISTAKES 3.6).
      Joe's decisions: destroy the VM AS CODE (`destroy-vm.yml`, PR #189, with an
      address-answers refusal in provision-vm), re-provision at another address (network-swept,
      undeclared on every branch), and fold the runner declaration into the inventory
      (done; Semaphore record re-synced). PR #189 merged; `Destroy VM (Dev)` stopped the VM
      but Proxmox's unreferenced-disk scan aborted on its node ("no such logical volume
      pve/data": the cluster-wide local-lvm storage is absent on that node). Joe rejected
      skipping the scan (dead disks waste space); PR #190 makes the play sweep the node's
      ACTIVE image storages for leftover volumes itself. Merged; `Destroy VM (Dev)` task 1075
      destroyed the VM cleanly: the node's image storages swept, zero leftovers, vmid gone.
      DONE 2026-09-18: `Provision VM (Dev)` task 1076 re-created the VM at the new address (address
      guard passed, clone on the template's node + offline migrate); cloud-init done; Distribute SSH
      Keys 1078; Verify Host Access 1079 (controller side) + workstation key-only login;
      Harden SSH 1080 (password REJECTED, key CONFIRMED, NOPASSWD sudo). Apply Firewall waits
      for the gateway deploy so port auto-detection sees the containers. Per-service AppRole
      not created: the composable deploy runs under the controller AppRole like every other
      service (onboarding step 9 is optional)
- [x] 0.4 Validation gate: `openspec validate inference-gateway-agentgateway --store
      agent-cloud` passes; record file exists as Proposed; VM answers over the
      distributed key. 2026-09-18: all three hold (validate passes; plan 05 section is
      Proposed; key-only SSH to the VM from both the controller and a workstation)

## 1. Service onboarding
- [x] 1.1 `platform/services/agentgateway/deployment/compose.yml`: image
      `${AGW_IMAGE:-cr.agentgateway.dev/agentgateway:v1.5.0}`, command `-f
      /config.yaml`, config mounted read-only, publish `${AGW_BIND:-127.0.0.1}:${AGW_PORT:-4000}:4000`,
      `readinessAddr` health check, no admin port published; `compose.local.yml`
      overlay per the o11y pattern
      2026-09-17: done, plus `agentgateway-db` (Postgres for per-key budgets, operator's choice) — see 4.2; no compose healthcheck on the gateway (image has no shell), readiness probed from the sibling db container
- [x] 1.2 `templates/config.yaml.j2`: `llm.port: 4000`; one model entry per served name
      with `provider: {custom: {formats: [{type: completions}]}}`,
      `params.baseUrl: http://{{ agw_vllm_upstream }}/v1`, `params.apiKey: $VLLM_API_KEY`;
      `frontendPolicies.http.maxBufferSize: 20971520`; `config.statsAddr` on the LAN bind;
      `config.tracing.otlpEndpoint` to the o11y host; no `retry`, no `requestTimeout`
      2026-09-17: done; `config.database.url: $AGW_DATABASE_URL` added; limits shape per 4.2
      2026-09-28 correction (the checked text above is kept as written): two names in it
      are not what landed. The upstream variable is `agw_upstream_base_url`, rendered
      whole as `params.baseUrl: {{ agw_upstream_base_url }}` (`config.yaml.j2:190`;
      asserted at `deploy-agentgateway.yml:76-77`); `agw_vllm_upstream` appears in no
      commit under `platform/` (`git log -S`, 2026-09-28). Tracing is
      `frontendPolicies.tracing` (`config.yaml.j2:99-106`, PR #295), not
      `config.tracing.otlpEndpoint`. Also as landed: the listener is
      `gateways.default.port: 4000` (lines 56-58), and `statsAddr` is `0.0.0.0:19002`
      inside the container (line 35), published on `AGW_STATS_BIND`, which defaults to
      loopback (`templates/env.j2:13`). The same applies to 1.8's
      `config.logging.fields.add.identity`, now `frontendPolicies.accessLog.add.identity`
      (lines 85-87)
- [x] 1.3 `templates/env.j2` (gitignored `.env` at deploy): `VLLM_API_KEY` from OpenBao
      `secret/services/agentgateway:vllm_api_key`; client keys rendered into the
      `apiKey` policy from `secret/services/agentgateway/clients/*`. 2026-09-17: client
      keys live as fields `client_<name>` on the SAME secret path (manage-secrets reads
      one path per service and already does generate-once/reuse), rendered as sha256
      hashes — the rendered config carries no plaintext key
      2026-09-17: done (see the note above on where client keys live)
- [x] 1.4 Read `statsAddr` once on the running container and record what it serves in
      `context/architecture.md`; if it is not Prometheus text, find the documented
      metrics endpoint and update task 3.1. 2026-09-17: Prometheus text on `/metrics`
      (`/` and `/stats` 404); task 3.1 scrapes `/metrics` on the stats port
- [x] 1.5 `deploy-agentgateway.yml` (place-monorepo, manage-secrets, deploy.sh, verify
      readiness and one `/v1/models` through the gateway) and `clean-deploy-agentgateway.yml`
      2026-09-17: done; carries the zero-hosts pre-flight and the OpenBao transport guard (both found missing on the first runs — MISTAKES 2.21); verify runs over the compose network, no keyed request (a key on an exec argv is a token on a command line — the keyed proof is task 2's conformance script)
- [x] 1.6 `platform/tests/test_service_agentgateway.bats`: env-param image, pinned tag,
      readiness health check, no published admin port, config template has no literal
      key and no `retry` block, `deploy.sh` container-only
      2026-09-17: 18 tests green; full suite 576 BATS + 120 pytest
- [x] 1.7 Validation gate: second deploy reports no changes and readiness answers,
      proving scenario "Deploy converges"; `nc` to the admin port from a LAN host is
      refused, proving scenario "Admin interface is not exposed"

      2026-09-17 LOCAL (LM Studio upstream, local Semaphore tasks 595 then 596): second run `changed=2` — the deploy.sh shell step (`changed_when: true` by the repo's convention) and the monorepo copy; every other task unchanged, readiness 200 before and after, secrets reused. Admin port: zero published mappings and connection refused from the Mac; the LAN-host refusal is re-proven on the prod VM
- [x] 1.8 Operator UI (design §9): `gateways.ui` on :4001 + `ui.gateways: ui` +
      `ui.policies.oidc` (issuer/redirect from inventory, `$AGW_OIDC_CLIENT_SECRET`) +
      `authorization` admin-group rule in the config; `OIDC_COOKIE_SECRET` derived from a
      stored seed; compose publishes `AGW_UI_BIND:AGW_UI_PORT` (local overlay removes it);
      Authentik catalog entry `agentgateway` (oidc, admin tier, `prod_required`
      redirect/launch vars) + `agentgateway-oidc.yaml` with `!Env
      AGENTGATEWAY_OIDC_CLIENT_SECRET`, shared-read by the gateway deploy; step-ca trust
      via `SSL_CERT_FILE` locally; local Caddy route `admin.inference.<zone>` ->
      `agentgateway:4001` as a PLAIN proxy in the example, working inventory and the
      control plane's route table; BATS. Prod: site-config `caddy_managed_sites` block +
      `agentgateway_external_host` on the authentik host + `agentgateway` in `authentik_apps`
      Playground (Joe, 2026-09-17): `llm.gateways: [default, ui]` so the UI calls /v1 on
      its own origin through Caddy (verified apiKey-gated, not OIDC-gated; completion
      round-trips at admin.inference.agent-cloud.test/v1). Observability per upstream:
      `config.metrics.fields.add.identity` + `config.logging.fields.add.identity`
      (first-token latency is already a default log field on streams). Upstream key per
      the api-keys doc: `params.apiKey: $VLLM_API_KEY` env reference, omitted when the
      upstream takes no key; proven 2026-09-17 with LM Studio switched to a required token,
      seeded via `seed-openbao-key.yml` (task 616: completions 200, no plaintext in config)
- [ ] 1.9 Validation gate: unauthenticated browser to `https://admin.inference.<zone>` is
      redirected to Authentik; after `agent-cloud-admin` login the UI renders and
      `/ui/api/config_dump` is reachable only through that path, proving scenario "UI
      requires an admin login"; on prod, `nc` to :4001 from a non-Caddy LAN host is refused.
      2026-09-17 LOCAL: `admin.inference.agent-cloud.test` resolves (wildcard), the served
      cert carries `*.inference.agent-cloud.test`, an unauthenticated GET is a 302 to
      Authentik's authorize endpoint, the outpost ping answers 204, the worker applied
      the blueprint; the local overlay removes the UI host publish so no unauthenticated
      path exists. Browser login as `agent-cloud-admin` and the prod `nc` are Joe's/prod
      2026-10-06 (task id and output reported by the coordinator; the task output was not read
      here): prod network half PROVEN. Probe Reachability (Dev) task 2985, a TCP connect from
      the o11y host (a LAN host that is not the Caddy host) to the gateway host, reported
      `4001 closed; 4000 open`. 4001 is the UI listener
      (`platform/services/agentgateway/deployment/compose.yml:25,46`; `agw_ui_port: "4001"` in
      the private inventory, bound to the VM address), and `probe-reachability.yml`
      counts `closed` only for a refusal or timeout, never a resolution or network error; the
      open 4000 shows the probing host reaches the gateway. Still open: the browser half (302 to
      Authentik on prod, `agent-cloud-admin` login renders the UI, `config_dump` only through
      that path), which is Joe's. Not ticked.

- [x] 1.10 `UI_READ_ONLY=true` in the gateway environment, and a BATS assertion that it is
      rendered; the UI must refuse writes itself, not only fail at the read-only mount
      (design "Decisions recorded 2026-09-27"). v1.5.0 reads the variable and switches the
      config store to read-only (`crates/agentgateway/src/config.rs:390-392` at tag
      v1.5.0), and the UI refuses writes in that mode (`crates/agentgateway/src/ui.rs:53`).
      This is the only UI read-only task; `inference-personal-keys` relies on it. Proves
      scenario "The UI refuses configuration writes"
      2026-09-28: done in PR #303 (`templates/env.j2` renders `UI_READ_ONLY=true`
      unconditionally, plus a BATS assertion on the rendered env). Merged into `dev` on
      2026-09-28 (merge `255b251`). `agentgateway-observability`
      decision 5 makes it a prerequisite for `agw_content_logging: full`
- [x] 1.11 Operator UI in production: Cloudflare record `admin.inference` (applied
      2026-09-27, Apply Cloudflare Tofu (Dev) task 1616, zero-diff 1617), the Authentik skip
      rule (PR #289) applied, `agw_ui_enabled: true` (site-config #35), then Deploy
      agentgateway (Dev); proves gate 1.9
      2026-10-08: ticked; the evidence is already recorded in this change: Cloudflare record
      `admin.inference` applied and zero-diff (tasks 1616/1617, above); the skip rule
      `authentik-oidc-bypass-challenge` is declared in `platform/infra/cloudflare/waf.tf:135`;
      the `admin.inference` Caddy block applied through Manage Caddy Sites (Dev) tasks
      2463/2464 (task 6.2); `Deploy agentgateway (Dev)` tasks 2460/2462 ran with the UI
      listener (task 6.1). `agw_ui_enabled: true` (site-config #35) is as quoted in the
      task text above; it was not re-read in site-config here. Gate 1.9 stays open: its
      browser half (login as `agent-cloud-admin`, the UI rendering) is still the operator's to do
      2026-10-08 (review of PR #488): the skip rule is APPLIED, not only declared. Apply
      Cloudflare Tofu (Dev) task 3006 (2026-10-05) ran at `9edcd9ec`, which contains the rule's
      commit `ef115877`, and its plan reported `plan_changes: "0"`, `plan_actions: []`: the
      live zone already matched the declared rule. Output read from Semaphore on 2026-10-08.
- [x] 1.12 Change-aware deploy (design decision 11). Today `deploy.sh:47-53` runs
      `compose up -d --force-recreate` on every run, so every deploy, and every playbook
      that imports it, drops in-flight streams. First settle hot reload: v1.5.0 watches a
      file config source and reloads it on change (`crates/agentgateway/src/state_manager.rs:141-142`
      and `150-180` at tag v1.5.0, read 2026-09-28), but `compose.yml:48` mounts
      `config.yaml` as a single file, and a template write replaces the inode the container
      still holds (the lesson at `manage-caddy-sites.yml:14-20`). In local-dev, mount the
      config through a directory, change one `apiKey` entry, and record whether the running
      process serves the change with no restart and whether a failed reload keeps the old
      state (`state_manager.rs:301-311` logs the error and keeps the previous state). Then:
      - **Hot reload confirmed:** the config becomes a directory mount; a `config.yaml`
        change is left to the watch and proven by the verify phase's keyed probe (6.1a);
        only an `.env` change recreates, because a process's environment is fixed at start.
        The directory stays mounted `:ro` and holds `config.yaml` only: the deploy renders
        into a dedicated subdirectory, never mounts the deploy directory, and never places
        `.env` (0600, database URL and upstream key, `templates/env.j2:26`, `:32`) where
        the container can read it as a file. The drill's directory mount follows the same
        rule. `agentgateway-observability` decision 5 depends on this, and its task 2.7
        asserts it in BATS.
      - **Not confirmed:** `deploy.sh` computes the sha256 of the rendered `config.yaml` and
        `.env`, reads the value recorded in a label on the running `agentgateway`
        container, and passes `--force-recreate` only when the two differ; otherwise it
        runs plain `compose up -d`, which creates a missing container and starts a stopped
        one. unverified: that the podman compose provider starts a stopped container on
        plain `up -d`; the local drill checks it.
      Either way the deploy task's `changed_when` reports a recreate or a reload, not the
      constant `true` it carries today (`deploy-agentgateway.yml:206`). BATS: the recreate
      flag is conditional and the label is written. Validation: a second deploy with no
      input change leaves the container's start time unchanged; a rotated key is served
      after one deploy; a deploy whose `deploy.sh` is made to fail after the render is
      converged by the next plain deploy, because the label still names the old hash;
      with the container removed, a plain deploy brings it back. Together these prove
      scenario "An unchanged deploy does not restart the gateway"
      2026-10-02: change-aware deploy merged in PR #382. The live drill (unchanged deploy keeps
      the start time; rotated key served; failed deploy converged; removed container restored) is
      not run
      2026-10-04: `Deploy agentgateway (Dev)` re-run at `21891372` with unchanged inputs, task 2745
      (dry run 2744 first): deploy.sh reported "running gateway matches the rendered inputs and
      image" and "deploy-result: unchanged". The previous changed deploy was task 2696 (`79a8eb76`).
      Not ticked: the key-rotation leg (live key, operator) and the failed-deploy and
      removed-container legs (local-dev) are not done.
      2026-10-05: `Deploy agentgateway (Dev)` task 2880 recreated the gateway for a real input
      change (the stream-usage transformation, #437), the changed-deploy half. The legs above are
      still not done; not ticked.
      2026-10-09: ticked. The four validation legs ran on local-dev (local Semaphore, `dev` at
      `8c441f8d` plus the local Semaphore port fix), each through `scripts/local-dev.sh` and the
      worktree-bound (Local) templates, output read from the local Semaphore:
      - unchanged deploy, local task 5324: the gateway's start time was identical before and after;
      - removed container, 5325: `podman rm -f agentgateway`, then a plain deploy recreated and
        verified it; the input-hash label was the same value as before;
      - failed deploy converged: `deploy.sh` was made to fail after the render (a temporary
        `exit 1` on its first line, restored byte-exact after, `cmp` and a clean `git diff`)
        while `Manage agentgateway Client Key` rotated `dev-local`, 5328: the rotation was
        recorded, `deploy.sh` failed, the running gateway kept its start time and its old label
        (`dce56b02…`). The next plain deploy, 5329, reported "deploy-result: recreated (inputs
        changed)", the label moved to `fa0eea35…`, and the keyed probes as `dev-local` passed;
      - rotated key served after one run, 5330: a normal rotate re-rendered, recreated
        (label `fa0eea35…` to `097bf61f…`) and its verify passed with the new key.
      An inventory-only change could not serve as the leg-C input: the local Semaphore runs from
      the bootstrap's own static inventory (`bootstrap-local-dev.yml`, `agw_models` in the INI),
      not from `local-dev.yml`. The hot-reload experiment (directory mount, change one `apiKey`,
      watch for a reload without restart) was NOT run; the "not confirmed" branch is the one taken
      by default and built (PR #382), so `inference-personal-keys` takes its cohort-rotation branch.
      The text's "unverified: plain `compose up -d` starts a stopped container" is moot: `deploy.sh`
      never takes that path; a missing or stopped gateway sets a recreate reason and runs
      `up -d --force-recreate` (`deploy.sh`, the RECREATE_REASON branch), which leg B exercised.
      Between the cited runs, local task 5326 was the first leg-C attempt (an inventory-only model
      alias plus the failing `deploy.sh`; it failed as forced) and 5327 its follow-up, which changed
      nothing because that inventory edit never reached the local Semaphore's static inventory;
      leg C was then redone with a key rotation (5328/5329). The local Semaphore ran on a
      non-default host port through the fix in PR #498 (`local_semaphore_port`).

## 2. Conformance against direct vLLM
      Added 2026-09-22 (security review): the gateway's `platform-admins in jwt.groups` rule
      has never been shown DENYING anyone — log in as a platform-developers member and
      require a refusal at the gateway (the IdP-side admin binding is the other half).
- [x] 2.1 Conformance script `platform/services/agentgateway/deployment/tests/conformance.sh`
      (curl + jq): models list; chat completion thinking off; `reasoning_effort` each of
      the seven values; `chat_template_kwargs` override; tool call; streamed request with
      `xhigh` and timing of first token and inter-chunk gaps; one Responses API request;
      each sent to the gateway and to vLLM directly from the gateway VM
      2026-10-02: code landed, not yet run in production. The playbook is
      `run-agw-conformance.yml` (Semaphore `Run agentgateway Conformance`). The runner reads
      the keys from OpenBao and places them as 0600 files in a 0700 directory on the VM,
      removed in an `always:`. curl gets each key through a pipe, never argv. Under listener
      TLS the gateway is reached by its SAN with `--resolve` and the `agw-verifier` leaf.
      Output is one JSON line per case and target: status, a normalised-body sha256, a shape
      sha256, semantic fields, and timings, including first token and gap statistics for the
      stream. Thinking off is `chat_template_kwargs.enable_thinking: false`; the override is
      `chat_template_kwargs.reasoning_effort: low`. The seven efforts are dgx-spark's
      (`plans/development/openspec/specs/inference-endpoint/spec.md`, `vllm/public_probe.py`).
      `platform/tests/test_agw_conformance.py` runs it against stub servers.
      2026-10-04: DONE — `Run agentgateway Conformance (Dev)` task 2709 at `21891372` ran all 13
      cases (models, thinking off, the seven efforts, the override, tool call, `stream-xhigh`,
      `responses`) against the gateway and direct vLLM from the gateway VM: PASS, 13/13.
- [x] 2.2 Diff bodies (ignoring ids and timestamps); record first-token and gap deltas
      in `context/architecture.md`
      2026-10-02: `conformance.sh diff` is the comparison, and the playbook runs it. A case
      matches only when both sides succeeded (curl exit 0, 2xx) and agree on status, shape and
      semantic fields; any failure is an error naming each side's status (PR 409 review).
      Model names go through the inventory's name-to-`upstream_model` map, passed as a file. The exact-body hash is reported but does
      not decide the verdict. The playbook fails on any case that does not match. Recording
      in `architecture.md` waits for the first production run; the section is in place and
      says the results are pending.
      2026-10-03: first production run, Semaphore task 2592, as reported by the operator. It
      matched 0/13: every case was 200/200 with the same semantics and a different shape, but the
      report carried only a hash of each shape. The diff now names the differing key paths per
      case (`shape_diff`; paths and types only) and accepts only what
      `deployment/tests/conformance-shape-allow.json` lists. That list is committed empty; the
      operator fills it after reading the next run. Then this task is recorded.
      2026-10-03: production run Semaphore task 2614 (commit `1f8057aa`) matched semantics 12/13
      (corrected from 13/13: `responses` differs, the gateway drops vLLM's reasoning output item)
      and shape 0/13. Operator decision: accept every reported difference; the allowlist holds the
      reported paths scoped per case (78, none global) and `architecture.md` records the client-visible ones. Still
      open: the first-token and gap deltas are not recorded yet, so this task stays unticked.
      2026-10-03: production run Semaphore task 2681 (commit `80c27733`, allowlist in place)
      matched 12/13. `responses` fails on semantics only (shape accepted): `model` (gateway
      reports the served name, vLLM its own id) and the missing reasoning output item. Both come
      from the gateway translating `/v1/responses` to chat completions, because the provider
      declares only the `completions` format (agentgateway v1.5.0 source, see `architecture.md`).
      Open: decide whether to declare the `responses` format for passthrough or accept the case.
      2026-10-04: decision on the open question above (operator): declare the `responses`
      upstream format beside `completions` so the gateway passes `/v1/responses` through to vLLM
      instead of translating it (schema and policy evidence from agentgateway v1.5.0 source in
      `context/architecture.md`, "Upstream"). The next production run decides whether the case
      matches.
      2026-10-04: production run Semaphore task 2698 (commit `79a8eb76`, after the passthrough
      deploy, task 2696) matched 12/13. `responses` now matches (passthrough). `stream-xhigh`
      matched semantics and failed only on an unaccepted shape path: the gateway's chunk union
      carried `choices.[].delta`, which in task 2614 was on vLLM's side instead. The stream's chunk
      union varies run to run. Under the 2026-10-03 rule the path is now accepted as a gateway
      addition too; the allowlist must equal the union of the task 2614 and 2698 records. Found on
      the way: the run tested a different model each time (2614 and 2698 one name, 2681 another)
      because the model was the first item of a set intersection, whose order follows per-process
      hashing. It now follows `allowed_models` order, else `agw_models` order, and a repository test
      refuses taking the first item of a set operation anywhere under `platform/`. Timing deltas
      still not recorded, so this task stays unticked.
      2026-10-04: DONE — task 2709 at `21891372` matched 13/13. The `stream-xhigh` first-token and
      gap deltas (gateway minus direct) from production tasks 2614, 2681 and 2698 are recorded in
      `context/architecture.md` ("Conformance against direct vLLM"): first token +0.023/+0.026/+0.029
      s, gap p95 and max within ±0.002 s. Task 2709's saved output is truncated before the stream
      case, so its own stream deltas are not in the table.
- [x] 2.3 Confirm SSE keep-alive comment lines from vLLM pass through unchanged and
      unbuffered (needs dgx-spark `inference-endpoint-reliability` deployed)
      2026-10-12 (task output read from Semaphore by the lead): Run agentgateway Conformance
      (Dev) task 4910 with `agw_conformance_long_stream=true` (PR #566): PASS 15/15.
      `stream-long`: direct 174.6 s, gateway 171.6 s; first byte 0.149 s direct, 0.185 s
      gateway; longest gap 0.111 s direct, 0.116 s gateway; both 200 and `[DONE]`. That
      proves the unbuffered half (the gateway adds no buffering past 130 s). Keep-alive
      pass-through is UNPROVEN: vLLM sent 0 keep-alive comments on either side (steady
      generation; vLLM emits `: keep-alive` only after 15 s idle). Stays open until a stream
      with an idle stretch over 15 s (queue wait or long prefill) shows the comments pass
      through.
      2026-10-13: PR #578 merged. It adds the opt-in `agw_conformance_long_stream_saturate`
      (0, 8, 12 or 16), which queues the measured stream behind that many filler streams so
      vLLM emits `: keep-alive` while the measured stream waits. The production run with
      `agw_conformance_long_stream=true` and saturate=8 is pending: it occupies all 8 DGX
      slots, so it runs outside busy hours. Not ticked.
      2026-10-13 (task output read from production Semaphore by the lead): keep-alive
      pass-through PROVEN. After PR #578 and the survey publish (Publish Semaphore Template
      Surveys (Dev) 5292 put `agw_conformance_long_stream_saturate` on the live template),
      Run agentgateway Conformance (Dev) dry run 5309, then real run 5311 at 95554ab4, with
      `agw_conformance_long_stream=true` and saturate 8, outside busy hours and with
      dgx-spark's session confirming nothing in flight: PASS 15/15. `stream-long`: 8 fillers
      accepted on each side. Direct: 2 keep-alives, longest gap 15 s, first byte 15.043 s,
      246.9 s in all. Gateway: 2 keep-alives, longest gap 15 s, first byte 15.072 s, 279.4 s
      in all. `keepalives_seen_from_vllm` true, and the "keep-alive pass-through is unproven"
      warning task was skipped. vLLM's 15 s keep-alive interval (dgx-spark
      `sse-keep-alive-interval` 15, on the chat and completions routes) reaches the client
      through the gateway: the same number of comment lines, the same longest gap and first
      byte, and no buffering. Scope: `conformance.sh` counts lines that start with `:` and
      compares counts and timings. It does not compare the comment bytes, so "unchanged"
      holds for count and cadence, which is what a keep-alive is for (clients ignore SSE
      comments). Ticked on that scope.
- [x] 2.3a Streams and the budget: confirm whether a streamed completion is charged to the
      per-key budget, and whether a model `overrides: {stream_options: {include_usage: true}}`
      entry makes vLLM report usage on streams without changing the client-visible contract
      (the budget is otherwise best-effort for streams; PR 191 review)
      2026-10-05: DONE. Answer, from agentgateway v1.5.0 source and a local measurement on its
      image (`context/architecture.md`, "Streamed completions and the token budget"): a stream is
      charged only when it carries usage, and a client could avoid that (`include_usage: false`,
      or a disconnect before the final usage chunk). A per-model body `transformation`, not an
      `overrides` entry, forces `include_usage` and `continuous_usage_stats` on every streamed chat
      request (#437). The client-visible contract does change: every chunk carries `usage`.
      Production: `Deploy agentgateway (Dev)` task 2880, then `Run agentgateway Conformance (Dev)`
      task 2882 PASS 14/14, with `stream_usage.gateway` true on `stream-xhigh` and on the new
      `stream-options-without-usage` case. A production budget charge on a dropped stream was not
      measured; the 429 after a dropped stream is the local measurement.
- [x] 2.4 Validation gate: 2.2 proves scenario "Conformance against direct vLLM"; 2.3
      with a stream past 130 s proves scenario "Stream is not buffered"
      2026-10-12: "Stream is not buffered" is proven by task 4910 (see 2.3); 2.4 waits on
      2.3's keep-alive half. Not ticked.
      2026-10-13: 2.2 proved "Conformance against direct vLLM" (task 2709); "Stream is not
      buffered" is proven by 4910 and the keep-alive half by 5311 (2.3; keep-alive count and
      cadence, not comment bytes). Ticked.

## 3. Telemetry
- [x] 3.1 o11y: `scrape.d/agentgateway.yml.j2` for the stats endpoint (`/metrics` on the
      stats port; series `agentgateway_requests_total`, `agentgateway_request_duration_seconds_*`,
      `agentgateway_gen_ai_server_request_duration_*`, `agentgateway_gen_ai_client_token_usage_*`
      — read on the running container 2026-09-17); Alloy
      `otelcol.receiver.otlp` on the o11y host forwarding spans as structured log lines to
      Loki (Tempo deferred); inference dashboard gains the client-view row
      2026-10-02 code state: the scrape job (`scrape-agentgateway.yml.j2`, rendered only
      when inventory declares the stats endpoint) and the OTLP receiver already existed,
      with spans going to Tempo rather than being deferred. Added: inventory-gated
      `o11y_gateway_span_logs_enabled` (default off) span-to-Loki lines via
      `otelcol.connector.spanlogs`, the model request-duration series on the client view,
      and a link to it from the inference latency dashboard. Open until the private
      inventory declares the endpoint and 3.3 proves it on live traffic
      2026-10-03: PR #399 merged (span-log flag, default off). A deploy with the flag set waits on
      the private inventory
      2026-10-06 (task ids and output reported by the coordinator; the task output was not read
      here): scrape half PROVEN. site-config #62 set `o11y_gateway_span_logs_enabled` (the
      stats endpoint was already declared); Deploy o11y (Dev) task 2991 applied it
      (`changed=3`), and Verify o11y
      Metrics Target (Dev) task 3142 reported Prometheus has a healthy `agentgateway` target on
      the stats port 19002 and `agentgateway_gen_ai_server_time_to_first_token_bucket` series.
      Still open: no Loki readback of a span log line, and the client-view row is not proven on
      live traffic (task 3.3; dashboard-data proof PR pending). Not ticked.
      2026-10-13 (task output read from production Semaphore by the lead): the Loki readback
      is PROVEN. Verify o11y Service (Dev) with `expected_service` agentgateway,
      `emit_agentgateway_canary` true, `expect_traces` true, `expect_logs` true and
      `expect_metrics` false. The first dry run, 5250, failed all 100 canary requests: the
      canary sent plain http to a listener that has served TLS only since task 6.1 (a TLS
      alert record came back in the reply). PR #594 (merged as 08465c5f) sends the canary over
      the listener's TLS, as `probe-agentgateway-gate.yml` does. Dry run 5257 and real run
      5262 then both passed: "agentgateway verified: recent Loki log, metrics not requested,
      recent Tempo trace." That Loki query, `{service="agentgateway"}`, also matches the
      gateway's OTLP access logs (the `gateway_logs` processor in `config.alloy.j2` carries the
      same `service` label), so those two runs do not prove the span-log stream (review of
      #595; MISTAKES 1.21, occurrence 2). PR #597 (merged as d55530f9) added
      `expected_log_signal`; Publish Semaphore Template Surveys (Dev) 5292 put it on the live
      template. Dry run 5295 and real run 5299, with `expected_log_signal` span, then passed:
      "agentgateway verified: recent Loki log (signal span), metrics not requested, recent
      Tempo trace." `{service="agentgateway",signal="span"}` is set only by the
      `gateway_span_logs` processor, so spans reach Loki as log lines. The scrape half was
      proven 2026-10-06 (3142) and the client-view row by 4913 (task 3.3). Ticked.
- [x] 3.2 Check whether `localRateLimit` has a log-only mode in the v1.5.0 schema; record
      the answer in `design.md` and set the first-week policy accordingly. 2026-09-17: no
      such mode (`RateLimitSpec` has only `maxTokens`, `tokensPerFill`, `fillInterval`,
      `type`); first week runs with loose figures tightened from observed rates
- [x] 3.3 Validation gate: one hour of traffic renders p50 and p95 first-token latency and
      per-identity counts, proving scenario "Client-view latency on the dashboard"
      2026-10-06: the first-token histogram reaches Prometheus (task 3.1 note, Verify o11y
      Metrics Target (Dev) 3142). No receipt yet of the dashboard panels returning data over an
      hour of traffic; the dashboard-data proof is a pending PR. Not ticked.
      2026-10-07 (task id and output as quoted by the coordinator; the task output was not read
      here): Verify o11y Dashboard Data (Dev) task 3302, dashboard `agentgateway-client-view`,
      lookback 1h, FAILED: "Panels without data over 1h: First-token latency p50; First-token
      latency p95; 5xx request ratio". Two different causes. (1) The 5xx request ratio is a
      DASHBOARD DEFECT, not a traffic gap: its expression in
      `platform/services/o11y/deployment/config/grafana/dashboards/agentgateway-client-view.json`
      (panel id 5) is `sum(rate(agentgateway_requests_total{... status=~"5.." ...}[5m])) /
      clamp_min(sum(rate(agentgateway_requests_total{...}[5m])), 1e-9)`; with no 5xx series the
      numerator is an empty vector, so the division returns nothing and the panel stays empty
      for as long as the gateway returns no 5xx, however much traffic arrives. Follow-up: `or
      vector(0)` on the numerator, or exempt the panel from the has-data check. (2) The two
      first-token panels are empty by inference, not shown: the only gateway traffic the
      coordinator named is the five-minute non-streaming synthetic probe, and task 3142 saw
      `time_to_first_token_bucket` series exist; streaming client traffic arrives with the
      route switch (task 4.3). Not ticked.
      2026-10-08: cause (1) is fixed: panels 4 (4xx ratio) and 5 (5xx ratio) of
      `agentgateway-client-view.json` now wrap the numerator in `( ... ) or vector(0)`, the
      denominator unchanged, so each panel returns 0 instead of an empty vector while the
      gateway returns no such status. Pinned by
      `platform/tests/test_o11y_dashboard_asserts.py::test_client_view_error_ratio_numerator_is_zero_when_no_error_series`
      (mutated: red without the fallback). Not re-run live. Cause (2), the two first-token
      panels, still needs streaming traffic (task 4.3). Not ticked.
      2026-10-12 (task output read from Semaphore by the lead): Verify o11y Dashboard Data
      (Dev) task 4913, `agentgateway-client-view`, lookback 1h: all 6 panels have data (status
      pass), including first-token latency p50 and p95 and the error ratios, so the
      2026-10-08 fix is proven live (task 4842 earlier showed the 5xx ratio with data and only
      the first-token panels empty). The traffic in that hour was synthetic: Run agentgateway
      Conformance (Dev) tasks 4840 and 4910 (streamed cases through the gateway), not real
      client traffic. Ticked on scenario "Client-view latency on the dashboard"; real client
      traffic arrives with the route switch (4.3).

## 4. Identities, limits, re-route
- [ ] 4.1 Virtual-key lifecycle (design §10). DONE 2026-09-17 in code: the deploy mints
      `client_<name>` once per `agw_clients` entry (fields on the service's secret path,
      not a `clients/*` subpath); `manage-agentgateway-client-key.yml` rotates (name must
      be declared) or revokes (name must be removed first; merge-patch null) one identity
      and re-renders/reloads; per-identity `allowedModels` + budget overrides via
      `agw_client_policies`; Semaphore templates shared + local; BATS. Local rotate drill
      run through the local Semaphore 2026-09-17 (task 615 after the Authentik tombstone,
      MISTAKES 6.5): the morning's key answers 401, the rotated value 200, gateway 0 restarts. LEFT for prod: mint the
      first set (the four clients declared in site-config) by the first
      deploy, hand each out with `backup-credentials-to-site-config.yml`, and enrol the
      legacy shared key as identity `legacy-shared` with the tightest budget and
      `legacy_shared_expires: <date>` in inventory (task 5.1 enforces the expiry)
      2026-10-14 (task output read from production Semaphore by the lead): `legacy-shared` is now
      enrolled with an expiry and a budget (site-config #77): `legacy_shared_expires` 2026-10-28
      and an `agw_client_policies` entry of 30,000,000 tokens per hour. Sizing: Verify o11y
      Production Budgets (Dev) task 5349 (agent-cloud #606) read a 7-day near-rolling peak of
      19.0M prompt+generation tokens per hour (241k generation tokens per hour, 38.7 requests
      per minute, 8 running, 3 waiting). Every current client reaches vLLM with the shared key,
      so after the switch all traffic is `legacy-shared`; the 2M default would have blocked it.
      30M is about 1.6x the peak: a grace-period figure, deliberately above the "tightest
      budget" this task names. Deploy agentgateway (Dev) task 5357 rendered it. Still open:
      handing out the first key set. Not ticked.
      2026-10-14 (task output read from production Semaphore by the lead): the personal admin
      keys were minted by Deploy agentgateway (Dev) task 5671; the operator's key and
      `client_jacob` are handed out (Back Up Credentials to site-config (Dev) tasks 5638 and
      5673). Not ticked: the rest of the first key set is not handed out.
      2026-10-14/15 (task output read from production Semaphore by the lead): the admin identity
      `wisward` is retired. Authentik retired it through `platform_retired_accounts` (PR #640
      and site-config #90, merged 86ea5da6): Deploy Authentik (Dev) dry run 5711, real run 5713
      ('OK absent wisward', 'VERIFY OK: 13 blueprints, 5 accounts present, 2 retired'), rerun
      5714 changed=0. The gateway deploy 5718 dropped the identity, and Manage agentgateway
      Client Key (Dev) revoke dry run 5721, real run 5722 recorded "revoke of identity 'wisward'
      recorded in OpenBao". Not ticked: the rest of the first key set is not handed out.
- [ ] 4.2 `localRateLimit` per identity: `type: requests` per minute and `type: tokens`
      per hour, figures derived from the measured ceiling and written as comments.
      2026-09-17: v1.5.0 has no per-identity REQUEST bucket under `llm.policies` (`key`
      unreleased, `conditional` rejected); Joe chose per-key token BUDGETS backed by the
      service's own Postgres (design.md Open Questions). Shipped in the template: one
      global request bucket (`agw_rate_requests_per_minute_total`) + `hourly-tokens`
      budget per key (`agw_rate_tokens_per_hour`). Left for this task: derive both
      figures from the measured ceiling and write them into site-config inventory
      2026-10-14: provisional figures are drafted in site-config #78 (draft, awaiting operator
      review): 10M tokens per hour per identity, 120 requests per minute, and `o11y-probe`
      pinned at 2M. Deriving them from the measured capacity ceiling is still pending. Not
      ticked.
      2026-10-14 (task output read from production Semaphore by the lead): site-config #78
      merged, and Deploy agentgateway (Dev) task 5523 at 11019dd9 applied it: 10M tokens per
      hour per identity, 120 requests per minute globally, `o11y-probe` 2M, `legacy-shared`
      30M. `legacy-shared` 30M is sized at about 1.6x the measured 19.0M/h 7-day peak (see
      note above); 10M/h per identity, 120 req/min and `o11y-probe` 2M are the figures drafted
      in site-config #78, not derived in this repo from a capacity ceiling. Remaining: the
      ceiling derivation. Not ticked.
- [x] 4.3 site-config: production Caddy block upstream to the gateway; `Caddyfile.local.j2`
      `inference_api` unchanged in shape (upstream already comes from `r.upstream`);
      redeploy Caddy through Semaphore
      2026-10-12 (task output read from Semaphore by the lead): Probe agentgateway Gate (Dev)
      task 4875 (PR #559) read keyless `/health` as 401: in gateway mode the public `/health`
      would NOT answer 200. The route switch (site-config #66) needs a `/health` handler
      before it goes live, either a proxy to vLLM `/health` as direct mode does or the
      gateway's readiness endpoint. Also Probe Reachability (Dev) task 4851: caddy to
      gateway:4000 open; Verify agentgateway Runtime (Dev) task 4854: receipt matches the
      deployed revision `2e1799a3`. Not ticked.
      2026-10-13: the `/health` handler named above already exists on the route-switch branch:
      site-config `feat/inference-route-mode` commit bb54629 serves `/health` from the vLLM
      head node in BOTH route modes (only `/v1/*` follows `inference_route_mode`); the
      gateway's readiness port stays off the public edge. Gateway mode therefore keeps
      Caddy's path to vLLM `/health` open. Remaining: the operator's go on site-config #66,
      then the Caddy redeploy. Not ticked.
      2026-10-14 (task output read from production Semaphore by the lead): the route is
      switched. site-config #66 (route mode, direct) merged as a852c64d and #77 (the flip:
      Caddy `inference_route_mode` gateway, with the 4.7 expiry) merged as e05a726d; #79 adds an
      inventory-sync step to the runbook (83c9f38e). Production Semaphore runs from its own
      stored copy of the inventory, so `sync-inventory.yml` pushed site-config main after #66
      and after #77, each time with a key-path diff of exactly that PR's keys. Deploy
      agentgateway (Dev) dry run 5356 and real run 5357: "The shared key is admitted as
      legacy-shared until 2026-10-28."; the keyed round-trip there is the deploy's own verify
      from the gateway host, not a request through Caddy. Manage Caddy Sites (Dev)
      dry run 5365: the diff is only the inference block (`/v1/*` to the https gateway;
      `/health` unchanged, to the vLLM head node); real run 5366 at 04:29 UTC validated and
      restarted Caddy. Public edge afterwards: `/health` 200, keyless `/v1/models` 401,
      `/metrics` 403 (Cloudflare challenge). Rollback Inference Route (Dev) `gateway-config`
      dry run 5370 reads the live route as `/v1/*` through the gateway and `/health` direct to
      vLLM. This tick is on config scope: the declared block, the Caddy redeploy and route
      report 5370, which reads upstream destinations only. A keyed request through the public
      edge is 4.5's proof and is still owed.
      2026-10-14 (task output read from production Semaphore by the lead; model names): site-config
      #85 merged as `a38c60a3`: `agw_models` lists only `qwen3.8-flash-next` (vLLM serves both
      aliases; that is the canonical client id per dgx-spark). The inventory was synced; the
      key-path diff was exactly `agw_models`. Deploy agentgateway (Dev) dry run 5642 and real
      run 5643: as `stray`, `/v1/models` lists `qwen3.8-flash-next`, and a chat completion
      (32 tokens) came back from the upstream model `nvidia/Qwen3.8-Flash-Next-NVFP4`.
      dgx-spark (reported by the dgx-spark session) verified that the long alias answers 404
      through the gateway. Already ticked; this is a dated record only.
- [x] 4.4 Run dgx-spark's public-path verify play against the live hostname; update
      dgx-spark `docs/TEAM-ENDPOINT.md` (key changes, base URL does not)
      2026-10-14: dgx-spark's public-path verify play is requested; no result yet. Not
      ticked.
      2026-10-14: the operator reported that dgx-spark's public-path verify play passes. There
      is no Semaphore task id for it, and the update to dgx-spark `docs/TEAM-ENDPOINT.md` is
      unconfirmed. Not ticked.
      2026-10-14 (reported by the dgx-spark session): after the route flip, dgx-spark's
      public-path verify play PASSED, 16:39 to 16:49 EDT, against the public `/v1` base URL
      with the shared key: `/health` 200, keyless `/v1/models` 401, 7 of 7 reasoning efforts
      200, a long stream of 289.9 s (first token 0.48 s, 3625 chunks) that crossed the 125 s
      proxy timer alive, and 2 of 2 concurrent streams. The 12-stream burst was not run (it
      needs an operator window). dgx-spark `docs/TEAM-ENDPOINT.md` is updated: dgx-spark PR
      #33, merged as `0479c96`. Ticked: the verify play ran against the live hostname and the
      endpoint doc is updated.
- [ ] 4.5 Validation gate: an unenrolled key gets 401 at the gateway with no vLLM log
      line, proving scenario "Unknown key is rejected"; a burst from one identity gets
      429 while another identity is served, proving scenario "One client cannot exceed its
      share"; the gateway access log shows the public request, proving scenario "Public
      path traverses the gateway"; one inventory change and a Caddy redeploy restores the
      direct path, proving scenario "Rollback is one value during the grace period"
      2026-10-12 (task output read from Semaphore by the lead): pre-switch leg of "Unknown
      key is rejected" read from Probe agentgateway Gate (Dev) task 4875 (PR #559): keyless
      `/v1/models` 401 and invalid-key `/v1/models` 401. The "Public path traverses the
      gateway", burst and rollback scenarios need the route switch (4.3). Not ticked.
      2026-10-14: PARTIAL. After the switch, the keyless request is refused at the edge
      (`/v1/models` 401 after Caddy run 5366). The rollback path is proven by the 4.6 drill,
      but through the playbook, not as "one inventory change and a Caddy redeploy". The 429
      isolation scenario and the access-log scenario are not proven. Not ticked.
      2026-10-14 (evening): PR #632 (the isolation probe) encodes the scenario "One client
      cannot exceed its share". Local proof tasks 6565 and 6566: the burst got 200 then 429
      `budget_exceeded`, and the control identity got 200. The production leg needs a
      low-budget probe identity in site-config, which does not exist yet. Not ticked.
- [ ] 4.6 `rollback-inference-route.yml` with `mode=gateway-config|direct|restore` as in the
      proposal's Rollback Plan, each mode idempotent (re-run converges, no duplicate key
      publication, `restore` is a no-op once the upstream is the gateway and the key is
      rotated); BATS asserts the three modes exist and that `direct` never prints a key;
      drill `direct` then `restore` against the live route in a window Joe names, proving
      scenario "Rollback after retirement is the playbook"
      2026-10-02: code landed (PR #385); the drill is still owed. `restore` does not rotate
      the vLLM key (dgx-spark owns it); it refuses to retire the copies until the key is
      rotated and the running gateway holds the new one, proving scenario "Restore refuses
      while the published key is still live". One-step rotation needs dgx-spark to read the
      key from OpenBao (cross-repository, dgx-spark handoff). The tests are pytest, not
      BATS, because only a real run proves that no key is printed and that re-runs converge.
      `gateway-config` needs the deploy to keep the config it replaces as
      `config.yaml.previous`; until it does, that mode refuses
      2026-10-02: PRs #385 (playbook) and #386 (the deploy keeps `config.yaml.previous`, so
      `gateway-config` no longer refuses) merged. The live `direct`/`restore` drill is not run
      2026-10-09: the prod dry run of "Rollback Inference Route (Dev)" in `gateway-config` mode
      (Semaphore task 3762, at e2ad8305) failed at the task that names the identities the
      previous config enrols, with its output censored by `no_log`. Root cause: the Jinja macro
      in that task built JSON text and re-read it with `from_json`, and the prod runner's
      ansible-core 2.18.15 (read from the running Semaphore containers on the prod host,
      2026-10-09; Semaphore v2.17.31) turns that text into a Python tuple first, so the parse
      fails with `JSONDecodeError` (reproduced on 2.16.18 and 2.18.15 with the standard
      fixtures; 2.19 and 2.20 pass the same fixtures). The extraction now runs in a filter
      plugin (`agw_apikey_entries.py`) that returns native lists with no JSON text step, and the
      `gateway-config` tests pass on 2.18.15, 2.19.14 and 2.20.8. An unreadable or non-mapping
      config is refused by a visible task naming the file and the class of problem, never its
      content. The prod Semaphore upgrade to v2.19.11 (ansible-core 2.20.8) is planned. The 4.6
      drill is still owed
      2026-10-09: after PR #508 (root cause of 3762 confirmed) the `gateway-config` dry run
      (Semaphore task 3843) passed, but the real run (task 3844) failed: the previous config
      was put back, `deploy.sh --no-pull` recreated the gateway, and readiness did not answer
      within 90 s, so the run failed and left the gateway down for about 6 minutes until
      Deploy agentgateway (Dev) (task 3846) restored the current config (readiness OK, keyless
      401, keyed models OK). The public route was unaffected (it went direct to vLLM). Why the
      previous config failed is not established. The dry run could not
      have shown it, because check mode skips the recreate. This change closes both gaps:
      the previous config is now validated with the pinned gateway image before anything is
      moved (the deploy's own check, extracted to `tasks/agw-validate-config.yml` and shared;
      it also runs under `--check`), and if the recreated gateway is not ready the config it
      replaced is put back, the gateway recreated on it, and the run still fails, naming
      whether the restore held (that step reads the container's state and exit code only, not
      logs, because a config error can echo a value; deploy.sh prints its own redacted log tail
      when its readiness wait fails). The drill is to be re-run
      2026-10-09 correction: the drill re-run (Semaphore task 3904) failed again and its restore
      failed too. The cause, read from the task's stderr (it was already there in 3844): the gateway
      exited with `failed to watch configured file paths: /certs/agw-server/current/cert.pem`. Prod
      mounts `./certs` only through `compose.tls.yml`, which the deploy selects with the
      `COMPOSE_OVERLAYS` environment variable it gives `deploy.sh`; both of the rollback's
      `deploy.sh` calls omitted it. The "previous config is invalid" diagnosis above was wrong.
      The deploy.sh environment is now one mapping (`vars/agw-deploy-env.yml`, `_agw_deploy_env`)
      that the deploy, the rollback and the runtime verify all use, guarded by
      `test_agw_deploy_env.py`; validate-first and auto-restore stay as defence. The drill is to
      be re-run again
      2026-10-09T19:58Z: the production Semaphore upgrade planned above is done: v2.19.11, app and
      runner both reporting ansible-core 2.20.8 (site-config `scripts/semaphore-upgrade.sh`, VM
      snapshot first; `/api/info` and `ansible --version` in both containers read after it).
      2026-10-10T00:08Z: `gateway-config` drill PASSED on `dev` at 2e1799a3 (after PR #521). Dry run
      Semaphore task 3942 (validation ran and passed; `params.dry_run` recorded); real run task
      3943: the previous config was put back, `deploy.sh` reported `recreated (inputs changed)`,
      the gateway answered readiness, `failed=0`. Deploy agentgateway (Dev) task 3946 then
      rendered the current config from code again (recreated, verify passed). Still owed for this
      box: the live `direct` then `restore` drill, in a window Joe names
      2026-10-14 (task output read from production Semaphore by the lead): the live drill ran
      after a 30-minute soak on the gateway route. `mode=direct`: dry run 5378, real run 5380
      (route direct; it published the live vLLM key as `direct_stray`, `direct_opencode`,
      `direct_pi`, `direct_skynet` and `direct_o11y-probe` in `secret/services/agentgateway`).
      `mode=restore`: dry run 5384, real run 5385 put the route back on the gateway (declared
      and running config: `/v1/*` to the gateway on port 4000, `/health` to the head node on
      port 8000), then stopped by design at the grace-period check: "The route is back on the
      gateway, but direct_* still hold the LIVE vLLM key ... Rotate it, then run this again."
      The edge answered 200 and 401 throughout, except for seconds-long 525 windows during each
      Caddy restart. RESIDUAL: five copies of the live vLLM key sit in OpenBao as `direct_<name>`
      until dgx-spark rotates the key (5.1); a `restore` after that rotation retires them. The
      scenario "Rollback after retirement is the playbook" needs that rotation. Not ticked.
      2026-10-14 (task output read from production Semaphore by the lead): Rollback Inference
      Route (Dev) task 5385 (`mode=restore`) changed and restarted Caddy, and the route was
      verified back on the gateway (declared and running config). The task then failed BY
      DESIGN at the grace-period check: the five `direct_*` OpenBao fields still hold the live
      vLLM key, so it must be rotated first. Red until that rotation; not a regression. Not
      ticked.

- [x] 4.7 `legacy_shared_expires` = the route-switch date + 14 days (operator decision
      2026-09-27), set in site-config in the same change that switches the route
      2026-10-14: set in site-config #77 (`legacy_shared_expires` 2026-10-28 = the switch date
      2026-10-14 UTC + 14 days), in the same change that switched the route (4.3). Deploy
      agentgateway (Dev) real run 5357 reported it.

## 5. Retire the shared key, records
      2026-10-14 (task output read from production Semaphore by the lead): the expiry is now
      2026-11-13, not the 2026-10-28 above. site-config #81 moved `legacy_shared_expires`; Deploy
      agentgateway (Dev) 5523 applied it and 5643 reconfirmed it ("The shared key is admitted as
      legacy-shared until 2026-11-13."). dgx-spark's TEAM-ENDPOINT.md (PR #33) states 2026-11-13.
- [ ] 5.1 Retirement, enforced by the deploy rather than remembered: the config template
      renders `legacy-shared` only while today is before `legacy_shared_expires`; a deploy
      on or after that date drops the identity and fails if the vLLM key in OpenBao still
      equals the pre-rotation value, so a rerun cannot leave the shared key valid. Rotate
      `VLLM_API_KEY` at vLLM (dgx-spark `secrets/vllm_api_key` and a two-rank restart) and in
      OpenBao; record the retirement date in `context/architecture.md`; hand dgx-spark the
      gateway address for narrowing `vllm_api_allowed_cidr`
      2026-10-02: the deploy half landed (`config.yaml.j2` renders `legacy-shared` only before
      `legacy_shared_expires`; `tasks/agw-legacy-key-check.yml` records the shared key's sha256
      fingerprint once at `legacy_shared_key_sha256` during the grace period and, on or after the
      date, fails the deploy in its last play, after the gateway is recreated without the
      identity, while `vllm_api_key` still matches it; tests
      `platform/tests/test_agw_legacy_retirement.py`). Left: the rotation at vLLM and in OpenBao
      and the `vllm_api_allowed_cidr` narrowing are dgx-spark's and wait on the dgx-spark handoff;
      the retirement date is recorded in `context/architecture.md` once 4.7 sets it
      2026-10-03: the deploy half is PR #400 (merged). The vLLM rotation waits on the dgx-spark
      handoff, which has not been sent
      2026-10-14: still open. Two follow-ups. (1) The drill in 4.6 left five copies of the live
      vLLM key in OpenBao (`direct_<name>`); they stay until dgx-spark rotates the key, after
      which `restore` retires them. (2) From the agent-cloud #611 review: the grace-period probe
      false-reds if the vLLM key is rotated BEFORE `legacy_shared_expires`, because the stored
      fingerprint pins the old key. Rotate after the expiry, or skip the probe when the
      fingerprints differ. Not ticked.
      2026-10-14: the expiry moved. site-config #81 set `legacy_shared_expires` to 2026-11-13
      (it was 2026-10-28) and Deploy agentgateway (Dev) task 5523 at 11019dd9 applied it (task
      output read from production Semaphore by the lead). Earlier notes naming 2026-10-28 are
      left as written. Not ticked.
      2026-10-16: the operator did NOT approve dgx-spark ADR-0018 (OpenBao as the only source of the vLLM
      key, read by dgx-spark's deploy job). It stays Proposed. So the retirement on or after 2026-11-13
      is the two-step rotation in dgx-spark#34's plan:
      - dgx-spark writes the new value to its local key file and does one planned restart of both ranks,
        in a window the operator names. The operator may choose a two-restart variant instead: both keys
        in the first restart, the new key only in the second, which removes the 401 window
        (dgx-spark design decision 6, task 5.1);
      - dgx-spark hands the value to the operator out of band;
      - the operator seeds `secret/services/agentgateway:vllm_api_key` with `Seed OpenBao Key`;
      - the operator runs Deploy agentgateway (Dev), then Rollback Inference Route (Dev) in `restore` mode.
      Not ticked.
      2026-10-24: the agent-cloud side is complete. Follow-up (2) of 2026-10-14 landed in #631
      (`3fd86bb6`): when the vLLM key no longer matches the recorded fingerprint, the grace-period
      probe is skipped instead of failing. The retirement date, 2026-11-13, is now recorded in
      `context/architecture.md`, "Shared-key retirement". Left: the rotation itself (dgx-spark and the
      operator, on or after 2026-11-13) and the `vllm_api_allowed_cidr` narrowing. Not ticked.
- [x] 5.2 Accept the architecture record; append a dated pointer line to
      `plan/development/06-inference-skynet.md`; `platform/services/inference/` stub
      gains a README pointing at the gateway service and the dgx-spark roadmap record
      2026-10-04: the non-decision half is done — a dated pointer callout in
      `plan/development/06-inference-skynet.md`, and `platform/services/inference/README.md`
      replaces the two `.gitkeep` stubs, pointing at `platform/services/agentgateway/` and the
      dgx-spark `node-telemetry-and-placement-benchmark` roadmap record. Open: accepting the
      architecture record is Joe's decision, so this box stays unticked.
      2026-10-12: ticked. The operator accepted the architecture record (decision of 2026-10-12).
      `plan/architecture/05-platform-infra.md`, section "Inference gateway: agentgateway alongside
      skynet", gains a dated refresh (the "design.md decisions 1–8" pointer was stale: the design
      now numbers decisions 1 to 12) and an appended "Accepted 2026-10-12" line; its heading and
      Status paragraph are left as written. The remaining clause of this task was only the
      acceptance (the plan 06 pointer and the `platform/services/inference/` README landed
      2026-10-04, above)
- [ ] 5.3 Validation gate: 5.1 proves scenario "Grace period ends"; the record and the
      plan 06 line prove scenario "Decision is findable and plan 06 is amended"; on
      archive, retain the outcome (worked / dead end / corrected) into bank
      `agent-cloud-750a33b9`
      2026-10-14: the date this gate waits on is now 2026-11-13, not 2026-10-28 (see the 5.1
      note). Not ticked.

## 6. Transport security (decisions of 2026-09-27; needs `production-internal-ca`)
- [x] 6.1 Gateway API and UI listeners serve HTTPS from step-ca-issued certificates in a
      mounted directory (`current/`; the image has no shell); `tls.root` = the step-ca
      root, so a client certificate is required. This task is the single owner of the
      client allowlist (design decision 12): both gateways carry the `require` rule
      `source.subjectAltNames.exists(n, n in [<allowlist>])`, because `root` alone admits
      any leaf from the CA. The list renders from inventory `agw_client_cert_allowlist`,
      whose entries are leaf names from `production-internal-ca` design decision 4's table,
      resolved to their declared SANs (default `caddy` only; production adds
      `agw-verifier`, and `bench` once the benchmark VM exists); an entry naming no
      declared client-profile leaf fails the deploy before restart, naming the entry.
      In local-dev first: confirm the rule renders beside the `llm` shortcut and the gateway
      starts with it, and record the HTTP status a failed rule returns and whether the rule
      is evaluated before API-key authentication (both unverified). If the rule cannot be
      rendered there, fall back to a dedicated client-issuing hierarchy with the gateway's
      `root` set to it alone, and record why as an amendment to decision 12. BATS asserts
      both listeners render `tls` with `root` and the rule, that the default list is
      `caddy` alone, and that an undeclared entry is refused
      2026-10-02 production: listener TLS enabled by site-config #59; Deploy agentgateway (Dev)
      tasks 2460 (dry run) and 2462: keyless request 401, keyed models list and one completion
      through mutual TLS. Left open: this note does not re-verify the local-dev records, the
      allowlist refusal or the BATS assertions the task names
      2026-10-03: the assertions exist as pytest, not BATS — `platform/tests/test_agw_listener_tls.py`:
      both listeners render TLS with the CA root and the SAN rule
      (`test_both_listeners_serve_tls_require_a_ca_client_cert_and_carry_the_san_rule`), the
      default list is `caddy` alone (`test_the_default_allowlist_is_caddy_alone`), and an
      undeclared entry is refused naming it
      (`test_a_declaration_the_gateway_cannot_serve_is_refused_naming_it`, case `undeclared`).
      The local-dev records the task names are still not re-verified here
      2026-10-08: wording correction. The assertions this task and gate 6.4 call BATS are
      pytest: `platform/tests/test_agw_listener_tls.py` (see the 2026-10-03 note); the
      original text is left as written. Still open as before: the local-dev records the task
      names are not re-verified here
      2026-10-09: ticked, on production records (task ids reported by the coordinator; the task
      output was not read by the author of this note). The task asked for local-dev first;
      that was not possible: the local-dev listener TLS is not applied in local mode
      (`deploy-agentgateway.yml` adds `COMPOSE_OVERLAYS` only outside local mode), so
      production was used. A client leaf on no allowlist (`agw-drill`, its SAN on no
      allowlisted leaf, declared in site-config #68, issued by `Issue Internal Leaf (Dev)`
      task 3831) was probed by `Probe agentgateway Client TLS (Dev)` task 3834: HTTP 403 with
      no API key and HTTP 403 with an invalid key. Answers the two open questions: a failed
      SAN rule answers 403, and it is evaluated BEFORE API-key authentication (a key-first
      gateway would have answered the keyless request 401). The remaining clauses are
      covered elsewhere: the rule rendering beside `llm` and the gateway starting with it is
      the 2026-10-03 pytest (`platform/tests/test_agw_listener_tls.py`) plus the 2026-10-02
      production deploys that ran with listener TLS, and the drill's 403 is the rule live in the
      running gateway (a completed handshake refused with 403 comes from no other rule). The
      drill's first reading line was wrong ("no reading") on the runner's ansible-core 2.18.15
      because the statuses compared as strings; the class lines and the verdict were right,
      and the reading is fixed in the probe playbook
- [x] 6.1a The one gateway probe path. Every check that sends a request to the gateway
      from outside Caddy uses it: this deploy's own verify, the personal-key 401 gates
      (`inference-personal-keys`), the renewal proof for the client leaves that are probed
      directly (`production-internal-ca`), the benchmark VM's attribution check
      (`inference-benchmarking`) and the access-record verify of `agentgateway-observability`
      (its requirement "Every gateway request produces an access record in Loki"). Extract
      it from `deploy-agentgateway.yml` into a new `platform/playbooks/tasks/agw-probe.yml`
      whose inputs are the host it runs from (default the gateway host), the client leaf it
      presents (default `agw-verifier`; the benchmark VM passes `bench`), the path, an
      optional key (absent means keyless) and the expected status; the key-bearing call is
      `no_log` and its result is never printed. What changes when 6.1 lands, per probe in
      today's deploy: the readiness probe (`deploy-agentgateway.yml:266`, the busybox
      `wget` defined at line 227, from the sibling db container to `:19001`) is unaffected,
      because `readinessAddr` is a separate plain listener (`config.yaml.j2:34`), not a
      `gateways` listener. The keyless 401 probe (`deploy-agentgateway.yml:318-326`, the same
      `wget` over `http://` to `:4000`) cannot survive, because busybox `wget` presents no
      client certificate and the handshake fails before the key check; it becomes the
      shared task with no key, expecting 401. The keyed probes (`_verify_url`,
      `deploy-agentgateway.yml:349-352`) become the shared task over `https://` with
      `client_cert`/`client_key` = the presented leaf and `ca_path` = the internal bundle.
      The `uri` module at ansible-core 2.21.0 offers `ca_path`, `client_cert`, `client_key`
      and `validate_certs` and no server-name override (`ansible-doc uri`, run 2026-09-27;
      the Semaphore image's ansible-core version is unverified), so the URL names the
      gateway server leaf's SAN; `validate_certs: false` is never used. Interim name
      resolution: an idempotent `lineinfile` on the probing host maps that SAN to the
      published bind, with the trailing marker `# agent-cloud-managed: agw-probe (interim,
      task 7.2)` and a `regexp` on that marker, so code finds, updates and later removes the
      line. Local-dev keeps `agw_verify_base_url` and sets it to the SAN form; unverified:
      how the Semaphore container resolves that name on the `local-dev` network, settled
      before 6.1a lands
      2026-10-03: CODE LANDED — `platform/playbooks/tasks/agw-probe.yml` is the shared probe;
      `deploy-agentgateway.yml` sends its three probes through it. Name resolution is the one
      step `tasks/agw-probe-resolution.yml` (PR #418), used by `deploy-agentgateway.yml` and,
      read-only, by `renew-internal-certs.yml` (PR #420). The other consumers this task lists
      (personal keys, benchmark VM, access-record verify) are not checked here
      2026-10-04: NOT ticked. The deploy and the renewal proof use the shared probe
      (`deploy-agentgateway.yml:615,665,714`, `renew-internal-certs.yml:550`), but the task says every
      check from outside Caddy uses it: the personal-key gates, the benchmark VM's attribution check
      and the access-record verify do not exist in `platform/playbooks/` yet, and
      `run-agw-conformance.yml` reaches the gateway with its own curl and `--resolve`, not this probe.
      2026-10-05: CODE LANDED, not yet run — `run-agw-conformance.yml` is on the probe path. Before
      OpenBao is read it checks the server leaf's name through `tasks/agw-probe-resolution.yml`
      read-only (`_agwr_check_only`; Deploy agentgateway, on the same gateway VM, stays the one
      writer) and sends one keyless `/v1/models` through `tasks/agw-probe.yml` with the
      `agw-verifier` leaf, requiring 401. The cases stay curl in `conformance.sh`, because `uri`
      cannot time a stream's first token and chunk gaps; its `--resolve` input is removed, so curl
      resolves the SAN through the same host resolver. Tests: `platform/tests/test_agw_conformance.py`.
      Extra vars cannot aim or forge that gate (PR #456 review): the shared probe and resolution
      tasks refuse overrides of the results they write, for every caller; the playbook refuses
      every input name it passes them, and passes each explicitly. The gate's base URL and the
      cases' URL are one value, set after that refusal (the cases' is the base plus `/v1`), and
      both names are refused, so an extra var cannot split the gate from the cases.
      Proof still owed: one `Run agentgateway Conformance (Dev)` run in production. Still NOT ticked:
      the personal-key gates, the benchmark VM's attribution check and the access-record verify
      do not exist yet
      2026-10-06 (task ids and output reported by the coordinator; the task output was not read
      here): the conformance proof is in. Run agentgateway Conformance (Dev) task 3011 (dry run)
      then 3012 (real) reported "PASS, 14/14 cases match" through the shared probe path (#456).
      Still NOT ticked, for the same reason: the personal-key gates, the benchmark VM's
      attribution check and the access-record verify do not exist yet
      2026-10-08: the probe callers still missing belong to other
      changes: the personal-key 401 gates (`inference-personal-keys`), the benchmark VM's
      attribution check (`inference-benchmarking`) and the access-record verify
      (`agentgateway-observability`). Recorded, not ticked: this task stays open until
      each of those checks exists and sends through `tasks/agw-probe.yml`, or its text is
      restated.
      2026-10-12: restated by operator decision. 6.1a = the shared probe exists and every
      gateway verification gate in this change goes through it (deploy, renewal proof,
      conformance's keyless gate, client-TLS probe); the conformance cases themselves stay curl,
      as recorded 2026-10-05. Checks owned by sibling changes (benchmark, personal keys,
      observability) are tracked there. Ticked. Evidence read from `origin/dev`: the shared
      probe `tasks/agw-probe.yml` is included by `deploy-agentgateway.yml:713,763,812`,
      `renew-internal-certs.yml:558` (as `file:`), `run-agw-conformance.yml:221` and
      `probe-agentgateway-client-tls.yml:164,181,198`. `rollback-inference-route.yml` sends no
      gateway request: its `uri` calls (`:255,:269,:893,:908`) hit OpenBao only, and its
      readiness check execs `wget` in the db container (`:398`). The sibling checks are named in
      their own tasks: `inference-benchmarking/tasks.md:45`, `inference-personal-keys/tasks.md:172`,
      `production-internal-ca/tasks.md:313`
- [x] 6.2 Caddy's `inference` and `admin.inference` blocks proxy to `https://` with
      `transport http { tls_server_name <gateway SAN>; tls_trust_pool file <root>;
      tls_client_auth <cert> <key> }` (Caddy 2.11.4), the leaf files read from the Caddy
      host's mounted `current/` directory. The architecture document's deprecated
      directive is corrected by `production-internal-ca` task 9.1, not here
      2026-10-02 production: the `admin.inference` block proxies over mutual TLS. Manage Caddy
      Sites (Dev) tasks 2463 (dry run; only that block changed) and 2464; verified at the Caddy
      origin: 302 to the Authentik authorize endpoint with `client_id=agentgateway` (the public
      URL sits behind the Cloudflare challenge). Open: the `inference` block still dials vLLM
      directly until task 4.3
      2026-10-14 production: the `inference` block now proxies `https://` to the gateway with
      `tls_server_name`, `tls_trust_pool` and `tls_client_auth`, as `admin.inference` already
      did (2026-10-02 note). Manage Caddy Sites (Dev) dry run 5365 (only that block changed) and
      real run 5366; Rollback Inference Route (Dev) `gateway-config` dry run 5370 reads the
      route as `/v1/*` through the gateway and `/health` direct to vLLM. Task output read from
      production Semaphore by the lead. This tick is on config scope: the declared block, the
      redeploy and route report 5370, which reads upstream destinations only; a keyed request
      through the public edge is 4.5's proof, still owed.
- [ ] 6.3 The model's `tls: {root, hostname, cert, key}` (the `agw-upstream` client leaf,
      production-internal-ca task 5.4a; mutual TLS decided 2026-09-28) and an `https://`
      base URL, once dgx-spark
      serves vLLM over HTTPS (dgx-spark session: `--ssl-certfile`, `--ssl-keyfile`,
      `--enable-ssl-refresh`)
      2026-10-16: dgx-spark answered the handoff with dgx-spark#34 (merged 455129b5), change
      `inference-upstream-tls-and-key-retirement`, ADR-0017. The operator APPROVED ADR-0017 the same day,
      with agent-cloud's side held until the operator says go. The design:
      - spark-1 becomes a certificates-only consumer host of agent-cloud's internal-CA issuance, under
        its own unprivileged account. Its name and public key come from dgx-spark.
      - vLLM serves `--ssl-certfile`/`--ssl-keyfile`/`--enable-ssl-refresh`: server-only first, mutual
        TLS once every caller presents a leaf.
      - The daily renewal job re-issues the leaf once less than a third of its 30-day lifetime is left.
        vLLM serves the new leaf without a restart. Whether its refresher follows the symlink swap is
        unproven until dgx-spark's drill (its decision 3, task 3.4); the fallback is a fixed-name copy.
      agent-cloud's pieces, in order:
      - `internal_leaves` for `vllm-server`, `vllm-lifecycle` and `agw-upstream`;
      - a spark-1 inventory host in its own group;
      - `distribute-ca-root.yml` placing `step-ca-bundle.crt` beside the spark-1 leaves (dgx-spark task
        2.2 waits on it);
      - renewal proof paths for a non-gateway server leaf (`vllm-server`) and for a client leaf with
        reload `none` on a non-gateway host (`vllm-lifecycle`);
      - this task's `https://` upstream with `tls`;
      - Caddy `/health` and the o11y `dgx-spark-vllm` scrape over HTTPS;
      - Rollback Inference Route `direct` mode gaining an HTTPS transport (a point from dgx-spark's review).
      Not started; not ticked.
      2026-10-26 (dgx-spark handoff, operator-relayed): dgx-spark PR 38 (merged `76660cb`) made
      the consumer account `vllm-tls` live on spark-1: key-only, admitted only from the Semaphore
      host, keys read from a root-owned file, leaf directories under the account's `certs` directory. Done
      here, with the operator's go for these two launches: `Generate Service SSH Key (Dev)`
      `service_name=vllm-tls` (dry run 9407, real run 9408) and `Back Up Service SSH Key (Dev)`
      (dry run 9409; branch pushed, site-config #116). The operator sent the reply the same day. It
      carries the public half, confirms the Semaphore host as the issuance source, and
      counter-proposes the server SAN. dgx-spark proposed `vllm.<site>.<zone>`. The reply
      follows `internal-dns-naming` decision 14 instead: `vllm-primary.<site>.<zone>` plus
      `dgx01.vllm-primary.<site>.<zone>`, with `lifecycle.vllm-primary.<site>.<zone>` for the
      client leaf. Two additions the build side needs that the list above lacks:
      (a) the inventory name `spark-1` is already taken by `dgx_drill_svc` as `o11y-drill`, so
      the certificates host needs its own inventory name, which `internal_leaves[].host` names;
      (b) issuance, renewal and `distribute-ca-root.yml` must connect with the `vllm-tls` key
      from OpenBao, the way the fault drill does, because Semaphore's own key is not
      authorized for that account. The build itself still waits on the operator's go. Not
      ticked.
- [ ] 6.4 Validation gate: a request without a client certificate is refused at the
      gateway, proving scenario "A request without a client certificate is refused"; a
      throwaway client-profile leaf declared with a SAN on no allowlist entry (issued as in
      `production-internal-ca` task 4.7, then removed) completes the handshake and its
      request is refused, proving scenario "Another client leaf is refused at the gateway";
      a request with the `agw-verifier` leaf from the gateway host through 6.1a reaches the
      key check, proving scenario "An allowlisted non-Caddy client is served"; the BATS
      refusal of an undeclared entry proves scenario "An undeclared allowlist entry is
      refused at render"; `Deploy agentgateway (Dev)` still passes its whole verify phase
      after group 6 (readiness, the keyless 401, the keyed `/v1/models` and chat
      round-trip, all through 6.1a); the gateway refuses a vLLM certificate not issued by
      the internal CA; the public path works end to end with every hop encrypted
      2026-10-09 (task ids reported by the coordinator; the task output was not read by the
      author of this note): two scenarios proven in production by `Probe agentgateway Client
      TLS (Dev)` task 3834. "A request without a client certificate is refused": probe 1
      failed the TLS handshake (`TLSV13_ALERT_CERTIFICATE_REQUIRED`). "Another client leaf is
      refused at the gateway": the throwaway leaf `agw-drill` completed the handshake and was
      answered HTTP 403 with no key and with an invalid key (see the 6.1 note of the same
      date). Remaining legs, not ticked: the allowlisted `agw-verifier` leaf served through
      6.1a, the refusal of a vLLM certificate not issued by the internal CA, and the public
      path end to end. Cleanup recorded: the leaf removed (task
      3836), its declaration removed in site-config #69, inventory re-synced, `Deploy
      step-ca (Dev)` tasks 3829 (8 names while the leaf existed) and 3838 (back to 7), and the
      renewal dry run task 3839 passes classification. The drill also found the probe's
      reading bug, fixed on branch `fix/tls-probe-reading-types`
      2026-10-12 (task output read from Semaphore by the lead): the `agw-verifier` leg of
      this gate is proven by Probe agentgateway Gate (Dev) task 4875 (PR #559): an
      invalid-key `/v1/models` request answered 401, the key check. The refusal of a vLLM
      certificate not issued by the internal CA and the public path end to end remain. Not
      ticked.

## 7. Internal name resolution (follow-up; needs hickory-dns in production)
- [ ] 7.1 Server-side OIDC off the Cloudflare path: a split-horizon record for
      `auth.uhstray.io` answering with the Caddy host (declared in the site-config
      split-horizon list, answered only to the internal DNS's declared clients:
      `internal-dns-naming` decision 15 as amended 2026-09-29), so the gateway's discovery, JWKS and
      token calls (and OpenBao's, once `inference-personal-keys` adds its mount) reach
      Authentik through Caddy without transiting Cloudflare. The Cloudflare skip rule for
      Authentik's machine endpoints (`platform/infra/cloudflare/waf.tf:129-135`) is the
      interim fix and stays until this record is proven; then decide, as a recorded
      amendment, whether it is retired. hickory-dns runs in local-dev only today; production
      is planned (`platform/services/dns/context/architecture.md:7`, `:45`), so this task
      waits on it
- [ ] 7.2 Replace 6.1a's interim `lineinfile` entries with records in the internal zone
      for the gateway server leaf's SAN, and remove every line carrying the
      `agent-cloud-managed: agw-probe` marker through the same task
      Progress (2026-10-03): the code half is in place, gated. `deploy-dns.yml` derives one
      A record per SAN of each listener-TLS gateway's server leaf inside `dns_zone` (target:
      `agw_bind`, or `ansible_host` for an every-interface bind), refuses a loopback,
      non-IPv4 or `dns_records`-colliding record, and digs each one in its verify phase.
      `tasks/agw-probe-resolution.yml` is the one resolution step: interim hosts line while
      `agw_internal_dns_authoritative` is false (the default), and with it true removes every
      marker line (only those) and refuses a name the host resolver does not answer.
      Done (2026-10-03): `deploy-agentgateway.yml` and `renew-internal-certs.yml` include that
      task instead of an inline `lineinfile` (same interim line with the flag false); the deploy
      is the one writer, the daily renewal calls it read-only (`_agwr_check_only`: no write,
      no escalation, no OpenBao read; it fails naming Deploy agentgateway when the line is
      wrong), and a test refuses any other file carrying the marker. Open: the flag stays false until production
      hickory-dns runs and the probing hosts resolve through it (`internal-dns-naming` 6.1,
      6.1a, 6.2 — no production DNS template exists in `platform/semaphore/templates.yml`)
- [ ] 7.3 Validation gate: with the Cloudflare skip rule temporarily disabled in a declared
      window, a gateway restart loads Authentik's discovery document and serves `/v1`; no
      probing host carries the interim marker line
