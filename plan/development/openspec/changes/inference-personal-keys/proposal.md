# Personal inference keys issued through Authentik SSO

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Decisions by Joe, 2026-09-27.

Companion changes: `inference-gateway-agentgateway` (this change extends its key
rendering, budgets and telemetry; it must be deployed first), plus the OpenBao and
Authentik services this change configures. Related: agent-cloud PR #289 (the
Cloudflare skip rule that lets OIDC relying parties reach Authentik's machine endpoints,
merged to `dev` as `platform/infra/cloudflare/waf.tf:129-135`), which the deferred JWT
path below would depend on, as would this change's own OpenBao OIDC mount.

## Why

The inference gateway gives every client its own key, but "client" today means a name
an operator typed into inventory. `agw_clients` is a flat list; the deploy mints one
`client_<name>` field per entry on `secret/services/agentgateway`, enrols its sha256
hash, and a person receives the value through the site-config backup channel
(`platform/playbooks/vars/secret-declarations/agentgateway.yml:16-29`,
`platform/services/agentgateway/deployment/templates/config.yaml.j2:123-157`,
`platform/playbooks/manage-agentgateway-client-key.yml:19-25`). That works for four
agent and operator identities. It does not work for a team:

- **Access is not tied to a person's account.** Nobody's key follows their Authentik
  account. Offboarding someone means an operator remembering to edit inventory and run
  a revoke, in that order (`manage-agentgateway-client-key.yml:75-82`).
- **Handout is an operator task.** Every new key is a site-config branch an operator
  creates and a person is sent. There is no self-service path.
- **Keys never expire.** A minted key is reused on every deploy until someone rotates
  it by hand (`manage-agentgateway-client-key.yml:31-34`). A key copied into a laptop's
  shell profile two years ago still works.
- **Personal budgets need an API key.** In agentgateway v1.5.0 per-key token budgets
  attach only to API keys: the budget policy acts on budgets matched to an `apiKey`
  entry, and a JWT-authenticated caller gets no personal budget (Joe, 2026-09-27; the
  budget shape the gateway renders today is `config.yaml.j2:140-156`). So "log in with
  SSO and call the model" cannot, at this version, give each person their own spend
  guard.

## What Changes

- **Eligibility is an Authentik group.** A new `inference-users` group in the platform
  groups blueprint. Membership is declared as code in the platform users blueprint,
  the same way the other groups are assigned today
  (`platform/services/authentik/deployment/blueprints/platform-groups.yaml`,
  `platform-users.yaml:11-13`). It is a separate grant, not implied by any existing
  tier, because inference spend is a distinct resource.
- **SSO issues a personal API key.** For each active member of `inference-users` the
  platform mints one gateway key with identity `user-<authentik-username>`, stores it
  at `secret/users/<username>/inference` in OpenBao, and enrols its sha256 hash at the
  gateway with its own token budget. Agent identities stay in `agw_clients`; person
  identities there migrate to personal keys.
- **Self-service delivery through OpenBao.** OpenBao gains a dedicated OIDC auth mount
  against a new Authentik application for inference users. A user logs in to OpenBao
  (UI or CLI) through Authentik and can read only their own key, enforced by a
  templated ACL policy on their identity alias name. Policies stay `.hcl` code applied
  by playbook (AGENTS.md, "Policy and Configuration Changes — Code Only").
- **Reconcile from group membership.** A new Semaphore template, `Reconcile Inference
  User Keys`, lists the group's active members and converges OpenBao to it: mint for
  new members, rotate aged keys, drop expired previous keys, revoke keys of anyone no
  longer eligible, then import the gateway deploy, which restarts the gateway only when
  its rendered configuration changed (the gateway change's change-aware deploy). It runs
  hourly on a schedule declared in `templates.yml`, and the platform's shared
  scheduled-job-silent alert watches it.
- **30-day rotation with a grace overlap, enforced.** Every key has a hard 30-day
  lifetime written into its record. While a key change restarts the gateway, keys
  rotate together on one cohort day every 27 days, so rotation restarts the gateway a
  fixed number of times per cycle; if the gateway applies key changes without a
  restart, each key's successor is minted on its own day 27. Both keys are valid until
  the old one's expiry. The gateway
  deploy renders a key only while its recorded expiry is in the future and refuses a
  record with no parseable expiry, so expiry holds even if nobody remembers it — the
  same principle as `legacy_shared_expires` in the gateway change (its task 5.1).
- **Offboarding is removal from the group.** A user removed from `inference-users`, or
  deactivated in Authentik, loses their key on the next reconcile, at most one hour
  later. The record is deleted with all its versions.
- **The gateway UI stays read-only.** Keys are never created or edited in the gateway UI.
  The rendered config is mounted read-only (`compose.yml:48`), and the gateway change's
  task 1.10 sets `UI_READ_ONLY`, which v1.5.0 reads to make its config store read-only
  (`crates/agentgateway/src/config.rs:390-392` at tag v1.5.0). This change depends on
  that task and does not set the switch again.
- **Per-user observability and audit.** The gateway already labels every metric and
  access-log line with `apiKey.name` (`config.yaml.j2:44-56`), so `user-<name>` appears
  with no config change; the inference dashboard gains a per-user usage row. OpenBao
  gains an audit device configured as code, so "who read their key, and when" is
  answerable. No audit device is configured anywhere in the repository today (searched
  `platform/` and `plan/architecture/` for `sys/audit`, 2026-09-27: no match).
- **JWT at the gateway: considered and deferred.** Accepting Authentik access tokens
  directly (`jwtAuth` permissive composed with an optional `apiKey`) is possible at
  v1.5.0 (Joe, 2026-09-27) but gives JWT callers no personal budget, requires the
  gateway to fetch Authentik's JWKS through Cloudflare (the skip rule from PR #289,
  merged to `dev`), and does
  not fit the SDK clients the team uses, which hold a static bearer key. Recorded in
  design decision 11 with the conditions for revisiting.

## Capabilities

### New Capabilities
- `platform/inference-personal-keys`: personal, self-service, expiring inference keys
  for Authentik users, delivered through OpenBao and enforced at the gateway.

### Modified Capabilities
- None formally. `platform/inference-gateway` is still an open change
  (`inference-gateway-agentgateway`), not an archived spec, so the extensions to its
  key rendering are stated as requirements of the new capability and cross-referenced
  there on archive.

## Impact

- Authentik: `platform-groups.yaml` (new group), `platform-users.yaml` (memberships),
  a new `openbao-inference-oidc.yaml` blueprint (provider and application),
  `app-catalog.yml` (the application and a new `inference` access tier),
  `templates/zz-sso-bindings.yaml.j2` (the tier's gate, and an extra binding admitting
  the group to the existing `openbao` forward_auth application), `env.j2` and
  `deploy-authentik.yml` (the new client secret), and a read-only membership listing
  script run inside the server container, the pattern `deploy-authentik.yml:385-390`
  already uses.
- OpenBao: new policy template `inference-user-self.hcl.j2` under
  `platform/services/openbao/deployment/config/policies/`; the orchestrator policy
  file gains the user-key paths and the new auth mount; a new
  `configure-openbao-inference-oidc.yml` (auth mount, config, role) through a shared
  `tasks/configure-bao-oidc-mount.yml` extracted from `bootstrap-local-dev.yml`, which
  calls it too; `tasks/apply-openbao-policy.yml` made render-aware for `.hcl.j2`, with
  `apply-openbao-policies.yml` switched to it; an audit device playbook.
- Gateway: `deploy-agentgateway.yml` (read user records, fail closed on a malformed
  one), `config.yaml.j2` (render user keys), BATS.
- New playbook `reconcile-inference-user-keys.yml`; `platform/semaphore/templates.yml`
  (the scheduled template and its Dev variant).
- o11y: an inference dashboard row per user identity.
- site-config: memberships (the real usernames), prod redirect and launch URIs for the
  new application, budget figures.
- Docs: the gateway's `context/architecture.md` key-lifecycle table, a user-facing
  "get your inference key" page, `AGENTS.md` secrets-layout row for `secret/users/*`.

## Rollback Plan

- Stop issuing: disable the scheduled template in `templates.yml` and run
  `setup-templates.yml`. Existing keys keep working until their recorded expiry, then
  drop out on the next gateway deploy, because the deploy enforces expiry.
- Withdraw all personal keys at once: run the reconcile with the group emptied in the
  users blueprint and `-e allow_mass_revoke=true`, which deletes every record and
  re-renders the gateway. Agent identities in `agw_clients` are untouched.
- Remove self-service login: disable the `oidc-inference` auth mount through its
  configure playbook (`state: absent`) and remove the Authentik application from
  `authentik_apps`. The admin OIDC login is on a different mount and is unaffected.
- Nothing in this change alters the vLLM key, the Caddy route or the existing client
  identities, so no rollback touches the request path beyond the rendered key list.
