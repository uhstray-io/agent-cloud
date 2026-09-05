# Local Development

Laptop-resident agent-cloud per `plan/development/00-foundation-local-dev.md`:
**make bootstraps, Semaphore operates.** The bootstrap stands up a local
control plane; supported service profiles use local Semaphore templates and
OpenBao AppRole injection. Fixture seeds use `LOCAL_FAKE_`, while generated local
credentials are real local secrets. Do not import production service credentials.
The legacy NetBox app-only helper bypasses Semaphore and is not proof of the full
production discovery path.

## Quickstart

```bash
brew bundle                                  # toolchain (Brewfile; podman-compose + jq required)
podman machine start                         # if not already running
make local-bootstrap                         # secure foundation + local Semaphore
make local-dns-resolver                      # Mac resolver (sudo)
make local-tls-trust                         # local CA trust (sudo)
```

`make local-all` attempts a fixed subset after bootstrap: o11y, OPA, ERPNext,
the legacy direct NetBox app helper, and best-effort n8n, then Mac DNS/TLS wiring.
It does not deploy the full service catalog or fail when n8n alone fails. Use
`make local-deploy-<service>` for supported Semaphore profiles and
`make local-validate` for current checks. NetBox production discovery is the
current validation target; local Docker setup for NetBox is not established.

dns/step-ca/caddy/authentik are no longer separate bring-up steps — genesis owns them.

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

1. **OpenBao** (persistent file backend, `127.0.0.1:8200`) — AppRole auth, `local-semaphore`
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

Use the existing worktree-bound dispatcher instead of hand-building task requests:

```bash
./scripts/local-dev.sh run check-secrets '{"target_service":"uhhcraft_svc"}'
make local-deploy-n8n
make local-validate
```

It resolves the registered template by playbook name, passes explicit extra vars,
waits for completion and returns the task result. Operator state lives outside
the repository; do not print or copy its credential file into documentation.

The `make` targets (`local-init`, `local-bootstrap`, `local-deploy-<service>`,
`local-validate`, `local-clean`, `promote`) wrap this flow via
`scripts/local-dev.sh`, which also enforces the local-only guard (refuses
non-local inventories and non-local `openbao_addr`).

## Local port reference

Inventory and compose declarations own these values; this table is not a live
health inventory. Check the rendered configuration for the selected environment.

| Service | Local port | Notes |
|---|---|---|
| OpenBao (persistent) | 127.0.0.1:8200 | containers reach it at `http://local-openbao:8200` on the `local-dev` network |
| Semaphore | 127.0.0.1:3000 | prod-typical port |
| UhhCraft | 127.0.0.1:3001 | shifted from 3000 via `${UHHCRAFT_PORT:-3001}` |
| n8n (P2) | 127.0.0.1:5678 | |
| NocoDB (P2) | 127.0.0.1:8181 | compose default (`8181:8080`); its Postgres maps 5433 |
| NetBox | 127.0.0.1:8000 | App-only Podman helper exists; no current local Docker validation. Do not feed containers directly into IPAM as VMs |
| Postiz (P2) | 127.0.0.1:5001 | shifted — macOS AirPlay Receiver squats :5000 |
| hickory-dns | 127.0.0.1:5300 | declared profile; authoritative for `*.agent-cloud.test`; udp+tcp → :53 in-container; `make local-dns-resolver` points `/etc/resolver/<zone>` here |
| step-ca | 127.0.0.1:9000 | declared profile; internal CA (stable root in `step-ca-data`); Caddy reaches `step-ca:9000` on `local-dev`; issues the `*.agent-cloud.test` wildcard. `make local-deploy-step-ca` |
| Caddy | 127.0.0.1:8088 / 8443 | declared profile; serves the step-ca `*.agent-cloud.test` wildcard, reverse-proxies the control plane by name. `:8443` by default; `make local-https` adds a persistent root forwarder for clean port-free `https://semaphore.agent-cloud.test` (443→8443, 80→8088) |
| Authentik | 127.0.0.1:9300 | declared profile; central IdP/SSO (server+worker+Postgres+Redis). Container `:9000` (step-ca owns host `:9000` → debug maps to `:9300`); Caddy reaches `authentik-server:9000` on `local-dev`. `make local-deploy-authentik`. Gates Grafana (OIDC) + NetBox (forward_auth) — see SSO section below |
| ERPNext (P4) | 127.0.0.1:8080 | frontend; slim tier |
| OPA | 127.0.0.1:8281 | declared profile; Guardrail-layer policy engine. Agents/control-plane reach it as `opa:8181` on local-dev; host diagnostics on 8281 (8181 is NocoDB's bind). Policy-as-code in `policies/`; `make local-deploy-opa`. Phase 1 unauthenticated (returns decisions, not secrets) |
| o11y | 3002 / 9090 / 3100 | declared profile; grafana / prometheus / loki / alloy. Grafana behind Caddy at `grafana.agent-cloud.test` with Authentik OIDC. `make local-deploy-o11y` |

## Engine split

The local control plane uses Podman, with the VM's **rootful** engine socket
mounted into Semaphore. Production defaults to rootless Podman for most services;
NetBox's full-stack path defaults to Docker, while a separate local app-only
Podman script exists. Neither app-only success nor the engine override validates
the production privileged-discovery path.

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
- `no_log: true` belongs on credential-bearing steps. It is appropriate for
  secret fetches; health checks and sanitized result summaries should stay visible.
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
- **Cert warning (`NET::ERR_CERT_AUTHORITY_INVALID`) → `make local-tls-trust`.**
  Caddy serves `*.agent-cloud.test` from a step-ca-minted wildcard leaf (when
  `caddy_tls_cert` is set; else it falls back to Caddy's own internal CA).
  `make local-tls-trust` (sudo, idempotent) extracts the internal-CA root —
  step-ca's STABLE shared root (`/home/step/certs/root_ca.crt`) when step-ca is
  up, else Caddy's local root (`/data/caddy/pki/authorities/local/root.crt`) —
  and trusts it in the macOS System keychain by fingerprint (root CNs are
  year-stamped + rotate, so trust/untrust key on the SHA-1, not the name).
  `make local-tls-untrust` reverses it. The step-ca root is stable across Caddy
  redeploys; only a `step-ca-data` volume wipe ⇒ new root ⇒ re-run.
  Plan: `LOCAL-DEV-TLS-TRUST.md`.
- **Clean `:443` needs a privileged forwarder.** macOS requires root to bind
  ports <1024 and has no `ip_unprivileged_port_start` equivalent; podman-machine's
  forwarder (gvproxy) is non-root, so local Caddy can only publish `8443`/`8088`.
  `make local-https` installs a persistent root LaunchDaemon (`socat`) that
  forwards `443→8443` + `80→8088` — the one privileged hop for port-free URLs,
  idempotent + reboot-persistent. Default (`:8443`) needs none of it.
- **`ghcr.io/uhstray-io/uhhcraft` is private** — anonymous pulls 403 before
  the arch question is even observable. Local deploys of owned images need a
  `read:packages` PAT (or a local build override); backing images
  (postgres/redis/minio) pull fine through the mounted socket.

## SSO (Authentik)

Authentik is the central IdP. The local configuration declares these integrations:

- **Grafana — OIDC.** Grafana's `generic_oauth` redirects to Authentik; the login page shows an **Authentik** button.
- **NetBox + OpenBao — forward_auth.** Caddy authenticates each request against Authentik's embedded outpost and injects `X-authentik-*` identity headers. NetBox trusts them via `REMOTE_AUTH_*`; for OpenBao it is an edge gate in addition to OpenBao's native OIDC login. Do not equate reaching the UI with authenticating to the vault. The internal control-plane path to OpenBao (`local-openbao:8200`) is **ungated**, so Semaphore/Ansible are unaffected.

**Admission and application authorization are separate.** The rendered
`zz-sso-bindings.yaml.j2` policy admits Authentik superusers and members of
`platform-admins`, `platform-developers`, or `platform-business` to **member-tier**
apps. `platform-user` alone is denied. The `openbao-oidc` app is **admin-tier**:
only Authentik superusers or `platform-admins` pass its native-login gate.

The following expectations describe the checked-in local integrations, assuming
fresh accounts with no additional group memberships or application grants. They
are not live validation results:

| Identity | Grafana OIDC | NetBox local header auth | OpenBao UI / native OIDC |
|---|---|---|---|
| `platform-admins` | Org Admin | superuser + staff | reach UI / `platform-admin` policy |
| `platform-developers` | Editor | view-only via the helper's declared ObjectPermission | reach UI / native login denied |
| `platform-business` | Viewer | passes edge gate; no object permissions granted by this helper | reach UI / native login denied |
| `platform-user` only | denied at IdP | denied at edge | denied at edge / native login denied |
| Authentik superuser `akadmin`, without an allowed group | passes IdP, rejected by Grafana's allowed-groups check | passes edge; no NetBox superuser/staff or object grants from these declarations | reach UI / `platform-admin` policy |

Grafana independently checks its `groups` claim against `ALLOWED_GROUPS` and maps
admins/developers/business to Admin/Editor/Viewer. Authentik superuser status is
not a substitute for that claim. NetBox's local overlay maps only
`platform-admins` to superuser/staff; the legacy helper grants view permissions to
`platform-developers`. Existing NetBox permissions may change observed access.
OpenBao's local OIDC role grants `platform-admin` to every successful native
login, making the separate admin-tier IdP gate essential.

Sources: [SSO policy template](../platform/services/authentik/deployment/templates/zz-sso-bindings.yaml.j2),
[app catalog](../platform/services/authentik/deployment/app-catalog.yml),
[Grafana env template](../platform/services/o11y/deployment/templates/env.j2),
[NetBox auth overlay](../platform/services/netbox/deployment/docker-compose.local-auth.yml),
[legacy permission helper](../scripts/local-netbox-up.sh), and
[OpenBao genesis configuration](../platform/playbooks/bootstrap-local-dev.yml).
Declare users and memberships in inventory and apply the Authentik blueprints
through the deployment workflow.

**Deploy order** (each idempotent, through local Semaphore): `make local-deploy-authentik` → `local-deploy-caddy` (renders forward_auth routes) → `local-deploy-o11y` (Grafana OIDC). `make local-smoke` checks the configured gates. The separate `make local-netbox` helper is a legacy direct app-tier path, not a Semaphore-orchestrated production-discovery validation.

**Browser verification** (for configured integrations, after DNS/TLS wiring):

1. Declare test identities for the four groups above through inventory/blueprints;
   use isolated browser sessions to prevent an existing login masking admission.
2. Check both admission and effective application permissions against the table,
   including business access to Grafana and denial of business/developer native
   OpenBao login. Record any pre-existing application grants separately.
3. Test `akadmin` separately against each of these integrations. Expect Grafana's
   group rejection, no group-derived NetBox elevation, and OpenBao native admin
   access under the current local declaration. Do not infer access to other apps
   from these results; consult each enabled app's policy and role mapping.

NetBox checks apply only if that legacy local app profile is configured; they do
not establish production discovery readiness.

## Triage

| Symptom | Check |
|---|---|
| Bootstrap fails at "Assert podman machine" | `podman machine start` |
| NetBox/Grafana login redirect points at `0.0.0.0:9000` | Embedded outpost `authentik_host` unset — re-run `make local-deploy-authentik` (blueprint sets it) |
| NetBox SSO not gating (loads without login) | `make local-deploy-caddy` (renders the forward_auth route from inventory); confirm `caddy_routes` netbox entry has `forward_auth` |
| Semaphore container exits (2) | `podman logs local-semaphore` — dialect/image regression; keep the pinned tag |
| Task fails at OpenBao auth | Re-run bootstrap (regenerates AppRole secret-id + environment) |
| Task: "no hosts matched" | The static inventory in Semaphore is managed by bootstrap — re-run it; don't hand-edit |
| Control-plane reset | `make local-clean` is destructive: it removes vault/orchestrator data and tool state while other service volumes remain. Back up and plan matching service recovery before use |
