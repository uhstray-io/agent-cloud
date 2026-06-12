# ERPNext + LLM Integration Implementation Plan

> **Location:** `plan/development/ERPNEXT-DEPLOYMENT.md`
> **Date:** 2026-06-12 · **Status:** PROPOSED · **Owner:** uhstray-io
> **Context:** First stateful system-of-record for financial data on the platform. Greenfield ERPNext on dedicated Proxmox VMs (dev + prod, Podman), populated by cutover, followed by a three-plane LLM integration (local inference, read-only MCP, and a platform-level redaction-gate service).
>
> **For agentic workers:** Execute phase-by-phase with `superpowers:executing-plans` or `superpowers:subagent-driven-development`. Steps use checkbox (`- [ ]`) syntax. Every phase ends at a validation gate — do not start a phase until the prior gate passes.

**Goal:** Stand up ERPNext at `<erpnext-prod-domain>` through the standard composable deployment pattern — dev first, then prod with real books — then layer on LLM capability without any sensitive financial data ever leaving the perimeter.

**Architecture:** Two ERPNext VMs (dev with synthetic books and isolated credentials; prod with cutover books), each running frappe_docker adapted for podman-compose with its own MariaDB, Redis pair, and MinIO; backups cross-mirror between the VMs. Three LLM planes: (A) local inference via the planned WisAI stack, (B) read-only MCP consumed by Claude Desktop/Cowork, (C) **llm-gate** — a new platform service on its own minimal VM that holds the only Anthropic API key and enforces per-tenant allowlists, PII scrubbing, and budgets in code (ERPNext tenant #1, NetClaw #2).

**Tech stack:** ERPNext v16 (pinned tag), MariaDB 10.6, Redis 7, Podman + podman-compose, Ansible/Semaphore, OpenBao, Caddy (DNS-01/Cloudflare), FastAPI + `anthropic` SDK (llm-gate), Ollama (Plane A).

**Domain placeholders:** `<erpnext-prod-domain>` / `<erpnext-dev-domain>` stand in for the real service domains throughout this document — real values live in the private site-config repo (`site-config/erpnext/README.md`), alongside IPs and the filled-in inventory vars.

---

## Target outcome

When Phase 6's gate passes, the platform has its first financial system of record and its first production LLM integration — and the two are coupled only through code-enforced boundaries. Concretely, "done" means:

- **`<erpnext-prod-domain>` is the books.** UhhCraft revenue, marketplace payouts, and Zord license sales live in ERPNext — populated from a cutover date with opening balances and monthly CSV/OFX statement imports, and deployed, backed up, and validated exclusively through Semaphore templates [1].
- **A dev twin absorbs all experimentation.** `<erpnext-dev-domain>` (LAN-only DNS, isolated credentials) runs synthetic books through the LLM validation phases, becoming a standing mirror of prod at Phase 6; prod only ever sees `main`.
- **Finance questions are conversational.** Anyone with Claude Desktop asks in natural language and gets answers whose every figure traces to an ERPNext report; Claude Cowork produces the monthly close narrative the same way. The LLM never computes a number — it orchestrates and narrates.
- **The privacy boundary is a property of the system, not a policy document.** Exactly two paths lead toward Anthropic — the MCP exposure profile and the llm-gate service — both deny-by-default, both covered by CI tests that fail the build if PII could cross, both governed by the k-threshold rule (§6).
- **Spend is bounded by construction.** Per-tenant and global ledgers hard-stop at their ceilings, the Console workspace cap backstops them, and threshold crossings alert Discord before they become surprises.
- **The books survive VM loss.** Nightly backups land in MinIO on *both* VMs (cross-mirror), restores are proven quarterly via the dev mirror refresh, and an offsite/NAS target is planned future work.
- **Writes stay human.** Phase 7's draft-then-approve design exists on paper only until the read-only planes have production mileage.

The phase graph below shows how that end state is reached: solid arrows are hard sequencing enforced by validation gates (§19), the dotted edges are the one external dependency (WisAI) and the one soft input (Plane A feeding later use cases), and the tail phases are design-first future work.

```mermaid
flowchart LR
  WIS[WisAI deployment<br/>WISAI-DEPLOYMENT-PLAN] -. unblocks .-> P2
  P0[P0<br/>scaffolding + CI] --> P1A[P1A<br/>dev VM live - synthetic books + DR]
  P1A --> P1B[P1B<br/>prod live - cutover books + cross-mirror]
  P1B --> P3[P3<br/>Plane B read-only MCP - validated on dev]
  P3 --> P4[P4<br/>Plane C llm-gate service + budget]
  P4 --> P5[P5<br/>first use case]
  P2[P2<br/>Plane A local inference] -. later use cases .-> P5
  P5 --> P6[P6<br/>hardening + dev becomes mirror]
  P6 --> P7[P7<br/>gated writes - design first]
  P7 --> P8[P8<br/>custom MCP - future]
```

*Bracketed `[n]` markers here and throughout resolve in §23 — Source context & references.*

## 1. Problem

The platform has no accounting system of record. UhhCraft revenue (Stripe), marketplace payouts, and Zord license sales are reconciled by hand. We want ERPNext for the books **and** we want LLM assistance over those books — but customer PII, payment detail, and raw ledger lines must never reach a frontier API. The existing platform conventions (composable deploys, OpenBao-only secrets, Semaphore-only operations) must hold; ERPNext is the highest-stakes service yet, so this plan enforces them harder, not looser.

## 2. Locked decisions (from requirements intake)

| # | Decision | Consequence |
|---|----------|-------------|
| 1 | Greenfield ERPNext, Podman containers on Proxmox VMs, `<erpnext-prod-domain>`, **dev first then prod** | frappe_docker compose adapted for podman-compose; dev VM realized as a separate, credential-isolated host (see Decision criteria) |
| 2 | All PII / payment / sensitive data stays **local**; Claude only sees **anonymized/redacted/aggregated** data | Claude-facing MCP exposure profile + mandatory llm-gate on Plane C + k-threshold rule (§6) |
| 3 | **Read-only first**; write/draft is a later phase; auto-post thresholds when writes land | Phases 1–6 are read-only; Phase 7 introduces gated writes |
| 4 | Adopt native Frappe/ERPNext MCP servers first; custom MCP only for out-of-scope needs | Start with Frappe Assistant Core and/or mascor/frappe-mcp-server (repo names verified at P3 kickoff); custom MCP deferred to Phase 8 |
| 5 | Proposed use cases all in scope; tackle **one at a time** | Use-case backlog in §17, sequenced; one active per increment |
| 6 | Claude **Desktop** consumes MCP (interactive); Claude **Cowork** does reports + read-only; existing Anthropic account with a **budget ceiling** | Remote-capable MCP transport; per-tenant + global ledgers in llm-gate, Console spend cap as backstop |

Decisions made during the 2026-06-11 plan review (dev topology, books population, banking ingress, gate scope, backup DR, re-identification control, scheduling) are recorded with their alternatives in **Decision criteria** below.

### GPU / inference engineering notes

- Consumer RTX 30xx/40xx/50xx cards do **not** support MIG. The VRAM target (10–20% of the serving node's total) is enforced at the serving layer: Ollama model sizing/`keep_alive`, vLLM `--gpu-memory-utilization`, or Dynamo worker config. The earlier "10–20% of 36 GB ≈ 3.6–7.2 GB" figure assumes the final WisAI GPU topology — **confirm against the hardware decision gate in `UHHCRAFT-GPU-PASSTHROUGH.md` §1** before sizing. A 7–8B model at Q4/Q5 (≈4.5–5.5 GB + KV cache) fits any plausible outcome.
- The **ERPNext VMs need no GPU.** Inference runs on the WisAI GPU nodes; consumers call the OpenAI-compatible endpoint over the LAN.
- Serving starts on **Ollama** (the WisAI path); **NVIDIA Dynamo** is a Phase 6 validation spike, not a commitment.

## 3. Platform reality and dependencies

This plan was validated against what actually exists in agent-cloud and site-config (2026-06-09) — primarily the platform conventions [1], the deployed exemplars [16], the WisAI design [19], and the private inventory [24]. Phases below state these dependencies explicitly.

| Assumed component | Actual status | Impact on this plan |
|---|---|---|
| WisAI (Ollama + Open WebUI) | **Planned only** — `WISAI-DEPLOYMENT-PLAN.md`; `platform/services/inference/` is empty | Phase 2 **floats**: P3–P5 proceed without it; Plane-A-dependent backlog items wait |
| NVIDIA Dynamo | No code anywhere | Phase 6 spike with explicit go/no-go |
| Platform-wide MinIO / offsite backup | Neither exists; per-service MinIO is the established pattern (UhhCraft, ComfyUI, Hunyuan3D) | ERPNext VMs run their own MinIO; **cross-mirror between dev and prod** covers VM loss; NAS/offsite is planned future work |
| Banking data access | None — no bank API/aggregator integration anywhere | Cutover uses **CSV/OFX statement import** (no new credentials); bank/aggregator API is planned future work; dev uses synthetic data |
| Grafana/Prometheus/Loki | `platform/services/o11y/` is empty (planned) | Phase 6 observability scoped to what exists: Semaphore-scheduled health validation + Discord alerts; dashboards land when o11y does |
| Caddy | **Deployed**, per-site fragment system live (`sites/*.caddy`, `tasks/distribute-caddy-site.yml`) | Phase 0 ships `caddy-site.j2`; both VMs get fragments (`<erpnext-dev-domain>` resolves via pfSense only) |
| n8n | Deployed (legacy path); composable migration **HELD** | Phase 4 uses the running n8n via its API only; n8n never holds the Anthropic key |
| MCP configs in `agents/*/context/` | None exist yet | ERPNext establishes the convention: MCP config templates live in `platform/services/erpnext/context/mcp/` |
| Anthropic API usage | None deployed; NetClaw plan reserves its own key path | **Superseded**: the llm-gate platform service becomes the single key custodian; NetClaw onboards as tenant #2 instead of holding a key |
| Discord alerting | Pattern exists (UhhCraft ops webhooks) | Budget alerts reuse the Discord webhook pattern |
| SELinux concerns | VMs are Ubuntu 24.04 (AppArmor) — `:Z`/`:z` volume labels are inert | Dropped; the real Podman friction is podman-compose 1.0.6 ignoring `depends_on` conditions (see §7 deploy.sh) |

## 4. Design principles

1. **AI proposes → guardrails validate → automation executes.** No LLM output mutates ERPNext state in Phases 1–6.
2. **LLMs orchestrate; ERPNext computes.** No model — local or frontier — produces financial figures. Numbers come from ERPNext reports; models narrate.
3. **Secrets flow one way:** OpenBao → Ansible memory → Jinja2 → `.env`. Scripts read `.env`; scripts never call OpenBao (Critical Deployment Rules #2/#4).
4. **Every workflow independent** (rule #3): deploy, clean-deploy, backup, accounting bootstrap, and AI-user provisioning are separate playbooks/templates.
5. **Egress is enforced in code** at exactly two choke points: the MCP exposure profile (Plane B) and the llm-gate service (Plane C). The one procedural control — the k-threshold rule — rides on PR-gated config changes.
6. **Verify before hardening** (rule #5): every credential rotation and every auth change follows Create → Verify → Retire.
7. **Build code once.** One playbook codebase serves dev and prod via `erpnext_target`; one gate service serves every LLM-consuming tenant via config files.

## Decision criteria (alternatives considered)

The locked decisions in §2 came from requirements intake; the engineering decisions below were made while writing and reviewing this plan. Each was judged against the same criteria: platform-convention fit, precedent in deployed services, blast radius, and whether it couples this plan to work that doesn't exist yet. The rejected options are listed so future readers can tell a decision from an assumption.

| Decision | Chosen | Alternatives — and why they lost | Sources |
|---|---|---|---|
| Container runtime | Podman (rootless), the platform default | Docker — justified only where privileged containers or compose health-dependency chains demand it (the NetBox case); ERPNext needs neither | [4] |
| Compose tooling | podman-compose 1.0.6 as deployed today, with **staged startup in deploy.sh** replacing `depends_on` conditions | (a) docker-compose against the podman socket — no platform precedent; (b) blocking on podman-compose ≥ 1.3.0 — couples the deploy to an OS upgrade tracked in `PODMAN-UPGRADE-PLAN.md`. Staged startup works today and stays correct after the upgrade | [4][18] |
| Secret management | Composable `manage-secrets.yml` (OpenBao → Ansible memory → Jinja2 → `.env`) | Legacy deploy.sh-generated `secrets/` dir (the NocoDB/n8n path) — violates Critical Rules #2/#4 and reproduces the held-migration debt those services carry; unacceptable for a financial system | [1][14][23] |
| Dev/prod topology | **Separate dev VM with full credential isolation** (`service_name: erpnext-dev` → own OpenBao path, own passwords, own SSH keypair); LAN-only `<erpnext-dev-domain>` | One VM + branch deploys — cheaper, but intake said "dev first" and a financial system earns a real staging target; shared credentials — rejected because dev compromise would expose prod passwords, violating the blast-radius rules | [7] |
| Dev targeting | One playbook codebase + `erpnext_target` var; **separate Semaphore templates** pin dev vs prod | Single template with a target dropdown — one wrong selection deploys to prod; duplicate playbooks — violates build-once | [17] |
| Books population | **Cutover + opening balances** via a fixtures playbook (company/CoA/fiscal year as versioned fixtures; opening-balance journal entry is a one-time human task); dev runs a synthetic generator | Full historical import — a real data-migration work stream, deferred until wanted; sample-data-only — delays real value | — |
| Bank data ingress | **CSV/OFX statement import** at cutover and monthly | Aggregator API (Plaid-class) or bank-direct API — adds vendor onboarding + credential custody before any import pain exists; **planned future work** once volume justifies it | — |
| Backup DR | **Cross-mirror**: each VM's nightly backups also land on the peer VM's MinIO via a restricted write-only user | On-VM only — books and backups share fate on VM loss; NAS/offsite now — new infrastructure this plan doesn't otherwise need; **planned future work** | [16][21] |
| Re-identification control | **Documented k-threshold**: an aggregate may go to Claude only if it spans ≥10 records and no single counterparty dominates it; checked at every PR that touches the profile/tenant config | Qualitative-only allowlist — "conservative" is undefined at year-one volumes where most rollups sit on 1–5 transactions; code-enforced threshold — requires structured record-counts end-to-end; remains the §21 upgrade path | — |
| LLM integration topology | Three planes (local inference / read-only MCP / gated API) | Single LLM proxy (LiteLLM-style) — already rejected platform-wide in the WisAI plan; MCP-only — cannot serve scheduled server-side work and has no central redaction/budget point | [19] |
| Plane C gate scope | **Platform service (`llm-gate`) on its own minimal VM**, multi-tenant by config; ERPNext tenant #1, NetClaw #2 | ERPNext-scoped sidecar — duplicates the hardest security component per service; co-location on the ERPNext or n8n VM — puts the key inside another service's blast radius and defeats VM-scoped egress rules | [7][26] |
| Runtime OpenBao access | None — deploy-time only via Semaphore's AppRole | Dedicated runtime AppRole — no container has runtime secret needs; smallest blast radius wins; the code-managed AppRole path stays documented (§7.12) for the day dynamic DB creds arrive | [6][7] |
| MCP server | Deferred to a criteria-driven decision task at P3 kickoff (§10.1) | Building custom MCP first — rejected by locked decision #4; custom MCP is the Phase 8 escape hatch | [27] |
| Plane A serving engine | Ollama first | vLLM — reserved for 24 GB+ hardware per the WisAI plan; Dynamo — multi-node value unproven for one 7–8B model, P6 spike decides | [19][20] |
| ERPNext version | v16, pinned image tag | v15 LTS — older but battle-tested; v16 chosen as current stable with documented v15 fallback (P1A work item) | [25] |
| Scheduling | Gate-driven only — phases complete when gates pass | Dates + owners — better forcing function but stale-date rot in a committed doc | — |

The load-bearing rows are secret management, dev/prod isolation, LLM topology, and gate scope — get any of those wrong and the privacy posture degrades from *enforced* to *advisory*. Everything else is reversible at moderate cost.

## 5. Target architecture

The component map below shows the full perimeter: the prod ERPNext VM and the llm-gate VM ship with this plan (plus the dev twin), the platform services already exist (n8n on its legacy path), the WisAI subgraph is the one planned-but-not-built dependency, and the only line that crosses the perimeter is the gate's redacted, budgeted egress to Anthropic.

```mermaid
flowchart TB
  subgraph clients[Interactive AI clients]
    CD[Claude Desktop]
    CW[Claude Cowork]
  end

  subgraph perimeter[agent-cloud perimeter - on-prem, privacy-bounded]
    subgraph erpvm[ERPNext prod VM :8080]
      FE[Frappe/ERPNext<br/>frontend + backend + workers + scheduler]
      DB[(MariaDB 10.6)]
      RC[(Redis cache + queue)]
      MCP[MCP layer - read-only,<br/>Claude-facing exposure profile]
      MIN[(MinIO - backups)]
    end
    DEV[ERPNext dev VM<br/><erpnext-dev-domain><br/>synthetic books, isolated creds,<br/>own MinIO]
    subgraph gatevm[llm-gate VM :8088 - platform service]
      GATE[Redaction gate<br/>only Anthropic key holder,<br/>per-tenant allowlists + budgets]
    end
    subgraph infra[Platform services]
      CAD[Caddy - TLS DNS-01]
      BAO[OpenBao - secrets]
      N8N[n8n - legacy deploy, running]
      SEM[Semaphore - all operations]
    end
    subgraph wis[WisAI GPU nodes - PLANNED, WISAI-DEPLOYMENT-PLAN]
      OLL[Ollama via Open WebUI<br/>OpenAI-compatible API]
    end
  end

  EXT[Anthropic Claude API]

  CD & CW -->|MCP, read-only profile| MCP
  MCP --> FE
  FE --> DB & RC
  FE -.nightly backup.-> MIN
  MIN <-.cross-mirror.-> DEV
  N8N & FE -->|aggregated, k-threshold| GATE
  GATE -->|redacted, budgeted, logged| EXT
  N8N & FE -.sensitive tasks stay local.-> OLL
  CAD -->|reverse proxy :8080| FE
  BAO -.deploy-time secrets via Ansible.-> erpvm & DEV & gatevm
  SEM -.deploys, backups, validation.-> erpvm & DEV & gatevm
```

**Three planes**

- **Plane A — Local inference (WisAI).** OpenAI-compatible endpoint on the GPU nodes. Sensitive, high-volume, low-stakes tasks (categorization, embeddings, drafting). Data never leaves the perimeter. *Dependency: WisAI deployment; floats.*
- **Plane B — MCP + Claude (read-only).** Claude Desktop/Cowork drive ERPNext via MCP tools scoped to a non-sensitive/aggregated allowlist, authenticated, audit-logged. Validated against dev's synthetic books before prod ever connects.
- **Plane C — Claude API via llm-gate.** Server-side callers (n8n workflows, ERPNext server scripts, Cowork flows) POST to the llm-gate service; the gate validates the caller's tenant config, enforces its field allowlist, scrubs PII, enforces per-tenant + global budget ledgers, holds the only copy of the API key, and forwards to `/v1/messages`.

## 6. Data classification & egress policy

The spine of the privacy posture — enforced in code, not just documented.

| Class | Examples | May reach Claude (B/C)? | Engine |
|-------|----------|--------------------------|--------|
| **Sensitive** | Customer PII, payment/payout detail, **bank statements/transactions**, raw ledger lines (`GL Entry`) | **No** | Local (Plane A) only |
| **Aggregated** | Totals, ratios, period summaries, anonymized category rollups — *subject to the k-threshold rule* | Yes, through gate/profile | Claude (B/C) |
| **Redacted** | Records with PII fields stripped/tokenized | Yes, through gate | Claude (C) |
| **Non-sensitive metadata** | DocType schema, report definitions, chart-of-accounts structure | Yes | Claude (B/C) |

**Fail closed:** anything not explicitly classified is Sensitive.

**k-threshold rule (re-identification control):** an aggregate may be exposed to Claude only if it spans **≥10 underlying records** and no single counterparty dominates it. Rationale: with year-one volumes, "Zord license revenue, March: $1,200" over one sale *is* that customer's transaction, and subtraction across periods reconstructs individual records — the PII scrubber cannot catch context-identified amounts. The rule is checked by a human at every PR touching the MCP profile or a gate tenant config (PR review is the platform's change control); code enforcement is the §21 upgrade path.

Enforcement points:

1. The **Claude-facing MCP exposure profile** (Phase 3) excludes sensitive DocTypes/fields via allowlist — deny by default.
2. The **llm-gate service** (Phase 4) rejects payload fields not on the caller's tenant allowlist, scrubs PII patterns as a backstop, and ships with a CI test asserting zero PII egress on representative payloads.
3. **Routing policy** (§16) sends anything granular or sensitive to Plane A.

## 7. Phase 0 — Repo scaffolding & guardrails

**Deliverable:** Everything repo-side for ERPNext (dev + prod), mergeable and CI-green, before any VM exists. Follows the `SERVICE-INTEGRATION-PLAN.md` onboarding checklist Phases 0–4 and `AUTOMATION-COMPOSABILITY.md`. (The llm-gate service has its own scaffolding checklist in Phase 4.)

**Classification (onboarding Phase 0):** infrastructure tier, dedicated VMs, Podman, **deploy-time OpenBao access only** (Semaphore's AppRole; no runtime AppRole — containers never talk to OpenBao).

### 7.1 File structure

```text
platform/services/erpnext/
  deployment/
    compose.yml              frappe_docker adapted for podman-compose
    deploy.sh                container lifecycle only (staged startup)
    post-deploy.sh           app bootstrap: site creation, scheduler (reads .env only)
    backup.sh                bench backup + mc mirror local + cross-mirror to peer
    fixtures/
      company.json           company / fiscal year / CoA fixtures (versioned)
      chart-of-accounts.json
      bootstrap_accounting.py   idempotent bench-console script applying fixtures
      synthetic_books.py        dev-only generator: realistic-volume demo books
    templates/
      env.j2                 -> .env (rendered by manage-secrets)
      caddy-site.j2          -> central Caddy fragment (domain from inventory)
    CLAUDE.md / README.md    operational reference / quick-start
  context/
    mcp/                     MCP server configs + exposure profiles (Phase 3)
    prompts/                 report/summary prompt templates (Phase 5)
    runbooks/                failure modes, restore drill (Phases 1-6)

platform/playbooks/          all four take erpnext_target (default erpnext_svc)
  deploy-erpnext.yml         5-phase composable deploy + Caddy fragment
  clean-deploy-erpnext.yml   destructive wipe + fresh deploy
  backup-erpnext.yml         thin wrapper: runs backup.sh on the target
  bootstrap-erpnext-accounting.yml  fixtures -> books (synthetic flag for dev)
  provision-erpnext-ai-user.yml     read-only AI user + API keys + OpenBao writeback (Phase 3)

platform/semaphore/templates.yml   7 ERPNext template entries (§7.9)
platform/tests/test_service_erpnext.bats

site-config (private):
  proxmox/vm-specs.yml       erpnext (211) + erpnext-dev (212) entries
  inventory/production.yml   erpnext_svc + erpnext_dev_svc groups
  secrets/erpnext*/ , secrets/ssh/erpnext*/   DR backup copies (after first deploys)
```

### 7.2 `deployment/compose.yml`

frappe_docker translated to the platform's podman-compose rules: explicit `container_name` everywhere, fully-qualified images, healthchecks on every long-running service, **no reliance on `depends_on:` conditions** (podman-compose 1.0.6 ignores them — `deploy.sh` stages startup instead). Host ports: frontend `8080` (Caddy upstream) and MinIO `9000` (LAN — required for cross-mirror; root creds never leave the VM, the peer uses a restricted write-only user).

```yaml
name: erpnext

x-frappe: &frappe
  image: ${ERPNEXT_IMAGE:-docker.io/frappe/erpnext}:${ERPNEXT_VERSION:?set in .env}
  restart: unless-stopped
  networks: [erpnext]
  volumes:
    - sites:/home/frappe/frappe-bench/sites

services:
  db:
    image: docker.io/library/mariadb:10.6
    container_name: erpnext-db
    restart: unless-stopped
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --skip-character-set-client-handshake
      - --skip-innodb-read-only-compressed
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PASSWORD}
    volumes:
      - db-data:/var/lib/mysql
    networks: [erpnext]
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost --password=$$MYSQL_ROOT_PASSWORD --silent"]
      interval: 5s
      timeout: 5s
      retries: 12

  redis-cache:
    image: docker.io/library/redis:7-alpine
    container_name: erpnext-redis-cache
    restart: unless-stopped
    networks: [erpnext]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 12

  redis-queue:
    image: docker.io/library/redis:7-alpine
    container_name: erpnext-redis-queue
    restart: unless-stopped
    volumes:
      - redis-queue-data:/data
    networks: [erpnext]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 12

  # One-shot: writes common_site_config.json. Run via `compose run --rm configurator`.
  configurator:
    <<: *frappe
    container_name: erpnext-configurator
    restart: "no"
    entrypoint: ["bash", "-c"]
    command:
      - >
        ls -1 apps > sites/apps.txt;
        bench set-config -g db_host $$DB_HOST;
        bench set-config -gp db_port $$DB_PORT;
        bench set-config -g redis_cache "redis://$$REDIS_CACHE";
        bench set-config -g redis_queue "redis://$$REDIS_QUEUE";
        bench set-config -g redis_socketio "redis://$$REDIS_QUEUE";
        bench set-config -gp socketio_port $$SOCKETIO_PORT;
    environment:
      DB_HOST: db
      DB_PORT: "3306"
      REDIS_CACHE: redis-cache:6379
      REDIS_QUEUE: redis-queue:6379
      SOCKETIO_PORT: "9000"

  backend:
    <<: *frappe
    container_name: erpnext-backend
    healthcheck:
      # Connection-level check: gunicorn answers before the site exists;
      # full app health is gated via /api/method/ping through the frontend.
      test: ["CMD-SHELL", "curl -s -o /dev/null http://localhost:8000 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s

  frontend:
    <<: *frappe
    container_name: erpnext-frontend
    command: ["nginx-entrypoint.sh"]
    environment:
      BACKEND: backend:8000
      SOCKETIO: websocket:9000
      FRAPPE_SITE_NAME_HEADER: ${SITE_NAME}
      UPSTREAM_REAL_IP_ADDRESS: 127.0.0.1
      UPSTREAM_REAL_IP_HEADER: X-Forwarded-For
      UPSTREAM_REAL_IP_RECURSIVE: "off"
      PROXY_READ_TIMEOUT: "120"
      CLIENT_MAX_BODY_SIZE: 50m
    ports:
      - "8080:8080"   # LAN-exposed; central Caddy proxies to <vm-ip>:8080
    healthcheck:
      # Unhealthy until post-deploy.sh creates the site — expected on first deploy.
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/api/method/ping || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 120s

  websocket:
    <<: *frappe
    container_name: erpnext-websocket
    command: ["node", "/home/frappe/frappe-bench/apps/frappe/socketio.js"]

  queue-short:
    <<: *frappe
    container_name: erpnext-queue-short
    command: ["bench", "worker", "--queue", "short,default"]

  queue-long:
    <<: *frappe
    container_name: erpnext-queue-long
    command: ["bench", "worker", "--queue", "long,default,short"]

  scheduler:
    <<: *frappe
    container_name: erpnext-scheduler
    command: ["bench", "schedule"]

  minio:
    image: docker.io/minio/minio:RELEASE.2024-01-16T16-07-38Z
    container_name: erpnext-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      MINIO_BROWSER: "off"
    volumes:
      - minio-data:/data
    networks: [erpnext]
    ports:
      - "9000:9000"   # LAN — peer VM writes cross-mirror backups via restricted user
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:9000/minio/health/ready"]
      interval: 10s
      timeout: 5s
      retries: 12

networks:
  erpnext:
    driver: bridge

volumes:
  sites:
  db-data:
  redis-queue-data:
  minio-data:
```

Notes:

- `ERPNEXT_VERSION` is pinned in site-config inventory (`erpnext_version`), not floating. Verify the current stable v16 tag at Phase 1A start (`bench version` gate asserts it).
- The Phase 3 custom image (`ERPNEXT_IMAGE` override) is added in its phase.
- Redis instances are unauthenticated **inside the compose network only** (frappe_docker convention); neither publishes a host port.

### 7.3 `deployment/deploy.sh` — container lifecycle only

Mirrors `platform/services/uhhcraft/deployment/deploy.sh` [13][18]. Staged startup replaces `depends_on` conditions.

```bash
#!/usr/bin/env bash
# ERPNext — container lifecycle only.
#
# Usage:
#   ./deploy.sh [--no-pull] [ERPNEXT_URL]
#
# Required on disk before this script runs:
#   .env             — templated by Ansible (deploy-erpnext.yml) from OpenBao
#
# Steps (all idempotent):
#   1. Verify .env present (fail fast if Ansible didn't run)
#   2. Pull images (unless --no-pull)
#   3. Start backing services (db, redis, minio) and wait healthy
#   4. Run one-shot configurator
#   5. Start app tier (backend, websocket, workers, scheduler, frontend)
#
# Site creation/bootstrap happens in post-deploy.sh — NOT here.

set -euo pipefail

SKIP_PULL=false
ERPNEXT_URL=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    http://*|https://*) ERPNEXT_URL="$arg" ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: ./deploy.sh [--no-pull] [ERPNEXT_URL]"
      exit 1
      ;;
  esac
done

ERPNEXT_URL="${ERPNEXT_URL:-http://localhost:8080}"

step_verify_env() {
  info "Step 1: Verifying templated .env is present..."
  if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-erpnext.yml first."
  fi
  info "  .env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 2: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start_backing() {
  info "Step 3: Starting backing services (db, redis, minio)..."
  compose up -d db redis-cache redis-queue minio
  # podman-compose 1.0.6 ignores depends_on conditions — wait explicitly.
  wait_for_healthy "erpnext-db" 120
  wait_for_healthy "erpnext-redis-cache" 60
  wait_for_healthy "erpnext-redis-queue" 60
  wait_for_healthy "erpnext-minio" 60
}

step_configure() {
  info "Step 4: Running one-shot configurator..."
  compose run --rm configurator
}

step_start_app() {
  info "Step 5: Starting app tier..."
  compose up -d backend websocket queue-short queue-long scheduler frontend
  wait_for_healthy "erpnext-backend" 120
}

main() {
  info "=== ERPNext deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"

  step_verify_env
  step_pull_images
  step_start_backing
  step_configure
  step_start_app

  info "=== ERPNext container lifecycle complete ==="
  info "Next: run post-deploy.sh for site bootstrap (frontend reports healthy after it)."
}

main "$@"
```

### 7.4 `deployment/post-deploy.sh` — app bootstrap (reads `.env`, never OpenBao)

Idempotent, check-before-create. Consumes the admin/db passwords already templated into `.env` by Ansible. Runtime API-credential creation (Phase 3) and accounting data (Phase 1) are separate Ansible workflows, not this script.

```bash
#!/usr/bin/env bash
# ERPNext — application bootstrap. Idempotent. Reads .env only.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

set -a
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.env"
set +a

: "${SITE_NAME:?SITE_NAME missing from .env}"
: "${DB_PASSWORD:?DB_PASSWORD missing from .env}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD missing from .env}"
PUBLIC_URL="${PUBLIC_URL:-https://${SITE_NAME}}"

bench_exec() {
  compose exec -T backend "$@"
}

step_create_site() {
  info "Step 1: Ensuring site ${SITE_NAME} exists..."
  if bench_exec bash -lc "test -d sites/${SITE_NAME}"; then
    info "  Site exists — skipping new-site."
  else
    info "  Creating site (this takes a few minutes)..."
    bench_exec bench new-site "${SITE_NAME}" \
      --mariadb-user-host-login-scope='%' \
      --db-root-password "${DB_PASSWORD}" \
      --admin-password "${ADMIN_PASSWORD}" \
      --install-app erpnext \
      --set-default
  fi
}

step_ensure_app() {
  info "Step 2: Ensuring erpnext app installed on site..."
  if bench_exec bench --site "${SITE_NAME}" list-apps | grep -q '^erpnext'; then
    info "  erpnext already installed."
  else
    bench_exec bench --site "${SITE_NAME}" install-app erpnext
  fi
}

step_site_config() {
  info "Step 3: Setting host_name + scheduler..."
  bench_exec bench --site "${SITE_NAME}" set-config host_name "${PUBLIC_URL}"
  bench_exec bench --site "${SITE_NAME}" enable-scheduler
}

step_verify() {
  info "Step 4: Verifying app answers..."
  wait_for_http "http://localhost:8080/api/method/ping" "ERPNext" 180
  bench_exec bench version
}

main() {
  info "=== ERPNext post-deploy bootstrap ==="
  detect_runtime
  step_create_site
  step_ensure_app
  step_site_config
  step_verify
  info "=== Bootstrap complete: ${PUBLIC_URL} ==="
}

main "$@"
```

### 7.5 `deployment/templates/env.j2`

Approved namespaces only (`secrets.*`, inventory vars with defaults, `ansible_*`). No literals. Because dev's host sets `service_name: erpnext-dev`, `manage-secrets` automatically resolves a **separate secret set** for it.

```jinja2
{# Rendered by Ansible's tasks/manage-secrets.yml on each deploy.
   No literal secrets in this file — all come from Jinja variables. #}

# ERPNext — .env  (generated {{ ansible_date_time.iso8601 | default('<deploy-time>') }};
# do NOT edit by hand — re-run the playbook to refresh)

# --- Images ---
ERPNEXT_IMAGE={{ erpnext_image | default('docker.io/frappe/erpnext') }}
ERPNEXT_VERSION={{ erpnext_version }}

# --- Site ---
SITE_NAME={{ erpnext_site_name }}
PUBLIC_URL={{ erpnext_public_url }}

# --- MariaDB / Admin ---
DB_PASSWORD={{ secrets.mariadb_root_password }}
ADMIN_PASSWORD={{ secrets.admin_password }}

# --- MinIO (this VM; backup target) ---
MINIO_ROOT_USER={{ secrets.minio_root_user }}
MINIO_ROOT_PASSWORD={{ secrets.minio_root_password }}
BACKUP_BUCKET={{ erpnext_backup_bucket | default('erpnext-backups') }}

# --- Cross-mirror to peer VM (empty until the peer exists; backup.sh skips) ---
PEER_MINIO_URL={{ erpnext_peer_minio_url | default('') }}
PEER_BACKUP_KEY={{ secrets.peer_backup_key | default('') }}
PEER_BACKUP_SECRET={{ secrets.peer_backup_secret | default('') }}
```

### 7.6 `deployment/templates/caddy-site.j2`

Identical shape to the UhhCraft fragment [16]; distributed by the deploy playbook via `tasks/distribute-caddy-site.yml`. The domain comes from `erpnext_public_url`, so **the same template serves prod (`<erpnext-prod-domain>`) and dev (`<erpnext-dev-domain>`)** — dev's name resolves only via pfSense local DNS; DNS-01 issues its cert without a public record. Caddy handles WebSocket upgrades transparently.

```jinja2
{{ svc_domain }} {
    encode brotli gzip
    header {
        Strict-Transport-Security "max-age=15552000;"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
    reverse_proxy {{ svc_upstream }} {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

### 7.7 `platform/playbooks/deploy-erpnext.yml`

One codebase for dev and prod: every play targets `hosts: "{{ erpnext_target | default('erpnext_svc') }}"`; the Dev Semaphore templates pin `erpnext_target=erpnext_dev_svc`. Per the platform's redaction standard, **no `no_log: true` anywhere** — the callback plugin redacts [10]. Phase 1 (the contract) in full; phases 2–5 are line-for-line the `deploy-uhhcraft.yml` pattern [13] with the ERPNext values tabled below.

```yaml
---
# --- Phase 1: Clone + Secrets + Env Files ---
- name: "Phase 1: Clone repo + manage secrets + template env"
  hosts: "{{ erpnext_target | default('erpnext_svc') }}"
  gather_facts: false
  become: false
  vars:
    _monorepo_dir: "/home/{{ ansible_user }}/agent-cloud"
    _deploy_dir: "{{ _monorepo_dir }}/{{ monorepo_deploy_path }}"
    _branch: "{{ service_branch | default('main') }}"
    _bao_url: "{{ openbao_addr | default('') }}"
    _bao_role_id: "{{ bao_role_id | default(lookup('env', 'BAO_ROLE_ID')) }}"
    _bao_secret_id: "{{ bao_secret_id | default(lookup('env', 'BAO_SECRET_ID')) }}"
    _service_url: "{{ service_url | default('http://localhost:8080') }}"
    _secret_definitions:
      # Backing services (random generation, first deploy only)
      - { name: mariadb_root_password, type: random, length: 32 }
      - { name: admin_password, type: random, length: 24 }
      - { name: minio_root_user, type: random, length: 16 }
      - { name: minio_root_password, type: random, length: 32 }
      # Runtime-created (P3 writeback) / set when the peer VM exists (P1B)
      - { name: api_key, type: user }
      - { name: api_secret, type: user }
      - { name: peer_backup_key, type: user }
      - { name: peer_backup_secret, type: user }
    _env_templates:
      - { src: env.j2, dest: .env, mode: "0600" }

  tasks:
    - name: "Install git if needed"
      ansible.builtin.apt:
        name: git
        state: present
      become: true

    - name: "Clone or update agent-cloud monorepo"
      ansible.builtin.git:
        repo: "{{ monorepo_repo }}"
        dest: "{{ _monorepo_dir }}"
        version: "{{ _branch }}"
        force: true

    - name: "Ensure podman + compose entrypoint"
      ansible.builtin.include_tasks: tasks/install-podman-compose.yml

    - name: "Convenience symlink"
      ansible.builtin.file:
        src: "{{ _deploy_dir }}"
        dest: "/home/{{ ansible_user }}/{{ service_name }}"
        state: link
        force: true
      register: _symlink_result
      failed_when: >-
        _symlink_result is failed
        and 'permission denied' not in (_symlink_result.msg | default('') | lower)

    - name: "Manage secrets and template env file"
      ansible.builtin.include_tasks: tasks/manage-secrets.yml
```

| Play (mirrors `deploy-uhhcraft.yml`) | ERPNext specifics |
|---|---|
| Phase 2 — container lifecycle | `bash deploy.sh {{ _service_url }}` with `CONTAINER_ENGINE: "{{ container_engine \| default('podman') }}"` |
| Phase 3 — app bootstrap | `bash post-deploy.sh` (idempotent site creation) |
| Phase 4 — health verification | `uri` against `{{ _service_url }}/api/method/ping` until 200 (5 retries × 10 s) |
| Phase 5 — Caddy fragment | render `caddy-site.j2` with `svc_domain` from `erpnext_public_url`, `svc_upstream: "{{ ansible_default_ipv4.address }}:8080"`; push via `tasks/distribute-caddy-site.yml` when `caddy_svc` exists |

`type: user` secrets resolve to **empty strings** until their phase populates them (`api_key`/`api_secret` at the P3 writeback, `peer_backup_*` at P1B peer setup). Nothing consumes them earlier: `env.j2` references only the `peer_*` values (with `default('')`), and `backup.sh` skips the cross-mirror step unless **both** `PEER_MINIO_URL` and `PEER_BACKUP_KEY` are non-empty.

### 7.8 Companion playbooks

- **`clean-deploy-erpnext.yml`** — DESTRUCTIVE: `tasks/clean-service.yml` (containers + volumes + clone) then `import_playbook: deploy-erpnext.yml`. OpenBao secrets survive; same passwords are reused.
- **`backup-erpnext.yml`** — thin wrapper: runs `backup.sh` on the target with `CONTAINER_ENGINE` set (full script in §8.3).
- **`bootstrap-erpnext-accounting.yml`** — copies `fixtures/` into the sites volume, runs `bench console < bootstrap_accounting.py` (idempotent: company, fiscal year, CoA), and with `-e synthetic=true` also runs `synthetic_books.py` to generate realistic-volume demo books for dev. Exact ERPNext setup calls finalized at implementation (§21). **Dependency:** real opening balances need exported bank statements (CSV/OFX) — a human task at cutover; the playbook handles structure, not balances.
- **`provision-erpnext-ai-user.yml`** — Phase 3 (§10.3).

### 7.9 Semaphore templates (`platform/semaphore/templates.yml`)

Seven entries, applied via `setup-templates.yml` (config-as-code; never ad-hoc API calls) [17]. Separate dev/prod templates keep the deploy target explicit in the button you click; all reuse the same playbooks via `erpnext_target`.

| Template | Playbook | Pinned vars |
|---|---|---|
| Deploy ERPNext | deploy-erpnext.yml | — |
| Deploy ERPNext (Dev) | deploy-erpnext.yml | `erpnext_target=erpnext_dev_svc` |
| Clean Deploy ERPNext | clean-deploy-erpnext.yml | — *(prod: destructive; restore follows)* |
| Clean Deploy ERPNext (Dev) | clean-deploy-erpnext.yml | `erpnext_target=erpnext_dev_svc` |
| Backup ERPNext | backup-erpnext.yml | runs against both groups on schedule |
| Bootstrap ERPNext Accounting | bootstrap-erpnext-accounting.yml | `synthetic` survey var |
| Provision ERPNext AI User | provision-erpnext-ai-user.yml | `erpnext_target` survey var |

All deploy templates carry the standard `service_branch` survey var (default `main`) [11]. Example entry:

```yaml
  - name: Deploy ERPNext (Dev)
    playbook: platform/playbooks/deploy-erpnext.yml
    survey_vars:
      - name: service_branch
        title: "Branch"
        type: string
        required: false
        default_value: "main"
      - name: erpnext_target
        title: "Target (pinned)"
        type: string
        required: false
        default_value: "erpnext_dev_svc"
```

### 7.10 CI tests (`platform/tests/test_service_erpnext.bats`)

Mirror the existing `test_service_*.bats` structure; checks per `CI-TESTING-SPECIFICATION.md` [8]:

```bats
#!/usr/bin/env bats
# ERPNext service validation

SERVICE_DIR="platform/services/erpnext/deployment"

@test "erpnext compose.yml is valid YAML" {
  run python3 -c "import yaml; yaml.safe_load(open('${SERVICE_DIR}/compose.yml'))"
  [ "$status" -eq 0 ]
}

@test "erpnext compose.yml defines required services" {
  for svc in db redis-cache redis-queue configurator backend frontend websocket queue-short queue-long scheduler minio; do
    grep -q "container_name: erpnext-${svc}" "${SERVICE_DIR}/compose.yml"
  done
}

@test "erpnext compose.yml has no hardcoded credentials" {
  ! grep -E 'PASSWORD: [A-Za-z0-9]{8,}' "${SERVICE_DIR}/compose.yml"
}

@test "erpnext compose.yml has no RFC1918 IPs" {
  ! grep -E '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "${SERVICE_DIR}/compose.yml"
}

@test "erpnext deploy.sh is executable with bash shebang" {
  [ -x "${SERVICE_DIR}/deploy.sh" ]
  head -1 "${SERVICE_DIR}/deploy.sh" | grep -q bash
}

@test "erpnext deploy.sh sources common.sh and never hardcodes an engine" {
  grep -q 'common.sh' "${SERVICE_DIR}/deploy.sh"
  ! grep -E '^\s*(docker|podman) ' "${SERVICE_DIR}/deploy.sh"
}

@test "erpnext fixtures are valid JSON" {
  for f in "${SERVICE_DIR}"/fixtures/*.json; do
    python3 -c "import json; json.load(open('$f'))"
  done
}

@test "erpnext templates use approved Jinja2 namespaces only" {
  run grep -oE '\{\{ *[a-z_.]+' "${SERVICE_DIR}/templates/env.j2"
  for var in $output; do
    cleaned="${var#\{\{ }"
    [[ "$cleaned" =~ ^(secrets\.|erpnext_|ansible_) ]]
  done
}

@test "erpnext .env is not tracked by git" {
  ! git ls-files --error-unmatch "${SERVICE_DIR}/.env" 2>/dev/null
}
```

### 7.11 site-config additions (private repo — placeholders here)

`site-config/proxmox/vm-specs.yml` — VMIDs from the 200–299 range (confirm free); dev sized smaller until it becomes the standing mirror:

```yaml
  erpnext:
    name: erpnext-svc-01
    vmid: 211
    node: <proxmox-node>
    cores: 4
    memory: 12288
    disk: "80G"
    ip: "<erpnext-prod-ip>"
    tags: "ubuntu;24.04;erpnext;erp;finance;bizascode"
  erpnext-dev:
    name: erpnext-dev-01
    vmid: 212
    node: <proxmox-node>
    cores: 4
    memory: 8192
    disk: "60G"            # grow to prod spec when dev becomes the standing mirror (P6)
    ip: "<erpnext-dev-ip>"
    tags: "ubuntu;24.04;erpnext;erp;dev"
```

`site-config/inventory/production.yml` — two groups under `agent_cloud.children`. **`service_name: erpnext-dev` keys dev to its own OpenBao secret path and `.env` — full credential isolation falls out of `manage-secrets` for free.**

```yaml
    erpnext_svc:
      hosts:
        erpnext:
          ansible_host: <erpnext-prod-ip>
          service_name: erpnext
          service_url: "http://<erpnext-prod-ip>:8080"
          health_path: "/api/method/ping"
          health_status_codes: [200]
          monorepo_deploy_path: platform/services/erpnext/deployment
          proxmox_vmid: 211
          service_port: 8080
          container_engine: podman
          erpnext_version: "<pinned v16 tag>"
          erpnext_site_name: <erpnext-prod-domain>
          erpnext_public_url: "https://<erpnext-prod-domain>"
          erpnext_peer_minio_url: "<erpnext-dev-ip>:9000"
    erpnext_dev_svc:
      hosts:
        erpnext-dev:
          ansible_host: <erpnext-dev-ip>
          service_name: erpnext-dev
          service_url: "http://<erpnext-dev-ip>:8080"
          health_path: "/api/method/ping"
          health_status_codes: [200]
          monorepo_deploy_path: platform/services/erpnext/deployment
          proxmox_vmid: 212
          service_port: 8080
          container_engine: podman
          erpnext_version: "<pinned v16 tag>"
          erpnext_site_name: <erpnext-dev-domain>
          erpnext_public_url: "https://<erpnext-dev-domain>"
          erpnext_peer_minio_url: "<erpnext-prod-ip>:9000"
```

### 7.12 OpenBao layout additions

| Path | Contents |
|------|----------|
| `secret/services/erpnext` | `mariadb_root_password`, `admin_password`, `minio_root_user`, `minio_root_password`, `api_key`*, `api_secret`*, `peer_backup_key`*, `peer_backup_secret`* |
| `secret/services/erpnext-dev` | Same shape, **independently generated values** — dev compromise touches nothing in prod |
| `secret/services/ssh/erpnext`, `secret/services/ssh/erpnext-dev` | Per-host ed25519 keypairs |
| `secret/services/llm-gate`, `secret/services/ssh/llm-gate` | Phase 4 (§11.1) |

\* `user` type — populated by their phases (P3 writeback, P1B peer setup), never auto-generated. `peer_backup_*` holds the *other* VM's restricted MinIO write-only user — root MinIO creds never cross VMs.

No runtime AppRole: no container here authenticates to OpenBao; deploy-time access uses Semaphore's AppRole. If dynamic DB creds ever arrive (P6 option), provision a scoped AppRole code-managed via `tasks/manage-approle.yml` + an `.hcl` policy, mirroring `provision-orb-agent-approle.yml`.

### 7.13 Documentation updates (same PR)

- [ ] Root `CLAUDE.md`: secrets-table rows (§7.12), the ERPNext workflows in the Independent Workflows table, the dev-VM pattern (`<service>_dev_svc` + `erpnext_target`) as a documented convention, sub-directory docs entry, In Progress status.
- [ ] Root `README.md`: ERPNext in the service list.
- [ ] `platform/services/erpnext/deployment/CLAUDE.md` + `README.md`: stack, Semaphore template names, emergency read-only SSH commands, podman-compose staged-startup notes.

### 7.14 Phase 0 work items

- [ ] Create all files in §7.1–§7.10 on branch `feat/erpnext-phase0`; `chmod +x` the three scripts
- [ ] Lint locally: `shellcheck` (scripts), `yamllint`, `ansible-lint` (playbooks), `bats platform/tests/test_service_erpnext.bats`
- [ ] Mandatory Pre-Push Audit (staged-diff IP/credential grep) before commit
- [ ] PR with `/simplify` + `/security-review`; wait for all checks; fix findings; merge only after green
- [ ] Add site-config entries (§7.11) in the private repo
- [ ] Run `setup-templates.yml` via Semaphore to register the seven templates

### Phase 0 gate

| Check | How | Pass condition |
|---|---|---|
| Scripts contain no secret handling | `grep -E 'gen_secret\|put_secret\|get_secret\|bao_\|BAO_' platform/services/erpnext/deployment/*.sh` | No matches (scripts read `.env` only) |
| CI green | PR checks | shellcheck/ansible-lint/yamllint/trufflehog/BATS all pass |
| Templates registered | Semaphore UI / API | 7 ERPNext templates exist, wired to inventory 2 + env 2 |
| No real IPs/credentials in public repo | Pre-push audit + trufflehog | Clean |
| Playbook syntax | `ansible-playbook --syntax-check` on all five | Valid |

## 8. Phase 1 — ERPNext up: dev first (1A), then prod with real books (1B)

All operations through Semaphore (ACCESS-BOUNDARIES: no SSH deploys; SSH is read-only diagnostics only) [7].

### 8.1 Phase 1A — dev VM live with synthetic books

- [ ] Provision VM 212 via **Provision VM** template (clone 9000, Ubuntu 24.04, cloud-init)
- [ ] Generate dev SSH keypair → `secret/services/ssh/erpnext-dev` (+ site-config DR copy); **Distribute SSH Keys**; verify key auth; only then **Harden SSH** (rule #5)
- [ ] pfSense local-DNS host override for `<erpnext-dev-domain>` (no public record; DNS-01 still issues the cert)
- [ ] Pin `erpnext_version` to the current stable v16 tag — verify the tag exists on `docker.io/frappe/erpnext` and matches frappe_docker guidance (assumption [25]); v15 LTS fallback recorded here if needed
- [ ] **Deploy ERPNext (Dev)** from `feat/erpnext-phase1` branch, then from `main` after merge [11]
- [ ] **Bootstrap ERPNext Accounting** with `synthetic=true` — fixtures structure + realistic-volume demo books
- [ ] **Backup ERPNext** once manually; then run the restore drill (§8.4) on dev
- [ ] Add ERPNext blocks to `validate-all.yml` / `validate-secrets.yml`

**Gate 1A:**

| Check | How | Pass condition |
|---|---|---|
| Dev live over TLS | `curl -s https://<erpnext-dev-domain>/api/method/ping` (LAN) | `{"message":"pong"}`, valid cert, login works |
| Version | `bench version` via Semaphore/read-only SSH | Pinned `erpnext_version` |
| Synthetic books queryable | Run a P&L report on dev | Returns data across ≥12 months / ≥10 records per rollup (k-rule-compatible eval data) |
| Idempotency | Re-run **Deploy ERPNext (Dev)** | "Site exists — skipping"; no duplicate site/app; no secret churn in OpenBao |
| Backup + restore on dev | §8.3 run + §8.4 drill | Objects in dev MinIO; drill restores and verifies a known record |

### 8.2 Phase 1B — prod live, books cut over, cross-mirror on

- [ ] Provision VM 211; SSH keys (`ssh/erpnext`) → distribute → verify → harden; public DNS for `<erpnext-prod-domain>`
- [ ] **Deploy ERPNext** from `main`
- [ ] **Bootstrap ERPNext Accounting** (`synthetic=false`) — company/CoA/fiscal-year structure
- [ ] **Human cutover task:** export bank statements (CSV/OFX), import via ERPNext's native statement import, post the opening-balance journal entry as of the cutover date, verify trial balance against the source
- [ ] **Cross-mirror setup:** on each MinIO create a `backup-writer` user (`mc admin user add` + write-only policy on the backup bucket); store each side's writer creds in the *peer's* secret set (`peer_backup_*`); re-run both deploys to re-template `.env`
- [ ] Schedule **Backup ERPNext** nightly in Semaphore (both targets)

**Gate 1B:**

| Check | How | Pass condition |
|---|---|---|
| Public TLS + login | `https://<erpnext-prod-domain>` | Valid LE cert; login works; authenticated REST GET returns data |
| Books verified | Trial balance vs opening-balance source | Matches exactly |
| Containers healthy | `podman ps` (read-only SSH) | All 10 long-running containers healthy |
| Cross-mirror | Backup run on prod | Backup objects present on **dev's** MinIO under the prod bucket path |
| Nightly schedule | Semaphore | Both backup jobs scheduled and green |
| No plaintext secrets | Repo scan + VM check | Only `.env` (0600, gitignored) on VMs; no `secrets/` dirs |
| Validate templates | **Validate All**, **Validate Secrets** | ERPNext blocks pass for both hosts |

### 8.3 `deployment/backup.sh`

Container-ops only (reads `.env`). Nightly stamps expire after 30 days; first-of-month stamps are kept under `monthly/`; the cross-mirror step is skipped while `PEER_MINIO_URL` is unset (i.e., before the peer exists).

```bash
#!/usr/bin/env bash
# ERPNext — backup: bench backup --with-files, mirror to local MinIO, cross-mirror to peer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"
# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"
set -a
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.env"
set +a

: "${SITE_NAME:?}" ; : "${MINIO_ROOT_USER:?}" ; : "${MINIO_ROOT_PASSWORD:?}"
MC_IMAGE="${MC_IMAGE:-docker.io/minio/mc:latest}"   # pin exact tag at implementation
PREFIX="nightly"; [ "$(date +%d)" = "01" ] && PREFIX="monthly"
STAMP="${PREFIX}/$(date +%Y-%m-%d_%H%M)"

detect_runtime

info "Step 1: bench backup (db + files)..."
compose exec -T backend bench --site "${SITE_NAME}" backup --with-files

info "Step 2: resolving sites volume..."
SITES_VOL="$(${CONTAINER_ENGINE} inspect erpnext-backend \
  --format '{{ range .Mounts }}{{ if eq .Destination "/home/frappe/frappe-bench/sites" }}{{ .Name }}{{ end }}{{ end }}')"

info "Step 3: mirror to local MinIO + prune nightlies older than 30d..."
# Join the minio container's netns so 127.0.0.1:9000 resolves regardless of
# compose project/network naming differences under podman-compose.
${CONTAINER_ENGINE} run --rm \
  --network "container:erpnext-minio" \
  -v "${SITES_VOL}:/sites:ro" \
  -e MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@127.0.0.1:9000" \
  "${MC_IMAGE}" sh -c "
    mc mb --ignore-existing local/${BACKUP_BUCKET} &&
    mc mirror --overwrite /sites/${SITE_NAME}/private/backups local/${BACKUP_BUCKET}/${STAMP} &&
    mc rm -r --force --older-than 30d local/${BACKUP_BUCKET}/nightly || true
  "

if [ -n "${PEER_MINIO_URL:-}" ] && [ -n "${PEER_BACKUP_KEY:-}" ]; then
  info "Step 4: cross-mirror to peer (${PEER_MINIO_URL}) via restricted backup-writer..."
  ${CONTAINER_ENGINE} run --rm \
    -v "${SITES_VOL}:/sites:ro" \
    -e MC_HOST_peer="http://${PEER_BACKUP_KEY}:${PEER_BACKUP_SECRET}@${PEER_MINIO_URL}" \
    "${MC_IMAGE}" sh -c "
      mc mirror --overwrite /sites/${SITE_NAME}/private/backups peer/${BACKUP_BUCKET}/${STAMP}
    "
else
  info "Step 4: peer URL or backup-writer creds unset — skipping cross-mirror."
fi

info "Backup complete: ${BACKUP_BUCKET}/${STAMP}"
```

### 8.4 Restore drill (gate requirement — financial data DR is non-negotiable)

Quarterly, documented in `context/runbooks/restore.md` [21]. Through P5 the drill runs on dev (restore real prod backup → verify → **Clean Deploy ERPNext (Dev)** back to synthetic); from P6 onward dev becomes the **standing mirror** and the drill *is* the mirror refresh:

1. **Clean Deploy ERPNext (Dev)** — fresh stack, dev's own OpenBao secrets.
2. Pull the latest prod backup set from dev's mirrored copy (`mc cp --recursive`, same netns pattern as backup.sh).
3. Restore: `podman exec -it erpnext-backend bench --site <site> restore <db>.sql.gz --with-public-files <pub>.tar --with-private-files <priv>.tar --db-root-password "${DB_PASSWORD}"`
4. Verify a known record + trial balance; record the drill in the runbook.

## 9. Phase 2 — Local inference (Plane A) — *floats*

**Hard dependency:** WisAI Phases 1–2 (`WISAI-DEPLOYMENT-PLAN.md`) [19]. **Non-blocking for this plan:** P3–P5 proceed without it; only Plane-A backlog items (categorization, RAG, bank-feed assistance) wait. This phase consumes inference infrastructure; it does not build it.

- [ ] Confirm WisAI through its endpoint-live gate; endpoint + key at `secret/services/inference/*` per that plan
- [ ] Select the 7–8B model (candidates: Qwen3 8B, Llama 3.1 8B) by a 10-prompt categorization/extraction eval recorded in `context/prompts/plane-a-eval.md`
- [ ] Enforce the VRAM budget at the serving layer (model choice + Ollama `keep_alive`/`num_ctx`); document measured VRAM in the eval file
- [ ] Surface the endpoint to consumers (llm-gate env + n8n); ERPNext core config untouched until a use case needs it
- [ ] Confirm no Caddy fragment / no public route for inference

**Gate:** model within VRAM budget (`nvidia-smi`); completion succeeds from the ERPNext VM; latency baseline committed; no public route; endpoint-down fallback documented (sensitive tasks queue or fail — never fall back to Plane C).

## 10. Phase 3 — Read-only MCP + Claude Desktop (Plane B)

**Deliverable:** Claude Desktop (and Cowork) reading ERPNext through an authenticated, audit-logged, allowlist-scoped MCP layer — **validated entirely against dev's synthetic books before prod ever connects**. ERPNext establishes the platform's MCP config convention under `platform/services/erpnext/context/mcp/`.

### 10.1 MCP server selection (decision task, first item of the phase)

Verify current repos/maintenance (names from intake [27]; confirm with a web search at kickoff — both move fast):

| Criterion | Frappe Assistant Core (in-app Frappe app) | Standalone frappe-mcp-server (e.g. mascor / appliedrelevance) |
|---|---|---|
| Auth | OAuth2 via Frappe provider | ERPNext API key/secret |
| Permission model | Frappe roles/permissions, plugin-scoped | Field/DocType allowlist at tool boundary |
| Audit | In-app audit log | Server-side request log |
| Transport | HTTP (remote-capable for Desktop/Cowork connectors) | stdio (Desktop local) |
| Deploy shape | Installed into the bench (custom image) | Separate container/process |

Lean: **Frappe Assistant Core** for identity + audit; add the standalone server only where field-level redaction at the tool boundary proves necessary. Record the decision + rationale in `context/mcp/DECISION.md` before building.

### 10.2 Custom image (required for any in-bench app)

The stock `frappe/erpnext` image contains only frappe + erpnext; additional apps require a layered image (frappe_docker's documented custom-app build [25]):

- [ ] `deployment/apps.json` — `[{"url": "<assistant-core repo>", "branch": "<pinned>"}]`
- [ ] GitHub Actions job building `ghcr.io/uhstray-io/erpnext:<erpnext_version>-<short-sha>` via frappe_docker's layered Containerfile (`APPS_JSON_BASE64` build arg) — the WisBot prebuilt-GHCR pattern; hadolint applies
- [ ] Flip `erpnext_image`/`erpnext_version` in site-config (dev first, prod after Gate); `install-app` added to post-deploy.sh behind the same check-before-create guard

### 10.3 Read-only AI service user (`provision-erpnext-ai-user.yml`)

Independent workflow (rule #3). Creates the user + role, generates API keys **inside the app**, writes them back to OpenBao — the `manage-diode-credentials.yml` pattern [15]. No `no_log` (callback plugin redacts [10]).

The sequence below shows the full credential round trip. The original draft of this plan had `post-deploy.sh` doing this writeback, which would have violated Critical Rules #2/#4 — the corrected flow keeps the secret in Ansible memory between the bench call and the OpenBao PATCH, never on the VM's disk; subsequent deploys then render it into consumer env files via `manage-secrets.yml` like any other secret.

```mermaid
sequenceDiagram
    participant SEM as Semaphore template run
    participant ANS as Ansible playbook
    participant BE as erpnext-backend container
    participant BAO as OpenBao

    SEM->>ANS: launch with AppRole env (BAO_ROLE_ID / BAO_SECRET_ID)
    ANS->>BE: bench console - ensure AI user + AI Analyst role
    ANS->>BE: bench execute generate_keys(ai-readonly)
    BE-->>ANS: api_secret (stdout) + api_key (user record)
    ANS->>BAO: POST /v1/auth/approle/login
    BAO-->>ANS: client_token
    ANS->>BAO: PATCH secret/data/services/erpnext (merge api_key, api_secret)
    BAO-->>ANS: 200
    Note over ANS,BAO: secret lives only in Ansible memory between bench call and PATCH
    Note over BAO: next deploy renders creds into MCP env via manage-secrets.yml
```

```yaml
---
- name: "Provision ERPNext read-only AI user + API credentials"
  hosts: "{{ erpnext_target | default('erpnext_svc') }}"
  gather_facts: false
  become: false
  vars:
    _bao_url: "{{ openbao_addr | default('') }}"
    _bao_role_id: "{{ bao_role_id | default(lookup('env', 'BAO_ROLE_ID')) }}"
    _bao_secret_id: "{{ bao_secret_id | default(lookup('env', 'BAO_SECRET_ID')) }}"
    _site: "{{ erpnext_site_name }}"
    _ai_user: "ai-readonly@uhstray.io"

  tasks:
    - name: "Ensure AI user exists (API-key auth only; random unstored password)"
      ansible.builtin.shell: |
        {{ container_engine | default('podman') }} exec -i erpnext-backend \
          bench --site {{ _site }} console <<'PY'
        import frappe
        if not frappe.db.exists("User", "{{ _ai_user }}"):
            u = frappe.new_doc("User")
            u.email = "{{ _ai_user }}"
            u.first_name = "AI"
            u.last_name = "ReadOnly"
            u.send_welcome_email = 0
            u.new_password = frappe.generate_hash(length=32)
            u.insert(ignore_permissions=True)
            frappe.db.commit()
        print("ok")
        PY
      register: _user
      changed_when: "'ok' in _user.stdout"

    - name: "Apply read-only role profile (allowlist DocTypes from the exposure profile)"
      ansible.builtin.shell: |
        {{ container_engine | default('podman') }} exec -i erpnext-backend \
          bench --site {{ _site }} console < \
          /home/frappe/frappe-bench/sites/ai-role-profile.py
      changed_when: true
      # Rendered from context/mcp/profiles/claude-readonly.yml — creates role
      # "AI Analyst" with read-only DocPerms for exactly the allowlisted DocTypes.

    - name: "Generate API key + secret for the AI user"
      ansible.builtin.shell: |
        {{ container_engine | default('podman') }} exec erpnext-backend \
          bench --site {{ _site }} execute \
          frappe.core.doctype.user.user.generate_keys --args '["{{ _ai_user }}"]'
      register: _keys
      changed_when: true

    - name: "Read api_key off the user record"
      ansible.builtin.shell: |
        {{ container_engine | default('podman') }} exec erpnext-backend \
          bench --site {{ _site }} execute \
          frappe.client.get_value --kwargs '{"doctype":"User","filters":"{{ _ai_user }}","fieldname":"api_key"}'
      register: _apikey
      changed_when: false

    - name: "Authenticate to OpenBao"
      ansible.builtin.uri:
        url: "{{ _bao_url }}/v1/auth/approle/login"
        method: POST
        body_format: json
        body:
          role_id: "{{ _bao_role_id }}"
          secret_id: "{{ _bao_secret_id }}"
        status_code: [200]
      delegate_to: localhost
      register: _bao_auth

    - name: "Store AI user credentials in OpenBao (merge-patch)"
      ansible.builtin.uri:
        url: "{{ _bao_url }}/v1/secret/data/services/{{ service_name }}"
        method: PATCH
        headers:
          X-Vault-Token: "{{ _bao_auth.json.auth.client_token }}"
          Content-Type: "application/merge-patch+json"
        body_format: json
        body:
          data:
            api_key: "{{ (_apikey.stdout | from_json).message.api_key | default(_apikey.stdout) }}"
            api_secret: "{{ (_keys.stdout | from_json).api_secret | default(_keys.stdout) }}"
        status_code: [200]
      delegate_to: localhost
```

(Exact JSON parsing of `bench execute` output finalized against live output during implementation — the writeback path and storage location are fixed. `service_name` keys the write to the right secret set: dev creds land under `erpnext-dev`.)

### 10.4 Claude-facing exposure profile

`context/mcp/profiles/claude-readonly.yml` — deny-by-default allowlist consumed by the role-profile script and the MCP server config; every change is PR-reviewed against the k-threshold rule (§6). Starter set:

| Allowed (read-only) | Explicitly excluded |
|---|---|
| Company, Fiscal Year, Account (chart structure), Item, Item Group, report **definitions**, DocType metadata, aggregated report execution (P&L, Balance Sheet, AR/AP **summaries**) | Customer/Supplier (PII fields), Payment Entry detail, Bank Account/Transaction, `GL Entry` raw lines, Salary/HR DocTypes, Address/Contact, any custom DocType holding payout or bank detail |

### 10.5 Client wiring

- [ ] Run the full P3 stack against **dev** first: provision AI user on dev, connect Desktop to dev's MCP endpoint, exercise the profile against synthetic books
- [ ] `context/mcp/claude-desktop.example.json` — placeholder config (no secrets): remote connector URL + OAuth, or stdio command with `ERPNEXT_API_KEY`/`ERPNEXT_API_SECRET` env placeholders
- [ ] Only after the gate passes on dev: provision the prod AI user, connect Desktop/Cowork to prod
- [ ] MCP endpoint exposure: authenticated HTTPS through Caddy only (never anonymous); stdio variant stays LAN-only

### Phase 3 gate

| Check | How | Pass condition |
|---|---|---|
| Validated on dev first | Gate run order | All rows below pass on dev (synthetic) before prod connects |
| Tool discovery + read | Desktop lists tools; read chart of accounts | Correct data returned |
| Write denied | Attempt a create/update via MCP | Permission error (role has no write DocPerm) |
| Sensitive blocked | Query `Customer`, `GL Entry`, `Payment Entry` | Denied by allowlist/role; verified in audit log |
| Audit complete | Audit log for the test session | Every call recorded with caller/tool/args/status |
| Not anonymous | `curl` MCP endpoint unauthenticated | 401/403 |
| Creds in OpenBao | `check-secrets.yml` | `api_key`/`api_secret` under the right secret set; not in repo |

## 11. Phase 4 — llm-gate platform service + Cowork reporting (Plane C)

**Deliverable:** `llm-gate` — a new platform service on its own minimal VM, the **only** component on the platform holding an Anthropic API key. Multi-tenant by config file: ERPNext is tenant #1; NetClaw onboards later by adding one file. Includes per-tenant + global budget ledgers, Discord alerting, and Cowork producing a gated report.

Every Plane C request walks the same checkpoints in order — tenant lookup, field allowlist, PII scrub, budget ledger, then (and only then) the Anthropic call — so a failure at any checkpoint means zero egress, not partial egress:

```mermaid
sequenceDiagram
    participant C as Caller - n8n / server script / Cowork flow
    participant G as llm-gate VM :8088
    participant L as Ledger (sqlite)
    participant A as Anthropic /v1/messages
    participant D as Discord webhook

    C->>G: POST /v1/complete {tenant, task, context}
    G->>G: tenant config lookup - unknown tenant means 403
    G->>G: per-tenant field allowlist - unknown field means 422
    G->>G: PII scrub (regex backstop)
    G->>L: within tenant + global budget?
    alt budget exhausted
        G->>D: budget alert
        G-->>C: 429 budget exhausted
    else within budget
        G->>A: messages.create (gate is the only key holder)
        A-->>G: text + usage tokens
        G->>L: record(tenant, model, tokens)
        G-->>C: text + usage
    end
```

### 11.1 Service scaffolding (mini Phase 0 — same onboarding checklist [3])

```text
platform/services/llm-gate/
  deployment/
    compose.yml              gate container + data volume; LAN :8088 only
    deploy.sh                container lifecycle (common.sh pattern, as §7.3)
    Dockerfile               python:3.12-slim, non-root, hadolint-clean
    requirements.txt         fastapi, uvicorn, anthropic, pyyaml (pin at implementation)
    config/tenants/
      erpnext.yml            per-tenant allowlist + budget + model (below)
    app/                     main.py, redaction.py, ledger.py
    tests/                   test_redaction.py, test_budget.py, test_tenants.py
    templates/env.j2         -> gate.env (key, webhook, global budget)
    CLAUDE.md / README.md

platform/playbooks/deploy-llm-gate.yml       composable: secrets -> deploy.sh -> verify /healthz
platform/playbooks/clean-deploy-llm-gate.yml
platform/semaphore/templates.yml             + Deploy / Clean Deploy llm-gate
platform/tests/test_service_llm-gate.bats    same checks as §7.10

site-config: vm-specs llm-gate (vmid 213, 2 cores, 2048 MB, 20G); inventory llm_gate_svc
OpenBao: secret/services/llm-gate (anthropic_api_key, discord_budget_webhook — user type)
         secret/services/ssh/llm-gate
```

- [ ] Scaffold + CI green (same Phase 0 gates as §7); provision VM 213; keys → verify → harden
- [ ] Set `anthropic_api_key` + `discord_budget_webhook` in OpenBao; set the **Console workspace spend limit** as the upstream backstop
- [ ] **Deploy llm-gate** via Semaphore; verify `/healthz` lists the erpnext tenant
- [ ] pfSense egress rule: `api.anthropic.com` allowed **from VM 213 only** (now enforceable because the gate has its own VM)

### 11.2 Gate application

`config/tenants/erpnext.yml` — adding a consumer to the platform is adding one of these (PR-reviewed against the k-threshold rule):

```yaml
# ERPNext tenant — fields per the Aggregated/Metadata classes (§6)
allowed_fields: [period, company, currency, totals, ratios, category_rollup,
                 report_name, account_structure, summary_rows]
monthly_budget_usd: 50
model: claude-opus-4-8        # sonnet-4-6 / haiku-4-5 for high-volume tiers
```

Core of `app/main.py` (official `anthropic` SDK [26] — `claude-opus-4-8` $5/$25 per MTok; `claude-sonnet-4-6` $3/$15, `claude-haiku-4-5` $1/$5):

```python
import glob
import os
from pathlib import Path

import anthropic
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .ledger import Ledger
from .redaction import scrub

# Tenant configs are repo-versioned; adding a consumer = adding one file.
TENANTS = {
    Path(p).stem: yaml.safe_load(Path(p).read_text())
    for p in glob.glob("/config/tenants/*.yml")
}
GLOBAL_BUDGET = float(os.environ.get("GATE_GLOBAL_BUDGET_USD", "100"))
app = FastAPI()
client = anthropic.Anthropic()          # key from ANTHROPIC_API_KEY (gate.env only)
ledger = Ledger(path="/data/ledger.db",
                webhook=os.environ.get("DISCORD_BUDGET_WEBHOOK", ""))


class GateRequest(BaseModel):
    tenant: str                  # must match a config/tenants/<tenant>.yml
    task: str                    # instruction for the model (no data)
    context: dict[str, str]      # aggregated/redacted payload, field-allowlisted


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "tenants": sorted(TENANTS)}


@app.post("/v1/complete")
def complete(req: GateRequest) -> dict:
    cfg = TENANTS.get(req.tenant)
    if cfg is None:
        raise HTTPException(403, f"unknown tenant: {req.tenant}")
    unknown = set(req.context) - set(cfg["allowed_fields"])
    if unknown:
        raise HTTPException(422, f"fields not on egress allowlist: {sorted(unknown)}")
    clean = {k: scrub(v) for k, v in req.context.items()}   # PII regex backstop
    if not ledger.within_budget(req.tenant, cfg["monthly_budget_usd"], GLOBAL_BUDGET):
        raise HTTPException(429, "monthly Claude budget exhausted")

    model = cfg.get("model", "claude-opus-4-8")
    response = client.messages.create(
        model=model,
        max_tokens=int(cfg.get("max_tokens", 16000)),
        thinking={"type": "adaptive"},
        system=cfg.get("system", "You narrate summaries. Figures are provided; never invent numbers."),
        messages=[{"role": "user", "content": f"{req.task}\n\n<data>\n{clean}\n</data>"}],
    )
    ledger.record(req.tenant, model,
                  response.usage.input_tokens, response.usage.output_tokens)
    text = next(b.text for b in response.content if b.type == "text")
    return {"text": text, "usage": response.usage.to_dict()}
```

`redaction.py` (the regex backstop behind the allowlists):

```python
import re

SCRUBBERS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<EMAIL>"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "<PHONE>"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<SSN>"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "<CARDNUM>"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "<IBAN>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
]


def scrub(value: str) -> str:
    for pattern, token in SCRUBBERS:
        value = pattern.sub(token, value)
    return value
```

`tests/` (CI-enforced — the "no-PII-egress" gate tests):

```python
import pytest
from fastapi.testclient import TestClient

from gate.app.redaction import scrub

PLANTED = [
    "jane.doe@example.com", "+1 (555) 867-5309", "123-45-6789",
    "4111 1111 1111 1111", "DE89370400440532013000", "203.0.113.7",
]


@pytest.mark.parametrize("pii", PLANTED)
def test_no_pii_survives_scrub(pii):
    assert pii not in scrub(f"summary for {pii} in march")

# test_tenants.py: unknown tenant -> 403; field off erpnext allowlist -> 422;
#                  allowed aggregates pass (via TestClient against a fixture config)
# test_budget.py:  tenant ceiling -> 429 + webhook fired; global ceiling -> 429
# false-positive safety: legitimate aggregate payloads (periods like "2026-05",
#                  ratios, account-code strings) must pass scrub() unchanged;
#                  extend SCRUBBERS only with matching tests for both directions
```

`ledger.py` behavior: sqlite table `(month, tenant, model, in_tokens, out_tokens, usd)`; cost from a static price map; enforces per-tenant **and** global monthly ceilings; Discord webhook at ≥80% of either (once per month per threshold) and on every 429; every call appended to `/data/egress.jsonl` (tenant, fields, token counts).

### 11.3 Tenant wiring

- [ ] n8n workflows and ERPNext server scripts call `http://<gate-ip>:8088/v1/complete` with `tenant: erpnext` — **no caller ever holds the Anthropic key**
- [ ] Cowork reporting flow: MCP (Plane B) for figures + gate (Plane C) for prose
- [ ] Plane A endpoint (when live) surfaced via gate env for future local-routing decisions

### Phase 4 gate

| Check | How | Pass condition |
|---|---|---|
| Key custody | `grep -r ANTHROPIC` across both ERPNext VMs + repo scan | Key exists only in `gate.env` (0600) on VM 213 |
| Zero PII egress | `pytest` in CI + planted-PII payload through the live gate | All scrubbed; unknown fields 422 |
| Tenant isolation | Request with `tenant: nonexistent` | 403; nothing logged to Anthropic |
| Budget ceilings | Set tenant budget to $0.01, fire a request | 429 + Discord alert |
| Egress logged | `/data/egress.jsonl` | Every call recorded with tenant + token usage |
| Egress scoped | pfSense rule check | `api.anthropic.com` reachable from VM 213 only |
| NetClaw-ready | Add a dummy tenant config in dev, redeploy | New tenant served with zero code changes |
| Cowork report E2E | Cowork produces a monthly summary via profile + gate | Report renders; figures match ERPNext native report |

## 12. Phase 5 — First end-to-end read-only use case

Exactly **one** use case: natural-language querying of the books + a generated monthly summary. Figures come from ERPNext's own reports (via MCP on allowlisted aggregated reports); the LLM orchestrates and narrates only.

- [ ] `context/prompts/monthly-summary.md` — prompt template with the figures-are-canonical rule baked in
- [ ] **Eval runs on dev's synthetic books** (built to clear the k-threshold); 10 NL questions with known-correct answers from native reports
- [ ] Prod usage accepts the k-rule's early limits: low-volume months will legitimately block some rollups — that is the control working, not a bug
- [ ] Run via Claude Desktop (interactive) and Cowork (report) paths

### Phase 5 gate

| Check | Pass condition |
|---|---|
| NL query accuracy | 10/10 answers match native report values exactly (dev eval) |
| Reproducibility | Two runs over the same closed period produce identical figures |
| No fabrication | Every number in the narrative traces to a report cell |
| Perimeter | Audit log + gate egress log show only allowlisted/aggregated data left |
| Budget | Month-to-date ledger within ceiling |

## 13. Phase 6 — Production-grade hardening

- [ ] **Dynamo spike (decision gate):** benchmark the 7–8B slice on Dynamo vs Ollama; if the single small model shows no multi-node benefit, **stay on Ollama** and record the decision here
- [ ] **Dev becomes the standing mirror:** grow dev disk to prod spec; mirror refresh (= restore drill, §8.4) becomes the quarterly cadence; dev retains isolated credentials and stays off the MCP/gate prod configs
- [ ] **Observability:** until the platform o11y stack lands — Semaphore-scheduled **Validate All** + backup jobs with Discord alert on failure; gate ledger doubles as the cost dashboard. When o11y ships: container metrics, latency, backup age, gate spend panels
- [ ] **Offsite backup (planned future work):** evaluate NAS/B2 target; until then cross-mirror is the DR floor [21]
- [ ] **Rotation** (Create → Verify → Retire [6]): `rotate-erpnext-secrets.yml` for admin + MariaDB + MinIO (both VMs, independent secret sets); Anthropic key manual 90-day rotation
- [ ] **Banking API (planned future work):** evaluate aggregator vs bank-direct once CSV/OFX import pain is real; credentials would be `user`-type secrets; bank data stays Sensitive-class regardless
- [ ] **Optional k0s move:** only with the platform-wide Kubernetes phase; a Kyverno/OPA test must block a disallowed action before claiming the guardrail [22]
- [ ] **Load test:** k6/locust read-mix against the frontend + one inference-burst test; record baselines
- [ ] `context/runbooks/erpnext-failure-modes.md`: DB down, redis down, site corrupt, backup failed, gate over budget, inference endpoint down

**Gate:** serving decision recorded with benchmark data; mirror refresh passes within documented RTO; all credentials rotated without downtime (old creds verified dead); a killed validate/backup job produces a Discord alert; all runbook failure modes have tested procedures.

## 14. Phase 7 — Write/draft capability (future; design now, build later)

Design (build only after Phases 1–6 are stable in production):

- Write tools create **drafts only** (`docstatus=0`); posting requires human approval in ERPNext's native workflow.
- **Auto-post thresholds** configurable per DocType (auto-post below $X and above Y confidence; the rest routes to a human queue). Threshold config lives in a versioned fixture, not in prompts.
- A second service user (`ai-draft@uhstray.io`) with draft-only DocPerms — the read-only user never gains write perms.
- Full audit + reversibility: every AI-created draft tagged with origin metadata; cancel/amend path documented.
- Flagship n8n workflow: monthly marketplace reports → Plane A categorization → draft journal entries → human approval → post.
- Prompt-injection posture: untrusted marketplace/customer text never combined with write tools in the same context.

**Gate (design-complete):** drafts cannot post without approval; threshold logic verified at boundary values; blast-radius test proves the AI users cannot execute payments/payouts or exceed their roles.

## 15. Phase 8 — Custom MCP servers (future)

Only for capabilities native servers can't cover (marketplace reconciliation tools, bespoke aggregated views). Same profile + audit + allowlist model; mcp-builder patterns; configs in `context/mcp/`. **Gate:** custom tools pass the identical read-only/least-privilege/audit checks before any write capability is considered.

## 16. Model routing matrix

| Task | Engine | Data class | Notes |
|------|--------|-----------|-------|
| Transaction categorization | Local (A) | Sensitive | High volume, stays on-prem |
| Bank feed processing / reconciliation | Local (A) + deterministic rules | Sensitive | Never via Claude |
| Embeddings / semantic search over financial docs | Local (A) | Sensitive | On-prem vector store |
| NL query over non-sensitive/aggregated data | Claude via MCP (B) | Aggregated/metadata | k-threshold applies |
| NL query touching sensitive data | Local (A) | Sensitive | Never via Claude |
| Report prose / financial narrative | Claude (B/C) | Aggregated/redacted | Figures from ERPNext, prose from Claude |
| Multi-step agentic reasoning | Claude (B) | Aggregated/redacted | Local models too small to be reliable |
| Quick drafts/summaries of non-sensitive text | Local (A) | Non-sensitive | Cheap path |

Rule of thumb: **LLMs orchestrate; ERPNext computes.** Gate default model `claude-opus-4-8`; high-volume tenants/tasks route to `claude-sonnet-4-6`/`claude-haiku-4-5` via tenant config once volume justifies it.

## 17. Use-case backlog (one active increment at a time)

**Sequenced start (read-only):**

1. NL querying of the books + monthly summary (Phase 5).
2. Report/summary generation for Cowork (aggregated → Claude prose).
3. Transaction categorization (local, sensitive — needs Plane A).
4. Bank feed sync + reconciliation assist (local + deterministic — needs Plane A; API ingress per §13).
5. RAG over ERPNext docs/records (local embeddings — needs Plane A).

**Brainstormed additions (prioritize later):** marketplace payout reconciliation assistant; AR/AP aging in natural language; anomaly detection (local + deterministic rules); month-end close checklist + narrative; cashflow forecasting narrative; Zord license-sales insights; vendor/contract Q&A via RAG; tax-prep aggregated summaries.

Each becomes its own mini-increment with its own gate; none ship bundled.

## 18. Security considerations

- All secrets in OpenBao, deploy-time only via Semaphore's AppRole; scripts never touch OpenBao; no `secrets/` dirs on VMs; `.env`/`gate.env` 0600, gitignored, overwritten each deploy; no `no_log: true` anywhere (callback redaction [10]). (P0–P4)
- **Credential isolation per host:** dev (`erpnext-dev`) has its own secret set + SSH keypair — dev compromise touches nothing in prod; cross-mirror uses restricted write-only MinIO users, never root creds. (P0–P1)
- Dedicated least-privilege **read-only** AI user per environment; API-key auth only; deny-by-default exposure profile; k-threshold rule on every aggregate (§6). (P3)
- llm-gate: per-tenant allowlists + PII scrubbers + CI zero-egress test; only VM 213 holds the Anthropic key; per-tenant + global ledgers + Console cap; pfSense scopes Anthropic egress to that VM. (P4)
- Network exposure: frontends :8080 and MinIO :9000 LAN-only behind Caddy/pfSense; dev resolves via local DNS only; Redis/DB never publish host ports; inference endpoint never public. (P1–P4)
- Prompt-injection: untrusted text never co-resident with write capability (read-only by construction; explicit gate at P7).
- Backups cross-mirrored + quarterly proven restores; Create→Verify→Retire rotation; never disable an auth path before its replacement is verified. (P1, P6)
- Blast radius: AI user = read access to allowlisted aggregates; gate VM = the Anthropic key (rotate; Console cap bounds spend) but no ERPNext write path; one VM's SSH key = that VM only.

## 19. Validation criteria (master)

| Phase | Critical check | Command/template | Pass |
|---|---|---|---|
| 0 | CI + templates-as-code | PR checks; `setup-templates.yml` | Green; 7 templates registered |
| 1A | Dev live + DR proven on dev | LAN curl ping; restore drill | pong over TLS; drill passes; synthetic books queryable |
| 1B | Prod live + books verified + cross-mirror | Public curl; trial balance; peer MinIO listing | All pass; nightly schedule green |
| 1 | Idempotent | Re-run deploy (either target) | No duplicate site/app; no secret churn |
| 2 | Plane A live, internal, in budget | `nvidia-smi`; LAN curl; WAN curl | Within VRAM budget; completion OK; no public route |
| 3 | Read-only enforced + audited, dev-first | MCP write attempt; sensitive query; audit log | Both denied; all calls logged; validated on dev before prod |
| 4 | Zero PII egress + budgets + tenancy | `pytest`; overage + unknown-tenant probes | Green; 429/403 + Discord alert; NetClaw-ready check passes |
| 5 | No fabrication | 10-question eval vs native reports (dev) | Exact figure match |
| 6 | DR + rotation + alerting | Mirror refresh, rotate playbook, kill a job | All pass without downtime |
| 7 | Drafts-only writes | Approval-bypass attempt | Impossible by permission model |

## 20. Testing strategy

- **CI (every PR):** shellcheck (scripts), yamllint, ansible-lint (6 playbooks), hadolint (gate + custom-image Dockerfiles), ruff + pytest (gate), BATS (`test_service_erpnext.bats`, `test_service_llm-gate.bats`), trufflehog + IP/credential grep, fixtures JSON validation.
- **Branch testing:** feature branches deploy to **dev** via `service_branch`; gates run there; rollback = redeploy `main` [11].
- **Per-plane smoke tests** runnable from Semaphore: ERPNext ping (both hosts), inference completion, MCP read + denied write, gate redaction assertion, budget + tenant probes.
- **Idempotency:** every gate includes a re-run check; post-deploy.sh and the accounting bootstrap re-run must no-op.
- **Restore drill** as a first-class scheduled test (quarterly mirror refresh), not an incident-day improvisation.

## 21. Open decisions & risks

| Item | Status | Resolution path |
|---|---|---|
| MCP server pick (repo names unverified) | Open — blocks P3 | Decision task §10.1; verify repos at kickoff [27] |
| WisAI not yet deployed | Blocks P2 only (P2 floats) | Execute `WISAI-DEPLOYMENT-PLAN.md`; P0–P5 proceed regardless |
| ERPNext v16 tag stability | Open — P1A start | Pin exact tag; v15 LTS fallback documented |
| podman-compose 1.0.6 gaps | Mitigated | Staged startup; explicit container names; upgrade tracked in `PODMAN-UPGRADE-PLAN.md` |
| GPU topology / VRAM basis | Open (external) | `UHHCRAFT-GPU-PASSTHROUGH.md` §1 decision; P2 sizes against it |
| Dynamo fit for one small model | Deferred | P6 benchmark spike with go/no-go |
| Banking API (aggregator vs bank-direct) | Planned future | CSV/OFX until import pain justifies a vendor (§13); credentials `user`-type when it lands |
| Offsite/NAS backup | Planned future | Cross-mirror is the floor; evaluate at P6 |
| k-threshold code enforcement | Upgrade path | Documented rule + PR review now; structured record-counts end-to-end if policy proves insufficient |
| ERPNext setup calls in accounting bootstrap | Minor — P1A | Finalize fixture-apply calls against live bench during implementation |
| `bench execute` output parsing | Minor — P3 | Finalize JSON parsing against live output |

## 22. Convention compliance map

| Platform rule | Where this plan satisfies it |
|---|---|
| All deployments through Semaphore (rule #1) | Every operation is a registered template (§7.9, §11.1); gates name templates, not SSH |
| deploy.sh containers-only (rule #2) | §7.3/§7.4/§8.3 — scripts read `.env` only; grep-enforced in the P0 gate |
| Independent workflows (rule #3) | Deploy / Clean / Backup / Bootstrap-accounting / Provision-AI-user / gate playbooks all separate |
| No intermediary secret files (rule #4) | Only templated env files; runtime creds via Ansible PATCH writeback (§10.3); cross-mirror via restricted users |
| Verify before hardening (rule #5) | SSH verify-then-harden (§8.1/§8.2); Create→Verify→Retire rotation (§13) |
| Policy/config as code | Semaphore templates, gate tenant configs, MCP profile, fixtures — all repo-versioned, PR-gated |
| OpenBao layout + blast radius | §7.12 — per-host secret sets and SSH keys; no cross-VM credential sharing |
| `_secret_definitions`/`_env_templates` schema | §7.7 matches `manage-secrets.yml` interface exactly [14] |
| Podman default; engine never hardcoded | `container_engine: podman` in inventory; `detect_runtime`/`compose` wrapper everywhere |
| Caddy per-site fragments | §7.6 — one template serves prod + dev domains via `distribute-caddy-site.yml` [5] |
| New-service onboarding reused | llm-gate follows the full checklist as its own service (§11.1) [3] |
| Build once | One playbook codebase (`erpnext_target`); one gate for all tenants (principle 7) |
| CI testing spec | §7.10 + gate pytest + linters (§20) [8][9] |
| Branch workflow + docs updates | §7.13–§7.14; PR steps include `/simplify`, `/security-review`, full checks |
| Plan doc standards | Status header, mermaid-only diagrams, phase gates, validation table, security section, cited references, revision history [12] |

## 23. Source context & references

This plan instantiates, rather than invents, the platform's deployment doctrine. The five Critical Deployment Rules and the OpenBao secrets flow come from the root conventions [1]; the composable playbook shape, the `_secret_definitions`/`_env_templates` schema, and the task library from the composability architecture [2][14]; and the Phase 0–1 sequence follows the service-integration onboarding checklist step for step [3]. Operational boundaries (Semaphore-only operations, read-only emergency SSH) [7], CI obligations [8][9], the no-`no_log` redaction standard [10], the branch-test-then-merge flow [11], and this document's own structure [12] are all inherited, not local choices.

The concrete artifacts copy proven code rather than novel designs: the deploy playbook mirrors UhhCraft's [13], all scripts source the shared bash library [18], the compose file applies the podman-compose compatibility rules [4] to the established per-service scaffolding pattern — including the per-service MinIO precedent [16], public exposure uses the live Caddy fragment system [5], the AI-user credential writeback mirrors the Diode pattern [15], Semaphore templates follow templates-as-code [17], and the rotation/backup discipline comes from the credential-lifecycle and disaster-recovery plans [6][21]. Host variables, VM sizing, the VMID range, and the public/private boundary come from the private site-config schema [24].

Adjacent plans supply the rest: Plane A consumes the WisAI design, including its explicit rejection of a LiteLLM-style proxy [19], and is sized against the pending GPU-topology decision [20]; the legacy-path hazard that shaped the secret-management decision is documented in the held NocoDB/n8n migration plan [23]; optional policy-as-code arrives via the OPA plan [22]. Outside the repos, the container topology and custom-image build path derive from upstream frappe_docker [25], and the gate's model IDs, pricing, and usage-accounting fields come from the Anthropic API reference [26]; the MCP server candidates are named but unverified [27].

**Explicit assumptions:** [25] and [27] were not fetched during planning and are tracked as open items in §21 — verify at P1/P3 kickoff respectively. Repo citations reflect the 2026-06-09 convention review; line ranges are anchors from that review, not exact pins.

### References

Tags: *(repo)* agent-cloud file · *(private)* site-config · *(web)* external · *(skill)* bundled reference material.

1. *(repo)* `CLAUDE.md` — Critical Deployment Rules #1–#5; credential flow; OpenBao layout table; branch workflow.
2. *(repo)* `plan/architecture/AUTOMATION-COMPOSABILITY.md:229-344` — composable task library; `_secret_definitions`/`_env_templates` schema (§7.7).
3. *(repo)* `plan/architecture/SERVICE-INTEGRATION-PLAN.md:55-173` — new-service onboarding checklist (Phases 0–1, §11.1).
4. *(repo)* `plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md` — engine selection; podman-compose 1.0.6 gaps driving §7.2–§7.3; cross-runtime best practices.
5. *(repo)* `plan/architecture/CADDY-REVERSE-PROXY.md` — per-site fragment pattern + Cloudflare DNS-01 (§7.6).
6. *(repo)* `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` — storage paths; Create→Verify→Retire rotation (§13).
7. *(repo)* `plan/architecture/ACCESS-BOUNDARIES.md:15-76` — Semaphore vs SSH matrix; AppRole scopes; blast-radius table (§18).
8. *(repo)* `plan/architecture/CI-TESTING-SPECIFICATION.md:228-270` — per-service test requirements (§7.10).
9. *(repo)* `plan/architecture/SECURITY-TESTING-STANDARDS.md` — secret/IP scan gates; no-`no_log` rule (§20).
10. *(repo)* `plan/development/ANSIBLE-CREDENTIAL-REDACTION-PLAN.md` — callback-plugin redaction replacing `no_log`.
11. *(repo)* `plan/architecture/BRANCH-TESTING-WORKFLOW.md` — `service_branch` deploy/validate/rollback used by every phase.
12. *(repo)* `plan/architecture/architecture-reference.md:15-43` — plan-document standards (status lifecycle, mermaid-only diagrams).
13. *(repo)* `platform/playbooks/deploy-uhhcraft.yml` — exemplar composable deploy this plan's playbook mirrors, including the Caddy fragment phase.
14. *(repo)* `platform/playbooks/tasks/manage-secrets.yml` — fetch/generate semantics; `secrets.*` template namespace; `service_name`-keyed secret paths.
15. *(repo)* `platform/playbooks/tasks/manage-diode-credentials.yml` — runtime-credential writeback pattern mirrored in §10.3.
16. *(repo)* `platform/services/uhhcraft/deployment/` — compose.yml / deploy.sh / env.j2 / caddy-site.j2 exemplars; per-service MinIO precedent (also `inference-comfyui`, `inference-hunyuan3d`).
17. *(repo)* `platform/semaphore/templates.yml` + `setup-templates.yml:48-77` — templates-as-code schema; idempotent create-or-update apply.
18. *(repo)* `platform/lib/common.sh` — `detect_runtime`, `compose`, `wait_for_healthy`, `wait_for_http` used by all scripts.
19. *(repo)* `plan/development/WISAI-DEPLOYMENT-PLAN.md` — Plane A architecture; LiteLLM rejection; `secret/services/inference/*` paths (Phase 2 dependency).
20. *(repo)* `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md` — GPU hardware decision gate feeding §2/§9; VRAM constraints.
21. *(repo)* `plan/development/DISASTER-RECOVERY-PLAN.md` — backup/restore strategy and drill cadence (§8.3–§8.4, §13).
22. *(repo)* `plan/development/OPA-INTEGRATION-PLAN.md` — policy-as-code option (§13).
23. *(repo)* `plan/development/nocodb-n8n-composable-migration.md` — held legacy-path migration; stateful-secret hazard informing the secrets decision (§3).
24. *(private)* site-config: `inventory/production.yml`, `proxmox/vm-specs.yml`, `plan/ARCHITECTURE-REFERENCE.md` — host-variable schema; VMID 200–299 range; template VM 9000; public/private repo boundary; `secrets/` DR layout. Reviewed 2026-06-09.
25. *(web)* frappe/frappe_docker — `https://github.com/frappe/frappe_docker` — compose topology (configurator one-shot, backend/frontend/websocket/workers/scheduler) and the layered custom-image build (§10.2). **Assumption: from prior knowledge, not fetched; verify compose details and image tags at P1A.**
26. *(skill)* Anthropic Claude API reference (claude-api skill bundle; model/pricing tables cached 2026-05-26) — model IDs and $/MTok pricing; `/v1/messages` request shape; `usage` token fields backing the budget ledger (§11.2).
27. *(web)* Frappe Assistant Core / `mascor` / `appliedrelevance` frappe-mcp-server — candidate repo names from requirements intake. **Assumption: unverified; the P3 decision task (§10.1) confirms before any build.**

## 24. Revision history

| Date | Change |
|---|---|
| 2026-06-09 | Initial draft (six-lens cross-review; strategy-level) |
| 2026-06-09 | Full refinement: validated against agent-cloud/site-config conventions; corrected post-deploy OpenBao writeback to the Ansible pattern; per-service MinIO; WisAI/o11y dependencies made explicit; complete Phase 0–1 implementation detail; gate sidecar design with budget ledger; per-phase validation tables; convention compliance map |
| 2026-06-11 | Self-explaining pass: added Target outcome, Decision criteria, credential-writeback and gate-request sequence diagrams, and cited Source context & references |
| 2026-06-11 | Interactive review: dev VM with full credential isolation (`erpnext-dev`, `<erpnext-dev-domain>`); Phase 1 split into 1A (dev, synthetic books) / 1B (prod, cutover + opening balances via fixtures playbook, CSV/OFX bank import); cross-mirror backups with restricted writers + retention; **llm-gate promoted to a multi-tenant platform service on its own VM**; k-threshold re-identification rule; fail-closed classification; P2 floats on WisAI; banking API + offsite backup recorded as planned future work; simplification pass (boilerplate playbooks/templates compressed to contracts + exemplar pointers) |
| 2026-06-12 | Security-review fixes: owner email removed; service-domain literals replaced with `<erpnext-prod-domain>`/`<erpnext-dev-domain>` placeholders — real values moved to the private site-config repo (`site-config/erpnext/README.md`) |

---

*Cross-review note: this plan reconciles six review lenses (architecture, agent-cloud platform, AI engineering, ERPNext, automation, security). The sharpest resolved tension was the privacy boundary vs. Claude's reasoning value — resolved via the two-profile MCP + llm-gate + local-first routing. The main unresolved tensions are tracked in §21 with explicit resolution paths.*
