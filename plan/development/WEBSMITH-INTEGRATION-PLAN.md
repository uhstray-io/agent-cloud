# WebSmith + UhhCraft Integration Plan

> **Status:** Draft, awaiting user signoff
> **Author:** Claude (Cowork session, 2026-05-25)
> **Scope:** Integrate the external `website_framework` repo into agent-cloud as the **WebSmith** agent, and integrate its concrete output (UhhCraft) as a first-class platform service with GPU-backed inference sidecars.

This plan integrates two distinct things from `/Users/jacobhaig/Documents/GitHub/website_framework/`:

1. **The meta-framework** (markdown phase docs, catalogs, schemas, examples) → becomes the **WebSmith** agent at `agents/websmith/`. WebSmith is the agent that any future site builds are run through.
2. **The example output (UhhCraft)** → a Go + templ + HTMX storefront with Python AI sidecars. Becomes `platform/services/uhhcraft/` plus two new inference services. UhhCraft is the **first concrete site** built with WebSmith; future sites will follow the same pattern.

Both pieces have to be lifted *and rewired* to obey agent-cloud's conventions: OpenBao secrets, composable Ansible deploys, Semaphore orchestration, central Caddy, Podman containers, unified CI, and the four-layer guardrail model.

---

## 0. Locked decisions (from kickoff Q&A)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Framework location | New agent: `agents/websmith/` |
| 2 | AI sidecar packaging | Separate `inference-comfyui/` + `inference-hunyuan3d/` services |
| 3 | UhhCraft reverse proxy | Central Caddy route fragment (no per-service Caddy) |
| 4 | UhhCraft container runtime | Podman (matches repo convention; CLAUDE.md exception for NetBox stays the only Docker carve-out) |
| 5 | MinIO scope | Bundled under UhhCraft (not promoted to a shared service yet) |
| 6 | Hosting | New dedicated Proxmox VMs for `uhhcraft_svc`, `inference_comfyui_svc`, `inference_hunyuan3d_svc` |
| 7 | CI integration | Add Go / templ / sqlc / Python jobs to the unified `lint-and-test.yml` |

These constraints propagate into every phase below.

---

## 1. Mental model

```
                ┌──────────────────────────────────────────────────────┐
                │                    Cowork / NemoClaw                  │
                │             (delegates a site-build session)          │
                └────────────────────────────┬──────────────────────────┘
                                             │ "build me a website"
                                             v
                                ┌────────────────────────────┐
                                │     WebSmith (agent)       │
                                │  phases / catalogs / schemas
                                │  → produces SPEC.md         │
                                └────────────┬───────────────┘
                                             │  signed SPEC
                                             v
                          ┌──────────────────────────────────────┐
                          │   platform/services/<sitename>/       │
                          │   deployment/ + context/              │
                          │   composable Ansible deploy           │
                          └──────────────────────────────────────┘

UhhCraft is the first <sitename>. The framework is reusable; each output is its own service.
```

WebSmith is **decision-only** during phases 1-5 (per its own rules). It hands a `SPEC.md` to whoever (or whatever) implements the site. The implementation lands in `platform/services/<sitename>/` and follows agent-cloud's composable deploy pattern from there.

---

## 2. Phase-by-phase plan

Each phase has: **goal**, **deliverables (files in / files out)**, **work items**, **acceptance criteria**. Phases 1, 2, 3 are mostly mechanical moves. Phases 4-10 are integration work. Phase 11 is the "second site" recipe.

Phases that touch shared infra (CI, Caddy, Semaphore, OpenBao) ship as their own PR so blast radius is contained.

---

### Phase 1 — Stand up the WebSmith agent

**Goal:** Lift the framework markdown into `agents/websmith/` with no integration changes yet. The framework is fully usable in its new home before we wire UhhCraft up.

**Deliverables:**

```
agents/websmith/
├── README.md                       New — short index, links to context/
├── CLAUDE.md                       New — how this agent fits agent-cloud
├── deployment/                     Empty/stub — websmith has no runtime service today
│   └── README.md                   "WebSmith is a prompt agent, not a runtime service"
└── context/
    ├── AGENTS.md                   Moved from website_framework/AGENTS.md
    ├── KICKSTART.md                Moved from website_framework/KICKSTART.md
    ├── README.md                   Moved from website_framework/README.md (framework's)
    ├── og_prompt.md                Moved
    ├── questionnaire.md            Moved
    ├── verification.md             Moved
    ├── phases/                     Moved verbatim
    │   ├── 0-intake.md
    │   ├── 1-purpose.md
    │   ├── 2-template.md
    │   ├── 3-tooling.md
    │   ├── 4-style.md
    │   └── 5-considerations.md
    ├── catalogs/                   Moved verbatim
    ├── schemas/                    Moved verbatim
    ├── examples/                   Moved verbatim
    ├── architecture/               New
    │   └── integration-with-agent-cloud.md   How a signed SPEC.md becomes a platform service
    ├── prompts/                    New
    │   ├── kickoff.md              "Read AGENTS.md and walk me through..."
    │   └── handoff-to-implementer.md
    ├── skills/                     New
    │   ├── run-phase.md
    │   └── assemble-spec.md
    └── use-cases/                  New
        └── uhhcraft-walkthrough.md  How the example output was produced
```

**Work items:**

1. `git mv` framework markdown into `agents/websmith/context/`. No content changes yet.
2. Patch relative paths inside the moved files: every `./phases/...`, `./catalogs/...`, etc., resolves under `agents/websmith/context/`.
3. Write `agents/websmith/CLAUDE.md` describing:
   - This is a **prompt-only agent** — no deploy target.
   - WebSmith produces a signed `SPEC.md` that lives **with the site's service**, not in this directory.
   - Cross-link to `plan/architecture/WEBSITE-BUILDING-AGENT.md` (added in Phase 9).
4. Write `agents/websmith/README.md` — short, points at `context/KICKSTART.md` for humans and `context/AGENTS.md` for agents.
5. **Resolve naming collisions:**
   - Root `kickstart.md` (lowercase) vs framework `KICKSTART.md` (uppercase): keep both, they're in different directories. Note the distinction in root `kickstart.md`.
   - `AGENTS.md` only exists at `agents/websmith/context/AGENTS.md` — no root collision today.
6. **Do not move `output/`** in this phase — that becomes Phase 2.
7. Update root `README.md`, root `CLAUDE.md`, and root `kickstart.md`:
   - README: add WebSmith to the "AI Agents" table.
   - CLAUDE.md: add `agents/websmith/CLAUDE.md` to the sub-directory documentation list.
   - kickstart.md: add a WebSmith row to the "Where to look next" table.

**Acceptance:**

- An agent given the prompt *"read `agents/websmith/context/AGENTS.md` and run through Phase 1 with me"* completes the phase end-to-end without dangling links.
- `find agents/websmith -name '*.md' -exec grep -l 'phases/\|catalogs/\|schemas/' {} +` returns no broken relative paths.
- `yamllint` and `ansible-lint` are unaffected (no new YAML in this phase).

**Risks:**

- WebSmith's framework writes site output to a *separate* working directory (per its own rules). Adapting it to write into `platform/services/<sitename>/` is a Phase 11 concern. For now WebSmith stays unchanged in its behavior.

---

### Phase 2 — Carve UhhCraft into agent-cloud services

**Goal:** Move the concrete UhhCraft codebase into the platform tree, **split** along agent-cloud's deployment/context boundary, with the AI sidecars hoisted into their own services.

**Deliverables (new tree):**

```
platform/services/uhhcraft/
├── deployment/
│   ├── README.md                   Adapted from output/README.md (paths fixed, secrets ref OpenBao)
│   ├── deploy.sh                   New — lifecycle only (podman compose up -d + wait)
│   ├── post-deploy.sh              New — DB migrations, sqlc verify, templ generate, healthcheck
│   ├── compose.yml                 Adapted from output/docker-compose.yml (Podman-compatible)
│   ├── Dockerfile                  New — multi-stage Go build → distroless
│   ├── Makefile                    Moved from output/Makefile (paths fixed)
│   ├── go.mod                      Moved
│   ├── go.sum                      Moved
│   ├── sqlc.yaml                   Moved
│   ├── cmd/                        Moved from output/cmd/
│   ├── internal/                   Moved from output/internal/   (minus ai/ if any)
│   ├── web/                        Moved from output/web/
│   ├── db/                         Moved from output/db/ (migrations + queries)
│   ├── config/                     Moved from output/config/ (non-secret TOML)
│   └── templates/                  New — Jinja2 .env / config templates
│       ├── env.j2                  All compose env (DB URL, Redis URL, MinIO creds, Stripe, …)
│       ├── uhhcraft-app.env.j2     Subset injected into the Go app
│       └── caddy-site.j2           Caddy route fragment (rendered into central Caddy)
└── context/
    ├── README.md
    ├── CLAUDE.md                   Service-specific Claude guidance
    ├── spec/                       Moved from output/spec/
    │   ├── SPEC.md
    │   ├── intake.md
    │   ├── purpose.md
    │   ├── template.md
    │   ├── tooling.md
    │   ├── style.md
    │   └── considerations.md
    ├── architecture/
    │   ├── overview.md             How UhhCraft components fit together
    │   └── ai-sidecar-contract.md  HTTP contract with inference-comfyui + inference-hunyuan3d
    ├── prompts/                    Reusable prompts for ops + content updates
    ├── skills/                     "Add a material", "Pause sales for hiatus", etc.
    └── use-cases/                  Worked examples
```

```
platform/services/inference-comfyui/
├── deployment/
│   ├── README.md
│   ├── deploy.sh                   Podman lifecycle for ComfyUI + Flux.1 wrapper
│   ├── post-deploy.sh              Model weight checks, smoke generation
│   ├── compose.yml                 NVIDIA-enabled Podman compose
│   ├── Dockerfile                  Python FastAPI wrapper around ComfyUI
│   ├── main.py                     Moved from output/ai/image/main.py
│   ├── requirements.txt            Moved
│   └── templates/
│       └── env.j2                  COMFY_HOST, COMFY_PORT, model paths
└── context/
    ├── README.md
    ├── architecture/
    │   └── contract.md             POST /generate request/response schema (mirrors uhhcraft side)
    └── skills/
        └── add-flux-lora.md
```

```
platform/services/inference-hunyuan3d/
├── deployment/
│   ├── README.md
│   ├── deploy.sh
│   ├── post-deploy.sh              Verify model weights present
│   ├── compose.yml                 NVIDIA-enabled
│   ├── Dockerfile
│   ├── main.py                     Moved from output/ai/model3d/main.py
│   ├── requirements.txt            Moved
│   └── templates/env.j2
└── context/
    └── (same shape)
```

**Work items:**

1. Move `output/cmd`, `output/internal`, `output/web`, `output/db`, `output/config`, `output/go.{mod,sum}`, `output/sqlc.yaml`, `output/Makefile` into `platform/services/uhhcraft/deployment/`.
2. Move `output/spec/` into `platform/services/uhhcraft/context/spec/` — keep the signed SPEC.md as the contract for this service.
3. Move `output/ai/image/` → `platform/services/inference-comfyui/deployment/` (rename `main.py` and `requirements.txt` to live at deployment root or under an `app/` subfolder).
4. Move `output/ai/model3d/` → `platform/services/inference-hunyuan3d/deployment/`.
5. **Rewrite `output/docker-compose.yml` → `platform/services/uhhcraft/deployment/compose.yml`:**
   - Same Postgres + Redis + MinIO containers, but pinned image tags.
   - Replace the dev-only literal placeholder values (e.g., the dev Postgres credential, which upstream simply set to the literal string "password") with `${VAR}` references read from a templated `.env`.
   - Remove the dev-only port exposes that aren't safe in prod (Postgres `5432`, Redis `6379`); keep MinIO `9000` internal only.
   - Add the Go app as a service entry built from `Dockerfile` (currently the README says "production runs natively" — see deviation note below).
   - Healthchecks on every service. Compose `depends_on` with `condition: service_healthy`.
6. **Write the Go app `Dockerfile`** — multi-stage: `golang:1.23-alpine` builder → distroless runtime. Inject build args for templ/sqlc generation. (This is the deviation from UhhCraft's "native binary" assumption — see Phase 2.5 note.)
7. **Rewrite `output/Caddyfile` → `platform/services/uhhcraft/deployment/templates/caddy-site.j2`** — Jinja2 templated for domain + upstream host:port. Rendered into the central Caddy in Phase 5.
8. **Trim `deploy.sh` to lifecycle only:**
   - Source `platform/lib/common.sh`.
   - `podman compose pull`, `podman compose up -d`, wait for healthy.
   - **No** secret generation, **no** `goose migrate`, **no** templ generate at runtime. Those go in `post-deploy.sh` (called by Ansible).
9. **Write `post-deploy.sh`:** run `goose -dir db/migrations postgres "$DATABASE_URL" up`, `river migrate-up`, application bootstrap (idempotent seed inserts for materials if needed), final HTTP healthcheck.
10. **Update `platform/services/uhhcraft/deployment/README.md`** so all paths reference the new monorepo locations, the spec link points at `../context/spec/SPEC.md`, and the "production runs natively" line is replaced with the actual Podman-managed reality.
11. **Delete** `website_framework/output/` from the WebSmith agent copy (it's now redundant; the canonical UhhCraft lives in platform/services).

**Phase 2.5 — Deviation notice for UhhCraft's SPEC:**

UhhCraft's SPEC.md currently says "production runs these natively on the server — Docker is dev-only." Our decision is **Podman-managed containers in prod** for consistency with the rest of agent-cloud. This is a real deviation from the signed spec. We must:

- Add a `## Deviations from Spec` section to `platform/services/uhhcraft/context/spec/SPEC.md` documenting the change.
- Get explicit user signoff on the deviation (dated entry, same format the SPEC uses).
- Cross-reference from `plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md`.

**Acceptance:**

- `find website_framework/output -type f | wc -l` returns 0 (or the directory is gone).
- `tree platform/services/uhhcraft` matches the layout above.
- `tree platform/services/inference-{comfyui,hunyuan3d}` likewise.
- No real credentials in any committed file; all secrets are `{{ }}` references.
- `cd platform/services/uhhcraft/deployment && go build ./...` succeeds (compiles, even before secrets are wired).

**Risks:**

- The Go app's session store (`alexedwards/scs/postgresstore`) needs a Postgres connection at startup. Cold-start ordering matters; Podman `depends_on: service_healthy` handles it.
- `templ` and `sqlc` generated files — decide: **commit generated `_templ.go` and `db/sqlc/` artefacts** to simplify CI, OR generate in CI and `.gitignore` them. Recommended: **generate in CI**, commit nothing generated. CI gates on `make generate` being clean.

---

### Phase 3 — OpenBao secrets layout

**Goal:** Move every UhhCraft secret out of compose env literals into OpenBao, with policies and AppRoles in place.

**Deliverables:**

```
secret/services/uhhcraft                Master KV: db_password, redis_password,
                                        minio_root_user, minio_root_password,
                                        session_secret, stripe_secret_key,
                                        stripe_webhook_secret, resend_api_key,
                                        discord_webhook_url
secret/services/uhhcraft/database       Connection strings (built from above)
secret/services/inference-comfyui       comfy_internal_url, model_paths
secret/services/inference-hunyuan3d     Same shape
secret/services/ssh/uhhcraft            Per-service SSH keypair (private + public)
secret/services/ssh/inference-comfyui   Same
secret/services/ssh/inference-hunyuan3d Same
secret/services/approles/uhhcraft       role_id + secret_id (for the Go app to read its own secrets if needed at runtime — TBD)
```

**Work items:**

1. Create `platform/services/openbao/deployment/config/policies/uhhcraft-policy.hcl` — read access to its own secret paths only.
2. Create `inference-comfyui-policy.hcl` and `inference-hunyuan3d-policy.hcl`.
3. Create `platform/playbooks/apply-policy-uhhcraft.yml` (and the two inference equivalents) following the `apply-policy-orb-agent.yml` shape.
4. Decide AppRole vs. one-shot Ansible-only access:
   - **Recommended:** Ansible-only for now. The Go app reads its config from the templated `.env`; it does not call OpenBao at runtime. Future need (token rotation) can add an AppRole later via `tasks/manage-approle.yml`.
5. List `_secret_definitions` per service in the deploy playbooks (Phase 4) so `tasks/manage-secrets.yml` can generate any missing secrets on first deploy.

**Acceptance:**

- `vault policy list` shows the three new policies.
- A dry-run of `deploy-uhhcraft.yml` against a fresh OpenBao writes all secrets at the right paths with the right shapes.
- No secret value appears in any committed file.

---

### Phase 4 — Composable Ansible playbooks

**Goal:** Three new top-level playbooks plus update variants, all built from the composable task library.

**Deliverables:**

```
platform/playbooks/
├── deploy-uhhcraft.yml              5-phase composable (mirror deploy-netbox.yml)
├── update-uhhcraft.yml              Pull + restart + verify
├── clean-deploy-uhhcraft.yml        Destructive (uses tasks/clean-service.yml)
├── deploy-inference-comfyui.yml     5-phase
├── update-inference-comfyui.yml
├── deploy-inference-hunyuan3d.yml
├── update-inference-hunyuan3d.yml
├── apply-policy-uhhcraft.yml        (from Phase 3)
├── apply-policy-inference-comfyui.yml
└── apply-policy-inference-hunyuan3d.yml
```

**Tasks (new, in `platform/playbooks/tasks/`):**

- `tasks/run-migrations.yml` — generic `goose` runner that takes `migrations_dir`, `dsn_secret_path`, idempotent. Reusable by other Go services.
- `tasks/install-nvidia-toolkit.yml` — installs NVIDIA Container Toolkit for Podman on GPU hosts.
- `tasks/install-go-toolchain.yml` — installs `templ`, `sqlc`, `air`, `goose` binaries on a host. (Used only if we choose to build on the target VM; if we build in CI and ship images, this isn't needed.)
- `tasks/install-podman-compose.yml` — confirms `podman-compose` (or `podman compose` plugin) is present.

**Recommended structure of `deploy-uhhcraft.yml`:**

```yaml
# Phase 1: Manage secrets — fetch existing or generate new in OpenBao,
#          template env.j2 → /var/lib/uhhcraft/.env, caddy-site.j2 → /tmp/.
# Phase 2: Pull the Go app image from registry (or build on target; see CI section).
# Phase 3: Run deploy.sh — podman compose up -d, wait healthy.
# Phase 4: Run post-deploy.sh — goose migrate, river migrate, healthcheck.
# Phase 5: Distribute rendered caddy-site.j2 to the Caddy host
#          and reload Caddy (delegated task — see Phase 5).
```

**Work items:**

1. Write each playbook by adapting `deploy-netbox.yml` (the composable reference).
2. Add `_secret_definitions` and `_env_templates` blocks per service.
3. Verify the inference playbooks correctly install the NVIDIA toolkit before starting containers (`tasks/install-nvidia-toolkit.yml` as a prereq).
4. Health checks:
   - UhhCraft: `GET /healthz` → 200.
   - ComfyUI sidecar: `GET /health` → 200, model loaded.
   - Hunyuan3D sidecar: `GET /health` → 200, weights present.

**Acceptance:**

- `ansible-lint platform/playbooks/` clean.
- Dry-run (`--check`) succeeds against a staging inventory.
- All three deploy playbooks idempotent (re-run produces no changes).

---

### Phase 5 — Caddy integration

**Goal:** Add UhhCraft to the central Caddy reverse proxy as a routed site; no per-service Caddy.

**Deliverables:**

```
platform/services/caddy/deployment/
├── sites/                              (new directory if not present)
│   └── uhhcraft.caddy                  Rendered from caddy-site.j2 by deploy-uhhcraft.yml
└── deploy.sh                           Picks up sites/*.caddy via Caddyfile `import sites/*.caddy`
```

**Work items:**

1. Update `platform/services/caddy/deployment/Caddyfile` (or equivalent) to `import sites/*.caddy`.
2. Confirm Caddy's existing CloudFlare DNS-01 plugin handles `uhhcraft.uhstray.io` (per `plan/architecture/CADDY-REVERSE-PROXY.md`).
3. Add a delegated task to `deploy-uhhcraft.yml` that:
   - Renders `caddy-site.j2` locally.
   - Copies it to the Caddy host's `sites/` directory.
   - Reloads Caddy via `caddy reload --config /etc/caddy/Caddyfile`.
4. Update `plan/architecture/CADDY-REVERSE-PROXY.md` to document the per-site fragment pattern (this becomes the convention for all future sites).

**Acceptance:**

- `curl -I https://uhhcraft.uhstray.io` returns 200 and a valid Let's Encrypt cert.
- Security headers from the original Caddyfile are preserved (CSP, HSTS, frame options).
- Caddy reload is zero-downtime.

---

### Phase 6 — Hypervisor + inventory

**Goal:** Provision the three new VMs and add them to inventory.

**Deliverables in this repo:**

```
platform/inventory/local.yml             Placeholders for uhhcraft_svc, inference_comfyui_svc,
                                         inference_hunyuan3d_svc
platform/inventory/production.yml        Same (placeholders)
platform/hypervisor/proxmox/             VM provisioning configs:
                                         - uhhcraft-svc-01      (CPU VM)
                                         - inference-comfyui-01 (GPU VM, PCIe passthrough)
                                         - inference-hunyuan3d-01 (GPU VM, PCIe passthrough)
```

**Deliverables in site-config (private repo):**

- Host group entries with real IPs, `service_name`, `monorepo_deploy_path`, `service_url`.
- Per-host vars including `container_engine: podman`, GPU UUIDs for the inference hosts.

**Work items:**

1. **GPU sub-plan:** create `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md` covering:
   - Proxmox PCIe passthrough configuration (IOMMU groups, VFIO).
   - NVIDIA driver pinning on the host vs. inside the VM.
   - Capacity: which physical hosts hold the RTX 5070s (per UhhCraft spec)? Confirm with site-config.
   - Recovery: VM template + cloud-init for fast rebuild.
2. Add Proxmox cloud-init templates for each VM under `platform/hypervisor/proxmox/`.
3. Generate three SSH keypairs (one per service), store in OpenBao at `secret/services/ssh/<name>`.
4. Run `distribute-ssh-keys.yml` against each VM after provisioning.
5. Run `harden-ssh.yml` once key auth is verified (rule 5 from CLAUDE.md: verify before hardening).
6. Run `install-docker.yml` — wait, no: we're on Podman. Use a parallel `install-podman.yml` (this may already exist or need to be authored alongside `tasks/install-podman-compose.yml`).

**Acceptance:**

- All three VMs reachable via per-service SSH key only (password auth disabled).
- `validate-all.yml` returns green for the three new hosts.
- GPU services see the NVIDIA device (`nvidia-smi` inside the container succeeds).

---

### Phase 7 — Semaphore templates

**Goal:** Wire the new playbooks into Semaphore so deploys are triggerable through the UI.

**Deliverables:**

```
platform/semaphore/templates.yml         + Deploy UhhCraft
                                         + Update UhhCraft
                                         + Clean Deploy UhhCraft
                                         + Deploy ComfyUI
                                         + Update ComfyUI
                                         + Deploy Hunyuan3D
                                         + Update Hunyuan3D
                                         + Apply UhhCraft Policy
                                         + Apply ComfyUI Policy
                                         + Apply Hunyuan3D Policy
```

**Work items:**

1. Add template definitions to `templates.yml` mirroring NetBox entries.
2. Run `setup-templates.yml` to push the new templates into Semaphore.
3. Confirm Semaphore's AppRole policy covers the new secret paths (`secret/services/uhhcraft/*` etc.); if not, extend `platform/services/openbao/deployment/config/policies/semaphore-policy.hcl` and re-apply.

**Acceptance:**

- Semaphore UI shows the new templates.
- Triggering "Deploy UhhCraft" runs the full playbook end-to-end against the new VM.

---

### Phase 8 — CI / linting / testing

**Goal:** The unified `lint-and-test.yml` workflow gates Go, templ, sqlc, and the Python sidecars alongside the existing Ansible/Bash/Python jobs.

**Deliverables:**

`.github/workflows/lint-and-test.yml` extended with:

| Job | Tools | What it gates |
|-----|-------|---------------|
| `go-lint` | `golangci-lint` (default linters + `gosec`), `templ fmt --diff`, `sqlc verify` | UhhCraft Go style, security, generated-code drift |
| `go-test` | `go test ./...` with Postgres + Redis service containers (mirrors output/ci.yml) | UhhCraft unit + integration tests |
| `go-build` | `go build ./...`, image build via Buildah/Podman | Compile + container image health |
| Existing `python-lint` | Add path `platform/services/inference-*` to ruff + bandit scope | Python sidecars |
| Existing `python-test` | Add a `pytest platform/services/inference-*/tests` job if tests exist | Sidecar tests |

**Work items:**

1. Add a `go.mod` workspace at `platform/services/uhhcraft/deployment/go.mod` (already there post-Phase 2). Confirm GHA can `setup-go@v5` against it.
2. Pin Go to 1.23 (matches `go.mod`).
3. Install `templ` and `sqlc` in the workflow before linting.
4. Path filters so Go jobs only fire when `platform/services/uhhcraft/deployment/**` changes (avoid blocking unrelated PRs).
5. Update `pyproject.toml` (ruff config) so it doesn't try to parse Go source.
6. Update `.ansible-lint` excludes if `platform/services/uhhcraft/deployment/db/migrations/**` looks like Ansible to the linter (it shouldn't, but verify).
7. Update root `.gitignore`:
   ```
   platform/services/uhhcraft/deployment/web/templates/*_templ.go
   platform/services/uhhcraft/deployment/internal/db/sqlc/*.go    # if generated
   platform/services/uhhcraft/deployment/tmp/
   platform/services/uhhcraft/deployment/dist/
   ```
8. Update `CONTRIBUTING.md` pre-PR checklist with the Go-side commands.

**Acceptance:**

- A trivial Go change in `platform/services/uhhcraft/deployment/` triggers only the Go jobs.
- A pure Ansible change does not trigger the Go jobs.
- All jobs green on `main` after merge.

---

### Phase 9 — Architecture docs + cross-links

**Goal:** Make WebSmith and UhhCraft first-class in the docs landscape so future contributors can navigate to them.

**Deliverables:**

```
plan/architecture/WEBSITE-BUILDING-AGENT.md   New — how WebSmith fits the four-layer model;
                                              the SPEC → service handoff; the "second site" recipe.
plan/architecture/architecture-reference.md   + entry for WEBSITE-BUILDING-AGENT.md
plan/architecture/SERVICE-INTEGRATION-PLAN.md  + subsection: "Sites built via WebSmith"
plan/architecture/CADDY-REVERSE-PROXY.md      + per-site fragment pattern (from Phase 5)
plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md + UhhCraft as an example of Podman for a Go web app
README.md                                     + WebSmith in AI Agents table; UhhCraft in services
CLAUDE.md                                     + websmith / uhhcraft sub-directory CLAUDE.md links
kickstart.md                                  + WebSmith + UhhCraft in "Where to look next"
```

**Acceptance:**

- A new contributor reading top-level docs only can locate WebSmith and UhhCraft without grep.
- `grep -r 'WebSmith\|UhhCraft' README.md CLAUDE.md kickstart.md plan/` returns coherent, current references.

---

### Phase 10 — Validation + branch testing + rollback

**Goal:** End-to-end smoke before merging anything to main.

**Work items:**

1. Use `plan/architecture/BRANCH-TESTING-WORKFLOW.md` to deploy the integration branch to the new VMs.
2. Walk the user-facing happy path:
   - Browse catalog → /healthz green.
   - Generate from prompt → ComfyUI 200 → image stored in MinIO.
   - 3D canvas view → Hunyuan3D 200 → GLB stored in MinIO.
   - Add to cart → Stripe test mode → order created in Postgres.
   - Webhook → River job → fulfillment dispatch (Printful test).
3. Run `validate-all.yml` and confirm green on all four affected hosts (uhhcraft, comfyui, hunyuan3d, caddy).
4. Document rollback in `platform/services/uhhcraft/deployment/README.md`:
   - `clean-deploy-uhhcraft.yml` is the destructive reset.
   - `update-uhhcraft.yml --extra-vars "image_tag=<previous-sha>"` is the no-data-loss rollback.
5. Run `/security-review` on the full diff.

**Acceptance:**

- All branch-test smoke flows succeed.
- CodeRabbit + the three CI jobs green.
- Rollback procedure validated by intentionally deploying a known-bad image tag and rolling back.

---

### Phase 11 — The "second site" recipe

**Goal:** Codify how to build the *next* site so it lands in `platform/services/<sitename>/` without rediscovering the integration shape.

**Deliverables:**

```
agents/websmith/context/architecture/integration-with-agent-cloud.md
```

Contents:

1. **Where output goes.** Future WebSmith sessions write `spec/` and code into `platform/services/<sitename>/` (not a separate working directory as the framework currently assumes). Document the deviation from `KICKSTART.md`.
2. **The standard service shape.** Mirror UhhCraft: `deployment/{cmd,internal,web,db,config,templates,compose.yml,deploy.sh,post-deploy.sh}` + `context/{spec,architecture,prompts,skills,use-cases}`.
3. **The standard playbook shape.** Mirror `deploy-uhhcraft.yml`.
4. **The standard Caddy fragment shape.** Mirror `caddy-site.j2`.
5. **Stack picker → agent-cloud constraints.** When WebSmith Phase 3 (Tooling) is run, the agent must surface the agent-cloud defaults:
   - Database: Postgres (per CLAUDE.md).
   - Container runtime: Podman (NetBox is the only Docker exception).
   - Reverse proxy: central Caddy with DNS-01.
   - Secrets: OpenBao + Ansible templating.
   - CI: unified `lint-and-test.yml` with path filters.
   - Hosting: dedicated Proxmox VM(s).
   These belong in WebSmith's `catalogs/stacks.md` as an "agent-cloud preset."
6. **SPEC deviations register.** Every concrete service must keep a `## Deviations from Spec` section in its `context/spec/SPEC.md`.

**Acceptance:**

- Running WebSmith on a brand-new site idea produces a `SPEC.md` and a service skeleton that drops into agent-cloud with no Phase-2-style rewriting needed.

---

## 3. PR sequencing

Each phase is its own PR. Order matters:

| PR | Phase(s) | Title | Depends on |
|----|----------|-------|-----------|
| 1 | 1 | `feat(websmith): integrate website framework as new agent` | — |
| 2 | 2, 2.5 | `feat(uhhcraft): carve framework output into platform services` | PR 1 |
| 3 | 3 | `feat(uhhcraft): OpenBao policies and secret layout` | PR 2 |
| 4 | 4 | `feat(uhhcraft): composable Ansible playbooks for UhhCraft + inference` | PR 3 |
| 5 | 5 | `feat(caddy): per-site fragment pattern; UhhCraft route` | PR 4 |
| 6 | 6 | `feat(hypervisor): provision uhhcraft + inference VMs` | PR 5 |
| 7 | 7 | `feat(semaphore): UhhCraft + inference templates` | PR 6 |
| 8 | 8 | `ci: extend lint-and-test.yml with Go/templ/sqlc/Python` | PR 2 (can ship in parallel with 3-7) |
| 9 | 9 | `docs(architecture): WebSmith + UhhCraft cross-links` | PR 7 |
| 10 | 10 | `validation: branch deploy + smoke + rollback docs` | PR 9 |
| 11 | 11 | `feat(websmith): agent-cloud integration recipe` | PR 9 |

PRs 1 and 8 can land independently and early; PRs 2-7 are a linear chain because each depends on the previous's secret/playbook/template artefacts.

---

## 4. Open questions to resolve before execution

These are real questions whose answers change later phases. Flagging now rather than discovering mid-execution:

1. **Build location.** Does the UhhCraft container image build in CI (push to a registry, deploy pulls) or build on the target VM at deploy time? CI build is cleaner; target-VM build is what UhhCraft's current Makefile assumes. **Recommendation: CI build → registry.**
2. **Registry.** If we build in CI, do we use GHCR (`ghcr.io/uhstray-io/...`) or stand up a private registry (Harbor is mentioned in the K8s roadmap)? **Recommendation: GHCR now, Harbor later.**
3. **`templ` and `sqlc` generated files.** Commit or generate-in-CI? **Recommendation: generate-in-CI; nothing generated in git.**
4. **UhhCraft SPEC deviation signoff.** The SPEC says "Docker is dev-only, production runs natively." We're using Podman in prod. This needs explicit re-signoff before Phase 2 PR merges.
5. **GPU host capacity.** UhhCraft's spec implies RTX 5070 hosts already exist on the local network. Are these Proxmox-managed and ready for PCIe passthrough, or do they need to be added to the cluster? Confirm in `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md`.
6. **River migrations.** River has its own migration tool (`river migrate-up`). Confirm it plays nicely with `goose` running on the same database, or sequence them correctly in `post-deploy.sh`.
7. **AppRole at runtime for the Go app.** Does UhhCraft need to read secrets from OpenBao at runtime (e.g., for Stripe key rotation), or is `.env`-at-boot enough? **Recommendation: `.env`-at-boot for v1; add AppRole in a Phase 11 follow-up if rotation needs emerge.**
8. **Public Docker images.** Postgres, Redis, MinIO — pull from Docker Hub or mirror? Existing services pull from upstream; UhhCraft follows the same pattern.
9. **Discord webhook + Stripe — sandbox vs. prod.** During Phase 10 smoke tests, do we hit Stripe live mode or test mode? **Recommendation: test mode; promote to live only after a separate human-signed checklist.**
10. **CSP policy.** UhhCraft's Caddyfile has a tight CSP. When we move to Three.js + WASM workers for the 3D canvas, does the existing CSP allow `worker-src 'self' blob:`? Verify before Phase 10.

---

## 5. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|-----------|
| Podman + River + Compose healthcheck integration is fragile | Medium | Blocks Phase 4 | Smoke-test `podman compose` with the full stack locally in Phase 2 before writing playbooks |
| GPU passthrough fails on Proxmox | Medium | Blocks inference services | Phase 6 GPU sub-plan; have a fallback of running inference on the host (bare-metal) if passthrough is brittle |
| UhhCraft tests assume Docker in CI | Low | CI red | Translate `services:` blocks in output/ci.yml directly to the unified workflow |
| Caddy reload races with cert renewal | Low | Brief outage | Use `caddy reload`, not restart; ensure `import sites/*.caddy` is glob-stable |
| WebSmith authors a future site that doesn't fit the agent-cloud preset | Medium | Manual rework per site | Phase 11 hardens the agent-cloud preset in WebSmith's catalogs |
| `output/spec/SPEC.md` and reality drift | High over time | Future deviations not tracked | Mandate a `## Deviations from Spec` section in every service's `context/spec/SPEC.md`; PR template asks |
| Multiple `KICKSTART.md` / `README.md` files cause confusion | Low | Onboarding friction | Capitalisation + directory context disambiguates; root `kickstart.md` already references the WebSmith one |

---

## 6. Definition of done

The integration is complete when **all** of the following hold:

- [ ] `agents/websmith/` is a complete, self-contained agent following the `deployment/ + context/` convention.
- [ ] `platform/services/uhhcraft/`, `platform/services/inference-comfyui/`, `platform/services/inference-hunyuan3d/` exist and deploy via Semaphore.
- [ ] `https://uhhcraft.uhstray.io` serves the Go app behind the central Caddy with a valid Let's Encrypt cert.
- [ ] OpenBao holds every UhhCraft + inference secret; no secrets are in the repo.
- [ ] CI gates Go + templ + sqlc + Python alongside the existing Ansible/Bash/Python jobs.
- [ ] `validate-all.yml` returns green for the four affected hosts.
- [ ] `plan/architecture/WEBSITE-BUILDING-AGENT.md` describes the SPEC → service handoff.
- [ ] WebSmith's `catalogs/stacks.md` includes the "agent-cloud preset."
- [ ] Rollback procedure documented and exercised at least once.
- [ ] Root `README.md`, `CLAUDE.md`, and `kickstart.md` reference the new agent and services.

---

## 7. Next action

User reviews this plan and either:
- **Approves** → I create Phase 1 as a feature branch and PR (`feat/websmith-phase-1-agent-move`), and we proceed in PR sequence.
- **Requests changes** → I revise and re-circulate.
- **Defers a phase** → We update §3 to mark it out-of-scope and adjust dependencies.

Open questions in §4 can be answered now or at the start of the relevant phase, but **#4 (SPEC deviation signoff)** and **#5 (GPU host capacity)** should be resolved before Phase 2 starts.
