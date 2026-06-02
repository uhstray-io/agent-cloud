# WebSmith ↔ agent-cloud integration

> **Audience:** the LLM running a WebSmith session inside agent-cloud, and the humans reviewing what that session produced.
> **Architecture doc:** [`plan/architecture/WEBSITE-BUILDING-AGENT.md`](../../../../plan/architecture/WEBSITE-BUILDING-AGENT.md) — the platform-side architectural contract.

This document is the **second-site recipe**. It tells the agent how to take a signed `SPEC.md` and turn it into a real `platform/services/<sitename>/` service that drops into agent-cloud with no Phase-2-style rewriting work.

Read it before Phase 3 (Tooling). Read it again at the start of implementation. Hand it to whoever does the build.

---

## Where the output goes

WebSmith's standalone framework default is to write spec artefacts into a **separate working directory** outside the framework repo. Inside agent-cloud, that default is overridden.

| Artefact | Standalone default | Inside agent-cloud |
|----------|-------------------|--------------------|
| `intake.md`, `purpose.md`, `template.md`, `tooling.md`, `style.md`, `considerations.md`, `SPEC.md` | `~/projects/<sitename>/spec/` | `platform/services/<sitename>/context/spec/` |
| Source code | `~/projects/<sitename>/` | `platform/services/<sitename>/deployment/` |
| Per-site docs (architecture, prompts, skills, use-cases) | n/a in framework | `platform/services/<sitename>/context/{architecture,prompts,skills,use-cases}/` |

Spec artefacts colocate with the implementation they constrain. Reviewers can read both side-by-side without leaving the monorepo.

Tell the user this early in Phase 0 or Phase 1 so they understand the file layout the session will produce.

---

## The agent-cloud preset

At Phase 3 (Tooling), surface the **agent-cloud preset** as the recommended starting point:

- Catalog file: [`../catalogs/stack-presets/agent-cloud-preset.md`](../catalogs/stack-presets/agent-cloud-preset.md)
- It pre-decides: Postgres, Podman, central Caddy, OpenBao, Semaphore, dedicated Proxmox VM, generate-in-CI, unified `lint-and-test.yml`.
- It does **not** decide: web framework, CSS, page inventory, browser-side stack, auth model, background-worker library. Those are still real Phase 3 + Phase 4 decisions.

After picking the agent-cloud preset, layer one of the base presets (`go-templ-htmx`, `nextjs-typescript`, `rails`, `django`, `sveltekit`, …) for the actual web framework.

If the user pushes back on an agent-cloud preset opinion (e.g., wants Docker not Podman, or wants the site outside the monorepo), record the override in `SPEC.md` under `## Alignment with agent-cloud conventions`. Do not silently substitute.

---

## Standard service shape

Every site that lands at `platform/services/<sitename>/` mirrors UhhCraft's tree:

```text
platform/services/<sitename>/
├── CLAUDE.md                          Service-specific Claude guidance
├── deployment/
│   ├── README.md                      Dev quickstart + deploy story
│   ├── deploy.sh                      Container lifecycle only — invoked by Ansible
│   ├── post-deploy.sh                 Migrations + bootstrap + healthcheck — invoked by Ansible
│   ├── Dockerfile                     Multi-stage build (codegen + compile + distroless runtime)
│   ├── compose.yml                    Podman-compatible
│   ├── Makefile                       Dev helpers (codegen, run, test, lint)
│   ├── .env.example                   Local dev only; never tracked beyond .example
│   ├── .golangci.yml / pyproject.toml Language-specific lint config
│   ├── templates/
│   │   ├── env.j2                     Jinja2 → production .env from OpenBao
│   │   └── caddy-site.j2              Jinja2 → central Caddy site fragment
│   └── <language-specific source>     cmd/, internal/, web/, db/, config/ for Go;
│                                      app/, frontend/, etc. for other stacks
└── context/
    ├── spec/                          All six phase artefacts + SPEC.md
    │   └── SPEC.md                    With `## Alignment with agent-cloud conventions`
    ├── architecture/                  How-it-works docs, HTTP contracts with siblings
    ├── prompts/                       Reusable ops + content prompts
    ├── skills/                        Discrete capabilities (e.g., "Add a material")
    └── use-cases/                     Worked examples
```

[`platform/services/uhhcraft/`](../../../../platform/services/uhhcraft/) is the canonical reference. When in doubt, copy its shape.

---

## Standard playbook shape

The composable Ansible playbook lives at `platform/playbooks/deploy-<sitename>.yml`. Mirror [`deploy-uhhcraft.yml`](../../../../platform/playbooks/deploy-uhhcraft.yml):

```text
Phase 1 — Clone monorepo + manage secrets + template .env + verify podman
Phase 2 — Container lifecycle (deploy.sh — podman compose up + wait healthy)
Phase 3 — Application bootstrap (post-deploy.sh — migrations + healthcheck)
Phase 4 — Render + distribute Caddy fragment (tasks/distribute-caddy-site.yml)
Phase 5 — Health verification (HTTP /healthz)
```

Composable tasks available out-of-the-box:

- [`tasks/manage-secrets.yml`](../../../../platform/playbooks/tasks/manage-secrets.yml) — fetch/generate from OpenBao, template `env.j2` into `.env`.
- [`tasks/run-migrations.yml`](../../../../platform/playbooks/tasks/run-migrations.yml) — generic goose runner; container or host execution.
- [`tasks/install-podman-compose.yml`](../../../../platform/playbooks/tasks/install-podman-compose.yml) — verify `podman compose` or `podman-compose`.
- [`tasks/install-nvidia-toolkit.yml`](../../../../platform/playbooks/tasks/install-nvidia-toolkit.yml) — NVIDIA Container Toolkit + CDI for GPU services.
- [`tasks/distribute-caddy-site.yml`](../../../../platform/playbooks/tasks/distribute-caddy-site.yml) — slurp rendered fragment, push to central Caddy host, reload.
- [`tasks/clean-service.yml`](../../../../platform/playbooks/tasks/clean-service.yml) — destructive: wipe containers + volumes + clone.
- [`tasks/manage-approle.yml`](../../../../platform/playbooks/tasks/manage-approle.yml) — provision OpenBao AppRole bound to a policy (only if runtime vault access is needed).

Also add the matching wrappers:

- `platform/playbooks/update-<sitename>.yml` — thin `import_playbook: update-service.yml` with `target_service: <sitename>_svc`.
- `platform/playbooks/clean-deploy-<sitename>.yml` — `tasks/clean-service.yml` then `import_playbook: deploy-<sitename>.yml`.

---

## Standard Caddy fragment

Each site ships `templates/caddy-site.j2` in its `deployment/templates/`. The fragment is a full Caddy site block with Jinja2 variables for everything dynamic.

Minimum useful skeleton:

```jinja
{{ '{{' }} site_domain {{ '}}' }} {
    encode brotli gzip
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
    reverse_proxy {{ '{{' }} site_upstream {{ '}}' }} {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

The fragment is rendered on the service host by the deploy playbook, slurped, copied to the Caddy host's `sites/<sitename>.caddy`, and Caddy is reloaded — all via `tasks/distribute-caddy-site.yml`. Zero-downtime.

See [`plan/architecture/CADDY-REVERSE-PROXY.md`](../../../../plan/architecture/CADDY-REVERSE-PROXY.md) for the full convention.

---

## OpenBao secret layout

```text
secret/services/<sitename>                  Master KV — DB / Redis / MinIO / API keys / session_secret / webhook URLs
secret/services/<sitename>/<sub>            Optional sub-paths if rotation cadences differ
secret/services/ssh/<sitename>              Per-service SSH keypair (ed25519)
secret/services/approles/<sitename>         (Reserved; only created if/when runtime vault access is needed)
```

Define `_secret_definitions` in the deploy playbook listing what's `random` (auto-generated), `django` (long random with extra chars), or `user` (must be filled by the operator before first deploy). `tasks/manage-secrets.yml` handles the rest.

Add a reserved policy at `platform/services/openbao/deployment/config/policies/<sitename>.hcl` and an `apply-policy-<sitename>.yml` wrapper, even if no AppRole binds to it in v1. They document scope and survive future runtime-access additions.

---

## Semaphore templates

Add entries to [`platform/semaphore/templates.yml`](../../../../platform/semaphore/templates.yml):

```yaml
- name: Deploy <Sitename>
  playbook: platform/playbooks/deploy-<sitename>.yml
  survey_vars:
    - name: service_branch
      title: "Branch"
      type: string
      required: false
      default_value: "main"

- name: Update <Sitename>
  playbook: platform/playbooks/update-<sitename>.yml

- name: Clean Deploy <Sitename>
  playbook: platform/playbooks/clean-deploy-<sitename>.yml
  survey_vars:
    - name: service_branch
      type: string
      default_value: "main"

- name: Apply Policy - <Sitename>
  playbook: platform/playbooks/apply-policy-<sitename>.yml
```

Run `setup-templates.yml` to push them into Semaphore.

---

## Inventory

Add a host group to both `platform/inventory/local.yml` (localhost dev) and `platform/inventory/production.yml` (templated host var):

```yaml
<sitename>_svc:
  hosts:
    "{{ <sitename>_host }}":
      service_name: <sitename>
      service_url: "https://{{ <sitename>_domain | default('<sitename>.example.com') }}"
      health_path: "/healthz"
      monorepo_deploy_path: platform/services/<sitename>/deployment
      container_engine: podman
```

The real `<sitename>_host` value lives in the private site-config inventory, never in this repo.

---

## Proxmox VM spec

Add an entry to [`platform/hypervisor/proxmox/vm-specs.example.yml`](../../../../platform/hypervisor/proxmox/vm-specs.example.yml) documenting the VM shape. The real `vm-specs.yml` lives in site-config.

For GPU-using sites, also add a `hostpci` entry; see [`plan/development/UHHCRAFT-GPU-PASSTHROUGH.md`](../../../../plan/development/UHHCRAFT-GPU-PASSTHROUGH.md) for the host-side prep.

---

## CI

If the language is already covered (Go via the path-gated `go-*` jobs added in Phase 8 of the WebSmith integration plan), no CI changes needed — path filter picks them up automatically.

If the language is new, add a path-gated job triggered when `platform/services/<sitename>/deployment/**` changes. Use the existing Go jobs as the shape to mirror. Add the language to `pyproject.toml` exclusions if it would otherwise be misparsed (e.g., a Ruby service).

Container image build is handled by `go-build` for Go services; for other stacks, add a parallel `<lang>-build` job using `docker/build-push-action@v6`.

---

## SPEC deviation register

Every `platform/services/<sitename>/context/spec/SPEC.md` keeps a deviations register at the bottom:

```markdown
## Alignment with agent-cloud conventions

> Added <YYYY-MM-DD> during integration of <sitename> into the agent-cloud monorepo.
> These are updates to the original spec to align with platform conventions, not deviations.
> Approved by <name> on <date>.

### <Topic — e.g., Container runtime — Podman, not native binary>

- **Original spec:** ...
- **agent-cloud alignment:** ...
- **Rationale:** ...

(...one subsection per topic that needed alignment.)

## Tracking future deviations

Beyond integration alignment, any subsequent deviation must be added below as a new dated entry:

#### <YYYY-MM-DD> — <one-line summary>
- **What changed:** ...
- **Why:** ...
- **Approved by:** <name>, <date>
```

UhhCraft's [`SPEC.md`](../../../../platform/services/uhhcraft/context/spec/SPEC.md) shows the format. The alignment section is mandatory; the deviation register may be `(none yet.)` until the first real deviation lands.

---

## Implementation checklist (the canonical 16 steps)

For a new site after the spec is signed:

1. **Service skeleton** — `platform/services/<sitename>/{deployment,context}/{...}`.
2. **Spec home** — six phase artefacts + `SPEC.md` in `context/spec/`. `## Alignment` section added.
3. **Container image** — multi-stage `Dockerfile`, distroless runtime, build args for codegen.
4. **`compose.yml`** — Podman-friendly: no `version:`, `docker.io/library/` prefixes, loopback-only ports for app, no exposed ports for data services.
5. **`deploy.sh`** — lifecycle only. Sources `platform/lib/common.sh`. No secret gen, no migrations.
6. **`post-deploy.sh`** — migrations, schema bootstrap, healthcheck. Idempotent.
7. **`templates/env.j2`** — every env var referenced in `compose.yml` + the app.
8. **`templates/caddy-site.j2`** — central Caddy fragment with TLS, CSP, route handlers.
9. **Service `CLAUDE.md`** — site-specific Claude guidance.
10. **Composable Ansible** — `deploy-<sitename>.yml`, `update-<sitename>.yml`, optionally `clean-deploy-<sitename>.yml`.
11. **OpenBao policy** — reserved HCL + `apply-policy-<sitename>.yml`.
12. **Inventory** — host group in `platform/inventory/{local,production}.yml`.
13. **VM spec** — entry in `platform/hypervisor/proxmox/vm-specs.example.yml`.
14. **Semaphore templates** — Deploy / Update / (optionally Clean Deploy / Apply Policy) entries in `platform/semaphore/templates.yml`.
15. **CI** — path-gated jobs in `.github/workflows/lint-and-test.yml` if the language is new.
16. **Cross-references** — root `README.md`, `CLAUDE.md`, `kickstart.md`, `architecture-reference.md`.

If you find yourself doing 17 — adding something that's not in this list — pause and ask whether the recipe needs to grow, or whether you're adding bespoke complexity that future sites will have to undo.

---

## Cross-references

- Architecture: [`plan/architecture/WEBSITE-BUILDING-AGENT.md`](../../../../plan/architecture/WEBSITE-BUILDING-AGENT.md)
- Integration plan: [`plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md)
- agent-cloud preset (catalog): [`../catalogs/stack-presets/agent-cloud-preset.md`](../catalogs/stack-presets/agent-cloud-preset.md)
- Reference service: [`platform/services/uhhcraft/`](../../../../platform/services/uhhcraft/)
- Reference deploy playbook: [`platform/playbooks/deploy-uhhcraft.yml`](../../../../platform/playbooks/deploy-uhhcraft.yml)
- Caddy convention: [`plan/architecture/CADDY-REVERSE-PROXY.md`](../../../../plan/architecture/CADDY-REVERSE-PROXY.md)
- Composability foundation: [`plan/architecture/AUTOMATION-COMPOSABILITY.md`](../../../../plan/architecture/AUTOMATION-COMPOSABILITY.md)
- Service onboarding (general): [`plan/architecture/SERVICE-INTEGRATION-PLAN.md`](../../../../plan/architecture/SERVICE-INTEGRATION-PLAN.md)
- Container runtime conventions: [`plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md`](../../../../plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md)
