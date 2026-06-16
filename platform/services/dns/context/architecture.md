# DNS service — architecture (context for agents)

hickory-dns (Rust; formerly trust-dns) serving the platform's **internal** name
resolution. Read with the root [`CLAUDE.md`](../../../../CLAUDE.md) and the
plans it implements:

- Production: [`plan/development/DNS-SERVER-DEPLOYMENT.md`](../../../../plan/development/DNS-SERVER-DEPLOYMENT.md)
- Local-dev: [`plan/development/LOCAL-DEV-DEPLOYMENT.md`](../../../../plan/development/LOCAL-DEV-DEPLOYMENT.md) §5.1

## What it is

One container, one job: be **authoritative** for one internal zone (a wildcard
plus explicit records, all rendered from inventory vars) and **forward**
everything else to upstream resolvers. The recursor is intentionally OFF — it is
experimental upstream; forwarding rides hickory's production-grade resolver.

```text
query foo.<zone>   --> authoritative answer from the rendered zone file
query example.com  --> forwarded to dns_upstreams, answer relayed back
```

## Files

| File | Role |
|---|---|
| `deployment/compose.yml` | the single `dns` service; ports env-parameterized (`DNS_LISTEN`/`DNS_PORT`) so the local loopback shift needs no compose fork |
| `deployment/compose.local.yml` | slim overlay (mem/cpu caps) appended in `local_mode` |
| `deployment/deploy.sh` | container lifecycle only (pull, up, wait healthy) — no config rendering, no secrets |
| `deployment/templates/named.toml.j2` | hickory config: listen, the Primary zone, the `.` forward store |
| `deployment/templates/zone.local-dev.j2` | RFC 1035 master file (wildcard + records) |
| `deployment/templates/env.j2` | non-secret compose vars (`DNS_*`) |

`deployment/config/` and `.env` are rendered per-deploy and gitignored.

## How it deploys

Composable, through Semaphore on the normal/redeploy path (Critical Rule #1):
`deploy-dns.yml` renders the config + zone + `.env` from inventory vars, runs
`deploy.sh`, then verifies with `dig` (wildcard answer + a forwarded external
name). The one sanctioned exception is the **genesis bootstrap** — DNS is part
of the secure foundation (`OpenBao → dns → step-ca → caddy → authentik`) that
`bootstrap-local-dev.yml` stands up directly, before Semaphore exists, running
the same un-forked `deploy-dns.yml` (Genesis-Bootstrap Exemption,
ACCESS-BOUNDARIES.md). No OpenBao in Phase 1 — DNS holds no runtime credentials.
Prod Phase 2 adds a TSIG key (from OpenBao) for RFC 2136 dynamic updates so
Caddy can solve ACME DNS-01 against the internal zone.

## Conventions specific to this service

- **Records are code.** Edit inventory vars (`dns_records`, `dns_wildcard_target`,
  `dns_zone`) and re-run the deploy — never hand-edit a running zone. The one
  exception is the Phase 2 dynamic challenge sub-zone (transient TXT records).
- **No real zone in the public repo.** `dns_zone` defaults to the RFC 6761
  reserved `agent-cloud.test` locally; the real internal zone lives in the gitignored
  working inventory / site-config.
- **One engine, two environments.** The laptop and prod run the same image and
  templates, parameterized by env/inventory — never forked.
