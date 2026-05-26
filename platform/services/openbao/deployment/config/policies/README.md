# OpenBao policies

Source of truth for every OpenBao ACL policy in the platform. **Never modify policies via the API or UI** — edit the `.hcl` here, then run the corresponding `apply-policy-<name>.yml` playbook (or `apply-openbao-policies.yml` to apply all at once).

This directory is read by:

- `platform/playbooks/apply-openbao-policies.yml` — walks every `*.hcl` and applies via the OpenBao API.
- `platform/playbooks/apply-policy-<name>.yml` — wrappers that apply a single policy (one per file in this directory).

## Naming convention

```text
<service>[-<scope>].hcl
```

- `<service>` is the service name as it appears under `secret/services/<service>` (e.g., `uhhcraft`, `nemoclaw`, `semaphore`).
- `<scope>` is optional. Common values: `read`, `write`, `rotate`. Omit when the policy is read-only and there's only one policy per service (e.g., `uhhcraft.hcl`).
- The filename minus `.hcl` becomes the policy name in OpenBao.

## Current policies

| File | Status | Used by | Purpose |
|------|--------|---------|---------|
| `semaphore-read.hcl` | Active | Semaphore orchestrator AppRole | Read every `secret/data/services/*` + manage AppRoles + manage policies |
| `semaphore-write.hcl` | Active | Semaphore VM | Write its own credentials back to `secret/services/semaphore` |
| `orb-agent.hcl` | Active | NetBox orb-agent AppRole | Read NetBox + discovery credentials |
| `nemoclaw-read.hcl` | Active | NemoClaw agent AppRole | Read service credentials NemoClaw needs |
| `nemoclaw-rotate.hcl` | Active | NemoClaw rotation flows | Rotate specific secrets |
| `nocodb-write.hcl` | Active | NocoDB deploy.sh | Push runtime-generated API tokens back |
| `n8n-write.hcl` | Active | n8n deploy.sh | Push runtime-generated API keys back |
| `uhhcraft.hcl` | **Reserved** | (none yet) | UhhCraft read-only, awaiting AppRole if needed |
| `inference-comfyui.hcl` | **Reserved** | (none yet) | ComfyUI sidecar read-only, awaiting AppRole if needed |
| `inference-hunyuan3d.hcl` | **Reserved** | (none yet) | Hunyuan3D sidecar read-only, awaiting AppRole if needed |

## Reserved vs. Active

A policy is **Active** when at least one OpenBao AppRole or token role references it. A policy is **Reserved** when the `.hcl` exists but no AppRole is bound to it.

Reserved policies are useful as:

1. **Scope documentation.** Reviewers can see what a service _would_ read without grepping deploy code.
2. **Future-proofing.** If runtime OpenBao access becomes necessary (token rotation, dynamic credentials), the scope is already vetted and tested.
3. **Deployment-time access.** Even without an AppRole, Ansible (running with Semaphore's wildcard policy) writes secrets at the paths these policies cover, so the paths must already exist conceptually.

To **activate** a reserved policy: provision an AppRole via `tasks/manage-approle.yml` with the policy attached. Document the activation in the policy's header comment.

## `secret/services/*` path conventions

- `secret/services/<svc>` — the master KV entry for that service. Compose env-vars, API tokens, DB passwords, SMTP creds.
- `secret/services/<svc>/<sub>` — optional sub-paths when one service has logically separate secrets (e.g., `uhhcraft/stripe`, `uhhcraft/database`). Use only when rotation cadences differ or when scoping requires it.
- `secret/services/ssh/<svc>` — per-service SSH keypair (private + public).
- `secret/services/approles/<svc>` — `role_id` + `secret_id` for the service's AppRole. Bound to its policy (`<svc>.hcl` or `<svc>-read.hcl`).

## When to add an `<svc>-write.hcl`

Add a write-scoped policy when the service itself generates a credential at deploy time and needs to push it back. Examples: `nocodb-write.hcl` (API tokens minted by NocoDB on first boot), `n8n-write.hcl` (same pattern).

If Ansible generates the credential (the common case via `tasks/manage-secrets.yml`), the write happens with Semaphore's policy, and no service-specific write policy is needed.

## Editing a policy

```bash
# 1. Edit the .hcl
$EDITOR platform/services/openbao/deployment/config/policies/<name>.hcl

# 2. Apply (single)
ansible-playbook platform/playbooks/apply-policy-<name>.yml

# Or apply all
ansible-playbook platform/playbooks/apply-openbao-policies.yml

# 3. Commit
git add platform/services/openbao/deployment/config/policies/<name>.hcl
git commit -m "security(openbao): <what changed in the policy>"
```

Per root [`CLAUDE.md`](../../../../../../CLAUDE.md), this is the **only** way to change a policy. Ad-hoc API calls and UI edits are not allowed.

## Related

- Root [`CLAUDE.md`](../../../../../../CLAUDE.md) — "Policy and Configuration Changes — Code Only" rule.
- [`plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md`](../../../../../../plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md) — secret generation, rotation, retirement.
- [`platform/playbooks/tasks/apply-openbao-policy.yml`](../../../../../playbooks/tasks/apply-openbao-policy.yml) — the API-side mechanics.
- [`platform/playbooks/tasks/manage-approle.yml`](../../../../../playbooks/tasks/manage-approle.yml) — provision an AppRole bound to a policy (what activating a Reserved policy looks like).
