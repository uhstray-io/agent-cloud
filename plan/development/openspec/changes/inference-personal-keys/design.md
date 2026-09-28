# Design: personal inference keys issued through Authentik SSO

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-27 by reading the files named, unless marked otherwise.

- **Gateway key rendering.** `config.yaml.j2:131-165` renders one `apiKey` entry per
  `agw_clients` name: `keyHash: sha256:<hex>` of the OpenBao value (plaintext only
  under the local-dev flag, lines 134-138), `metadata.name` set to the identity,
  optional `allowedModels`, and a `budgets` entry `hourly-tokens` (`unit: Tokens`,
  `window.rolling: 1h`, `onBudgetExceeded: Block`) whose amount comes from
  `agw_client_policies.<name>.tokens_per_hour` or `agw_rate_tokens_per_hour`. Metrics
  carry `identity: apiKey.name` (lines 44-47), and so do access-log lines (lines 85-87)
  and the OTLP access-log export (line 96). One global `localRateLimit` request bucket
  (lines 175-179). Line numbers re-read 2026-09-28 at `f92b0bf`.
- **Where client keys live.** `vars/secret-declarations/agentgateway.yml:16-29`
  declares `client_<name>` as `random`, length 48, on the service's own secret path,
  so `manage-secrets` mints once and reuses. The deploy asserts identity names match
  `^[a-z0-9][a-z0-9-]*$` because they land in YAML, JSON and a CEL literal
  (`deploy-agentgateway.yml:86-93`).
- **Rotate and revoke today.** `manage-agentgateway-client-key.yml`: rotate requires the
  name in inventory, revoke requires it gone (lines 67-82); the new value is pinned in
  a fact before the write (lines 102-109); writes go through `tasks/bao-merge-keys.yml`
  (merge-patch, siblings preserved, removal by null); the run ends by importing the
  deploy (line 145).
- **Reload is a container recreate today.** `deploy.sh:47-53` runs `up -d
  --force-recreate` on every run, because a changed bind-mounted config is not a compose
  change. The gateway change makes the deploy change-aware (`inference-gateway-agentgateway`
  decision 11, task 1.12): it recreates only when the rendered files differ from what the
  running container started with, or when no container is running, and it settles
  whether v1.5.0 applies a key change without a restart. This change relies on that
  deploy and adds no restart logic of its own.
- **UI.** The config is mounted read-only (`compose.yml:48`). v1.5.0 reads `UI_READ_ONLY`
  and switches its config store to read-only (`crates/agentgateway/src/config.rs:390-392`
  at tag v1.5.0, read 2026-09-28), and the UI refuses writes in that mode
  (`crates/agentgateway/src/ui.rs:53`). The gateway change's task 1.10 sets
  `UI_READ_ONLY=true` in `env.j2`, done in PR #303 (open against `dev` on 2026-09-28;
  this tree at `f92b0bf` does not carry it yet, grep 2026-09-28). This change depends on
  that task instead of setting it again.
- **Budgets and JWT at v1.5.0 (Joe, 2026-09-27, not re-verified here).** Per-key token
  budgets attach only to API keys; a JWT-authenticated caller gets no personal budget;
  `jwtAuth` in permissive mode can be composed with an optional `apiKey`.
- **OpenBao OIDC exists only in local-dev.** `bootstrap-local-dev.yml:533-624` enables
  the `oidc` auth method with the root token (the mount, config and role writes are the
  inline tasks at lines 551-562, 579-602 and 604-624), configures it against the Authentik
  application `openbao-oidc` (`default_role: platform-admins`), and writes one role
  `platform-admins` with `user_claim: sub`, `groups_claim: groups`, **no
  `bound_claims`**, and `token_policies: ["platform-admin"]`, a policy of
  `path "*"` with every capability including `sudo`. The comment at lines 537-543
  states the protection is the Authentik side: the `openbao-oidc` application is
  admin-tier, so only platform admins can complete that login. `deploy-openbao.yml`
  contains no OIDC configuration (grep, 2026-09-27), so production has none in code.
- **Authentik gates.** `zz-sso-bindings.yaml.j2:17-40` defines two gates:
  `platform-member` (admins, developers, business; superusers pass) and
  `platform-admin`; line 69 picks one per application from its catalog `tier`, which
  accepts `member` or `admin` only (`app-catalog.yml:30`). The OpenBao UI also sits
  behind a forward_auth application `openbao` at tier `member`
  (`app-catalog.yml:60-63`, `openbao-forward-auth.yaml`). The bindings comment at
  lines 74-76 records that applications use the default `any` policy mode.
- **Claims.** Authentik's default `profile` scope mapping emits `preferred_username`
  (the username) and `groups` (group names) — read on upstream `main`,
  `blueprints/system/providers-oauth2.yaml:36-44`. The pinned image is
  `ghcr.io/goauthentik/server:2024.12.3` (`compose.yml:16`); the same claims at that
  version are not verified (task 1.4).
- **OpenBao policy templating** (openbao.org, concepts/policies, fetched 2026-09-27):
  `identity.entity.aliases.<mount accessor>.name` is an available template parameter,
  and per-user KV paths are the documented example. The JWT/OIDC auth doc (openbao
  repository, `website/content/docs/auth/jwt/index.mdx`) states the role's user and
  groups claims set up identity aliases, and that a `bound_claims` list value matches
  if the claim matches one of the items, with the example
  `"bound_claims": { "groups": ["mygroup/mysubgroup"] }`.
- **Controller authority.** The orchestrator policy
  (`config/policies/semaphore-read.hcl`) grants create/read/update/patch/list on
  `secret/data/services/*`, read/list on `secret/metadata/services/*`, and
  create/read/update/delete on `sys/policies/acl/*` and `auth/approle/role/*`. It has
  nothing on `secret/*/users/*`, `sys/auth/*` or any `auth/oidc*` path. Which policy
  set the live controller AppRole carries was not queried.
- **Policy application.** `apply-openbao-policies.yml:44-63` applies every `*.hcl`
  file in the policies directory verbatim with its own inline `find` and `uri` loop; a
  `.hcl.j2` file is not matched by its `find`. The per-component playbooks
  (`apply-policy-*.yml`) include the shared `tasks/apply-openbao-policy.yml`, which reads
  the file with `lookup('file', _policy_file)` (line 33), so it cannot render a template
  either.
- **Membership listing precedent.** `deploy-authentik.yml:385-390` runs a script inside
  the server container with `exec -i authentik-server python -`, which reads
  Authentik's own models without an API token.
- **Schedules as code.** `templates.yml:447-448` and `862-863` show the
  `schedule: {cron: ...}` form on a template (grep for `schedule:`, 2026-09-28).
- **Audit.** No OpenBao audit device is configured in the repository (grep for
  `sys/audit` under `platform/` and `plan/architecture/`, 2026-09-27). Whether
  production has one enabled live is not known.
- **Production OpenBao** comes back sealed after a reboot and uses manual Shamir unseal
  (`platform/services/openbao/deployment/README.md`, "Production layout").

## Goals / Non-Goals

Goals: a person in `inference-users` can get their own gateway key without an operator;
only they can read it; it has its own budget and shows under its own identity in
metrics; it stops working 30 days after issue without anyone remembering; leaving the
group ends access within an hour; every step runs through Semaphore from code.

Non-Goals: JWT or browser-session authentication at the gateway (deferred, decision
11); per-user request-rate buckets (v1.5.0 has none under the `llm` shortcut, gateway
change design §4); key notification by email or chat (open question 3); changing the
admin OIDC login beyond the hardening in decision 5; moving agent identities off
`agw_clients`.

## Decisions

1. **A personal API key per user, not a JWT.** Because per-key budgets attach only to
   API keys at v1.5.0, SSO is used to *issue* a key, not to authenticate each request.
   The gateway sees one more `apiKey` entry per person, and every existing guarantee
   (hash-only config, strict mode, budget, identity label) applies unchanged.
   Alternative rejected: JWT at the gateway (decision 11). Alternative rejected: keep
   people in `agw_clients`, because it keeps handout and offboarding manual and ties
   nothing to the person's account.

2. **Identity `user-<username>`, from the Authentik username.** The prefix keeps person
   identities apart from agent identities in the same key list and on the same metric
   label. The username must match `^[a-z0-9][a-z0-9-]*$` so the identity meets the
   deploy's existing charset rule; a member whose username does not is refused **by
   name** and gets no key (never silently normalised, since two usernames could
   normalise to one identity). The deploy refuses any `agw_clients` entry that begins
   with `user-`, so an inventory name cannot shadow a person. A username change is an
   offboard plus an onboard: the old record is revoked and a new key minted.
   Only an operator changes a username, in the users blueprint; a user never can. The
   key path, the identity and the self-read policy are all keyed on the username, so a
   user who could rename themselves could take a departed member's name. Authentik
   `2024.12.3` (the image `compose.yml:16` pins) keeps that off with the tenant setting
   `default_user_change_username`, default false (`authentik/tenants/models.py:63-65` at
   tag `version/2024.12.3`). Blueprints cannot hold it: `Tenant` is in the blueprint
   importer's excluded models at that tag (`authentik/blueprints/v1/importer.py:85-117`).
   So `deploy-authentik.yml` sets it to false from an in-container script, the way it
   already runs `verify-users.py.j2` (`deploy-authentik.yml:383-390`), and its read-back
   verify fails naming the setting when it is not false.
   Alternative rejected: the Authentik `sub` claim, because it is an opaque hash that
   makes the metric label and the storage path unreadable to operators.

3. **Eligibility: a new `inference-users` group, membership as code.** Declared in
   `platform-groups.yaml`, members assigned in `platform-users.yaml` like every other
   membership. Only **active** members are eligible; a deactivated account counts as
   removed. Alternative rejected: derive eligibility from `platform-developers` or the
   member tier, because inference spend is a separate grant — the business tier, for
   example, is admitted to member-tier applications (`zz-sso-bindings.yaml.j2:25`) and
   should not receive GPU spend by that fact alone.

4. **Storage: `secret/users/<username>/inference` on the existing KV-v2 mount.** No new
   secrets engine. Fields: `api_key`, `identity` (`user-<username>`), `issued_at`,
   `expires_at` (RFC 3339 UTC), and during a rotation `previous_api_key` and
   `previous_expires_at`. The record is written only by the controller AppRole through
   the shared merge task. The metadata sets `max_versions` to 2 so retired keys do not
   accumulate in version history. The path is outside `secret/services/*` so no
   existing service-wide grant covers it. Alternative rejected: more `client_*` fields
   on `secret/services/agentgateway`, because a per-user read policy cannot be scoped
   to one field of a shared record, and the gateway's own record would then be
   readable in part by every user.

5. **Login: a dedicated OIDC mount and Authentik application for inference users.**
   A new OpenBao auth mount `oidc-inference`, configured against a new Authentik OAuth2
   application `openbao-inference` (client id `openbao-inference`, redirect URIs for
   the OpenBao UI callback of that mount and the CLI loopback
   `http://localhost:8250/oidc/callback`, the same loopback `openbao-oidc.yaml:21`
   registers). One role, `inference-user`: `user_claim: preferred_username`,
   `groups_claim: groups`, `bound_claims: {groups: [inference-users]}`,
   `bound_audiences: [openbao-inference]`, `token_policies: [inference-user-self]`,
   `token_ttl: 15m`, `token_max_ttl: 1h`, and it is the mount's `default_role`. On the
   Authentik side the application gets a new catalog tier `inference`, whose gate
   admits active members of `inference-users` only. The mount, its config and its role
   are written by a shared `tasks/configure-bao-oidc-mount.yml`, extracted from the inline
   local-dev tasks (`bootstrap-local-dev.yml:551-562`, `579-602`, `604-624`); the
   bootstrap then calls the same task for the admin mount, so both mounts are configured
   by one piece of code that takes the mount path, config and role as inputs.

   Why a separate mount: the existing admin role grants a root-equivalent policy to
   anyone who completes the admin login and relies entirely on the Authentik gate
   (Context). Adding a second role to that mount and opening its Authentik
   application to inference users would let an inference user request the admin role
   by name. A separate mount has its own client and its own roles, so no login through
   it can reach the admin policy. Alternative rejected: second role on the `oidc`
   mount, for that reason. **Hardening in scope:** the admin role also gains
   `bound_claims: {groups: [platform-admins]}` wherever it is configured, so it no
   longer depends on the Authentik gate alone.

   The OpenBao UI is also behind the `openbao` forward_auth application at tier
   `member`, which does not admit `inference-users`. That application gains a second
   binding admitting the group (applications evaluate bindings in `any` mode). How the
   CLI reaches OpenBao's API through that gate is open question 1.

6. **Self-read policy, templated on the alias name, rendered with the mount accessor.**
   `inference-user-self.hcl.j2`:

   ```hcl
   path "secret/data/users/{{identity.entity.aliases.<accessor>.name}}/inference" {
     capabilities = ["read"]
   }
   path "secret/metadata/users/{{identity.entity.aliases.<accessor>.name}}/inference" {
     capabilities = ["read"]
   }
   ```

   Read only; no `list` anywhere, so a user cannot discover who else holds a key. The
   accessor exists only after the mount is enabled and differs per environment, so the
   file is a Jinja template; OpenBao's own `{{identity...}}` braces are escaped in it.
   No new policy playbook: the shared `tasks/apply-openbao-policy.yml` becomes
   render-aware (a `_policy_file` ending in `.j2` is read with the `template` lookup, any
   other with `file`, as today), and its callers read the auth mount listing once
   (`sys/auth`) into a mount-to-accessor map that templates reference; a template naming
   a mount that does not exist fails the render. `apply-openbao-policies.yml` switches
   from its inline loop to that task and finds `*.hcl.j2` as well as `*.hcl`, so "apply
   all" still means all, and the inference OIDC configure playbook applies the
   self-read policy through the same task right after it enables the mount.
   Alternative rejected: a dedicated `apply-policy-inference-users.yml` with its own
   render step, because a second render path for one file is the fork the shared task
   exists to prevent.
   Alternative rejected: `identity.entity.name`, because entities created by an OIDC
   login receive a generated name, not the username (not verified; it would need the
   reconcile to pre-create named entities and aliases, more moving parts than a
   rendered accessor).

7. **Controller authority is extended in the orchestrator policy file.** The
   orchestrator policy gains `create/read/update/patch/delete` on
   `secret/data/users/*`, `read/list/delete` on `secret/metadata/users/*`, and
   management of `sys/auth/oidc-inference` and `auth/oidc-inference/*`, plus `read` on
   `sys/auth` so the policy playbook can find the mount's accessor. This is a
   small widening in practice: the same policy can already write any ACL policy and
   create AppRoles with any policy attached (Context), so it is already effectively
   administrative. Alternative rejected: configure the mount once, operator-side, with
   the root token, because a mount configured outside code drifts and cannot be
   rebuilt; the local bootstrap's root-token path stays local-only.

8. **Reconcile: one scheduled playbook, source of truth is group membership.**
   `reconcile-inference-user-keys.yml`:
   1. On the Authentik host, a listing script inside the server container prints, as
      JSON, whether the `inference-users` group was found and the usernames of its
      active members — names only. It exits non-zero on any lookup error, and a group
      that does not exist is reported as not found, never as an empty member list.
   2. On the controller, list `secret/metadata/users/`, read each `inference` record,
      and compute, per user: **mint** (eligible, no record), **rotate** (eligible,
      no previous key pending, and due under decision 9's rotation rule),
      **expire-previous** (`previous_expires_at` passed), **revoke** (record exists,
      not eligible: delete metadata, all versions), or nothing.
   3. Guards, in two classes:
      - **Failed listing.** A non-zero exit, output that does not parse, or a group
        reported as not found fails the run before any write. `allow_mass_revoke`
        does not override this: with no trustworthy listing there is nothing to
        converge to.
      - **Verified listing with a large drop.** The group exists and the lookup
        succeeded, so an empty member list is a real answer and is reconciled like
        any other. One fixed rule, in code and not in inventory: the run refuses,
        unless `-e allow_mass_revoke=true`, a plan that would revoke more than half of
        the existing records, unless it revokes a single record. So the last remaining
        member leaving (one revoke) is removed on the next scheduled run like anyone
        else, which keeps the one-hour offboarding bound, while a blueprint or
        membership fault that empties a populated group still stops for an operator.
        Alternative rejected: inventory thresholds for the count and the fraction,
        because two knobs for one guard invite a per-environment value nobody reviews.
   4. Every write is a merge through `tasks/bao-merge-keys.yml` with the new value
      pinned in a fact first (the double-evaluation lesson at
      `manage-agentgateway-client-key.yml:102-109`); all key-bearing tasks are
      `no_log` and nothing else is.
   5. Import the gateway deploy on every run. The deploy is change-aware
      (`inference-gateway-agentgateway` decision 11): it renders the key list from the
      records as they are now (current and previous keys, each only while its expiry is
      in the future) and brings up a new gateway only when that rendering differs from
      the one the running container started with, or when no gateway container is
      running. So an hourly run with nothing to converge restarts nothing, a deploy that
      failed is retried by the next run even when no record changed, and a key whose
      expiry passed drops out of the rendering and is refused after the next run without
      any record write. Alternative rejected: a drift detector in this playbook (desired
      key hashes against the `keyHash` entries read back from the gateway host, plus the
      rendered file's modification time against the container's start time), because it
      reimplements, for one caller, the comparison the deploy owns for every caller.
      Alternative rejected: deploying only when this run wrote a record, because a
      failed deploy then leaves a revoked or expired key accepted with nothing to retry
      it.
   6. Emit a step result with counts, identity names and whether the deploy recreated
      the gateway, never values, and push one result line through
      `tasks/push-loki-lines.yml` (extracted by `production-internal-ca`) so the shared
      "Scheduled job silent" rule of that change watches this job, declared at three
      hours (three missed hourly runs).

   Schedule: hourly (`cron: "17 * * * *"`) as code in `templates.yml`, with a Dev
   variant. Hourly bounds both offboarding latency and expiry overshoot to about one
   hour. Alternative rejected: daily, which would leave a departed user's key valid
   for up to a day. Alternative rejected: an Authentik webhook on group change,
   because it adds a second trigger path and still needs the schedule for expiry.

9. **Monthly lifetime, 3-day overlap, rotated as one cohort, expiry enforced by the
   deploy.** Joe, 2026-09-28, chose a fixed calendar day for the cohort ("Fixed calendar
   day"), accepting that a key can live up to 34 days: a month of up to 31 days plus the
   3-day grace. The expiry a key is minted with depends on the branch
   (`inference_key_grace_days: 3` on both):
   - Cohort branch: `expires_at` is the start (00:00 UTC) of the first cohort day after
     issue plus the grace days, at most 34 days after issue. `inference_key_lifetime_days`
     is not read on this branch.
   - Hot-reload branch: `expires_at = issued_at + inference_key_lifetime_days` (30),
     with the successor minted on day 27.

   The cohort expiry is tied to the cohort day, not to a fixed lifetime, because a fixed
   30-day lifetime locks users out. Two consecutive cohort days can be 31 days apart
   (rotation day 1: 1 October to 1 November). A key minted on 1 October with
   `issued_at + 30 days` would expire on 31 October, a day before 1 November mints its
   successor, and its user would hold no valid key for that day. Tied to the next cohort
   day, every key stays valid for the whole grace window after its successor is minted,
   whatever the length of the month. When successors are minted depends on what a key
   change costs at the gateway, which the gateway change's task 1.12 settles:
   - **A key change restarts the gateway (no hot reload).** Keys rotate as one cohort,
     so rotation costs a fixed number of restarts, not one per user. The cohort day is a
     fixed day of each calendar month, `inference_key_rotation_day` (1-28, so it exists in
     every month). On a cohort day's first run the reconcile mints a
     successor for every current key issued before that day. Each old key's
     `previous_expires_at` is its own `expires_at`, which is this cohort day plus the
     grace days because the key was minted with the cohort expiry above; the successor's
     `expires_at` is the next cohort day plus the grace days. Rotation then restarts the
     gateway twice per cycle, on the cohort day and three days later when the previous
     keys drop, whatever the number of users. A member who joins mid-cycle gets a key
     that expires at the next cohort day plus the grace days, and that cohort day mints
     its successor early. Why a
     calendar day and not every 27 days from an anchor: Joe chose the calendar day,
     2026-09-28, for a rotation date people can remember; the 27-day anchor kept the
     30-day maximum but drifts across the calendar. Rejected on that decision.
   - **Hot reload confirmed.** Each key gets its successor on its own day 27, because a
     key change then drops no stream and staggered rotations spread the user-facing
     churn; the rotation-day variable is not used.
   In both branches both keys are enrolled until the old key's recorded expiry. No key
   is ever valid past its recorded expiry (at most 34 days after issue on the cohort
   branch, 30 on the hot-reload branch), no key expires before its successor is minted,
   and the user has three days to swap. The gateway
   deploy renders a key only while `now < expires_at` (and the previous key only while
   `now < previous_expires_at`), and **fails** on a record whose expiry field is missing
   or unparseable, so a damaged record cannot become a non-expiring key. Keys are 48
   characters, like the existing client keys. Alternative rejected: mint on day 30 and
   let the old key run to day 33, because then the stated 30-day lifetime is not true.
   Alternative rejected: no overlap, because a key swapped out from under a running
   agent session fails it mid-work. Alternative rejected: per-key rotation with a
   restart per change, because with a team of users the gateway would restart on most
   days and drop every stream in flight each time.

10. **Rendering: user keys join the same `apiKey` list, with their own defaults.** The
    deploy reads the user records (list plus one read each, `no_log`), filters by
    expiry, and passes them to the template, which renders them after the
    `agw_clients` entries: `keyHash`, `metadata.name: user-<username>`,
    `metadata.team: uhstray` (the group `inference-users` is the team, so every personal
    key is a team key; `agentgateway-observability` requires the marker on every entry
    while content logging is on, under its requirement "Content is kept only for
    uhstray.io team identities"), `allowedModels`
    from `agw_user_allowed_models` when set, and the `hourly-tokens` budget with
    `agw_user_tokens_per_hour`. The local-dev plaintext flag never applies to user
    keys: they are always hashes. During the overlap both keys carry the same identity
    name; whether v1.5.0 counts a budget per key entry or per identity is not verified
    (task 1.2). If per entry, a user's ceiling doubles for at most three days, which
    is accepted and written into the architecture page.

11. **Deferred: Authentik JWTs accepted at the gateway directly.** Joe states v1.5.0 can
    compose `jwtAuth` in permissive mode with an optional `apiKey`, so a request could
    carry either an SSO access token or a key. This deferral is recorded here and on
    the gateway's architecture page (task 6.2), not as a requirement. Deferred because:
    (a) JWT callers get no personal budget at this version, which is the main thing a
    person's identity is for here; (b) the gateway would fetch Authentik's JWKS from the
    public issuer, which sits behind Cloudflare's challenge and needs the skip rule from
    PR #289 (merged to `dev`, `platform/infra/cloudflare/waf.tf:129-135`; its production
    apply is gateway task 1.11) or the LAN split-horizon record of gateway task 7.1; (c) the Authentik provider must sign with a certificate (RS256, published as
    JWKS) — the existing providers do reference the self-signed certificate
    (`agentgateway-oidc.yaml:48-49`, `openbao-oidc.yaml:36-37`), so this one is not a
    blocker; (d) the clients in use (OpenAI-compatible SDKs, OpenCode, pi) hold a static
    bearer key and do not refresh short-lived tokens. Revisit when a release ties
    budgets to JWT claims and the gateway reaches Authentik's JWKS without a challenge.

12. **Observability and audit.** Nothing changes in the gateway config for metrics: the
    identity label already exists, and the label's cardinality grows by the group's
    size, which is bounded and known. The inference dashboard gains a per-user row
    (requests, tokens, budget blocks). Per-user usage becomes visible to operators;
    that is stated on the user-facing page. OpenBao gains a file audit device
    configured as code (`configure-openbao-audit.yml`), its log collected by the o11y
    stack, so a query answers who read `secret/data/users/*` and when. Whether values
    in audit entries are hashed by default is to be confirmed against OpenBao's audit
    docs before the device is enabled (task 1.5); the device is enabled only if they
    are.

## Risks / Trade-offs

- [An inference user reaches the admin policy] → separate mount and client (decision
  5); `bound_claims` on both roles; a validation gate logs in as a non-admin member
  and requires the admin role to refuse.
- [Authentik outage revokes everyone] → a failed listing or a missing group fails
  before any write, and a verified listing that would revoke most records at once
  stops for an operator (decision 8, step 3).
- [A failed deploy leaves a revoked or expired key accepted] → every run imports the
  change-aware deploy, which brings up a new gateway whenever the running one started
  from a different rendering (decision 8, step 5).
- [Every change restarts the gateway and drops streams] → the deploy restarts only on a
  changed rendering, and rotations run as one cohort, two restarts per cycle (decision
  9); if the gateway change finds v1.5.0 applies key changes without a restart, neither
  constraint costs a stream.
- [The reconcile stops running and nobody notices] → the shared "Scheduled job silent"
  rule fires after three hours without a result line (decision 8, step 6).
- [Production OpenBao is sealed after a reboot] → users cannot fetch keys until it is
  unsealed; the gateway keeps serving its rendered config, and a key that expires
  meanwhile still drops on the next deploy. Recorded, not solved here.
- [A username changes] → offboard plus onboard (decision 2); the user sees a new key.
- [A user misses the three-day overlap] → the key expires on schedule; the user logs
  in and reads the current one. Notification is open question 3.
- [Budget double-counting during overlap] → bounded to three days (decision 10).
- [OpenBao's listener is plaintext inside its host] (`openbao.hcl`: `tls_disable = 1`)
  → users reach it only through Caddy's TLS; the OIDC redirect URIs are HTTPS except
  the CLI loopback on the user's own machine.

## Migration Plan

1. Authentik: group, the `openbao-inference` application, the `inference` tier and
   the extra forward_auth binding; memberships for the first users. Local-dev first.
2. OpenBao: extend the orchestrator policy; configure the `oidc-inference` mount and
   role; render and apply the self-read policy; harden the admin role with
   `bound_claims`; enable the audit device.
3. Gateway: user-record read and expiry filter in the deploy; template rendering;
   BATS. The UI read-only switch and the change-aware deploy come from the gateway
   change (its tasks 1.10 and 1.12) and land before this step.
4. Reconcile playbook and schedule; launch the template once from Semaphore, then enable
   the schedule.
5. Move person identities out of `agw_clients` once each person holds a personal key,
   using the existing revoke flow (remove from inventory, then revoke).
6. Promote through `dev` to `main`; repeat 1 to 4 against production.

## Open Questions

1. How does the CLI reach OpenBao in production? The OpenBao UI is behind a
   forward_auth gate that expects a browser session; `bao login -method=oidc` and the
   following API read are not browser requests. Options: a path-scoped forward_auth
   exemption for `/v1/auth/oidc-inference/*` and `/v1/secret/data/users/*` (both
   still authenticated by OpenBao), or UI-only delivery at first.
2. Production hostname for OpenBao's UI: `openbao-oidc.yaml` carries only local-dev
   defaults, and the production values are site-config's.
3. Should users be told when a successor key exists? No production SMTP exists yet
   (`platform-users.yaml:15-17`). A Discord message naming the identity (never the
   value) is one option.
4. Default per-user hourly token budget and model allow-list, from the measured
   ceiling (gateway change task 4.2).
5. Should `platform-admins` members be added to `inference-users` automatically, or
   explicitly like everyone else? Default if unanswered: explicitly.
6. Decided 2026-09-28 (Joe): the cohort rotates on a fixed calendar day each month, with
   a lifetime of up to 34 days.
