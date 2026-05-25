# Kickstart

A walkthrough for new contributors to **agent-cloud**. By the end of this doc you should understand what this repo is, how it's laid out, where things live, and how to get hands-on — whether you're deploying a service, editing a playbook, or wiring up a new agent.

If you only read one other document, read the top-level [`README.md`](README.md). If you only read two, also read [`CLAUDE.md`](CLAUDE.md) — it captures the conventions and deployment rules that apply to all work in this repo.

---

## 1. What you're looking at

**agent-cloud** is the unified platform monorepo for [uhstray-io](https://github.com/uhstray-io) — a privacy-focused, open-source AI platform. One repo holds:

- **Service deployments** (NetBox, OpenBao, NocoDB, n8n, Semaphore, Caddy, WisAI inference, ...)
- **AI agent configurations** (NemoClaw, NetClaw, Claude Cowork, WisBot)
- **Ansible playbooks** that orchestrate every deploy
- **Kubernetes manifests** (Kustomize) for the future multi-site path
- **Shared bash libraries** (`platform/lib/common.sh`, `bao-client.sh`)
- **Architecture and development plans** (`plan/`)

Real IPs, production inventory, and credential backups live in a **separate private repo** called `site-config`. This repo contains only templates, placeholders, and code.

### The four-layer mental model

```
AI Layer          NemoClaw, NetClaw, Claude Cowork, WisBot
                  Backed by WisAI (Ollama + Open WebUI; OpenAI-compatible API)

Guardrail Layer   OpenBao (secrets), Kyverno (k8s), OPA (policy), AppRole scoping
                  AI proposes -> guardrails validate -> automation runs

Automation Layer  Ansible playbooks, Semaphore orchestration, n8n workflows
                  Deterministic, idempotent, auditable

Platform Layer    Docker (NetBox), Podman (other services),
                  Compose/Podman (single-site) <-> Kubernetes/k0s (multi-site),
                  Proxmox VMs for all hosting
```

Read these top-down: AI agents *propose* changes; guardrails *validate* them; automation *executes* them on the platform. AI never writes secrets or pushes to production directly.

---

## 2. Prerequisites

You don't need all of these to read or contribute docs, but you'll want them for serious work:

**Always useful:**
- Git
- A GitHub account with access to `uhstray-io`
- A reasonable shell (zsh, bash 5.x)

**For running playbooks locally / linting / testing:**
- Python 3.11+ (`brew install python@3.11`)
- Ansible (`pip install ansible ansible-lint`)
- `ruff`, `yamllint`, `bandit` (`pip install ruff yamllint bandit`)
- `shellcheck`, `bats-core`, `hadolint` (`brew install shellcheck bats-core hadolint`)
- `terraform` (for HCL `fmt` if touching OpenBao policies)

**For deploying / hitting real infra:**
- A Proxmox cluster (or any Linux VMs with Docker/Podman)
- A running OpenBao instance (see `platform/services/openbao/deployment/`)
- Semaphore deployed somewhere reachable
- Access to the private `site-config` repo for inventory + credentials

**One-shot bootstrap:**
```bash
git clone https://github.com/uhstray-io/agent-cloud.git
cd agent-cloud
bash platform/scripts/setup-project.sh   # installs Ansible collections, sets up pre-commit
```

---

## 3. Repository layout — where everything lives

Top-level:

```
agent-cloud/
├── README.md              Project overview (start here)
├── CLAUDE.md              AI agent + human conventions, deployment rules
├── CONTRIBUTING.md        Branch / PR / commit conventions
├── SECURITY.md            Security disclosure policy
├── kickstart.md           <- you are here
├── pyproject.toml         Ruff + pytest config
├── requirements.yml       Ansible collection requirements (-> collections/)
├── .ansible-lint          Ansible lint config
├── .yamllint.yml          YAML lint config
├── .pre-commit-config.yaml
├── platform/              All service + automation code
├── agents/                AI agent definitions and context
├── plan/                  Architecture and implementation plans
├── collections/           Vendored Ansible collections (auto-installed)
├── roles/                 (Reserved for future shared Ansible roles)
└── .github/workflows/     CI pipeline (lint, security, test)
```

### `platform/` — services and orchestration

```
platform/
├── services/<name>/
│   ├── deployment/        How to run it
│   │   ├── deploy.sh      Container lifecycle only — no secrets
│   │   ├── compose.yml    (or docker-compose.yml)
│   │   ├── templates/     Jinja2 .env / config templates
│   │   ├── config/        Static config files
│   │   └── post-deploy.sh (optional) app-level bootstrap
│   └── context/           How AI agents interact with it
│       ├── skills/        Skill definitions
│       ├── prompts/       Reusable prompts
│       └── use-cases/     Worked examples
│
├── playbooks/             Ansible orchestration
│   ├── README.md          Playbook reference + conventions
│   ├── deploy-<name>.yml  One playbook per service
│   ├── update-<name>.yml  Pull-and-restart variants
│   ├── clean-deploy-<name>.yml  Destructive rebuilds
│   ├── apply-policy-<*>.yml     OpenBao policy applies
│   ├── distribute-ssh-keys.yml, harden-ssh.yml, install-docker.yml
│   ├── validate-all.yml, check-secrets.yml, validate-secrets.yml
│   └── tasks/             Composable task library (the building blocks)
│       ├── manage-secrets.yml
│       ├── manage-approle.yml
│       ├── manage-diode-credentials.yml
│       ├── deploy-orb-agent.yml
│       ├── clean-service.yml
│       ├── clone-and-deploy.yml
│       ├── apply-openbao-policy.yml
│       └── ...
│
├── lib/                   Shared bash libraries
│   ├── common.sh          Logging, compose wrapper, health checks
│   └── bao-client.sh      OpenBao HTTP API client (curl + jq)
│
├── inventory/             Placeholder inventory (real IPs live in site-config)
│   ├── local.yml
│   └── production.yml
│
├── semaphore/             Semaphore template definitions
│   ├── templates.yml      Declarative template list (source of truth)
│   └── setup-templates.yml  Playbook that applies templates.yml
│
├── hypervisor/proxmox/    VM provisioning, cloud-init templates
├── k8s/                   Kustomize manifests (base / overlays / bootstrap)
├── scripts/               Project setup helpers
└── tests/                 BATS tests for shared bash libs
```

### `agents/` — AI agent definitions

```
agents/
├── nemoclaw/      Headless engineer: background automation, CI/CD, monitoring
│   ├── deployment/
│   └── context/{architecture,prompts,skills,use-cases}
├── netclaw/       Network engineer: topology, SNMP/LLDP, config backup
│   ├── deployment/
│   └── context/...
├── cowork/        Interactive architect (Claude Cowork)
│   └── context/
├── websmith/      Website-building agent (prompt-only — produces signed SPEC.md per site)
│   ├── deployment/  (stub — WebSmith has no runtime)
│   └── context/{phases,catalogs,schemas,examples,architecture,prompts,skills,use-cases}
└── workflows/     (Cross-agent workflows — reserved)
```

Each agent's `context/` is the AI-facing half: the skills it can run, the prompts it ships with, the architecture docs it grounds itself in. `deployment/` is the human-facing half: how to actually run the agent on a VM.

### `plan/` — architecture and roadmap

```
plan/
├── architecture/          Cross-cutting design docs (mostly stable)
│   ├── architecture-reference.md      <- index, document standards
│   ├── AUTOMATION-COMPOSABILITY.md    <- read this before adding a service
│   ├── SERVICE-INTEGRATION-PLAN.md    <- onboarding checklist
│   ├── CREDENTIAL-LIFECYCLE-PLAN.md
│   ├── ACCESS-BOUNDARIES.md           Semaphore vs SSH access rules
│   ├── CADDY-REVERSE-PROXY.md
│   ├── PODMAN-VS-DOCKER-COMPOSE.md
│   ├── SECURITY-TESTING-STANDARDS.md
│   ├── CI-TESTING-SPECIFICATION.md
│   ├── TESTING-AND-LINTING-PLAN.md
│   ├── BRANCH-TESTING-WORKFLOW.md
│   ├── LINTING-AND-TESTING.md
│   └── skills-recommendation.md
│
├── development/           Active implementation plans (per feature)
│   ├── IMPLEMENTATION_PLAN.md
│   ├── NETBOX-DISCOVERY-EXPANSION.md
│   ├── WISAI-DEPLOYMENT-PLAN.md
│   ├── NETCLAW-INTEGRATION-PLAN.md
│   ├── OPA-INTEGRATION-PLAN.md
│   └── ... (one per initiative)
│
└── archive/               Completed or superseded plans
```

**Rule:** every multi-step change starts as a plan in `plan/development/` before code is written.

---

## 4. The five rules you must internalise

These come straight from `CLAUDE.md` and they govern every change in this repo:

1. **All deployments go through Semaphore.** Never SSH to a VM and run `deploy.sh` by hand — Semaphore is what injects OpenBao credentials into the environment.
2. **`deploy.sh` handles containers only.** No secret generation, no OpenBao calls. Ansible owns the full credential lifecycle.
3. **Each workflow is independent.** Don't bundle optional pieces (orb-agent, pfsense-sync) into a service deploy. Make a separate playbook.
4. **No intermediary secret files.** Secrets flow `OpenBao -> Ansible memory -> Jinja2 templates -> .env on the VM (gitignored)`. There is no `secrets/` directory anywhere on a VM.
5. **Verify before hardening.** Never disable an auth method (SSH password, old credentials) without first confirming the replacement works.

---

## 5. Secrets — the one diagram you must understand

```
                                +-------------------+
                                |  OpenBao (truth)  |
                                +---------+---------+
                                          |
                       generate / fetch   |   write back runtime creds
                                          v
                                +---------+---------+
                                |  Ansible (memory) |
                                |  manage-secrets   |
                                +---------+---------+
                                          |
                              Jinja2 template render
                                          v
                                +---------+---------+
                                | .env / config on  |
                                |   VM (gitignored) |
                                +---------+---------+
                                          |
                                          v
                                +---------+---------+
                                |  Docker Compose   |
                                |   (reads .env)    |
                                +-------------------+
```

**Key facts:**
- OpenBao is the only source of truth.
- Semaphore holds *only* an AppRole `role_id` + `secret_id` in its environment — every other secret is fetched at playbook runtime.
- `.env` files on disk are a minimal bridge for Compose. They are gitignored, overwritten every deploy, and never authoritative.
- Policy / AppRole / template changes are **code-only**: edit the `.hcl` file or `templates.yml`, run the apply playbook. Never poke OpenBao or Semaphore via ad-hoc API calls.

OpenBao secret layout (high level):

| Path | Contents |
|------|----------|
| `secret/services/ssh` | Management SSH keypair |
| `secret/services/ssh/<service>` | Per-service SSH keypairs |
| `secret/services/netbox` | NetBox DB, Redis, Diode, Hydra, superuser, orb-agent |
| `secret/services/approles/<name>` | AppRole `role_id` + `secret_id` |
| `secret/services/proxmox` | Proxmox API token |
| `secret/services/{nocodb,n8n,semaphore,github,discord}` | API tokens / URLs |

The full layout lives in `CLAUDE.md` and `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md`.

---

## 6. Your first 30 minutes — guided tour

You can do all of this without any deploy credentials.

```bash
# 1. Clone and enter the repo
git clone https://github.com/uhstray-io/agent-cloud.git
cd agent-cloud

# 2. Install dev tooling
bash platform/scripts/setup-project.sh

# 3. Skim the high-leverage docs
$EDITOR README.md
$EDITOR CLAUDE.md
$EDITOR plan/architecture/architecture-reference.md
$EDITOR plan/architecture/AUTOMATION-COMPOSABILITY.md

# 4. Look at one reference service end-to-end
ls   platform/services/netbox/deployment/
cat  platform/services/netbox/deployment/CLAUDE.md
cat  platform/services/netbox/deployment/deploy.sh
cat  platform/playbooks/deploy-netbox.yml
cat  platform/playbooks/tasks/manage-secrets.yml

# 5. Look at how Semaphore knows about templates
cat  platform/semaphore/templates.yml

# 6. Run the linters locally (they're fast)
ruff check .
yamllint -c .yamllint.yml .
ansible-lint platform/playbooks/
bats platform/tests/
```

NetBox is the **reference implementation** for the composable pattern. When in doubt, read its files; everything new should look like NetBox, not like the legacy services.

---

## 7. Deploying a service (the happy path)

There are two patterns currently in use — same shape, different mechanics:

### Composable pattern (preferred, all new services)

Used by NetBox today. Multi-phase playbook:

1. **Phase 1 — Secrets.** `tasks/manage-secrets.yml` fetches existing or generates new secrets in OpenBao, then renders Jinja2 templates into `.env` and config files on the target VM.
2. **Phase 2 — Containers.** `deploy.sh` runs on the VM and only touches Docker/Podman Compose lifecycle (pull, build, up).
3. **Phase 3 — Application bootstrap.** `post-deploy.sh` (optional) runs migrations, creates users, registers OAuth2 clients.
4. **Phase 4 — Runtime credential sync.** Tasks like `manage-diode-credentials.yml` capture any credentials the service generated at runtime and push them back to OpenBao.
5. **Phase 5 — Health check.** HTTP-level verification that the service is alive.

### Legacy pattern (most other services, for now)

A thin wrapper playbook imports `deploy-service.yml` with a `target_service` variable. `deploy-service.yml` clones the monorepo to the VM, runs `deploy.sh`, and health-checks. Migration to the composable pattern is planned for each.

### Triggering a deploy

```text
You don't.  Semaphore does.
```

You:

1. Push your branch.
2. Open a PR; CI runs the lint + security + test jobs.
3. After merge (or for branch-testing), the Semaphore task template for that service is what actually pulls the repo and runs the playbook.

The local `bash deploy.sh` invocation shown in the README is correct, but **only Semaphore has the OpenBao AppRole credentials it needs**. Outside Semaphore, expect missing secrets.

---

## 8. Making a change — the PR workflow

Cribbed from `CLAUDE.md` and `CONTRIBUTING.md`:

```bash
# 1. Branch
git checkout -b feat/<short-description>
#    Types: feat | fix | docs | ci | refactor | chore | security

# 2. Make changes; if it's non-trivial, add a plan to plan/development/

# 3. Update docs in the same PR
#    - top-level README.md if you added a service or feature
#    - the most relevant sub-directory README.md / CLAUDE.md
#    - the root CLAUDE.md if you introduced a cross-cutting convention

# 4. Lint locally
ruff check .
find . -name '*.sh' ! -path '*/netbox-docker/*' -exec shellcheck -S warning {} +
ansible-lint platform/playbooks/
yamllint -c .yamllint.yml .
bats platform/tests/

# 5. Secret scan (mandatory; CLAUDE.md calls this out)
git diff --staged | grep -iE '^\+.*192\.168\.' | grep -v 'target\|host:\|subnet\|scope\|example'
git diff --staged | grep -iE '^\+.*password\s*[:=]\s*[A-Za-z0-9]{8}|^\+.*secret_id[:=]\s*[a-f0-9-]{30}'

# 6. Commit (conventional commits, no AI attribution)
git commit -m "feat(<scope>): <short summary>"

# 7. Push and open a PR
git push -u origin feat/<short-description>
gh pr create
```

**Wait for all PR checks to pass before merging.** This applies equally to code, plans, and docs. CodeRabbit + the three CI jobs (static analysis, security, unit tests) are gating.

### Common gotchas

- **No IPs, no usernames, no real credentials in commits** — `{{ }}` Jinja2 references only. The CI security scan will reject them.
- **No `--no-verify` on commits or pushes.** If a hook fails, fix the underlying issue.
- **Each PR gets a fresh branch.** Don't reuse merged branches.
- **Update `plan/` alongside code changes** when you change architecture, not after.

---

## 9. Adding a new service (cheat sheet)

The authoritative checklist is `plan/architecture/SERVICE-INTEGRATION-PLAN.md`. The short version for the composable pattern:

1. Create `platform/services/<name>/deployment/`:
   - `deploy.sh` (container lifecycle only, sources `../../lib/common.sh`)
   - `compose.yml`
   - `templates/*.j2` (Jinja2 env / config templates)
2. Add `_secret_definitions` and `_env_templates` for the service.
3. Create `platform/playbooks/deploy-<name>.yml` using `tasks/manage-secrets.yml` -> `deploy.sh` -> verify.
4. Create `platform/playbooks/clean-deploy-<name>.yml` using `tasks/clean-service.yml`.
5. Add the host to the **site-config** inventory with `service_name`, `monorepo_deploy_path`, `service_url`.
6. Add Semaphore templates to `platform/semaphore/templates.yml`; run `setup-templates.yml`.
7. Generate an SSH keypair, store at `secret/services/ssh/<name>`, run `distribute-ssh-keys.yml`.
8. Optionally provision a dedicated AppRole via `tasks/manage-approle.yml`.

Model everything on `platform/services/netbox/deployment/` + `platform/playbooks/deploy-netbox.yml`.

---

## 10. AI agents at a glance

| Agent | Type | Where it lives | What it does |
|-------|------|----------------|--------------|
| **NemoClaw** | Headless engineer | `agents/nemoclaw/` | Background automation, API integrations, CI/CD, health monitoring |
| **NetClaw** | Network engineer | `agents/netclaw/` | Topology discovery, SNMP/LLDP, config backup, security auditing |
| **Claude Cowork** | Interactive architect | `agents/cowork/` | Research, architecture decisions, document generation |
| **WebSmith** | Website builder | `agents/websmith/` | Prompt-only — walks users through a 5-phase workflow producing a signed `SPEC.md` for a new website service |
| **WisBot** | Community interface | [separate repo](https://github.com/uhstray-io/WisBot) | Discord voice/chat bot |

All four are clients of **WisAI** — the local-inference backbone (`platform/services/inference-ollama/` + `inference-webui/`), which exposes an OpenAI-compatible API via Open WebUI. Future GPU hardware will add vLLM workers (`inference-vllm/`, currently reserved).

Each agent's `context/` directory follows the same shape:
- `architecture/` — system docs the agent grounds itself in
- `prompts/` — reusable system / user prompts
- `skills/` — discrete capabilities the agent can invoke
- `use-cases/` — worked examples

Agents *propose*; the guardrail layer *validates*; the automation layer *executes*. Agents never write secrets or push to production directly.

---

## 11. Where to look next

| If you want to… | Read |
|------------------|------|
| Understand the repo's purpose and surface area | [`README.md`](README.md) |
| Learn the conventions that gate every PR | [`CLAUDE.md`](CLAUDE.md), [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Add a new service | `plan/architecture/SERVICE-INTEGRATION-PLAN.md`, `plan/architecture/AUTOMATION-COMPOSABILITY.md` |
| Understand the playbook library | `platform/playbooks/README.md` |
| See the reference deployment | `platform/services/netbox/deployment/CLAUDE.md` + `platform/playbooks/deploy-netbox.yml` |
| Understand secret flow + rotation | `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` |
| Understand Semaphore vs direct SSH | `plan/architecture/ACCESS-BOUNDARIES.md` |
| Pick Docker vs Podman | `plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md` |
| Set up reverse-proxy / TLS | `plan/architecture/CADDY-REVERSE-PROXY.md` |
| Test a feature branch end-to-end | `plan/architecture/BRANCH-TESTING-WORKFLOW.md` |
| Find Claude Code skills worth installing | `plan/architecture/skills-recommendation.md` |
| Build a new website inside agent-cloud | `agents/websmith/README.md` (human start) + `agents/websmith/context/AGENTS.md` (agent start) |
| Integrate a WebSmith spec into a service | `plan/development/WEBSMITH-INTEGRATION-PLAN.md` |
| Browse what's planned next | `plan/development/IMPLEMENTATION_PLAN.md` and siblings |

---

## 12. Getting help

- **Discord:** [discord.uhstray.io](https://discord.uhstray.io/)
- **Email:** bac@uhstray.io
- **Issues / PRs:** [github.com/uhstray-io/agent-cloud](https://github.com/uhstray-io/agent-cloud)
- **Code of Conduct:** [uhstray.io/code-of-conduct](https://www.uhstray.io/en/code-of-conduct)

Welcome aboard.
