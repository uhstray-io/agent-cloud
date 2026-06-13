# Local Development

Laptop-resident agent-cloud per `plan/development/LOCAL-DEV-DEPLOYMENT.md`:
**make bootstraps, Semaphore operates.** The bootstrap stands up a local
control plane; everything after runs through local Semaphore templates using
the unchanged composable playbooks, with AppRole credential injection exactly
as in production. No real credentials ever exist on the machine — every
generated value carries the `LOCAL_FAKE_` prefix.

## Quickstart (current state — Phase 1)

```bash
brew bundle                                  # toolchain (Brewfile)
podman machine start                         # if not already running
make local-bootstrap                         # control plane up + configured
make local-deploy-dns                        # local DNS (fully working today)
make local-dns-resolver                      # opt-in: macOS /etc/resolver (sudo)
make local-deploy-<service>                  # e.g. local-deploy-uhhcraft
make local-validate
```

`make local-deploy-dns` is the **reference working deploy** — it runs entirely
through the local Semaphore, renders the zone + config from inventory vars,
starts hickory-dns, and verifies resolution with `dig` (wildcard answer +
forwarded external name). `make local-dns` does that *and* wires the host
resolver in one step.

### Host name resolution is repeatable, not a one-off

Two things make `*.<zone>` resolve **natively** on the Mac (so `curl
http://openbao.<zone>:8200` works without `--resolve`), and both are
idempotent — safe to re-run on any machine, any time:

1. **`make local-dns-resolver`** writes `/etc/resolver/<zone>` →
   `127.0.0.1:5300`. It reads the zone/port from the inventory, **no-ops when
   the file is already correct** (no needless sudo), accepts `--yes` /
   `ASSUME_YES=1` for scripting, warns if local DNS isn't up yet, and verifies
   the system resolver afterward via `dscacheutil`.
2. **`REFRESH=1 make local-init`** regenerates the gitignored working inventory
   from the committed example. Plain `make local-init` *warns* when the example
   has gained a service group your working copy lacks (e.g. `dns_svc`) — that
   drift is why a resolver run can't find the zone — and points you here.

**Why this needs sudo and can't go through Semaphore:** `/etc/resolver` is a
macOS *host* file outside the podman VM. Semaphore runs in a container in the
VM and can't touch it, so resolver wiring is a *host-bootstrap* step (make's
job, like `brew bundle`) — the sudo is intrinsic, not a gap. Once written, the
file persists across reboots; the DNS container restarts itself and re-publishes
`5300`, so resolution keeps working without re-running anything.

**Teardown:** `make local-clean` leaves `/etc/resolver/<zone>` in place (it
needs sudo to remove). Drop it with `sudo rm /etc/resolver/<zone>` if you stop
using local DNS, or it will add a failed lookup for that zone once DNS is gone.

This DNS path is for **Mac-host / developer** access. Container-to-container
traffic uses podman's own network DNS (`local-openbao:8200` on the `local-dev`
network) — containers don't query hickory and don't need to.

The bootstrap is idempotent (safe after a podman-machine reset) and provisions:

1. **OpenBao** (dev mode, `127.0.0.1:8200`) — AppRole auth, `local-semaphore`
   policy + role, `LOCAL_FAKE_` seed secrets for local service groups
2. **Semaphore** (`127.0.0.1:3000`, single container, SQLite) — admin
   `localadmin` / `LOCAL_FAKE_semaphore_admin`, **API token created
   automatically**, project/key/repository/inventory/environment provisioned
3. **Templates-as-code** — `setup-templates.yml` registers the full shared
   catalog plus `templates-local.yml` against the local instance
4. **Engine + working-tree + shared-deploy wiring** — three mounts make the
   socket model work: the VM's rootful podman socket (deploys drive the real
   engine), the **repo working tree** at `/workspace/agent-cloud` (the
   Semaphore repository is a local path, so tasks run your *uncommitted*
   changes), and a **shared deploy dir** `/var/lib/agent-cloud-deploy` mounted
   at the *same absolute path* in the container and on the VM (see "host
   bind-mounts" below)

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

The `make` targets (`local-init`, `local-bootstrap`, `local-deploy-<service>`,
`local-validate`, `local-clean`, `promote`) wrap this flow via
`scripts/local-dev.sh`, which also enforces the local-only guard (refuses
non-local inventories and non-local `openbao_addr`).

## Port map (registry of record)

| Service | Local port | Notes |
|---|---|---|
| OpenBao (dev) | 127.0.0.1:8200 | containers reach it at `http://local-openbao:8200` on the `local-dev` network |
| Semaphore | 127.0.0.1:3000 | prod-typical port |
| UhhCraft | 127.0.0.1:3001 | shifted from 3000 via `${UHHCRAFT_PORT:-3001}` |
| n8n (P2) | 127.0.0.1:5678 | |
| NocoDB (P2) | 127.0.0.1:8181 | compose default (`8181:8080`); its Postgres maps 5433 |
| NetBox (P2, Docker Desktop) | 127.0.0.1:8000 | app tier only — no orb-agent/discovery locally |
| Postiz (P2) | 127.0.0.1:5001 | shifted — macOS AirPlay Receiver squats :5000 |
| hickory-dns | 127.0.0.1:5300 | **deployed + working**; udp+tcp → :53 in-container; `make local-dns-resolver` points `/etc/resolver/<zone>` here |
| Caddy local (P4) | 127.0.0.1:8088 / 8443 | local Caddyfile variant, internal CA |
| ERPNext (P4) | 127.0.0.1:8080 | frontend; slim tier |
| OPA (P4) | 127.0.0.1:8281 | 8181 is NocoDB's local bind; diagnostics 8282 stays internal |
| o11y (reserved) | 3002 / 9090 / 3100 | grafana / prometheus / loki — stack still a stub |

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
- **Working-tree repository:** the Semaphore repository's `git_url` is the
  absolute path `/workspace/agent-cloud` (the bind-mounted working tree) — a
  URL would make every task silently test GitHub `main` instead of your
  uncommitted changes. Local-mode plays **copy** the workspace with
  `tar --exclude .git` (not `git clone`, which only sees committed state; not
  `cp -a`, which fails trying to preserve the host-uid ownership of the
  virtiofs mount).
- **Engine socket:** `/run/podman/podman.sock` (VM, rootful) is mounted into
  the Semaphore container with `--security-opt label=disable` — the podman
  machine VM enforces SELinux, which otherwise denies the cross-container
  socket even to root. `CONTAINER_HOST` in the Semaphore environment points
  podman/podman-compose at it.
- **Host bind-mounts (config files) need a same-path shared dir.** podman-compose
  runs *inside* the Semaphore container, but a `./config`-style bind-mount source
  is resolved on the **VM engine**, which can't see the container's private
  filesystem (`statfs ... no such file or directory`). Fix: `/var/lib/agent-cloud-deploy`
  is mounted into Semaphore at the *same absolute path* it has on the VM, and
  local-mode deploys copy the working tree there (not `~/agent-cloud`) — so the
  compose project dir is identical on both sides and host mounts resolve. The DNS
  service is the first to need this (its zone files); services using only named
  volumes + `env_file` (uhhcraft) don't. Containers reading those mounts also need
  `security_opt: [label=disable]` (SELinux) — in `compose.local.yml`, never prod.
- **`ansible_user` must be defined in local inventories** even with
  `ansible_connection=local`: playbook defaults like
  `local_monorepo_dir | default('/home/' ~ ansible_user)` fail on undefined
  `ansible_user` *even when the left side is set* — Jinja evaluates filter
  arguments eagerly.
- **`ghcr.io/uhstray-io/uhhcraft` is private** — anonymous pulls 403 before
  the arch question is even observable. Local deploys of owned images need a
  `read:packages` PAT (or a local build override); backing images
  (postgres/redis/minio) pull fine through the mounted socket.

## Triage

| Symptom | Check |
|---|---|
| Bootstrap fails at "Assert podman machine" | `podman machine start` |
| Semaphore container exits (2) | `podman logs local-semaphore` — dialect/image regression; keep the pinned tag |
| Task fails at OpenBao auth | Re-run bootstrap (regenerates AppRole secret-id + environment) |
| Task: "no hosts matched" | The static inventory in Semaphore is managed by bootstrap — re-run it; don't hand-edit |
| Full reset | `podman rm -f local-openbao local-semaphore && podman volume rm local-semaphore-data` then re-bootstrap |
