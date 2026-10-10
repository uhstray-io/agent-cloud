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
- [ ] 2.3 Confirm SSE keep-alive comment lines from vLLM pass through unchanged and
      unbuffered (needs dgx-spark `inference-endpoint-reliability` deployed)
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
- [ ] 2.4 Validation gate: 2.2 proves scenario "Conformance against direct vLLM"; 2.3
      with a stream past 130 s proves scenario "Stream is not buffered"

## 3. Telemetry
- [ ] 3.1 o11y: `scrape.d/agentgateway.yml.j2` for the stats endpoint (`/metrics` on the
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
- [x] 3.2 Check whether `localRateLimit` has a log-only mode in the v1.5.0 schema; record
      the answer in `design.md` and set the first-week policy accordingly. 2026-09-17: no
      such mode (`RateLimitSpec` has only `maxTokens`, `tokensPerFill`, `fillInterval`,
      `type`); first week runs with loose figures tightened from observed rates
- [ ] 3.3 Validation gate: one hour of traffic renders p50 and p95 first-token latency and
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
- [ ] 4.2 `localRateLimit` per identity: `type: requests` per minute and `type: tokens`
      per hour, figures derived from the measured ceiling and written as comments.
      2026-09-17: v1.5.0 has no per-identity REQUEST bucket under `llm.policies` (`key`
      unreleased, `conditional` rejected); Joe chose per-key token BUDGETS backed by the
      service's own Postgres (design.md Open Questions). Shipped in the template: one
      global request bucket (`agw_rate_requests_per_minute_total`) + `hourly-tokens`
      budget per key (`agw_rate_tokens_per_hour`). Left for this task: derive both
      figures from the measured ceiling and write them into site-config inventory
- [ ] 4.3 site-config: production Caddy block upstream to the gateway; `Caddyfile.local.j2`
      `inference_api` unchanged in shape (upstream already comes from `r.upstream`);
      redeploy Caddy through Semaphore
- [ ] 4.4 Run dgx-spark's public-path verify play against the live hostname; update
      dgx-spark `docs/TEAM-ENDPOINT.md` (key changes, base URL does not)
- [ ] 4.5 Validation gate: an unenrolled key gets 401 at the gateway with no vLLM log
      line, proving scenario "Unknown key is rejected"; a burst from one identity gets
      429 while another identity is served, proving scenario "One client cannot exceed its
      share"; the gateway access log shows the public request, proving scenario "Public
      path traverses the gateway"; one inventory change and a Caddy redeploy restores the
      direct path, proving scenario "Rollback is one value during the grace period"
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

- [ ] 4.7 `legacy_shared_expires` = the route-switch date + 14 days (operator decision
      2026-09-27), set in site-config in the same change that switches the route

## 5. Retire the shared key, records
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
- [ ] 5.2 Accept the architecture record; append a dated pointer line to
      `plan/development/06-inference-skynet.md`; `platform/services/inference/` stub
      gains a README pointing at the gateway service and the dgx-spark roadmap record
      2026-10-04: the non-decision half is done — a dated pointer callout in
      `plan/development/06-inference-skynet.md`, and `platform/services/inference/README.md`
      replaces the two `.gitkeep` stubs, pointing at `platform/services/agentgateway/` and the
      dgx-spark `node-telemetry-and-placement-benchmark` roadmap record. Open: accepting the
      architecture record is Joe's decision, so this box stays unticked.
- [ ] 5.3 Validation gate: 5.1 proves scenario "Grace period ends"; the record and the
      plan 06 line prove scenario "Decision is findable and plan 06 is amended"; on
      archive, retain the outcome (worked / dead end / corrected) into bank
      `agent-cloud-750a33b9`

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
- [ ] 6.1a The one gateway probe path. Every check that sends a request to the gateway
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
- [ ] 6.2 Caddy's `inference` and `admin.inference` blocks proxy to `https://` with
      `transport http { tls_server_name <gateway SAN>; tls_trust_pool file <root>;
      tls_client_auth <cert> <key> }` (Caddy 2.11.4), the leaf files read from the Caddy
      host's mounted `current/` directory. The architecture document's deprecated
      directive is corrected by `production-internal-ca` task 9.1, not here
      2026-10-02 production: the `admin.inference` block proxies over mutual TLS. Manage Caddy
      Sites (Dev) tasks 2463 (dry run; only that block changed) and 2464; verified at the Caddy
      origin: 302 to the Authentik authorize endpoint with `client_id=agentgateway` (the public
      URL sits behind the Cloudflare challenge). Open: the `inference` block still dials vLLM
      directly until task 4.3
- [ ] 6.3 The model's `tls: {root, hostname, cert, key}` (the `agw-upstream` client leaf,
      production-internal-ca task 5.4a; mutual TLS decided 2026-09-28) and an `https://`
      base URL, once dgx-spark
      serves vLLM over HTTPS (dgx-spark session: `--ssl-certfile`, `--ssl-keyfile`,
      `--enable-ssl-refresh`)
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
