# step-ca service — architecture (context for agents)

Smallstep `step-ca` as the platform's **internal** CA. Read with the root
[`CLAUDE.md`](../../../../CLAUDE.md) and the plan it implements:
[`plan/development/INTERNAL-CA-DEPLOYMENT.md`](../../../../plan/development/INTERNAL-CA-DEPLOYMENT.md).

## What it is — and is NOT

- **IS:** the internal CA for `*.agent-cloud.test` / internal zones. A stable root +
  intermediate, auto-initialized on first boot and persisted in the
  `step-ca-data` volume (the win over Caddy's ephemeral `local_certs` root —
  this root survives Caddy redeploys and is shareable across hosts/devs).
- **IS NOT:** the public CA. Public/customer TLS is Caddy automatic-HTTPS +
  Let's Encrypt (`plan/architecture/CADDY-REVERSE-PROXY.md` → TLS strategy).
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
certificate`, no challenge) and Caddy serves it. ACME-native issuance is the
prod/future path, gated on dns-01 via hickory RFC 2136 (`DNS-SERVER-DEPLOYMENT.md`
Phase 2).

## Files
| File | Role |
|---|---|
| `deployment/compose.yml` | the `step-ca` service; auto-init via `DOCKER_STEPCA_INIT_*`; persistent volume; HTTPS :9000 |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches it) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait healthy) |
| `deployment/templates/env.j2` | non-secret config + `STEPCA_INIT_PASSWORD` (key password from OpenBao) |

`deployment/.env` is rendered per-deploy and gitignored. Keys live encrypted in
the volume; only the password is in OpenBao (`secret/services/step-ca`).
