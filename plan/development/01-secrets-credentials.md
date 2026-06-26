# 01 — Secrets & Credential Lifecycle (OpenBao)
> **Consolidates:** OPENBAO-HA-DEPLOYMENT.md, OPENBAO-KV-MOUNT-PARAMETERIZATION.md, CREDENTIAL-LIFECYCLE-IMPLEMENTATION.md, APPROLE-TTL-ENFORCEMENT-PLAN.md, ANSIBLE-CREDENTIAL-REDACTION-PLAN.md (originals archived in `plan/archive/`)
>
> **Depends on:** 00
>
> Part of the dependency-ordered `plan/development/` set (00–10). The source
> plans are merged verbatim below under provenance dividers to preserve all
> detail; read in numbered order to execute.


<!-- ═══════════════════════ source: OPENBAO-HA-DEPLOYMENT.md ═══════════════════════ -->

# OpenBao HA & Resilience Deployment Plan

> **Location:** `plan/development/01-secrets-credentials.md`
> **Date:** 2026-06-14 · **Status:** PROPOSED · **Owner:** uhstray-io
>
> **Context:** OpenBao is the platform's root of trust — every deploy, every AppRole login, and every service `.env` flows from it (root `CLAUDE.md` → Secrets Management). Yet it is today the least-resilient component in the stack. Local-dev *was* in-memory (a `podman machine` restart wiped all secrets and bricked every stateful volume — see [[local-openbao-is-in-memory-vm-restart-resets-all-secrets]]); that is now fixed with a persistent file backend (Track A, Phase A1 — landed). Production runs a **single-node Raft** vault with **manual Shamir unseal** (sealed after every reboot — a standing "High" risk in `IMPLEMENTATION_PLAN.md:1616` and a manual SSH operation in `ACCESS-BOUNDARIES.md:57`), **no TLS** (`tls_disable = 1`), **no snapshots**, and a **legacy bash `deploy.sh`** that generates secrets + writes policies + creates AppRoles itself — a direct violation of Critical Rules #2/#4. This plan makes local-dev resilient for testing and brings production to a convenient, self-healing HA cluster.
>
> **For agentic workers:** Execute phase-by-phase; every phase ends at a validation gate. The root/recovery keys and unseal material are the crown jewels — `no_log` every task that touches them, escrow them to site-config, never commit them. Real domains/IPs stay in site-config. TLS (Phase B0) is a hard prerequisite for Raft HA — do not reorder it.

**Goal:** A single source of truth for platform secrets that (a) survives restarts unattended, (b) tolerates a node failure in production without a credential outage, and (c) is deployed and operated entirely through the composable Semaphore/Ansible pattern — no hand-rolled bootstrap, no manual unseal, no on-VM `secrets/` directory.

**Architecture:** Both environments converge on **Integrated Storage (Raft)** — single-node locally, a 3-node quorum in production. Production nodes **auto-unseal** via an OpenBao **Transit** seal so they come back unsealed (and therefore HA-capable) after any reboot. Listener + cluster traffic run over **TLS issued by the existing step-ca internal CA**. Services stop hand-rolling token lifecycle and receive secrets through the **OpenBao Agent** (auto-auth + templating). Deploy/operate is composable: `templates/openbao.hcl.j2` → `deploy.sh` (containers only) → minimal genesis init/unseal play → `apply-openbao-policies.yml` → `manage-approle.yml`, with scheduled `backup-openbao.yml` Raft snapshots.

**Tech stack:** OpenBao (Integrated Storage/Raft, Transit auto-unseal, Agent), step-ca (internal TLS, already deployed — `INTERNAL-CA-DEPLOYMENT.md`), composable Ansible tasks (`manage-secrets`, `manage-approle`, `apply-openbao-policies`, `clean-service`), Semaphore templates-as-code, Proxmox VMs.

---

## Problem

| # | Gap | Evidence | Impact |
|---|-----|----------|--------|
| 1 | **Single-node Raft, no quorum/failover** | prod `config/openbao.hcl`: `node_id="node1"`, `cluster_addr=http://127.0.0.1:8201` (loopback) | OpenBao VM loss = total platform credential outage |
| 2 | **Manual Shamir unseal, no auto-unseal** | `site-config/CLAUDE.md:95` "OpenBao starts sealed"; `IMPLEMENTATION_PLAN.md:1616` ("High"), `:1664` ("Auto-unseal — deferred"); `ACCESS-BOUNDARIES.md:57` | Every reboot needs a manual SSH unseal; a **sealed standby cannot fail over** [4] |
| 3 | **No snapshot/backup** | no `bao operator raft snapshot` anywhere in repo; DR Scenario 3 is an unchecked stub | No recovery path for storage corruption / lost quorum |
| 4 | **TLS disabled** | `tls_disable = 1` in prod + local HCL; `openbao_addr: http://192.168.1.164:8200` | Secrets cross the LAN in plaintext; **TLS is a hard prerequisite for Raft cluster traffic** [1][4] |
| 5 | **No OpenBao Agent** | clients use bespoke curl/`uri` (`bao-client.sh`, `manage-secrets.yml`) | Token lifecycle hand-rolled; no caching/renewal sidecar |
| 6 | **Legacy non-composable deploy** | `deploy.sh` generates secrets + writes policies + creates AppRoles + seeds (violates Rules #2/#4); "Deploy OpenBao" template wraps it; no `clean-deploy-openbao.yml` | Unauditable, non-idempotent, contradicts every other service |
| 7 | **Init/secret escrow gaps** | root token never revoked (`IMPLEMENTATION_PLAN.md:1666`); `site-config/secrets/openbao/` referenced but **absent**; no audit device (`:1670`) | Long-lived root token on VM; no audit trail; manual key backup |
| 8 | **Storage-backend fork local↔prod** | local-dev = `file`; prod = `raft` | Local never exercises the Raft path prod runs (init/unseal/snapshot untested) |

---

## Design Principles

Aligned with root `CLAUDE.md` → *Engineering Principles — Foundational Over One-Shot* and `AUTOMATION-DECLARATIVE-VS-IMPERATIVE.md`:

1. **One codebase, no forks.** Local-dev and prod use the **same Raft backend** and the **same `openbao.hcl.j2`**, differing only by inventory vars (`node_id`, `retry_join`, TLS paths, seal stanza). Kill the file-vs-raft fork (Problem #8).
2. **Genesis is the only imperative step.** First-init, first-unseal, and the auto-unseal bootstrap are irreducibly one-directional (`AUTOMATION-DECLARATIVE-VS-IMPERATIVE.md:99`); everything else (policies, AppRoles, secrets, snapshots) reconciles declaratively. Do **not** reproduce the 594-line hand-rolled `deploy.sh`.
3. **deploy.sh handles containers only** (Critical Rule #2). Secret generation, policy writes, AppRole creation, and seeding move to `manage-secrets` / `apply-openbao-policies` / `manage-approle`.
4. **Convenience = unattended recovery.** A node (or the whole cluster) must come back **unsealed and serving** after a reboot with zero human steps — this is the entire point of auto-unseal, and the prerequisite for real HA standbys [4].
5. **Resiliency = no single point of total loss.** Quorum survives one node; snapshots survive storage loss; recovery keys survive a seal-service outage [seal-concept].
6. **The blast radius is the platform.** TLS in transit, an audit device, least-privilege policies applied **everywhere** (including local), and a revocable root token.

---

## Decision Criteria

This section makes the rejected options visible so the chosen path reads as a decision, not an assumption.

### D1 — Storage backend

| Option | HA-capable | External dep | Verdict |
|--------|-----------|--------------|---------|
| **Integrated Storage (Raft)** | **Yes** | None (embedded) | **CHOSEN** — doc-recommended "for most use cases"; transactional; HA built-in [1, raft] |
| PostgreSQL | Yes | External DB to run + monitor + back up | Rejected — adds an HA dependency we'd also have to make HA |
| `file` | No | None | Rejected for prod (not transactional, no file locking, "not for production" [1]); **retained as the shipped local baseline** until A5 migrates local to single-node Raft |
| `inmem` | No | None | The original bug — never again |

Raft also forbids a separate `ha_storage` stanza (it *is* the HA layer) [1], simplifying config.

### D2 — Unseal strategy

| Option | Reboot behavior | Convenience | Resiliency caveat | Verdict |
|--------|-----------------|-------------|-------------------|---------|
| **Transit auto-unseal** (dedicated seal vault) | Auto-unseals on boot → standby-ready | High (zero human steps) | Seal service is a hard dependency/SPOF; mitigate with HA seal node + **recovery keys** [seal-concept] | **CHOSEN for prod** |
| Cloud KMS auto-unseal (AWS/GCP/Azure) | Auto-unseals | High | External cloud dependency — conflicts with privacy-focused/self-hosted posture | Rejected (no cloud trust anchor) |
| Manual Shamir | **Stays sealed** until human unseals | Low | Sealed standbys can't fail over [4] | Rejected for prod (the current pain); **kept for local** single-node (1-of-1, key escrowed) |

**Why Transit, not KMS:** the platform is self-hosted and privacy-focused (root `CLAUDE.md`). A small dedicated OpenBao "seal" instance gives auto-unseal with no third party. The SPOF is acknowledged and mitigated: the seal vault is itself snapshotted, and **recovery keys** (Shamir-split, PGP-encrypted, escrowed to site-config) authorize break-glass root-token generation and seal migration if the seal service is ever lost [seal-concept].

### D3 — Secret delivery to services

| Option | Token lifecycle | Verdict |
|--------|-----------------|---------|
| **OpenBao Agent** (auto-auth + template/env-inject) | Owned by the Agent sidecar — auto-renew, cache, re-render | **CHOSEN (phased, B5)** — replaces bespoke `bao-client.sh` token juggling [2] |
| Bespoke API (`manage-secrets.yml` / `bao-client.sh`) | Hand-rolled; `renew-self` sprinkled across policies | Status quo — kept for deploy-time `.env` templating; superseded for long-running runtime secret refresh |

### D4 — HA topology

3-node Raft is the MVP (quorum 2, tolerates 1 failure); 5 nodes is the later high-tolerance option. Odd counts only. All nodes **must be unsealed** to act as standbys [4] — which is exactly why D2 chose auto-unseal.

### D5 — Local-dev backend (the live tension)

Phase A1 shipped a **`file`** backend (proven, persistent, solves the immediate restart bug). The no-fork principle (D1, Principle #1) wants local on Raft too. Resolution: **keep `file` as the working baseline** and treat **single-node Raft migration as Phase A5** — a clean, low-risk upgrade that (a) makes local exercise prod's exact backend and (b) unlocks `bao operator raft snapshot` locally. We do *not* rip out the just-landed, currently-recovering file backend to chase parity mid-incident.

---

## Architecture

### Target production topology

Three OpenBao nodes form a Raft quorum behind Caddy; each auto-unseals from a dedicated Transit seal vault on boot, so a reboot or single-node loss never seals the cluster. Services obtain secrets via a local OpenBao Agent rather than calling the API directly.

```mermaid
flowchart TD
    subgraph SEAL["Seal vault (auto-unseal anchor)"]
        TV["openbao-seal<br/>single node, Shamir<br/>transit/ engine"]
    end
    subgraph CLUSTER["OpenBao Raft cluster (quorum 2 of 3)"]
        N1["node1 (active)"]
        N2["node2 (standby)"]
        N3["node3 (standby)"]
        N1 <-->|"cluster_addr :8201 (TLS)<br/>Raft replication"| N2
        N2 <-->|Raft| N3
        N1 <-->|Raft| N3
    end
    N1 -->|"seal transit: decrypt root key on boot"| TV
    N2 --> TV
    N3 --> TV
    CADDY["Caddy<br/>api_addr redirect"] -->|"client requests -> active"| N1
    subgraph HOST["Service host"]
        AGENT["OpenBao Agent<br/>auto-auth (approle) + template"]
        SVC["service<br/>reads .env / env vars"]
        AGENT -->|renders secrets| SVC
    end
    AGENT -->|"login + fetch (TLS)"| CADDY
    BKP["backup-openbao.yml<br/>raft snapshot save -> MinIO/off-box"] -.->|scheduled| N1
```

### Why each address matters (HA correctness)

`api_addr` is the URL standbys hand back to clients for redirect; `cluster_addr` (`:8201`, always TLS) is the server-to-server forwarding/replication address and is **required** for Raft [1][4]. Misconfiguring either silently breaks HA — clients get redirected nowhere, or nodes can't replicate. Both must be the node's **reachable** address (not loopback, the current bug in Problem #1).

### Seal/unseal key hierarchy

Auto-unseal moves the top of the key chain out of human hands: the external Transit service decrypts the root key on boot, which unlocks the keyring, which unlocks data [seal-concept]. Recovery keys exist for break-glass but **cannot themselves unseal** — if the seal service is permanently lost, the cluster is unrecoverable even from backups [seal-concept]. That caveat drives the seal-vault-must-be-backed-up requirement.

```mermaid
flowchart LR
    DATA["encrypted data"] --> EK["encryption key (keyring)"]
    EK --> RK["root key"]
    RK -->|"Shamir (local) / Transit (prod)"| UNSEAL["unseal mechanism"]
    RK -.->|"recovery keys: authorize root-token/seal-migration<br/>NOT unseal"| REC["recovery keys (escrow)"]
```

---

## Implementation Phases

### Track A — Local-dev resilience

#### Phase A1 — Persistent file backend *(LANDED — 2026-06-14)*

`bootstrap-local-dev.yml` Stage 1 now runs `openbao server` (not `-dev`) on a `file` backend in the named volume `local-openbao-data` (at `/openbao/file`, the image-owned path), with idempotent init (1 share / 1 threshold) + unseal using the key persisted to `~/.agent-cloud-local/openbao-init.json` (0600), KV v2 enable, a recreate-if-still-dev migration, and a lost-key guard. `make local-clean` is the sole intentional wipe (now also removes the data volume + init/config artifacts).

**Acceptance (met):** container restart preserves secrets; re-unseal with the persisted key restores access (validated). `ansible-playbook --syntax-check` + shellcheck clean.

#### Phase A2 — Local snapshot / restore

- **Files:** `Makefile` (`local-bao-snapshot`, `local-bao-restore` targets), `scripts/local-dev.sh` (subcommands).
- File backend: snapshot = `podman stop local-openbao` → tar the volume → timestamped artifact under `~/.agent-cloud-local/snapshots/`; restore = reverse. After A5 (Raft), switch to `bao operator raft snapshot save/restore`.
- **Acceptance:** snapshot before a risky op, corrupt the volume, restore → secrets intact.

#### Phase A3 — Restart self-heal (unseal-on-boot)

- **Problem:** the bootstrap `podman run` has no `--restart` and unseal only happens when the operator re-runs `make`; a bare machine reboot leaves the container stopped/sealed.
- **Files:** convert local OpenBao to a **podman Quadlet** (`~/.config/containers/systemd/local-openbao.container`) or add `--restart=always` + a tiny `local-openbao-unseal` boot hook that reads `openbao-init.json` and POSTs `/v1/sys/unseal`.
- **Acceptance:** `podman machine stop && start` → vault auto-unseals with no `make` invocation.

#### Phase A4 — Init-key escrow

- Copy `openbao-init.json` into a second location (and optionally age/gpg-encrypt it) so a single-file loss isn't fatal. Document in `docs/LOCAL-DEV.md`.
- **Acceptance:** delete the primary init file, restore from escrow, bootstrap unseals.

#### Phase A5 — Migrate local to single-node Raft *(kills the fork)*

- Switch the inline local HCL to `storage "raft"` + `node_id` + `cluster_addr` (required for Raft [1]); same `openbao.hcl.j2` prod uses, single-node params.
- **Acceptance:** fresh `make local-clean && make local-up` inits a Raft node; `bao operator raft snapshot save` works locally; A2 switches to raft snapshots.

#### Phase A6 — Policy parity

- Run `apply-openbao-policies.yml` against the local instance so `config/policies/*.hcl` (least-privilege per-service) are exercised locally, not just the inline `local-semaphore` policy.
- **Acceptance:** local vault carries the same named policies as prod; a per-service AppRole login is denied paths it shouldn't read.

### Track B — Production HA

#### Phase B0 — TLS (prerequisite for everything else)

- **Files:** `templates/openbao.hcl.j2` (listener `tls_cert_file`/`tls_key_file`, `cluster_addr`), `tasks/mint-internal-cert.yml` reuse (step-ca already issues internal certs — `INTERNAL-CA-DEPLOYMENT.md`), site-config TLS path vars.
- Issue an OpenBao server cert from step-ca; enable listener TLS (`tls_min_version = "tls12"`); flip `openbao_addr` to `https://`.
- **Acceptance:** `bao status` over TLS; AppRole login over `https://`; step-ca root trusted by clients. **Gate: no plaintext :8200.**

#### Phase B1 — Composable deploy/operate (replace legacy deploy.sh)

- **Files:** `templates/openbao.hcl.j2` (parameterized: `node_id`, `retry_join` peers, TLS, seal stanza, `performance_multiplier = 1`, `disable_mlock = true`), slimmed `deploy.sh` (containers only), new `deploy-openbao.yml` (place-monorepo → manage-secrets → deploy.sh → minimal genesis init/unseal reconcile → verify), `clean-deploy-openbao.yml` (via `tasks/clean-service.yml`), `platform/semaphore/templates.yml` (repoint "Deploy OpenBao", add "Clean Deploy OpenBao").
- Delete secret-gen / policy-write / AppRole-create / seed logic from `deploy.sh`; policies come from `apply-openbao-policies.yml`, AppRoles from `manage-approle.yml`. Keep a documented `-e github_deploy_key=` / `-e bao_init=` break-glass for the irreducible genesis chicken-and-egg.
- **Acceptance:** a clean prod deploy stands up OpenBao with **zero secrets generated in bash**; re-running converges; `clean-deploy` wipes + rebuilds.

#### Phase B2 — Transit auto-unseal

- **Files:** stand up `openbao-seal` (single node, Shamir, `transit/` engine, snapshotted); add `seal "transit" { ... }` to `openbao.hcl.j2` (address, token, key_name, mount_path, tls_*); init cluster nodes with `recovery_shares`/`recovery_threshold` (+ `recovery_pgp_keys`); escrow recovery keys to `site-config/secrets/openbao/` (**create this dir**).
- The transit token must be **orphan + periodic** with `update` on `transit/encrypt/<key>` + `transit/decrypt/<key>` only [seal-transit].
- **Acceptance:** restart a node → it auto-unseals (no human); recovery keys generate a root token; **seal-vault backup verified** (its loss = cluster loss [seal-concept]).

#### Phase B3 — 3-node Raft cluster

- **Files:** Proxmox provisioning for `openbao-{1,2,3}` (`hypervisor/proxmox/`), per-node inventory (`node_id`, `raft_retry_join` peer list, `api_addr`, `cluster_addr`), autopilot defaults.
- Bring up node1, `retry_join` node2/node3; verify `bao operator raft list-peers`; step down active and confirm a standby takes over (all unsealed via B2).
- **Acceptance:** kill the active node → a standby becomes active, secret reads continue; rejoin → autopilot re-adds it.

#### Phase B4 — Snapshots / backup / DR

- **Files:** `backup-openbao.yml` (`bao operator raft snapshot save` → MinIO/off-box), Semaphore "Snapshot OpenBao" template + schedule, documented restore in `DISASTER-RECOVERY-PLAN.md` (Scenario 3).
- **Acceptance:** scheduled snapshot lands off-box; restore into a fresh cluster reproduces secrets.

#### Phase B5 — OpenBao Agent rollout

- **Files:** Agent config per service host (`auto_auth` approle reading a **response-wrapped** secret_id, `cache { persist }`, `template`/`env_template` rendering `.env` or injecting env vars), incrementally replacing `bao-client.sh` runtime calls [2].
- Start with one service (e.g. n8n), prove auto-renew across token expiry, then fan out.
- **Acceptance:** a service's secrets refresh without a redeploy when rotated in OpenBao; no service-side login/renew code.

#### Phase B6 — Hardening

- Enable an **audit device** (`bao audit enable`); **revoke the genesis root token** after AppRoles exist; finalize all Semaphore templates-as-code; pin `openbao_svc` `container_engine` in production.yml.
- **Acceptance:** audit log captures secret access; no live root token; `setup-templates.yml` reproduces every OpenBao template.

---

## Validation Criteria

| Check | Command / signal | Pass condition |
|-------|------------------|----------------|
| Local persistence (A1) | restart `local-openbao`, re-unseal | secret read back intact ✅ (done) |
| Local self-heal (A3) | `podman machine stop && start` | vault unsealed, no `make` run |
| Local Raft (A5) | `bao operator raft list-peers` | one node, leader |
| TLS (B0) | `bao status` / login over `https://` | no plaintext :8200 reachable |
| Composable deploy (B1) | grep `deploy.sh` for secret/policy/approle gen | none; deploy converges on re-run |
| Auto-unseal (B2) | restart a node | comes back unsealed unattended |
| HA failover (B3) | stop active node | standby serves reads within seconds |
| Snapshot/restore (B4) | restore snapshot into fresh cluster | secrets reproduced |
| Agent (B5) | rotate a secret in OpenBao | service picks it up without redeploy |
| Audit (B6) | read a secret, tail audit log | access recorded; root token revoked |

---

## Security Considerations

- **Crown-jewel material** — unseal key (local), recovery keys + transit token (prod), genesis root token: every task touching them carries `no_log: true` (per [[feedback_no_log_and_permissions]] — `no_log` is reserved for exactly these credential-handling tasks, written as discrete functions). They are escrowed to `site-config/secrets/openbao/`, never committed.
- **Seal-service SPOF** — auto-unseal makes the Transit seal vault a hard dependency: its permanent loss makes the cluster unrecoverable *even from backups* [seal-concept]. Mitigations: the seal vault is itself snapshotted (B4), and **recovery keys** provide break-glass root-token generation / seal migration.
- **TLS everywhere (B0)** closes the current plaintext-secret-on-LAN exposure (Problem #4) and is mandatory for Raft cluster traffic.
- **Least privilege** — `config/policies/*.hcl` applied in **both** environments (A6); the `semaphore-read` super-policy (`sys/policies/acl/*` + `auth/approle/role/*`) stays scoped to the orchestrator only.
- **Audit + root revocation (B6)** — given read-all policies exist (`nemoclaw-read`, `semaphore-read`), an audit device and a revoked genesis root token are required to bound blast radius.
- **Break-glass genesis** — OpenBao's own deploy can't fetch its GitHub deploy key from itself; the documented `-e github_deploy_key=` extra-var is the *only* sanctioned manual injection, scoped to first deploy.

---

## Cross-references

- Root `CLAUDE.md` — Secrets Management, Critical Deployment Rules #2/#4, OpenBao Secrets Layout
- `plan/architecture/01-automation-model.md` — composable task pattern this plan adopts
- `plan/architecture/01-automation-model.md` — genesis-only-imperative principle (`:96`, `:99`)
- `plan/development/00-foundation-local-dev.md` — step-ca, the TLS issuer for Phase B0
- `plan/development/00-foundation-local-dev.md` — local control-plane bootstrap (Track A host)
- `plan/development/10-infra-resilience.md` — Scenarios 2/3/7 fulfilled by B4
- `plan/archive/development/IMPLEMENTATION_PLAN.md` — `:1616` (sealed-after-reboot risk), `:1664–1672` (auto-unseal/audit/root-revoke deferred), `:1813`/`:1827` (3-node Raft, snapshots)
- `plan/architecture/04-credentials-access.md` — `:57` (manual unseal as standing SSH op, retired by B2)
- site-config — `inventory/production.yml` (`openbao_svc`, `openbao_addr`), `CLAUDE.md:95` (sealed-on-boot known issue), `secrets/openbao/` (to be created in B2)

### References (OpenBao docs, accessed 2026-06-14)

1. Configuration — https://openbao.org/docs/configuration/ (+ storage/raft, listener/tcp, seal children)
2. Agent & Proxy — https://openbao.org/docs/agent-and-proxy/
3. Internals: Architecture — https://openbao.org/docs/internals/architecture/
4. Internals: High Availability — https://openbao.org/docs/internals/high-availability/
- seal-concept — https://openbao.org/docs/concepts/seal/ · seal-transit — https://openbao.org/docs/configuration/seal/transit/

> **Unverified against fetched pages (confirm before quoting as OpenBao-official):** exact quorum/fault-tolerance numbers and autopilot operator defaults (`min_quorum`, `cleanup_dead_servers`, …); `bao operator raft snapshot save/restore` + `list-peers`/`remove-peer` CLI syntax; the precise `disable_mlock` guidance text; multi-seal `priority`. These live in the operator/CLI docs not fetched here.

---

## Revision History

| Date | Summary |
|------|---------|
| 2026-06-14 | Initial draft. A1 (persistent local file backend) landed; Tracks A/B and decision criteria authored from OpenBao docs + repo current-state assessment. |

<!-- ═══════════════════════ source: OPENBAO-KV-MOUNT-PARAMETERIZATION.md ═══════════════════════ -->

# OpenBao KV Mount Parameterization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parameterize the OpenBao KV v2 **mount path** (hardcoded `secret/` today) behind a `bao_kv_mount` variable that defaults to `secret`, so multiple agent-cloud instances can each own a separately-named secret vault (e.g. `secret/`, `agent-cloud-b/`) with zero behavior change for existing instances.

**Architecture:** A single inventory/playbook variable `bao_kv_mount` (Ansible) / `BAO_KV_MOUNT` (bash) is threaded through every place the mount appears: the KV-enable step, all KV v2 API path construction, the OpenBao policy files (which become Jinja templates), the `hashi_vault` lookups, and the Semaphore environment. The KV-v2 `data/`/`metadata/` infixes are preserved. Defaulting to `secret` everywhere means omitting the variable reproduces today's layout exactly — no data migration.

**Tech stack:** Ansible (`ansible.builtin.uri`, `community.hashi_vault`), bash libs (`bao-client.sh`, `common.sh`), HCL policies, OpenBao KV v2 API, Semaphore template env.

**Scope decision (chosen by maintainer):** *Per-instance mount* isolation — each instance gets its own KV engine mount. This is **orthogonal** to the documented-but-unimplemented `vault_secret_prefix` (which parameterizes the path *prefix within* a mount, e.g. `secret/data/<prefix>/…`). This plan parameterizes the **mount only**; the `services` path segment stays literal. The two compose later if prefix-namespacing is ever implemented (`{{ bao_kv_mount }}/data/{{ vault_secret_prefix }}/…`).

---

## Background

Today the mount name `secret` is hardcoded in ~33 files across five categories (audited 2026-06-16):

| Category | Files | Nature |
|----------|-------|--------|
| KV v2 API paths | `tasks/manage-secrets.yml` (3 sites), ~15 playbooks, `lib/bao-client.sh`, service `deploy.sh`, `scripts/local-dev.sh` | `secret/data/services/*`, `secret/metadata/services/*` — safe string substitution |
| KV engine enable | `bootstrap-local-dev.yml` (`sys/mounts/secret`), `services/openbao/deployment/deploy.sh` (`secrets enable -path=secret`) | **migration-sensitive** — creates the mount |
| OpenBao policies | 9 `.hcl` files in `services/openbao/deployment/config/policies/` | `path "secret/data/services/*"` — must template |
| `hashi_vault` lookups | `distribute-ssh-keys.yml`, `harden-ssh.yml`, `proxmox-validate.yml`, `provision-vm.yml` | `secret=secret/data/…` lookup args |
| Documentation | `CLAUDE.md` (layout table) + several `plan/` docs | reference only |

**Why now:** prod will run multiple agent-cloud instances; per-instance vaults keep their secrets, policies, and AppRoles isolated under distinct mounts.

## Design decisions

1. **Variable + default.** `bao_kv_mount` (Ansible) and `BAO_KV_MOUNT` (bash), default `secret`. Reference it **inline with the default at every site** — `{{ bao_kv_mount | default('secret') }}` / `${BAO_KV_MOUNT:-secret}` — rather than relying on a single group_vars definition. This guarantees that any playbook run without the variable set behaves exactly as today, even outside the normal inventory.
2. **KV-v2 infix preserved.** API paths are `<mount>/data/<path>` (read/write) and `<mount>/metadata/<path>` (metadata/list). Only the leading `<mount>` token is parameterized; `data`/`metadata`/`services` are unchanged.
3. **Non-breaking, no migration.** Default `secret` keeps the existing mount. A *new* instance setting `bao_kv_mount: agent-cloud-b` gets a fresh empty mount created by the KV-enable step. **Renaming an existing instance's mount is out of scope** (would require `bao secrets move`); we only parameterize for new instances.
4. **Policies become templates.** The 9 static `.hcl` files become `.hcl.j2`, rendered with `bao_kv_mount` at apply time. `apply-openbao-policy.yml` switches `lookup('file', …)` → `lookup('template', …)`. Non-secret paths in the policies (`sys/policies/acl/*`, `auth/approle/*`) stay literal.
5. **Semaphore env.** Semaphore must export `BAO_KV_MOUNT` into the deploy environment (alongside `BAO_ROLE_ID`/`BAO_SECRET_ID`) so `bao-client.sh` and service `deploy.sh` resolve the same mount.

---

## Task 1: Define the variable + document the default

**Files:**
- Modify: `platform/inventory/local-dev.yml.example` (add commented `bao_kv_mount: secret` under the OpenBao/all vars, with explanatory comment)
- Reference (site-config, private): each prod instance's inventory sets `bao_kv_mount: <instance-mount>`

- [ ] **Step 1: Add the documented default to the inventory example**

```yaml
    # OpenBao KV v2 mount that holds this instance's secrets. Default 'secret'
    # reproduces the legacy layout (secret/data/services/<svc>). Set a distinct
    # value per agent-cloud instance to isolate vaults (e.g. agent-cloud-b).
    # Changing this on an EXISTING instance requires `bao secrets move` — only
    # set a non-default value when bootstrapping a fresh instance.
    bao_kv_mount: secret
```

- [ ] **Step 2: Verify** — `grep -n bao_kv_mount platform/inventory/local-dev.yml.example` shows the entry. No functional change yet.

---

## Task 2: Parameterize the KV engine enable (migration-sensitive)

**Files:**
- Modify: `platform/playbooks/bootstrap-local-dev.yml` (the "Enable KV v2 at secret/" task, ~L242)
- Modify: `platform/services/openbao/deployment/deploy.sh` (`enable_secrets_engines`, ~L99)

- [ ] **Step 1: bootstrap — parameterize the mount path in the enable URL**

```yaml
    - name: "Enable KV v2 at {{ bao_kv_mount | default('secret') }}/ (idempotent)"
      ansible.builtin.uri:
        url: "{{ _bao_url_host }}/v1/sys/mounts/{{ bao_kv_mount | default('secret') }}"
        method: POST
        headers:
          X-Vault-Token: "{{ _bao_root_token }}"
        body_format: json
        body: { type: kv, options: { version: 2 } }
        status_code: [200, 204, 400]   # 400 = already mounted
      register: _kv_enable
```

- [ ] **Step 2: openbao deploy.sh — parameterize the enable + the existence grep**

```bash
enable_secrets_engines() {
  local token="$1"
  local mount="${BAO_KV_MOUNT:-secret}"
  info "Step 4: Enabling secrets engines..."
  local enabled
  enabled=$(bao_auth "$token" secrets list -format=json 2>/dev/null | jq -r 'keys[]')

  echo "$enabled" | grep -q "^${mount}/$" || bao_auth "$token" secrets enable -path="${mount}" kv-v2
  echo "$enabled" | grep -q "^database/$" || bao_auth "$token" secrets enable database
  info "Secrets engines ready."
}
```

- [ ] **Step 3: Verify** — `shellcheck platform/services/openbao/deployment/deploy.sh` clean; warm re-run of `make local-bootstrap` returns 400 (already mounted at `secret/`) with no new mount created.

---

## Task 3: Parameterize the bash client (`bao-client.sh`)

**Files:**
- Modify: `platform/lib/bao-client.sh` (`bao_kv_get`, `bao_kv_get_field`, `bao_kv_put`, `bao_kv_patch`)

- [ ] **Step 1: Add a mount helper at the top of the KV section**

```bash
# KV v2 mount that holds this instance's secrets (per-instance isolation).
# Default 'secret' = legacy layout. Semaphore/Ansible export BAO_KV_MOUNT.
_bao_kv_mount() { printf '%s' "${BAO_KV_MOUNT:-secret}"; }
```

- [ ] **Step 2: Use it in all four helpers** (replace the literal `secret/`):

```bash
bao_kv_get() {
  local path="$1"
  _bao_api GET "/$(_bao_kv_mount)/data/${path}" | jq -r '.data.data'
}

bao_kv_get_field() {
  local path="$1" field="$2"
  _bao_api GET "/$(_bao_kv_mount)/data/${path}" | jq -r --arg f "$field" '.data.data[$f] // empty'
}

bao_kv_put() {
  local path="$1" json_data="$2"
  _bao_api POST "/$(_bao_kv_mount)/data/${path}" -d "$(jq -n --argjson data "$json_data" '{"data": $data}')"
}
# bao_kv_patch: replace the hardcoded
#   "${OPENBAO_ADDR}/v1/secret/data/${path}"
# with
#   "${OPENBAO_ADDR}/v1/$(_bao_kv_mount)/data/${path}"
```

- [ ] **Step 3: Verify** — `shellcheck platform/lib/bao-client.sh` clean; `grep -n 'secret/data' platform/lib/bao-client.sh` returns nothing.

---

## Task 4: Parameterize `manage-secrets.yml` (the reusable secrets task)

**Files:**
- Modify: `platform/playbooks/tasks/manage-secrets.yml` (fetch-existing, fetch-shared, store — 3 URL sites)

- [ ] **Step 1: Replace all three `/v1/secret/data/services/…` URLs**

```yaml
# fetch existing:
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ service_name }}"
# fetch shared (loop):
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ item.from_service }}"
# store:
    url: "{{ _bao_url }}/v1/{{ bao_kv_mount | default('secret') }}/data/services/{{ service_name }}"
```

- [ ] **Step 2: Verify** — `grep -n 'v1/secret/data' platform/playbooks/tasks/manage-secrets.yml` returns nothing; `ansible-lint platform/playbooks/tasks/manage-secrets.yml` clean.

---

## Task 5: Parameterize the remaining playbooks

**Files (each contains one or more `/v1/secret/data/services/…` or `/secret/metadata/…` references — substitute the same pattern as Task 4):**
- `platform/playbooks/bootstrap-local-dev.yml` (the OpenBao OIDC client-secret read + any KV reads)
- `platform/playbooks/seed-n8n-secrets.yml`
- `platform/playbooks/sync-secrets-to-openbao.yml`
- `platform/playbooks/check-secrets.yml`, `validate-secrets.yml`
- `platform/playbooks/deploy-*.yml` and `clean-deploy-*.yml` that read/write KV directly (audit with the grep in Step 2)

- [ ] **Step 1: Find every remaining reference**

```bash
grep -rn "v1/secret/data\|v1/secret/metadata\|/secret/data/services\|/secret/metadata/services" platform/playbooks/
```

- [ ] **Step 2: Substitute** `secret` → `{{ bao_kv_mount | default('secret') }}` in each (preserving `data`/`metadata`/`services`).

- [ ] **Step 3: Verify** — the grep from Step 1 returns nothing (except the `| default('secret')` lines).

---

## Task 6: Parameterize the `hashi_vault` lookups

**Files:**
- `platform/playbooks/distribute-ssh-keys.yml`
- `platform/playbooks/harden-ssh.yml`
- `platform/playbooks/proxmox-validate.yml`
- `platform/playbooks/provision-vm.yml`

- [ ] **Step 1: Replace the embedded mount in each lookup** — e.g.

```yaml
# before: lookup('community.hashi_vault.hashi_vault', 'secret=secret/data/services/ssh:...')
# after:
"{{ lookup('community.hashi_vault.hashi_vault', 'secret=' ~ (bao_kv_mount | default('secret')) ~ '/data/services/ssh:...') }}"
```

- [ ] **Step 2: Verify** — `grep -rn "secret=secret/" platform/playbooks/` returns nothing.

---

## Task 7: Template the OpenBao policies

**Files:**
- Rename: 9 files `platform/services/openbao/deployment/config/policies/*.hcl` → `*.hcl.j2`
  (`inference-comfyui`, `inference-hunyuan3d`, `n8n-write`, `nemoclaw-read`, `nemoclaw-rotate`, `nocodb-write`, `orb-agent`, `semaphore-read`, `semaphore-write`)
- Modify: `platform/playbooks/tasks/apply-openbao-policy.yml` (file → template lookup)
- Modify: `platform/playbooks/apply-policy-*.yml` (7 files) + `apply-openbao-policies.yml` — update `_policy_file` extension to `.hcl.j2`

- [ ] **Step 1: In each policy, parameterize only the KV paths**

```hcl
path "{{ bao_kv_mount | default('secret') }}/data/services/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
}
path "{{ bao_kv_mount | default('secret') }}/metadata/services/*" {
  capabilities = ["read", "list"]
}
# sys/policies/acl/*, auth/approle/* paths stay literal (not KV mounts).
```

- [ ] **Step 2: Switch the apply task to render the template**

```yaml
- name: "Apply policy — {{ _policy_name }}"
  ansible.builtin.uri:
    url: "{{ _bao_url }}/v1/sys/policies/acl/{{ _policy_name }}"
    method: PUT
    headers:
      X-Vault-Token: "{{ _bao_auth.json.auth.client_token }}"
    body_format: json
    body:
      policy: "{{ lookup('template', _policy_file) }}"   # was lookup('file', _policy_file)
    status_code: [200, 204]
  delegate_to: localhost
```

- [ ] **Step 3: Update `_policy_file` paths** in `apply-policy-*.yml` + `apply-openbao-policies.yml` (e.g. `…/semaphore-read.hcl` → `…/semaphore-read.hcl.j2`).

- [ ] **Step 4: Verify** — `ansible-playbook --syntax-check` on an apply playbook; apply against live OpenBao (default mount) and confirm the rendered policy still reads `secret/data/services/*` (diff the policy via `bao policy read semaphore-read`). Update `config/policies/README.md` to note the `.j2` + `bao_kv_mount`.

---

## Task 8: Parameterize `scripts/local-dev.sh`

**Files:**
- Modify: `scripts/local-dev.sh` (the `creds()` reads of `secret/data/services/authentik`, `…/n8n`, etc.)

- [ ] **Step 1: Resolve the mount once + use it** — add near the top of `creds()` (and any other KV read):

```bash
local mount="${BAO_KV_MOUNT:-secret}"
# ... curl "${OPENBAO_ADDR}/v1/${mount}/data/services/authentik" ...
```

- [ ] **Step 2: Verify** — `shellcheck scripts/local-dev.sh` clean; `make local-creds` still prints the SSO logins (default mount).

---

## Task 9: Inject `BAO_KV_MOUNT` into the Semaphore environment

**Files:**
- Modify: `platform/semaphore/templates.yml` and/or `platform/semaphore/templates-local.yml` (the environment that already carries `BAO_ROLE_ID`/`BAO_SECRET_ID`/`OPENBAO_ADDR`)
- Modify: `platform/semaphore/setup-templates.yml` if the env is templated there

- [ ] **Step 1: Add `BAO_KV_MOUNT` to the Semaphore environment** sourced from `bao_kv_mount` so service `deploy.sh` + `bao-client.sh` resolve the instance mount. (Local default `secret`.)

- [ ] **Step 2: Verify** — re-run `setup-templates.yml`; a deploy task's env shows `BAO_KV_MOUNT=secret`.

---

## Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md` — the "OpenBao Secrets Layout" table: note paths are under `{{ bao_kv_mount }}/` (default `secret/`); add a one-liner under Secrets Management explaining per-instance mounts and the `bao_kv_mount` var.
- Modify: `plan/architecture/04-credentials-access.md` — add a note that `bao_kv_mount` (mount-level) is **orthogonal** to `vault_secret_prefix` (path-prefix); they compose.
- Modify: `platform/services/openbao/deployment/config/policies/README.md` — `.hcl.j2` rendering + the variable.

- [ ] **Step 1:** Make the edits above. **Step 2:** `grep -n bao_kv_mount CLAUDE.md` shows the note.

---

## Task 11: Tests (bats guards)

**Files:**
- Modify: `platform/tests/test_common.bats` (or a new `test_bao_mount.bats`)

- [ ] **Step 1: Guard that the mount is parameterized, not hardcoded**

```bash
@test "bao: KV mount is parameterized (no hardcoded secret/data in code paths)" {
  # manage-secrets, bao-client, scripts must use bao_kv_mount / BAO_KV_MOUNT
  run grep -rn "v1/secret/data/services" "$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  [ "$status" -eq 1 ]
  run grep -q 'BAO_KV_MOUNT' "$REPO_ROOT/platform/lib/bao-client.sh"
  [ "$status" -eq 0 ]
}

@test "bao: default mount resolves to 'secret' (non-breaking)" {
  run grep -q "default('secret')" "$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  [ "$status" -eq 0 ]
  run grep -q 'BAO_KV_MOUNT:-secret' "$REPO_ROOT/platform/lib/bao-client.sh"
  [ "$status" -eq 0 ]
}

@test "bao: policies are templated with the mount" {
  run bash -c "ls $REPO_ROOT/platform/services/openbao/deployment/config/policies/*.hcl.j2 | wc -l"
  [ "$output" -ge 9 ]
  run grep -q "lookup('template'" "$REPO_ROOT/platform/playbooks/tasks/apply-openbao-policy.yml"
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 2: Verify** — `bats platform/tests/test_common.bats` (or the new file) passes.

---

## Task 12: Validation & rollout

- [ ] **Step 1: Warm re-run (default mount, regression).** `make local-bootstrap` + `make local-deploy-<svc>` for a couple of services with `bao_kv_mount` unset → KV-enable returns 400 (already at `secret/`), secrets read/write succeed, policies render identically. **No churn, no new mount.**
- [ ] **Step 2: Second-instance smoke (the actual capability).** In a throwaway inventory set `bao_kv_mount: agent-cloud-test`, bootstrap OpenBao only → confirm a new `agent-cloud-test/` mount is created and a service's secrets land under `agent-cloud-test/data/services/<svc>` (verify with `bao kv get agent-cloud-test/services/<svc>`), and the rendered policy scopes to `agent-cloud-test/data/services/*`.
- [ ] **Step 3:** `log()` / document that renaming a live instance's mount is **not** supported by this change (needs `bao secrets move`).

---

## Out of scope (record, don't implement here)

- **`vault_secret_prefix`** (path-prefix namespacing within a mount) — documented separately; composes with this if ever implemented.
- **OpenBao OIDC role group-binding** — the prod follow-up where `platform-developers` must NOT inherit the `platform-admin` policy (grounding/security finding 2026-06-16). Belongs in the prod OIDC hardening, not this mount refactor.
- **Migrating an existing mount's data** — `bao secrets move`; never triggered by defaulting to `secret`.

## Risks

| Risk | Mitigation |
|------|------------|
| A missed reference → a deploy reads/writes the wrong (or legacy) mount | Task 5 Step 1 grep is the completeness gate; Task 11 bats guard fails if `secret/data/services` literals remain in the core paths |
| Policy template not rendered (stale `lookup('file')`) → policy applied with literal `{{ }}` | Task 7 Step 4 reads back the live policy to confirm rendering |
| Semaphore env missing `BAO_KV_MOUNT` → bash libs default to `secret` while Ansible uses the instance mount (split-brain) | Task 9 makes the env explicit; second-instance smoke (Task 12 Step 2) would catch a split |
| KV-enable creates a new empty mount on a typo'd `bao_kv_mount` | Documented; per-instance value is set deliberately in site-config, reviewed |

<!-- ═══════════════════════ source: CREDENTIAL-LIFECYCLE-IMPLEMENTATION.md ═══════════════════════ -->

# Credential Lifecycle Implementation Plan

**Date:** 2026-04-05 (extracted from governance doc 2026-05-07)
**Status:** PROPOSED
**Context:** Implementation phases for the credential lifecycle governance defined in `plan/architecture/04-credentials-access.md`. No phases have been started. Phase 3 (AppRole TTL enforcement) has a dedicated plan at `plan/development/01-secrets-credentials.md`.

---

## Current Problems

1. **Credential accumulation** — Diode OAuth2 clients created every deploy, never deleted (no `delete_client` in plugin API)
2. **No expiry** — AppRole secret_ids have TTL=0, static KV secrets never expire
3. **No audit trail** — No tracking of creation time, creator, last use, or purpose
4. **No revocation workflow** — Decommissioned VMs leave orphaned credentials forever
5. **No multi-site path strategy** — `secret/services/*` has no mechanism for per-site isolation when scaling beyond one site
6. **Static database passwords** — Postgres credentials persist indefinitely (highest risk)

---

## Implementation Phases

| Phase | What | Effort | Impact | Depends On |
|-------|------|--------|--------|------------|
| 1. Composable vault paths | Add `vault_secret_prefix` to site-config inventory, update `manage-secrets.yml` to use it | Low | Foundation for multi-site | — |
| 2. Credential metadata | Implement `write-secret-metadata.yml` task | Low | Audit visibility | Phase 1 |
| 3. AppRole TTL enforcement | secret_id_ttl=90d, token_num_uses=25 | Low | Limits blast radius | — |
| 4. Diode rotation playbook | Create→Verify→Retire with Hydra admin delete | Medium | Stops credential accumulation | Phase 2 |
| 5. Audit playbook + logging | `audit-credentials.yml` + OpenBao audit backend | Medium | Compliance, detection | Phase 2, o11y integrated |
| 6. Dynamic DB secrets | Configure database engine for Postgres | High | Eliminates static DB passwords | Phase 1 |
| 7. Site lifecycle playbooks | `provision-site.yml`, `decommission-site.yml` | Medium | Multi-site readiness | Phases 1-5 |

---

## Phase 1: Composable Vault Paths

Add `vault_secret_prefix: "services"` to site-config inventory. Update `manage-secrets.yml` to construct paths from `{{ vault_secret_prefix }}/{{ service_name }}`. No migration needed — current paths continue to work.

When adding a second site: create a new inventory with `vault_secret_prefix: "sites/<site_id>/services"`.

---

## Phase 2: Credential Metadata

Implement `write-secret-metadata.yml` task that writes KV v2 custom metadata after every secret store operation. Required metadata fields defined in the governance doc.

---

## Phase 3: AppRole TTL Enforcement

See dedicated plan: `plan/development/01-secrets-credentials.md`.

Summary: Update `manage-approle.yml` to use `secret_id_ttl: 2160h` (90 days) and `token_num_uses: 25` with `| default()` patterns. Semaphore orchestrator AppRole explicitly overrides to unlimited (documented exception in governance).

---

## Phase 4: Diode Client Rotation

**Problem:** `netbox_diode_plugin.client` has `create_client()` and `list_clients()` but NO `delete_client()`.

**Solution:** Use Hydra admin API directly for deletion:

```bash
docker exec netbox-hydra-1 hydra admin clients delete <client_id>
```

**Rotation playbook: `rotate-diode-credentials.yml`**
1. List current clients via `list_clients()` in NetBox manage.py shell
2. Create new client via `create_client()`
3. Verify new client: `POST /diode/auth/oauth2/token` with new credentials
4. If verified: delete old clients via `hydra admin clients delete`
5. Store new credentials in OpenBao with `created_at` timestamp
6. Update `.env` on the VM

**Schedule:** Monthly, independent of deploy-orb-agent.yml

---

## Phase 5: Audit Playbook + Logging

**OpenBao audit backend:**
```bash
bao audit enable file file_path=/openbao/audit/audit.log
```

Pipe to observability stack (Loki) for alerting per governance requirements.

**Credential inventory playbook: `audit-credentials.yml`**

Scheduled weekly via Semaphore:
1. List all credentials under `{{ vault_secret_prefix }}` with creation dates
2. Compare against site-config inventory for expected services
3. List all Hydra OAuth2 clients with ages
4. List all AppRoles and their secret_id ages
5. Report: active, stale (>30 days unused), expired, orphaned
6. Flag credentials missing metadata

---

## Phase 6: Dynamic Database Secrets

Configure OpenBao's database secrets engine for Postgres:

```hcl
resource "vault_database_secret_backend_connection" "netbox_pg" {
  backend       = "database"
  name          = "netbox-postgres"
  allowed_roles = ["netbox-app", "netbox-worker"]
  postgresql {
    connection_url = "postgresql://{{username}}:{{password}}@postgres:5432/netbox"
  }
}

resource "vault_database_secret_backend_role" "netbox_app" {
  backend             = "database"
  name                = "netbox-app"
  db_name             = "netbox-postgres"
  creation_statements = ["CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT ALL ON ALL TABLES IN SCHEMA public TO \"{{name}}\";"]
  default_ttl         = 3600   # 1 hour
  max_ttl             = 86400  # 24 hours
}
```

Requires compose changes to fetch credentials at container startup via entrypoint script.

---

## Phase 7: Site Lifecycle Playbooks

### Adding a New Site (`provision-site.yml`)

1. Create site in NetBox (DCIM > Sites)
2. Add site inventory in site-config with `vault_secret_prefix: "sites/<site_id>/services"`
3. Run `provision-site.yml` — creates metadata, SSH keys, AppRoles, discovers architecture
4. Deploy services using standard playbooks (paths resolve via inventory)

### Decommissioning a Site (`decommission-site.yml`)

Per governance requirements: stop services → revoke credentials → archive for 90 days → permanent deletion.

---

## Cross-Team Review Summary

| Reviewer | Key Finding |
|----------|------------|
| **Security** | secret_id TTL=0 is critical risk. 90-day lifecycle for Diode clients. Per-site AppRole isolation. |
| **Network** | Composable vault path prefix per site via inventory. Central OpenBao, path-based isolation. |
| **Infrastructure** | Dynamic DB secrets highest impact. Token usage limits. Audit backend to Loki. |
| **Automation** | No delete_client in Diode plugin — use Hydra admin API. Create→Verify→Retire pattern. |
| **Architecture** | Composable `vault_secret_prefix` driven from site-config inventory. KV v2 metadata for audit. |

---

## Cross-References

- `plan/architecture/04-credentials-access.md` — governance standards this plan implements
- `plan/development/01-secrets-credentials.md` — Phase 3 detailed implementation
- `plan/architecture/04-credentials-access.md` — access and escalation policies
- `plan/architecture/01-automation-model.md` — composable task library used by all phases

<!-- ═══════════════════════ source: APPROLE-TTL-ENFORCEMENT-PLAN.md ═══════════════════════ -->

# AppRole TTL Enforcement Plan

**Date:** 2026-05-06
**Status:** PROPOSED
**Context:** The `manage-approle.yml` task hardcodes `secret_id_ttl: 0` and `token_num_uses: 0`, meaning AppRole credentials never expire and tokens have unlimited uses. This contradicts the 90-day TTL requirement established in CREDENTIAL-LIFECYCLE-PLAN.md and the secure defaults documented in AUTOMATION-COMPOSABILITY.md.

**References:**
- [CREDENTIAL-LIFECYCLE-PLAN.md](../architecture/CREDENTIAL-LIFECYCLE-PLAN.md) -- Defines 90-day secret_id TTL, 25 token_num_uses
- [AUTOMATION-COMPOSABILITY.md](../architecture/AUTOMATION-COMPOSABILITY.md) -- Documents intended defaults and security rationale
- [ACCESS-BOUNDARIES.md](../architecture/ACCESS-BOUNDARIES.md) -- AppRole scope boundaries and blast radius

---

## Problem

### Current Code (manage-approle.yml, lines 59-61)

```yaml
body:
  token_policies:
    - "{{ _approle_name }}"
  token_ttl: "{{ _approle_token_ttl | default('30m') }}"
  token_max_ttl: "{{ _approle_token_max_ttl | default('2h') }}"
  secret_id_ttl: 0
  token_num_uses: 0
```

### What the Documentation Says

CREDENTIAL-LIFECYCLE-PLAN.md specifies:

| Setting | Current Value | Required Value | Risk of Current |
|---------|--------------|----------------|-----------------|
| `secret_id_ttl` | `0` (never expires) | `2160h` (90 days) | A leaked secret_id grants indefinite access to the AppRole's scope |
| `token_num_uses` | `0` (unlimited) | `25` | An intercepted token can make unlimited API calls |

AUTOMATION-COMPOSABILITY.md section "AppRole Management (Composable)" documents these as the intended defaults:
- `_approle_secret_id_ttl` default: `"2160h"` (90 days)
- `_approle_token_num_uses` default: `25`

But the actual task body ignores these variables and hardcodes `0` for both.

### Impact

- **orb-agent AppRole** -- secret_id never expires; a compromised agent credential grants permanent read access to NetBox Diode and SNMP secrets
- **Any future AppRole** created via `manage-approle.yml` -- inherits the same no-expiry behavior
- **Semaphore orchestrator** -- intentionally unlimited (documented exception), but this should be explicit, not a side effect of the hardcoded defaults

---

## Required Changes

### Step 1: Update manage-approle.yml to Use Variables with Secure Defaults

Replace the hardcoded values with variable references that default to the documented secure values:

```yaml
# Before (current)
secret_id_ttl: 0
token_num_uses: 0

# After (proposed)
secret_id_ttl: "{{ _approle_secret_id_ttl | default('2160h') }}"
token_num_uses: "{{ _approle_token_num_uses | default(25) }}"
```

This is a two-line change. Existing callers that do not set `_approle_secret_id_ttl` or `_approle_token_num_uses` will automatically get the secure defaults (90-day TTL, 25 uses).

### Step 2: Explicitly Override for Semaphore Orchestrator

The Semaphore orchestrator AppRole is the documented exception. Its callers must explicitly pass the unlimited values:

```yaml
# In deploy-openbao.yml or wherever Semaphore's AppRole is provisioned
- include_tasks: tasks/manage-approle.yml
  vars:
    _approle_name: "semaphore"
    _approle_secret_id_ttl: "0"    # Orchestrator exception: unlimited
    _approle_token_num_uses: 0      # Orchestrator exception: unlimited
    _approle_policy: "{{ semaphore_policy }}"
```

### Step 3: Add secret_id Rotation Playbook

With a 90-day TTL, secret_ids will expire. A rotation playbook must be created and scheduled in Semaphore to run before expiry (e.g., every 60 days):

```yaml
# rotate-approle-secrets.yml (new playbook)
# For each AppRole with bounded TTL:
#   1. Generate new secret_id via manage-approle.yml
#   2. Update the stored credentials in OpenBao
#   3. Verify the new secret_id authenticates successfully
#   4. Old secret_id expires naturally via TTL (no manual revocation needed)
```

### Step 4: Add Semaphore Template

Add a new template to `platform/semaphore/templates.yml`:

```yaml
- name: Rotate AppRole Secrets
  playbook: platform/playbooks/rotate-approle-secrets.yml
  schedule: "0 3 1 */2 *"  # 3 AM on the 1st of every other month (60-day cycle)
```

---

## Testing Approach

### Unit Testing (Before Merge)

1. **Verify variable substitution** -- Run `ansible-playbook --check` with debug to confirm the `default()` filter produces `2160h` and `25` when no overrides are passed
2. **Verify Semaphore override** -- Confirm that passing `_approle_secret_id_ttl: "0"` produces `0` in the API call body
3. **Lint** -- Ensure `ansible-lint` passes on the modified task file

### Integration Testing (After Merge, Before Production)

1. **Create a test AppRole** with the new defaults:
   ```
   Semaphore -> manage-approle.yml with _approle_name: "test-ttl-enforcement"
   ```
2. **Verify the AppRole configuration** via OpenBao API:
   ```
   GET /v1/auth/approle/role/test-ttl-enforcement
   -> secret_id_ttl should be 7776000 (2160h in seconds)
   -> token_num_uses should be 25
   ```
3. **Verify secret_id expiry** (accelerated test with short TTL):
   ```
   Create AppRole with _approle_secret_id_ttl: "5m"
   Authenticate successfully with the secret_id
   Wait 6 minutes
   Attempt authentication -> should fail with "secret_id expired"
   ```
4. **Verify token_num_uses** enforcement:
   ```
   Create AppRole with _approle_token_num_uses: 3
   Authenticate and make 3 API calls -> succeed
   Make a 4th API call -> should fail with "token has been used too many times"
   ```
5. **Clean up test AppRole** after testing

### Regression Testing

1. **Deploy NetBox** via Semaphore after the change -- verify full 5-phase deploy succeeds
2. **Deploy Orb Agent** -- verify agent starts and authenticates to OpenBao at runtime
3. **Run validate-all.yml** -- confirm all services remain healthy

---

## Rollout Strategy

### Phase 1: Update Code (Low Risk)

1. Modify `manage-approle.yml` (the two-line change)
2. Update any callers that need the Semaphore exception
3. PR, CI checks, CodeRabbit review
4. Merge to main

**Risk:** None. Existing AppRoles are not retroactively affected. The change only applies when `manage-approle.yml` is next invoked for a given AppRole.

### Phase 2: Apply to New AppRoles First

1. Create any new per-service AppRoles (e.g., `netbox-deploy`, `nocodb-deploy`) with the new defaults
2. These AppRoles will have 90-day TTL from creation
3. Validate that deployments work correctly with bounded TTLs

### Phase 3: Rotate Existing AppRoles

Apply the new settings to existing AppRoles one at a time, in order of increasing blast radius:

| Order | AppRole | Action | Verification |
|-------|---------|--------|-------------|
| 1 | `orb-agent` | Re-run `manage-approle.yml` (gets new defaults) | Verify orb-agent authenticates, discovery works |
| 2 | Future per-service AppRoles | Created with defaults | Verify deploy playbook succeeds |
| 3 | `semaphore` | Re-run with explicit `_approle_secret_id_ttl: "0"` override | Verify all Semaphore templates still execute |

**Note:** The Semaphore orchestrator AppRole is the LAST to be touched, and it retains `secret_id_ttl: 0` intentionally. The rotation for Semaphore is a re-application of the explicit override to confirm the code path works, not a TTL change.

### Phase 4: Schedule Rotation

1. Create `rotate-approle-secrets.yml` playbook
2. Add Semaphore template with 60-day cron schedule
3. Run manually once to verify
4. Enable scheduled execution

---

## AppRoles Affected

| AppRole | Current TTL | Target TTL | Exception? | Notes |
|---------|------------|------------|------------|-------|
| `semaphore` | 0 (unlimited) | 0 (unlimited) | Yes -- orchestrator | Broad scope requires unlimited; compensated by Semaphore runner isolation |
| `orb-agent` | 0 (unlimited) | 2160h (90 days) | No | Runtime agent; should have bounded lifetime |
| Future per-service | N/A | 2160h (90 days) | No | Created with secure defaults from day one |

---

## Success Criteria

- [ ] `manage-approle.yml` uses `default('2160h')` for `secret_id_ttl` and `default(25)` for `token_num_uses`
- [ ] Semaphore orchestrator AppRole explicitly overrides to `0` (documented exception)
- [ ] New AppRoles created via the task get 90-day TTL without callers needing to specify it
- [ ] orb-agent AppRole re-provisioned with 90-day TTL and continues to function
- [ ] Rotation playbook exists and is scheduled in Semaphore
- [ ] All existing CI tests pass after the change

<!-- ═══════════════════════ source: ANSIBLE-CREDENTIAL-REDACTION-PLAN.md ═══════════════════════ -->

# Ansible Credential Redaction Plan

**Date:** 2026-05-06
**Status:** PLANNING
**Depends on:** Callback plugin implementation, testing in non-production Semaphore run

---

## Problem Statement

The codebase uses `no_log: true` on 41 Ansible tasks to prevent secrets from appearing in terminal or Semaphore output. This approach has critical drawbacks:

1. **Debugging is impossible** -- when a `no_log` task fails, the entire output is replaced with `CENSORED`, including the error message, HTTP status code, and response body
2. **Manual annotation is error-prone** -- every new task that touches secrets must remember to add `no_log: true`, and forgetting leaves secrets exposed
3. **No partial redaction** -- `no_log` is all-or-nothing; you cannot show the task name and error while hiding just the secret value
4. **No audit trail** -- there is no indication in logs that redaction occurred or what was redacted

## Solution: Custom Callback Plugin

A callback plugin named `redact_secrets` will intercept all Ansible output events, scan for values that match known sensitive variable patterns, and replace them with `***REDACTED***`. The actual values remain in Ansible memory for use by subsequent tasks.

## Design

### Plugin Location

```
platform/playbooks/callback_plugins/redact_secrets.py
```

### Plugin Class

```python
"""Ansible callback plugin that redacts sensitive values from output.

Replaces no_log: true across all playbooks. Values matching sensitive
variable name patterns are replaced with ***REDACTED*** in displayed
output while remaining available to subsequent tasks.
"""

from ansible.plugins.callback.default import CallbackDefault

DOCUMENTATION = """
    name: redact_secrets
    type: stdout
    short_description: Redact sensitive values from task output
    description:
        - Replaces values of known-sensitive variables with ***REDACTED***
        - Preserves task names, status, and error messages
        - Actual values remain in memory for task execution
    requirements:
        - Set as stdout_callback in ansible.cfg
"""

# Variable name patterns that contain secrets.
# Matching is case-insensitive and uses fnmatch-style globs.
SENSITIVE_PATTERNS = [
    # OpenBao authentication
    "_bao_auth",
    "_bao_existing",
    "_admin_auth",
    "client_token",
    "X-Vault-Token",

    # Resolved/generated secrets
    "_resolved",
    "_existing",

    # AppRole credentials
    "_bao_role_id",
    "_bao_secret_id",
    "_new_role_id",
    "_new_secret_id",
    "role_id",
    "secret_id",

    # Diode credentials
    "_orb_client_secret",
    "_new_creds",
    "_orb_approle_resp",

    # SSH keys
    "_key_content",
    "_ssh_private_key",
    "_ssh_public_key",

    # Generic patterns (fnmatch)
    "*_password",
    "*_secret*",
    "*_token",
    "*_key",
    "*_credential*",
]

# Value patterns that indicate a secret even if the variable name
# does not match (e.g., inline vault tokens in URIs).
VALUE_PATTERNS = [
    r"hvs\.[A-Za-z0-9]{20,}",          # Vault/OpenBao tokens
    r"s\.[A-Za-z0-9]{20,}",            # Legacy vault tokens
    r"ghp_[A-Za-z0-9]{36,}",           # GitHub PATs
    r"BEGIN .* PRIVATE KEY",            # PEM key material
]


class CallbackModule(CallbackDefault):
    """Stdout callback that redacts sensitive variable values."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "redact_secrets"

    def __init__(self):
        super().__init__()

    def _redact_dict(self, data, depth=0):
        """Recursively redact sensitive values in a dict."""
        if depth > 10:  # prevent infinite recursion
            return data
        if isinstance(data, dict):
            redacted = {}
            for key, value in data.items():
                if self._is_sensitive_key(key):
                    redacted[key] = "***REDACTED***"
                elif isinstance(value, (dict, list)):
                    redacted[key] = self._redact_dict(value, depth + 1)
                elif isinstance(value, str) and self._is_sensitive_value(value):
                    redacted[key] = "***REDACTED***"
                else:
                    redacted[key] = value
            return redacted
        elif isinstance(data, list):
            return [self._redact_dict(item, depth + 1) for item in data]
        return data

    def _is_sensitive_key(self, key):
        """Check if a key matches sensitive patterns."""
        import fnmatch
        key_lower = str(key).lower()
        for pattern in SENSITIVE_PATTERNS:
            if fnmatch.fnmatch(key_lower, pattern.lower()):
                return True
        return False

    def _is_sensitive_value(self, value):
        """Check if a value matches known secret formats."""
        import re
        for pattern in VALUE_PATTERNS:
            if re.search(pattern, str(value)):
                return True
        return False

    def _redact_result(self, result):
        """Create a redacted copy of a task result for display."""
        result_copy = result._result.copy()
        return self._redact_dict(result_copy)

    def v2_runner_on_ok(self, result):
        """Intercept successful task results and redact secrets."""
        result._result = self._redact_result(result)
        super().v2_runner_on_ok(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        """Intercept failed task results and redact secrets."""
        result._result = self._redact_result(result)
        super().v2_runner_on_failed(result, ignore_errors)

    def v2_runner_on_skipped(self, result):
        """Pass through skipped tasks unchanged."""
        super().v2_runner_on_skipped(result)
```

### Configuration

Add to `ansible.cfg` (or create one at the playbook root):

```ini
[defaults]
# Use the redact_secrets callback plugin for all output
stdout_callback = redact_secrets
callback_plugins = ./callback_plugins

# Ensure callbacks are whitelisted
callback_whitelist = redact_secrets
```

For Semaphore, the `ansible.cfg` must be present in the playbook directory that Semaphore clones. Since Semaphore clones agent-cloud and runs playbooks from `platform/playbooks/`, the config file goes at:

```
platform/playbooks/ansible.cfg
```

### Variable Name Patterns to Redact

These patterns are derived from the 41 `no_log: true` instances currently in the codebase:

| Source File | Variables Registered/Set | Pattern |
|-------------|------------------------|---------|
| `tasks/manage-secrets.yml` | `_bao_auth`, `_bao_existing`, `_resolved` | Exact match |
| `tasks/manage-approle.yml` | `_admin_auth`, `_new_role_id`, `_new_secret_id`, `_secret_id_resp` | Exact match |
| `tasks/manage-diode-credentials.yml` | `_bao_auth`, `_orb_client_secret`, `_new_creds` | Exact match |
| `tasks/seed-discovery-credential.yml` | `_bao_auth`, credential fields | Exact match |
| `tasks/update-vault-field.yml` | `_bao_auth`, field values | Exact match |
| `tasks/apply-openbao-policy.yml` | `_bao_auth` | Exact match |
| `deploy-orb-agent.yml` | `_bao_auth`, `_orb_approle_resp` | Exact match |
| `deploy-netbox.yml` | `_bao_auth` (via include_tasks) | Exact match |
| `check-secrets.yml` | `_bao_auth`, `_bao_data` | Exact match |
| `validate-secrets.yml` | `_bao_auth`, `_bao_data` | Exact match |
| `sync-secrets-to-openbao.yml` | `_bao_auth`, secret data | Exact match |
| `distribute-ssh-keys.yml` | `_key_file` content | Exact match |
| `harden-ssh.yml` | `_key_file` content | Exact match |
| `update-proxmox-token.yml` | `_bao_auth` | Exact match |
| `apply-openbao-policies.yml` | `_bao_auth` | Exact match |
| `provision-template.yml` | SSH key content | Exact match |
| `seed-discovery-credentials.yml` | `_bao_auth` | Exact match |
| All URI tasks | `body.role_id`, `body.secret_id`, `headers.X-Vault-Token` | Nested key match |

### What Gets Redacted vs. What Stays Visible

**Visible (preserved in output):**
- Task name (e.g., "Authenticate to OpenBao")
- Task status (ok, failed, changed, skipped)
- Error messages (e.g., "Status code was 403, expected 200")
- Non-sensitive fields (e.g., `status_code`, `url`, `method`)
- Loop labels (already designed to show safe values)

**Redacted (replaced with `***REDACTED***`):**
- `client_token` values in auth responses
- `role_id` and `secret_id` in request bodies
- `X-Vault-Token` header values
- Full `_resolved` dictionary contents
- SSH key file contents
- Any string matching vault token patterns (`hvs.xxx`, `s.xxx`)

### Example Output Comparison

**Before (with `no_log: true`):**
```
TASK [Authenticate to OpenBao] ****
ok: [netbox-vm] => {"censored": "the output has been hidden due to the fact that 'no_log: true' was set"}
```

**After (with callback plugin):**
```
TASK [Authenticate to OpenBao] ****
ok: [netbox-vm] => {
    "json": {
        "auth": {
            "client_token": "***REDACTED***",
            "policies": ["semaphore-read", "default"],
            "token_type": "service"
        }
    },
    "status": 200,
    "url": "https://openbao.example.com:8200/v1/auth/approle/login"
}
```

**Before (with `no_log: true`, task fails):**
```
TASK [Store secrets in OpenBao] ****
fatal: [netbox-vm]: FAILED! => {"censored": "the output has been hidden due to the fact that 'no_log: true' was set"}
```

**After (with callback plugin, task fails):**
```
TASK [Store secrets in OpenBao] ****
fatal: [netbox-vm]: FAILED! => {
    "msg": "Status code was 403, not one of [200]",
    "status": 403,
    "url": "https://openbao.example.com:8200/v1/secret/data/services/netbox",
    "json": {
        "errors": ["permission denied"]
    }
}
```

The error message, status code, and URL are all visible for debugging. Only the token in the request header was redacted.

## Testing Approach

### Unit Tests

Create `platform/playbooks/callback_plugins/tests/test_redact_secrets.py`:

```python
"""Tests for the redact_secrets callback plugin."""

import pytest
from callback_plugins.redact_secrets import CallbackModule

@pytest.fixture
def plugin():
    return CallbackModule()

class TestRedactDict:
    def test_redacts_exact_match_key(self, plugin):
        data = {"client_token": "hvs.abc123", "status": 200}
        result = plugin._redact_dict(data)
        assert result["client_token"] == "***REDACTED***"
        assert result["status"] == 200

    def test_redacts_glob_pattern_key(self, plugin):
        data = {"db_password": "secret123", "db_host": "localhost"}
        result = plugin._redact_dict(data)
        assert result["db_password"] == "***REDACTED***"
        assert result["db_host"] == "localhost"

    def test_redacts_nested_dict(self, plugin):
        data = {"auth": {"client_token": "hvs.abc", "policies": ["default"]}}
        result = plugin._redact_dict(data)
        assert result["auth"]["client_token"] == "***REDACTED***"
        assert result["auth"]["policies"] == ["default"]

    def test_redacts_value_pattern(self, plugin):
        data = {"response": "token is hvs.ABCDEFghijklmnopqrst"}
        result = plugin._redact_dict(data)
        assert result["response"] == "***REDACTED***"

    def test_preserves_non_sensitive(self, plugin):
        data = {"status": 200, "url": "https://example.com", "method": "POST"}
        result = plugin._redact_dict(data)
        assert result == data

    def test_handles_empty_dict(self, plugin):
        assert plugin._redact_dict({}) == {}

    def test_handles_list_of_dicts(self, plugin):
        data = [{"secret_id": "abc"}, {"name": "test"}]
        result = plugin._redact_dict(data)
        assert result[0]["secret_id"] == "***REDACTED***"
        assert result[1]["name"] == "test"

    def test_depth_limit(self, plugin):
        # Build deeply nested dict
        data = {"a": "value"}
        current = data
        for _ in range(15):
            current["nested"] = {"a": "value"}
            current = current["nested"]
        current["secret_id"] = "should-not-crash"
        result = plugin._redact_dict(data)
        assert result is not None

class TestIsSensitiveKey:
    @pytest.mark.parametrize("key", [
        "_bao_auth", "_bao_existing", "_admin_auth", "_resolved",
        "_new_role_id", "_new_secret_id", "client_token",
        "X-Vault-Token", "db_password", "redis_password",
        "secret_key", "_orb_client_secret",
    ])
    def test_sensitive_keys(self, plugin, key):
        assert plugin._is_sensitive_key(key)

    @pytest.mark.parametrize("key", [
        "status", "url", "method", "msg", "policies",
        "token_type", "changed", "failed",
    ])
    def test_non_sensitive_keys(self, plugin, key):
        assert not plugin._is_sensitive_key(key)
```

### Integration Test

Run a test playbook that exercises vault interactions with verbose output and verify:

1. Task names are visible
2. Error messages are visible when tasks fail
3. Secret values are replaced with `***REDACTED***`
4. Subsequent tasks can still use the actual secret values

```yaml
# test-redaction.yml -- manual integration test
---
- name: Test credential redaction
  hosts: localhost
  gather_facts: false
  vars:
    test_password: "SuperSecret123"
    test_token: "hvs.TestTokenValue12345678901234"
  tasks:
    - name: "Set a fact with sensitive data"
      ansible.builtin.set_fact:
        _bao_auth:
          json:
            auth:
              client_token: "{{ test_token }}"
              policies: ["default"]

    - name: "Display the auth result (should be redacted)"
      ansible.builtin.debug:
        var: _bao_auth

    - name: "Use the token in a subsequent task (should work)"
      ansible.builtin.debug:
        msg: "Token length: {{ _bao_auth.json.auth.client_token | length }}"
```

## Migration Plan

### Phase 1: Deploy Plugin (non-breaking)

1. Create `platform/playbooks/callback_plugins/redact_secrets.py`
2. Create `platform/playbooks/ansible.cfg` with `stdout_callback = redact_secrets`
3. Run unit tests
4. Test in a single Semaphore run (e.g., `check-secrets.yml` which is read-only)
5. Verify output shows redacted values, not `CENSORED`

### Phase 2: Remove no_log (per-file)

Remove `no_log: true` from each file, one at a time, testing after each removal:

| Order | File | Instances | Risk |
|-------|------|-----------|------|
| 1 | `check-secrets.yml` | 2 | Low (read-only) |
| 2 | `validate-secrets.yml` | 5 | Low (read-only) |
| 3 | `tasks/manage-secrets.yml` | 5 | Medium (write ops) |
| 4 | `tasks/manage-approle.yml` | 4 | Medium (write ops) |
| 5 | `tasks/manage-diode-credentials.yml` | 4 | Medium (write ops) |
| 6 | `tasks/seed-discovery-credential.yml` | 4 | Medium (write ops) |
| 7 | `tasks/update-vault-field.yml` | 2 | Low |
| 8 | `tasks/apply-openbao-policy.yml` | 1 | Low |
| 9 | `deploy-orb-agent.yml` | 3 | Medium |
| 10 | `deploy-netbox.yml` | 1 | Medium |
| 11 | `distribute-ssh-keys.yml` | 1 | Low |
| 12 | `harden-ssh.yml` | 1 | Low |
| 13 | `sync-secrets-to-openbao.yml` | 4 | Medium |
| 14 | `update-proxmox-token.yml` | 1 | Low |
| 15 | `apply-openbao-policies.yml` | 1 | Low |
| 16 | `seed-discovery-credentials.yml` | 1 | Low |
| 17 | `provision-template.yml` | 1 | Low |

After each removal, run the affected playbook in Semaphore and verify the output is properly redacted.

### Phase 3: Enforce Ban

Add a CI lint step that fails if any `no_log: true` is found:

```yaml
- name: Enforce no_log ban
  run: |
    FOUND=$(grep -rn 'no_log:\s*true' platform/playbooks/ --include='*.yml' || true)
    if [ -n "$FOUND" ]; then
      echo "ERROR: no_log: true is banned. Use the redact_secrets callback plugin."
      echo "$FOUND"
      exit 1
    fi
```

Update `platform/playbooks/README.md` to remove the `no_log: true` convention and document the callback plugin.

### Phase 4: Ongoing Maintenance

- New sensitive variable patterns are added to `SENSITIVE_PATTERNS` in the plugin
- Unit tests are updated to cover new patterns
- Plugin is version-controlled alongside playbooks
- No task in `platform/playbooks/` may use `no_log: true`

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Plugin fails to load | Ansible falls back to default callback; secrets may appear in output. Add health check in CI. |
| Pattern misses a new secret variable | Pattern list uses globs (`*_password`, `*_secret*`); most new secrets match existing patterns. Review new tasks for novel patterns. |
| False positive redaction | Non-sensitive values like `token_type` or `key_name` could match glob patterns. Use exact matches for ambiguous names. Test thoroughly. |
| Performance overhead | Redaction adds a dict walk per task result. For typical playbook runs (< 100 tasks), overhead is negligible. |
| Semaphore callback compatibility | Semaphore uses Ansible's default stdout callback. Replacing it with `redact_secrets` (which extends `CallbackDefault`) maintains compatibility. |

## Files to Create

| File | Purpose |
|------|---------|
| `platform/playbooks/callback_plugins/redact_secrets.py` | The callback plugin |
| `platform/playbooks/callback_plugins/__init__.py` | Package marker |
| `platform/playbooks/callback_plugins/tests/test_redact_secrets.py` | Unit tests |
| `platform/playbooks/ansible.cfg` | Plugin configuration |

## Files to Modify

| File | Change |
|------|--------|
| All 17 playbook/task files listed above | Remove `no_log: true` |
| `platform/playbooks/README.md` | Update credential handling docs |
| `.github/workflows/lint-and-test.yml` | Add `no_log` ban enforcement step |
| `CLAUDE.md` | Document callback plugin convention |

## Revision History

| Date | Change |
|------|--------|
| 2026-05-06 | Initial plan -- plugin design, migration order, testing approach |
