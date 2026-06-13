# Local Development

Laptop-resident agent-cloud per `plan/development/LOCAL-DEV-DEPLOYMENT.md`:
**make bootstraps, Semaphore operates.** The bootstrap stands up a local
control plane; everything after runs through local Semaphore templates using
the unchanged composable playbooks, with AppRole credential injection exactly
as in production. No real credentials ever exist on the machine — every
generated value carries the `LOCAL_FAKE_` prefix.

## Quickstart (current state — Phase 0B)

```bash
brew bundle                                  # toolchain (Brewfile)
podman machine start                         # if not already running
ansible-playbook platform/playbooks/bootstrap-local-dev.yml --tags bootstrap
```

The bootstrap is idempotent (safe after a podman-machine reset) and provisions:

1. **OpenBao** (dev mode, `127.0.0.1:8200`) — AppRole auth, `local-semaphore`
   policy + role, `LOCAL_FAKE_` seed secrets for local service groups
2. **Semaphore** (`127.0.0.1:3000`, single container, SQLite) — admin
   `localadmin` / `LOCAL_FAKE_semaphore_admin`, **API token created
   automatically**, project/key/repository/inventory/environment provisioned
3. **Templates-as-code** — `setup-templates.yml` registers the full shared
   catalog plus `templates-local.yml` against the local instance

State lands in `~/.agent-cloud-local/credentials.env` (0600, outside the repo).

## Driving the local Semaphore

```bash
set -a; source ~/.agent-cloud-local/credentials.env; set +a

# list templates
curl -s -H "Authorization: Bearer $SEMAPHORE_TOKEN" \
  "$SEMAPHORE_URL/api/project/$SEMAPHORE_PROJECT_ID/templates" | jq '.[].name'

# run one (example: Check Secrets against the uhhcraft_svc group)
curl -s -X POST -H "Authorization: Bearer $SEMAPHORE_TOKEN" -H "Content-Type: application/json" \
  -d '{"template_id": <id>, "project_id": 1, "environment": "{\"target_service\": \"uhhcraft_svc\"}"}' \
  "$SEMAPHORE_URL/api/project/$SEMAPHORE_PROJECT_ID/tasks"
```

`make` targets wrapping this flow (`local-init`, `local-bootstrap`,
`local-deploy-<service>`, `local-validate`, `local-clean`, `promote`) are the
next Phase 0B increment.

## Port map (registry of record)

| Service | Local port | Notes |
|---|---|---|
| OpenBao (dev) | 127.0.0.1:8200 | containers reach it at `http://local-openbao:8200` on the `local-dev` network |
| Semaphore | 127.0.0.1:3000 | prod-typical port |
| UhhCraft | 127.0.0.1:3001 | shifted from 3000 via `${UHHCRAFT_PORT:-3001}` |
| NocoDB (P2) | 127.0.0.1:8081 | |
| n8n (P2) | 127.0.0.1:5678 | |
| NetBox (P2, Docker Desktop) | 127.0.0.1:8000 | app tier only — no orb-agent/discovery locally |

## Engine split

podman machine is the default engine (prod's default); Docker Desktop is used
**only** for root-requiring services (today: the NetBox app profile). Both VMs
should not run heavy workloads simultaneously on small machines — see the
reference-machine allocations in the plan (§5).

## Known facts & decisions discovered in bootstrap bring-up

- **Semaphore image pin:** `semaphoreui/semaphore:v2.18.12-ansible2.16.5`.
  `latest` (v2.19 beta) **and** the `bolt` dialect both panic
  (`unknown store type`, pro Terraform-store factory) — the supported embedded
  store is **SQLite**. The `-ansible` variant ships ansible for task execution.
- Semaphore auto-installs `collections/requirements.yml` from the cloned repo
  per task; the bootstrap additionally installs `hvac` +
  `community.hashi_vault` in the container for `hashi_vault` lookups.
- Templates API (≥ v2.18) requires integer ids — `setup-templates.yml`
  serializes its body inside Jinja (`to_json`) to keep native types.
- `check-secrets.yml` still carries `no_log: true` (predates the no-`no_log`
  standard) — cleanup candidate when next touched.

## Triage

| Symptom | Check |
|---|---|
| Bootstrap fails at "Assert podman machine" | `podman machine start` |
| Semaphore container exits (2) | `podman logs local-semaphore` — dialect/image regression; keep the pinned tag |
| Task fails at OpenBao auth | Re-run bootstrap (regenerates AppRole secret-id + environment) |
| Task: "no hosts matched" | The static inventory in Semaphore is managed by bootstrap — re-run it; don't hand-edit |
| Full reset | `podman rm -f local-openbao local-semaphore && podman volume rm local-semaphore-data` then re-bootstrap |
