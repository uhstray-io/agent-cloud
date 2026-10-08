# step-ca service — architecture (context for agents)

Smallstep `step-ca` as the platform's **internal** CA. Read with the root
[`CLAUDE.md`](../../../../CLAUDE.md) and the plan it implements:
[`plan/development/00-foundation-local-dev.md`](../../../../plan/development/00-foundation-local-dev.md).

## What it is — and is NOT

- **IS:** the internal CA for `*.agent-cloud.test` / internal zones. A stable root +
  intermediate, auto-initialized on first boot and persisted in the
  `step-ca-data` volume (the win over Caddy's ephemeral `local_certs` root —
  this root survives Caddy redeploys and is shareable across hosts/devs).
- **IS NOT:** the public CA. Public/customer TLS is Caddy automatic-HTTPS +
  Let's Encrypt (`plan/architecture/05-platform-infra.md` → TLS strategy).
  Operating a public CA is out of scope — a separate trust domain entirely.

## Trust model (the unavoidable client step)

A browser trusts a cert only if its CA root is in the client's trust store.
step-ca's root must be trusted **once per client** (`make local-tls-trust`,
adapted to extract the step-ca root). What step-ca buys over Caddy's own CA is a
**stable, shared root** — trust it once, reused everywhere, surviving redeploys.

## Issuance for `*.agent-cloud.test` (local): token-mint, not in-network ACME

step-ca runs an ACME provisioner, **but** ACME domain validation (http-01 /
tls-alpn-01 / dns-01) requires step-ca to reach or DNS-prove the requested name
— and `*.agent-cloud.test` is not resolvable/reachable *inside* the podman network
(containers use podman DNS by name; hickory's wildcard→127.0.0.1 is for the Mac
host). So locally Caddy does **not** ACME against step-ca; instead the deploy
**mints a wildcard `*.agent-cloud.test` leaf via a provisioner token** (`step ca
certificate`, no challenge) and Caddy serves it. Production does not use ACME
either: its CA is initialised without the ACME provisioner (see below).

## Production (own host) — deployed 2026-09-29

The design and its decisions are the openspec change `production-internal-ca`
(`plan/development/openspec/changes/production-internal-ca/`). The CA's name, its DNS
names (including `ca.<site>.<zone>`) and its address are site values, declared in
site-config only.

- **Own VM, nothing else on it.** The intermediate key signs every internal identity, so
  the host holding it runs only this container.
- **API on the host loopback, no ACME.** Production declares `stepca_bind: 127.0.0.1`
  and `stepca_init_acme: "false"`. After every deploy, Phase 3 asserts the port is
  published on that bind only and that no ACME provisioner exists. Issuance will reach
  the CA over SSH, by running `step` inside the container; no consumer contacts it.
- **First-boot settings are checked before anything is written.** First boot writes the
  name, the DNS names, the ACME switch and the admin provisioner into the volume for good.
  `tasks/assert-step-ca-first-boot.yml` refuses a production deploy, and a production
  reset, unless the name and the DNS names (including `localhost`) are declared, ACME is
  not turned on, and the bind and the admin provisioner keep their loopback and `admin`
  values, which have defaults.
- **Two issuing provisioners.** `issuer-server` and `issuer-client` are JWK
  provisioners, one per leaf profile, each with its own password in
  `secret/services/step-ca`. Their leaf lifetime comes from `stepca_leaf_dur`. The deploy
  plans them from `ca.json` and adds or updates only on a difference, and it reloads the
  CA whenever `ca.json` and the running CA disagree, so an interrupted run recovers.
- **The root is kept.** Every run prints the root fingerprint; the recorded value lives
  in site-config. Two consecutive production runs printed the same one, and the second
  changed nothing.
- **Reset is guarded.** `Clean Deploy step-ca (Dev)` destroys the root only when the
  launch names the host in `confirm_reset`, and checks the first-boot settings before
  anything is destroyed.
- **Templates are dev-bound until promotion.** `Deploy step-ca (Dev)` and
  `Clean Deploy step-ca (Dev)` run from `dev`, because `main`'s playbooks lack these
  guards.

Status of the rest of the change, as its `tasks.md` records it (2026-10-03):

- **Firewall (group 3):** applied on the CA host (3.1 done; `fw-harden` and
  `systemd-enablement` ran, 3.2/3.3 partial). Still to run: Snapshot Firewall, the reboot,
  `service-validate`, and the LAN reachability gate (3.4).
- **Cross-host issuance (group 4):** proven in production — the throwaway leaf of 4.7 and
  the edge leaves of 5.2 were each issued with the key made on the consumer and nothing left
  in the CA container; the declared-name guard (4.2) and root distribution (4.3) are done.
  Open: the wildcard path through `deploy-caddy.yml` (4.1/4.6) and the 4.8 gate.
- **Consumer leaves (group 5):** `agw-server`, `agw-verifier` and `caddy` issued and inspected
  (5.2 partial); `bench` waits on its VM.
- **Renewal and alerting (group 6):** `renew-internal-certs.yml` runs in production as
  `Renew Internal Certs (Dev)` on a daily schedule (6.1/6.2 partial: nothing yet re-issued by a
  real renewal), and the expiry alert rules are deployed (6.4 done). Open: the rotation drill
  (6.3) and the alert drill (6.5).
- **Backup and restore (group 7):** `backup-step-ca-to-site-config.yml` and its template are
  code (7.1/7.2); no backup run and no restore drill are recorded.

Recorded risk: the step-ca 0.30.2 entrypoint keeps the key password in the volume beside
the keys and prints it to the first boot's container log (the change's design,
"Recorded risk 2026-09-29").

## Files

| File | Role |
|---|---|
| `deployment/compose.yml` | the `step-ca` service; auto-init via `DOCKER_STEPCA_INIT_*`; persistent volume; HTTPS :9000 |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches it) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait healthy) |
| `deployment/templates/env.j2` | non-secret config + `STEPCA_INIT_PASSWORD` (key password from OpenBao) |

`deployment/.env` is rendered per-deploy and gitignored. Keys live encrypted in
the volume; only the password is in OpenBao (`secret/services/step-ca`).
