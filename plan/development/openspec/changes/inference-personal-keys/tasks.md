# Tasks: personal inference keys issued through Authentik SSO

Order: this change starts after `inference-gateway-agentgateway` has the gateway serving
with inventory identities (its tasks 1 and 4.1), including its UI read-only switch (task
1.10) and its change-aware deploy (task 1.12). Pull requests only when Joe asks for them
(repo rule).

## 0. Branch and preconditions
- [ ] 0.1 Feature branch from `dev`: `feat/inference-personal-keys`, in its own worktree
- [ ] 0.2 Record Joe's answers, or the stated defaults, for design open questions 1 to 6
      in `design.md` before task 2 starts
- [ ] 0.3 Validation gate: `openspec validate inference-personal-keys` passes

## 1. Verify the upstream facts this design leans on
The UI read-only switch (formerly 1.1) and hot reload of a changed config (formerly 1.3)
are the gateway change's tasks 1.10 and 1.12; this change reads their answers.
- [ ] 1.2 agentgateway v1.5.0: confirm budgets attach only to `apiKey` entries and that
      two key entries may share one `metadata.name`; record whether budget usage is
      counted per entry or per identity (feeds design decision 10)
- [ ] 1.4 Authentik 2024.12.3 (the pinned image): confirm the default `profile` scope
      emits `preferred_username` and `groups` as on upstream `main`
- [ ] 1.5 OpenBao: confirm an OIDC login creates an entity alias named from
      `user_claim`, that `{{identity.entity.aliases.<accessor>.name}}` resolves to it in
      an ACL path, and how audit entries treat secret values by default
- [ ] 1.6 Validation gate: each answer is written into `design.md` Context with its
      source; any answer that contradicts a decision reopens that decision before code

## 2. Authentik
- [ ] 2.1 `platform-groups.yaml`: add `inference-users` (not superuser)
- [ ] 2.2 `platform-users.yaml`: declare the first memberships (usernames stay in
      site-config through the existing `!Env` pattern)
- [ ] 2.3 New blueprint `openbao-inference-oidc.yaml`: OAuth2 provider
      `openbao-inference` (confidential, signing key set, `openid`/`email`/`profile`
      mappings, UI-callback and CLI-loopback redirect URIs from inventory) and
      application `openbao-inference`; client secret via `!Env`, generated once in
      `deploy-authentik.yml` secret definitions and templated by `env.j2`
- [ ] 2.4 `app-catalog.yml`: the application at a new tier `inference` with its
      `prod_required` and `verify_redirect` vars; `zz-sso-bindings.yaml.j2`: the tier's
      gate (active `inference-users` members only) and a second binding on the
      `openbao` forward_auth application admitting the group
- [ ] 2.5 Membership listing script (`templates/list-group-members.py.j2`), run inside
      the server container like `verify-users.py.j2`; prints, as JSON, whether the group
      was found and the active usernames of its members, nothing else; exits non-zero on
      any lookup error; a missing group is reported as not found, never as an empty
      member list
- [ ] 2.6 BATS: group declared; tier gate admits only the group; the listing script
      prints names only
- [ ] 2.6a `deploy-authentik.yml`: an in-container step sets the tenant setting
      `default_user_change_username` to false (idempotent: changes nothing when already
      false), and the read-back verify fails naming the setting when it is not false.
      Blueprints cannot carry it at `2024.12.3`, which excludes `Tenant` from blueprint
      import (`authentik/blueprints/v1/importer.py:85-117` at tag `version/2024.12.3`;
      design decision 2). BATS: the step and the verify assertion exist
- [ ] 2.7 Validation gate: local Authentik deploy converges twice, the second run
      reporting the username setting unchanged and false, proving scenario "Users cannot
      rename themselves"; a non-member is
      refused at the `openbao-inference` authorize step, proving scenario "A non-member
      cannot use the inference login" (Authentik half); a platform-business member not
      in the group is absent from the listing, proving scenario "Other tiers do not
      imply eligibility"

## 3. OpenBao
- [ ] 3.1 Orchestrator policy file: add the user-key paths, the `oidc-inference`
      mount management paths and `read` on `sys/auth` (design decision 7); apply via
      `apply-openbao-policies.yml`
- [ ] 3.2 Extract `platform/playbooks/tasks/configure-bao-oidc-mount.yml` from the inline
      local-dev tasks (`bootstrap-local-dev.yml:551-562` enable, `579-602` config,
      `604-624` role); inputs are the mount path, the config body, the role name and body,
      and the request headers; the config write stays `no_log`, nothing else is. The
      bootstrap calls it for the admin mount with the values it writes today, so local-dev
      behaviour is unchanged. Then `configure-openbao-inference-oidc.yml` (Semaphore
      template) calls the same task to enable `oidc-inference`, write its config (discovery
      URL, client id, secret shared-read from `secret/services/authentik`, `default_role:
      inference-user`) and the `inference-user` role exactly as design decision 5;
      idempotent. BATS: the bootstrap carries no inline `auth/oidc` writes after the
      extraction
- [ ] 3.3 Admin role hardening: add `bound_claims: {groups: [platform-admins]}` to the
      `platform-admins` role in `bootstrap-local-dev.yml`, and to its production
      equivalent when that is brought into code
- [ ] 3.4 `config/policies/inference-user-self.hcl.j2` (design decision 6). Make
      `tasks/apply-openbao-policy.yml` render-aware: a `_policy_file` ending in `.j2` is
      read with the `template` lookup instead of `lookup('file', ...)` (line 33 today);
      callers read `sys/auth` once into a mount-to-accessor map the template references.
      `apply-openbao-policies.yml` replaces its inline loop (lines 44-63) with that task
      and also finds `*.hcl.j2`; `configure-openbao-inference-oidc.yml` applies the
      self-read policy through it after enabling the mount. No new policy playbook. BATS:
      a `.hcl.j2` renders through the shared task; an absent mount fails the render
- [ ] 3.5 `configure-openbao-audit.yml`: a file audit device as code, only after task
      1.5 confirms values are not logged in clear; o11y collects the log
- [ ] 3.6 BATS: the self-read template grants `read` only, never `list`, never a
      `services` path; the role binds the group claim; the admin role binds
      `platform-admins`
- [ ] 3.7 Validation gate: as a local member, log in through the UI and through
      `bao login -method=oidc -path=oidc-inference`; read own record succeeds, another
      user's record, the metadata list and `secret/data/services/agentgateway` are 403,
      proving scenarios "A user reads their own key" and "A user cannot read another
      user's key"; the same member naming the admin role on either mount is refused,
      proving scenario "An inference user cannot request the admin role"

## 4. Gateway rendering
- [ ] 4.1 `deploy-agentgateway.yml`: list `secret/metadata/users/`, read each
      `inference` record (`no_log`), fail on a missing or unparseable expiry naming the
      identity, keep only unexpired current and previous keys; refuse `agw_clients`
      names beginning with `user-`
- [ ] 4.2 `config.yaml.j2`: render the user keys after `agw_clients`, hash only (the
      plaintext flag never applies), identity `user-<username>`, `metadata.team:
      uhstray` on every user key (the marker `agentgateway-observability` requires under
      its requirement "Content is kept only for uhstray.io team identities"; that
      change's render guard refuses a key without it while content logging is on), budget `agw_user_tokens_per_hour`, optional
      `agw_user_allowed_models`
- [ ] 4.4 BATS: user keys are never plaintext; every rendered user key carries
      `team: uhstray`; the prefix refusal exists; the expiry filter and the fail-closed
      branch exist
- [ ] 4.5 Validation gate: a local record with a past expiry renders nothing and one
      with no expiry fails the deploy, proving scenario "A damaged record cannot become
      a permanent key"; two users with a small budget show one blocked while the other
      is served, proving scenario "A personal key has its own budget"; an `agw_clients`
      entry `user-x` fails the deploy, proving scenario "Inventory cannot shadow a
      person"

## 5. Reconcile and schedule
- [ ] 5.1 `reconcile-inference-user-keys.yml` as design decision 8: listing on the
      Authentik host, plan on the controller (mint, rotate, expire-previous, revoke), the
      two guard classes (a failed listing or a group not found fails before any write; a
      verified listing refuses, unless `allow_mass_revoke`, a plan revoking more than half
      of the existing records, unless it revokes a single record, a fixed rule in code),
      writes through `tasks/bao-merge-keys.yml` with values pinned in facts, KV metadata
      `max_versions: 2`, revoke deletes metadata; then import the gateway deploy on every
      run, whether or not this run wrote a record (the deploy restarts the gateway only
      on a changed rendering or a missing container, gateway task 1.12);
      `tasks/emit-step-result.yml` with counts, identities and whether the deploy recreated
      the gateway; one result line through `tasks/push-loki-lines.yml`
- [ ] 5.2 Lifetime vars `inference_key_lifetime_days: 30` and
      `inference_key_grace_days: 3`, plus `inference_key_rotation_day` (1-28) for the cohort
      branch of design decision 9 (built only if gateway task 1.12 finds no hot reload);
      the deploy and the reconcile read the same values. On the cohort branch a minted
      key's `expires_at` is the start (00:00 UTC) of the first cohort day after its issue
      plus `inference_key_grace_days`, and `inference_key_lifetime_days` is not read; a
      rotated key's `previous_expires_at` is its own `expires_at`. The hot-reload branch
      is unchanged: `issued_at + 30 days`, successor at day 27
- [ ] 5.2a Cohort-branch lockout drill, 31-day interval (design decision 9). The plan step
      reads the current time from one fact, which a local-dev-only extra variable pins
      (proposed name `inference_key_plan_now`; the reconcile refuses it outside
      `local_mode`). With `inference_key_rotation_day: 1`:
      - BATS or pytest over the plan arithmetic: a key issued 2026-10-01T00:17Z (1 October
        to 1 November is 31 days) gets `expires_at` 2026-11-04T00:00Z; stepped hourly from
        issue to expiry, no step finds the key expired while it has no successor; the
        successor appears at the first step on 2026-11-01. The same holds for every month
        pair of one year, February's 28 days included. Mutate the rule once to
        `issued_at + inference_key_lifetime_days` and watch the test go red on the
        October step 2026-10-31T00:17Z
      - Local drill, through the local Semaphore, with the pinned clock at 2026-10-01T00:17Z
        (mint), 2026-10-31T23:17Z (the key is served, no successor yet), 2026-11-01T00:17Z
        (successor minted, both keys served), 2026-11-03T23:17Z (both served) and
        2026-11-04T00:17Z (the old key gets 401, the successor is served). The served and
        401 checks use the probe path of 5.4a. Proves scenario "A key is never expired
        before its successor is minted". unverified: whether the gateway's own expiry
        filter (task 4.1) reads the same pinned time; if it reads the wall clock, the
        drill writes records whose times are shifted by the same offset instead
- [ ] 5.3 `templates.yml`: template `Reconcile Inference User Keys`, `dev_variant: true`,
      `schedule: {cron: "17 * * * *"}`; run `setup-templates.yml`. Declare the job in the
      shared "Scheduled job silent" list (`production-internal-ca` task 6.4) at three hours;
      this task waits on that rule and on `tasks/push-loki-lines.yml` (that change's task
      6.0) and adds neither of its own
- [ ] 5.4 BATS: no key-bearing task outside `no_log`; no visible task loops over a
      protected result (`platform/tests/test_no_request_in_loop_items.py` covers it);
      both guard classes exist and the mass-revoke rule reads no inventory threshold; the
      deploy import is unconditional; the rotation rule matches the branch design
      decision 9 records
- [ ] 5.4a Every "gets 401" or "is served" check in 4.5 and 5.5 uses the gateway probe path
      (`inference-gateway-agentgateway` task 6.1a) from the gateway host, so once the
      gateway requires client certificates the probe presents the `agw-verifier` leaf and
      a 401 is the key check refusing the key, never a handshake failure
- [ ] 5.5 Validation gate, local: a second run with no change recreates nothing,
      proving scenario "Unchanged membership does not restart the gateway"; emptying a
      group of three members refuses, proving scenario "A large drop does not revoke
      everyone"; removing the only member revokes without the override, proving scenario
      "The last member's removal revokes normally"; a listing pointed at a group name
      that does not exist, and a listing script forced to exit non-zero, both fail with
      no write, proving scenario "A failed listing changes nothing"; a revoke whose
      gateway deploy is made to fail (the gateway image reference deliberately broken in
      local inventory for one run) is converged by the next run with no record change,
      proving scenario "A revoked key stays refused after a failed deploy"; a key left to
      pass its short local expiry with no other change gets 401 after the next run,
      proving scenario "Expiry converges without a record change"; with the gateway
      container removed and no record change, the next run deploys it, proving scenario
      "A missing gateway container is redeployed"; a username with a dot is refused by
      name while others reconcile, proving scenario "A non-conforming username is refused
      by name"; a new member gets a key, proving scenario "A declared member becomes
      eligible"; with short local lifetime values and, on the cohort branch, a rotation day
      set so a cohort day falls in the test, every key rotates in one run with at most one
      gateway recreate and the old keys then get 401, proving scenarios "Rotation
      overlaps" and "The old key stops working at its expiry"; a removed member's key gets
      401 after the next run and the record has no versions left, proving scenario
      "Offboarding revokes the key"; with the local schedule paused past a shortened
      silence, the shared alert names the reconcile job, proving scenario "A stopped
      reconcile is detected"

## 6. Observability, docs, promotion
- [ ] 6.1 Inference dashboard: per-user row (requests, tokens, budget blocks) filtered
      on `identity=~"user-.*"`
- [ ] 6.2 Gateway `context/architecture.md`: personal-key lifecycle table, the
      overlap budget note, the rotation branch in force, the JWT deferral (design decision
      11, recorded there and in this design, not as a requirement); a user-facing page on
      getting and rotating a key; `AGENTS.md` secrets-layout row for `secret/users/*`
- [ ] 6.3 Migrate person identities out of `agw_clients` once each person holds a
      personal key (remove from inventory, then the existing revoke)
- [ ] 6.4 Production: tasks 2 to 5 through Semaphore against production inventory,
      after `dev` → `main` promotion
- [ ] 6.5 Validation gate: one hour of personal-key traffic renders the per-user row,
      proving scenario "Per-user usage on the dashboard"; an audit query lists a key
      read with time and identity and no value in clear, proving scenario "Who read a
      key is answerable"; on archive, retain the outcome (worked / dead end / corrected)
      into bank `agent-cloud-750a33b9`
