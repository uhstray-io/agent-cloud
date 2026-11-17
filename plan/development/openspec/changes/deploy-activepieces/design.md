Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Context

Activepieces is the platform's workflow-automation service. It replaces n8n, which was retired on 2026-11-12. The approved plan is `plan/development/26-activepieces.md`. It fixes thirteen decisions, D1 to D13, and eight open items, O1 to O8. This design summarises those decisions and records the risks and open questions. The plan holds the full reasoning, the upstream facts with their source links, and the validation criteria.

The composable-service shape comes from the Postiz and Plane changes. `plan/development/14-postiz-social-publishing.md` and `plan/development/openspec/changes/deploy-plane-work-management/` are the closest precedents.

## Goals and Non-Goals

**Goals**

- One workflow-automation service, deployed and operated through Semaphore playbooks with secrets from OpenBao.
- Flows run without privileged containers.
- The encryption key is generated once and survives every redeploy.
- Sign-in is limited to platform administrators, and account creation is closed once the owner exists.
- The database can be backed up and restored without losing a live copy.

**Non-Goals**

- Migrating any data from n8n (D7).
- The inference-gateway client certificate and identity, which belong to plan 27.
- The content flows, which belong to plan 28.
- S3 object storage, SAML and paid SSO.

## Decisions

Each decision is recorded in full in `plan/development/26-activepieces.md`.

- **D1.** Community edition, with `AP_EDITION=ce` set explicitly. See the API-key risk below.
- **D2.** Authentik forward-auth at Caddy, bound to `platform-admins`. Administrators hold a second, local login.
- **D3.** `AP_EXECUTION_MODE=SANDBOX_CODE_ONLY`, so no container is privileged. npm is unavailable inside Code steps.
- **D4.** File storage in the database. S3 is deferred.
- **D5.** One app container, one worker, Postgres and Redis, in one compose project on rootless podman. The worker reaches the app at `http://app`.
- **D6.** Image `ghcr.io/activepieces/activepieces:0.93.0`, pinned by digest.
- **D7.** Greenfield, with no data migrated from n8n.
- **D8.** Four generated-once secrets, and a fifth only if O2 requires it. No deploy rotates them.
- **D9.** Telemetry off. The queue UI off.
- **D10.** The first-account window is closed by creating the owner before the port is admitted, by a default-deny firewall, and by opening the route last.
- **D11.** `AP_NETWORK_MODE=STRICT`, with an empty SSRF allow list by default.
- **D12.** No inbound webhook is exempt by default. Each exemption is an exact path, approved per flow.
- **D13.** Out of scope, as listed in Non-Goals.

## Risks and Trade-offs

- **API key on Community (F27, O8).** The docs say API keys come from a dashboard that is available only in the Platform and Enterprise editions. If a Community instance has no supported way to create a key, the flows-as-code and connections-by-playbook uses in plan 28 have no authenticated path. This is unverified at runtime. Task 1.5 runs an early spike. A refuted result returns the edition decision to Joseph, and no workaround is built.
- **Object storage (D4).** Upstream calls S3 a hard requirement for its throughput figures (F13). Database storage is accepted for this single-tenant, low-volume instance. The decision is revisited when an object store exists or flow volume grows.
- **Two sign-ins (D2).** Each administrator signs in through Authentik and also holds a local Activepieces login. This is the accepted cost of avoiding a paid SSO tier.
- **Readiness (F18, O1).** The health route does not report migration completion. Until a readiness signal is found in task 1.2, the deploy starts the app alone, waits for its health route, and then starts the worker.
- **Worker token (F16, O2).** The upstream compose worker sets no token. Whether a co-located worker needs one is decided in tasks 1.2 and 1.3.
- **Secret format (R1, F4, O3).** `manage-secrets` cannot emit hexadecimal values, and the upstream docs disagree on the JWT length. Task 1.3 adds the type and checks the format before the first generate.
- **First-account window (F20, O5, O6).** The invitation check runs only when a sign-up carries a platform id. The docs say the first account becomes the administrator. Task 1.4 proves that an uninvited sign-up is refused on a live instance. Captcha is not the control, because it is off unless both Turnstile keys are set (F26).

## Migration Plan

None. The service is greenfield (D7). Nothing is migrated from n8n or from any earlier instance.

## Open Questions

- **O1.** Is there a readiness signal for migration completion? Task 1.2 finds it or records the gap.
- **O2.** Does a co-located compose worker need `AP_WORKER_TOKEN`? Tasks 1.2 and 1.3 decide it, and the playbook mints it without a manual step if it is needed.
- **O3.** Which secret format and length do the JWT and encryption keys accept? Task 1.3 checks before the first generate.
- **O4.** Does the image need pgvector or plain Postgres? Task 1.2 decides it, and the chosen image is pinned by digest.
- **O5.** With no Turnstile key, does the deploy create the owner without a captcha token? Task 1.4 proves it.
- **O6.** How do platform administrators get local accounts without SMTP? Task 1.4 decides it.
- **O7.** Which flows, if any, need an exact-path webhook exemption? Task 2.5 decides it, with Joseph's approval per flow.
- **O8.** Does a Community instance at 0.93.0 allow one authenticated API call? Early spike in task 1.5. A refuted result goes to Joseph.
