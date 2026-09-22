# Design: service deployment workflow

Motivation and scope: see `proposal.md`. Requirements: see `specs/platform/*/spec.md`. The
approved architecture, the twenty-two-step table and decisions D1 to D11 are in plan 15
(`plan/development/15-service-deployment-workflow-agents.md`); this document does not repeat
them. It records the seven explore threads of 2026-09-22 and the technical choices they
forced.

## Context

Verified on 2026-09-22:

- **110 playbooks, none use check mode.** No file under `platform/playbooks/` contains
  `check_mode` or `ansible_check_mode`; three accept a bespoke `dry_run` variable.
- **`ansible.builtin.uri` has no check-mode support** (module attributes: `check_mode:
  support: none`). A throwaway run with `--check` reported the probe as `skipping`, so any
  verification built on HTTP reads silently does nothing under a dry run unless the task sets
  `check_mode: false`.
- **`ansible.builtin.set_stats` fully supports check mode** and, with
  `ANSIBLE_SHOW_CUSTOM_STATS=true`, the default callback prints one line
  `RUN: { ...json... }` under `CUSTOM STATS:` (throwaway run, ansible-core 2.21.0). The
  plan-15 idea of a `debug` line with an embedded JSON string came out escaped
  (`"msg": "STEP-RESULT {\"schema\":...}"`) in the same probe.
- **Semaphore turns a task's `dry_run` into `--check` and `diff` into `--diff`**, and a task's
  `git_branch` replaces the repository's branch for that run. Evidence (task 0.2,
  2026-09-22), read from the source at the commit the local controller runs (`semaphore
  version` in the container: `v2.18.12^0-8a4dcf0`): `services/tasks/LocalJob.go:432-439`
  appends `--diff` and `--check`; `LocalJob.go:811-819` applies the template's branch, then
  the task's. Not yet exercised by a live task, because no operator API token exists on the
  workstation and the operating guide forbids reading the controller's own; the first
  section-2 run through the UI with "Dry run" ticked confirms it.
- **Tags need a template flag; the playbook path may not.** A task's `tags`, `skip_tags`
  and `limit` apply only when the template allows the override
  (`services/tasks/LocalJob.go:491-500`), and `setup-templates.yml` sets none of those
  flags, so an agent cannot launch `--tags verify` today. It does not need to: under check
  mode the verify tasks run (`check_mode: false`) and writes are skipped, so the
  orchestrator verifies by launching with `dry_run`. Separately, the runner takes a
  task-level `playbook` before the template's (`LocalJob.go:345`, `db/Task.go:49`) and task
  creation does not visibly clear it (`services/tasks/TaskPool.go:766-778`); if the API
  accepts it, a token with run rights can run any playbook in the repository under any
  template's inventory and keys. Source-level only, not exercised; not used by this change.
- **Dry run proven live on the local controller** (v2.18.12, Ansible 2.16.5, 2026-09-22):
  `DRY_RUN=1 scripts/local-dev.sh run check-secrets` sent `params.dry_run`; the step result
  reported `check_mode: true`, both OpenBao calls ran (not skipped) and the task succeeded
  (Semaphore tasks 958 normal, 959 check). On Ansible 2.16 templated values in a step result
  render as strings (`key_count: "3"`) and an absent error as `""`; consumers treat `""` and
  `null` alike. `--tags verify` cannot be launched through Semaphore, because
  `setup-templates.yml` sets no template's `allow_override_tags`; tag coverage is proven with
  `--list-tasks --tags verify`, and the orchestrator verifies by dry run.
- **Version note (2026-09-22): production is pinned to v2.19.11**, where the runner moved to
  `services/tasks/local_executor.go`. There `--check`/`--diff` still follow `dry_run`/`diff`
  (lines 512-516) and the task-level `playbook` is still read first (line 423), but a
  task's `git_branch` applies only when the template sets `allow_override_branch_in_task`
  (line 938). The next bullet describes v2.18.12, which local-dev keeps: its pin records that
  v2.19 (then a beta) panicked on the local sqlite store (`bootstrap-local-dev.yml:95-98`), and
  whether 2.19.11 still does is unverified. Local moves only after a local bootstrap on
  2.19.11 starts cleanly; until then local and production differ on branch override.
- **The template's "allow override branch" flag is enforced only in the web UI** (v2.18.12).
  `web/src/components/TaskForm.vue:123` hides the branch field unless the flag is set; the
  API path validates only the branch name's syntax (`db/git_branch.go`,
  `db/Task.go:166-169`) and the runner applies it unconditionally. Any caller with an API
  token and run rights can run any branch that exists on the remote, feature branches
  included. For agents, OPA's branch rule (task 4.5) is therefore the only branch control;
  recorded as `docs/MISTAKES.md` 1.9.
- **Semaphore sees `main` and `dev` only**, through two repository records; `dev_variant:
  true` is set on 35 templates in `templates.yml`, each generating a `(Dev)` twin. Local-dev
  has its own controller and `templates-local.yml` bound to the working tree.
- **skynet's Tier 1 is a Go gateway embedding the Bifrost SDK** (`gateway/go.mod`:
  `maximhq/bifrost/core v1.5.22`) with auth, placement scheduler, a llama-server fleet,
  metrics and OTel. Tier 2 checkpoints with LangGraph's in-memory saver.
- **agentgateway is not on `dev` yet.** Its service, playbooks and tests live on
  `feat/inference-gateway-agentgateway-impl`; its config attaches an `llm` block to a
  `default` gateway (`:4000`) and a `ui` gateway (`:4001`) with a strict key policy.
- **agentgateway v1.5.0 forwards `response_format`.** Its passthrough completions `Request`
  (`crates/llm/src/types/completions.rs:14-55`, tag `v1.5.0`, commit `fe67324`) types a
  fixed set of fields and captures every other field in `#[serde(flatten, default)] rest`,
  so `response_format` survives parse and re-serialisation; other providers read it from
  that map (`conversion/vertex_gemini.rs:725`). Source-level only; the live request is in
  task 3.3 (task 0.5, 2026-09-22).
- **The DGX API is reachable from local-dev's host.** This Mac sits on the controller network
  and `spark-1`'s `/health` on the API port answered 200 from it; SSH to the node also
  answered. The node's address is known to the operator and must stay out of this repo;
  `platform/inventory/local-dev.yml` is gitignored (`.gitignore:71`).
- **`firewall_ssh_cidrs` is the only SSH source list** in `apply-firewall.yml` (required, at
  least one entry, rules added before enable). Nothing names the controller explicitly, and
  whether each host's list contains the controller's source was not checked (site-config).
- **NetBox virtual machines accept custom fields** on the pinned base image
  (`Dockerfile-Plugins`: `netboxcommunity/netbox:v4.5-4.0.0`; source read at `v4.5.10`):
  `VirtualMachine` extends `PrimaryModel` → `NetBoxModel` → `NetBoxFeatureSet`, which
  includes `CustomFieldsMixin` (`netbox/models/__init__.py:25-37`). Fields are created with
  `POST /api/extras/custom-fields/` (`extras/api/urls.py:12`) and bound with `object_types:
  ["virtualization.virtualmachine"]`, parsed as `app_label.model`
  (`netbox/api/fields.py:112`); values are written through the object's `custom_fields` key
  (task 0.4, 2026-09-22).
- **Local NetBox** has a written fix plan (plan 04, "NetBox Local Engine"): app tier under
  podman through the local controller, discovery excluded. `production.yml` in this repo is a
  placeholder template; real ranges are in site-config only.

## Goals / Non-Goals

**Goals:**
- Every registry step runnable three ways from one template: verify (read-only), check
  (dry run of the change), converge (normal run).
- One way for any template to report its result, readable by the collector and the graph.
- skynet driving the workflow end to end on local-dev against real inference before anything
  touches production.

**Non-Goals:**
- Replacing skynet's placement scheduler or local llama-server fleet. They stay skynet's
  concern for skynet-local models; this change only moves the agent-cloud path off Bifrost.
- Converting playbooks to roles. The reference records roles as the Ansible structuring unit;
  migration is not in scope.
- The standalone dashboard service (D4 long term) and production NetBox discovery changes.

## Decisions

### Thread A: agentgateway owns the hostname; skynet is a capability under it

```
                     inference.uhstray.io  (Caddy TLS → agentgateway, agent-cloud)
                     ├── /v1/*        → vLLM on the DGX pair        (model API)
                     └── /skynet/*    → skynet Tier 2 API           (orchestration)
                                          │
                                          └── model calls ──▶ agentgateway /v1
                                              one client key per agent role
```

- agentgateway (agent-cloud) is the single authority for the hostname: keys, budgets, rate
  limits, telemetry, routes. skynet's orchestration API (start a run, run status, eval
  replay) is one more route on the gateway, key-gated like `/v1`.
- skynet's agent-cloud path drops Bifrost: Tier 2 calls agentgateway directly. Each role
  (`infra-agent`, `security-agent`, `o11y-agent`, `service-agent`) plus `skynet-eval` is an
  entry in `agw_clients`, so per-role budgets and attribution come from the gateway's
  existing per-identity accounting instead of new code.
- Bifrost's pre-inference gates (model authorization, cloud-egress DLP) map to the gateway:
  per-identity `allowedModels` covers model authorization; there is no cloud-bound provider
  on this path, so egress DLP has nothing to guard here.
- Rejected: skynet Tier 1 as a provider behind the gateway (two gateways in series, two key
  systems, the operator's explicit call against Bifrost); skynet serving `/v1` on its own
  hostname (splits authority for the inference plane).

### Thread B and E: Ansible check mode is the dry run; `set_stats` is the result

- **Dry run = `--check`.** No bespoke variables. Three task patterns cover every playbook:
  read-only probes (`uri` GET, `command` that only reads, `stat`) set `check_mode: false`;
  state-changing `command`/`shell`/`uri` that cannot simulate carry
  `when: not ansible_check_mode`; modules with native support (`template`, `copy`,
  `lineinfile`, `authorized_key`) need nothing. Diff-sensitive templates set `diff: false`
  (official guidance, because diff mode can reveal secrets).
- **Verify = `--tags verify`.** Each state-changing playbook tags its verification block
  `verify`; those tasks are read-only by construction and `check_mode: false`, so
  `--tags verify` and `--check` both prove state without changing it. Snapshot templates are
  verify-only playbooks.
- **Result = `ansible.builtin.set_stats`** through one shared task
  (`tasks/emit-step-result.yml`), with `ANSIBLE_SHOW_CUSTOM_STATS=true` set in the controller
  environment. The collector parses the `CUSTOM STATS` block from task output; it is JSON and
  survives the callback unchanged. This replaces plan 15's `STEP-RESULT` debug line.
- **Legacy `dry_run`**: the three playbooks keep accepting it for one release by treating
  `dry_run | bool or ansible_check_mode` as the dry-run condition and printing a deprecation
  notice.
- **Enforcement**: a pytest over the playbook YAML flags unguarded writes and probes without
  check mode (spec: "The check-mode contract is enforced mechanically"). ansible-lint stays
  the lint gate; a custom ansible-lint rule was rejected because the repo already runs its
  structural guards as pytest/BATS and the rule would need a second rule-loading path.
- **Standards reference**: `plan/architecture/08-ansible-automation-standards.md`, written
  from the official pages fetched 2026-09-22 (check mode and diff mode, variables, inventory,
  error handling, roles, tips and tricks, sample setup, ansible-lint, `set_stats`, `uri`).
- Rejected: base64 payload after a marker (non-standard, unreadable in the Semaphore UI);
  the JSON callback for all runs (changes every operator's output format).

### Schemas and registry live together, outside the policy tree

The proposal schemas sit in `platform/workflows/service-onboarding/schemas/` beside the
registry. The first placement, `policies/agentcloud/schemas/`, broke OPA: `opa test` loads
every JSON file under the policy directory as data and failed with `merge error` on the
schema files (OPA 1.0.0, 2026-09-22), and the deployed service mounts the same directory.

### Thread C: skynet on local-dev against the DGX vLLM

- A `skynet_svc` local-dev group and `deploy-skynet.yml` in agent-cloud, composed from the
  shared tasks (place source, manage-secrets, deploy script, verify). Local-dev mounts the
  skynet working tree the same way local templates mount agent-cloud's; the production clone
  path (private repo, deploy key) is designed but not exercised in this change.
- The local agentgateway's upstream moves from LM Studio to the DGX API. The address and the
  API key are local-only: the address in `local-dev.yml`, the key seeded into the local
  OpenBao at `secret/services/agentgateway:vllm_api_key` with `Seed OpenBao Key`, never on an
  argv or in a committed file.
- Durable checkpointer: skynet replaces the in-memory saver with a Postgres-backed one, using
  a small Postgres in the skynet compose (the gateway's budget database is not shared: one
  service, one database). Package choice is a skynet-repo task.
- Reachability from inside the local controller's container is a separate fact from the host
  probe above; task 3.1 proves it before anything depends on it.

### Thread D: SSH from the orchestrator is a protected rule

- Correction to the explore note: SSH is required, from the controller. The inventory gains
  `firewall_controller_cidr` (the Semaphore host's source) alongside `firewall_ssh_cidrs`;
  `apply-firewall.yml` asserts the controller source is in the SSH allow set before enabling.
- OPA denies a firewall proposal that omits the controller source for port 22, or allows 22
  from anything outside `firewall_ssh_cidrs` (spec: "Proposal content is policed"). The
  snapshot carries both lists so the model and the rule see the same facts.

### Thread F: simplify Semaphore without losing the boundaries

Three boundaries matter: production vs local-dev, `main` vs `dev`, and which agent may run
what. The current model encodes the second one by duplicating 35 templates.

| Option | Boundary for main vs dev | Verdict |
|---|---|---|
| **Branch at launch, one catalog (chosen)** | Task-level `git_branch` in the launch body, OPA checks it | Halves the catalog; one registry name per step; the branch is visible and policed per task |
| Keep `(Dev)` twins | A second template per playbook | Doubles names the registry and OPA must track; the recorded gap where a twin ran the `main` tree persists |
| Separate Semaphore projects for main and dev | Project | Doubles inventories, keys and environments |

- Production vs local-dev stays two controllers (already true), and the registry names base
  templates only; skynet's configuration carries the controller URL and project per
  environment.
- On v2.19.11 a task-level branch needs `allow_override_branch_in_task: true` on the template,
  so branch-at-launch means `setup-templates.yml` sets that flag on every workflow template.
  The flag lets a caller name ANY pushed branch, so OPA's branch rule is what keeps agents
  on `main` or `dev`. The twins go after one live `dev` launch by branch confirms it; until
  then both coexist.

### Thread G: local NetBox, discovery on local-dev only

- Follow plan 04's local engine fix (app tier under podman via the local controller) and add
  discovery back with a fail-closed allowlist: the deploy reads the local podman networks'
  subnets from the live engine and refuses any declared target outside them. The allowlist is
  observed, not declared, because production ranges exist only in site-config.
- The privileged orb-agent runs on the local controller's rootful podman socket; if that
  cannot grant the capabilities discovery needs, discovery stays disabled and the deploy says
  so (spec scenario "No targets means no discovery" covers the disabled path).
- The NetBox custom fields the collector writes are created by a playbook, locally first.

### Tracking and rollout

Unchanged from plan 15 D4 and D5, with two refinements: the collector reads `CUSTOM STATS`
instead of a marker line, and it tolerates an absent NetBox by recording the write failure
and continuing with Loki (spec scenario "NetBox outage does not block deployment").

## Risks / Trade-offs

- [Check-mode retrofit of 110 playbooks introduces regressions in normal runs] → edits are
  `check_mode`/`when` guards that are inert outside check mode; each wave runs the playbook
  normally and with `--check` on local-dev before merge; the pytest guard catches omissions.
- [Semaphore does not honour a task-level branch] → twins stay; the registry resolves the
  `(Dev)` name by suffix; only Thread F's simplification is lost.
- [Semaphore's `dry_run` field does not map to `--check`] → templates add `--check` through
  the launch arguments instead; recorded by task 0.2.
- [OPA content rules drift from the proposal schemas] → rego tests use the recorded eval
  proposals as fixtures, so a schema change without a rule change fails CI.
- [Local controller container cannot reach the DGX] → the gateway upstream falls back to the
  public edge for local-dev runs, which is subject to the edge rate limit; recorded as a
  degraded mode, not the design.
- [Removing OPA's human gate on reasoning steps] → content rules plus template allowlists
  bound what an approved proposal can do; the destructive-template gate is unchanged.

## Migration Plan

1. Land in order: standards reference and check-mode patterns, registry and contracts, OPA,
   Semaphore branch spike, local NetBox, skynet on local-dev, tracking, backfill, sweep,
   pilot (tasks.md sections 1 to 10).
2. Production is untouched until section 8 (agentgateway backfill); everything before runs on
   local-dev through the local controller.
3. Rollback per piece is in `proposal.md` §Rollback Plan.

## Open Questions

- Which Postgres checkpointer package skynet adopts (skynet-repo choice; no spec impact).
- The greenfield pilot service (operator picks in section 10).
