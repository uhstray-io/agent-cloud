# Tasks: personal inference keys issued through Authentik SSO

Order: this change starts after `inference-gateway-agentgateway` has the gateway serving
with inventory identities (its tasks 1 and 4.1). Pull requests only when Joe asks for
them (repo rule).

## 0. Branch and preconditions
- [ ] 0.1 Feature branch from `dev`: `feat/inference-personal-keys`, in its own worktree
- [ ] 0.2 Record Joe's answers, or the stated defaults, for design open questions 1 to 5
      in `design.md` before task 2 starts
- [ ] 0.3 Validation gate: `openspec validate inference-personal-keys` passes

## 1. Verify the upstream facts this design leans on
- [ ] 1.1 agentgateway v1.5.0 binary: confirm the UI read-only switch's exact name and
      behaviour (Joe: `UI_READ_ONLY`); record in the gateway's `context/architecture.md`
- [ ] 1.2 agentgateway v1.5.0: confirm budgets attach only to `apiKey` entries and that
      two key entries may share one `metadata.name`; record whether budget usage is
      counted per entry or per identity (feeds design decision 10)
- [ ] 1.3 agentgateway v1.5.0: confirm whether a changed config file is hot-reloaded
      without a container recreate (feeds the recreate trade-off)
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
      the server container like `verify-users.py.j2`; prints active usernames of one
      group as JSON, nothing else; exits non-zero on any lookup error
- [ ] 2.6 BATS: group declared; tier gate admits only the group; the listing script
      prints names only
- [ ] 2.7 Validation gate: local Authentik deploy converges twice; a non-member is
      refused at the `openbao-inference` authorize step, proving scenario "A non-member
      cannot use the inference login" (Authentik half); a platform-business member not
      in the group is absent from the listing, proving scenario "Other tiers do not
      imply eligibility"

## 3. OpenBao
- [ ] 3.1 Orchestrator policy file: add the user-key paths, the `oidc-inference`
      mount management paths and `read` on `sys/auth` (design decision 7); apply via
      `apply-openbao-policies.yml`
- [ ] 3.2 `configure-openbao-inference-oidc.yml` (Semaphore template): enable the
      `oidc-inference` mount, write its config (discovery URL, client id, secret
      shared-read from `secret/services/authentik`, `default_role: inference-user`) and
      the `inference-user` role exactly as design decision 5; idempotent; `no_log` only
      on the secret-bearing write
- [ ] 3.3 Admin role hardening: add `bound_claims: {groups: [platform-admins]}` to the
      `platform-admins` role in `bootstrap-local-dev.yml`, and to its production
      equivalent when that is brought into code
- [ ] 3.4 `config/policies/inference-user-self.hcl.j2` (design decision 6) and
      `apply-policy-inference-users.yml`, which reads the mount accessor and renders it;
      `apply-openbao-policies.yml` gains the same render step for `*.hcl.j2`
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
      plaintext flag never applies), identity `user-<username>`, budget
      `agw_user_tokens_per_hour`, optional `agw_user_allowed_models`
- [ ] 4.3 `compose.yml` / `env.j2`: set the UI read-only switch confirmed in task 1.1
- [ ] 4.4 BATS: user keys are never plaintext; the prefix refusal exists; the expiry
      filter and the fail-closed branch exist
- [ ] 4.5 Validation gate: a local record with a past expiry renders nothing and one
      with no expiry fails the deploy, proving scenario "A damaged record cannot become
      a permanent key"; two users with a small budget show one blocked while the other
      is served, proving scenario "A personal key has its own budget"; an `agw_clients`
      entry `user-x` fails the deploy, proving scenario "Inventory cannot shadow a
      person"; a UI key edit is refused, proving scenario "The UI cannot change keys"

## 5. Reconcile and schedule
- [ ] 5.1 `reconcile-inference-user-keys.yml` as design decision 8: listing on the
      Authentik host, plan on the controller (mint, rotate, expire-previous, revoke),
      the empty-listing guard with `allow_mass_revoke`, writes through
      `tasks/bao-merge-keys.yml` with values pinned in facts, KV metadata
      `max_versions: 2`, revoke deletes metadata; deploy imported only on change;
      `tasks/emit-step-result.yml` with counts and identities
- [ ] 5.2 Lifetime vars `inference_key_lifetime_days: 30` and
      `inference_key_grace_days: 3`; the deploy and the reconcile read the same values
- [ ] 5.3 `templates.yml`: template `Reconcile Inference User Keys`, `dev_variant: true`,
      `schedule: {cron: "17 * * * *"}`; run `setup-templates.yml`
- [ ] 5.4 BATS: no key-bearing task outside `no_log`; no visible task loops over a
      protected result (`platform/tests/test_no_request_in_loop_items.py` covers it);
      the guard exists; the deploy import is conditional
- [ ] 5.5 Validation gate, local: a second run with no change recreates nothing,
      proving scenario "Unchanged membership does not restart the gateway"; an empty
      listing refuses, proving scenario "An empty listing does not revoke everyone"; a
      username with a dot is refused by name while others reconcile, proving scenario
      "A non-conforming username is refused by name"; a new member gets a key, proving
      scenario "A declared member becomes eligible"; with the clock shifted by the
      lifetime vars (short values in local inventory), rotation overlaps and the old
      key then gets 401, proving scenarios "Rotation overlaps" and "The old key stops
      working at its expiry"; a removed member's key gets 401 after the next run and
      the record has no versions left, proving scenario "Offboarding revokes the key"

## 6. Observability, docs, promotion
- [ ] 6.1 Inference dashboard: per-user row (requests, tokens, budget blocks) filtered
      on `identity=~"user-.*"`
- [ ] 6.2 Gateway `context/architecture.md`: personal-key lifecycle table, the
      overlap budget note, the JWT deferral (design decision 11); a user-facing page on
      getting and rotating a key; `AGENTS.md` secrets-layout row for `secret/users/*`
- [ ] 6.3 Migrate person identities out of `agw_clients` once each person holds a
      personal key (remove from inventory, then the existing revoke)
- [ ] 6.4 Production: tasks 2 to 5 through Semaphore against production inventory,
      after `dev` → `main` promotion
- [ ] 6.5 Validation gate: one hour of personal-key traffic renders the per-user row,
      proving scenario "Per-user usage on the dashboard"; an audit query lists a key
      read with time and identity and no value in clear, proving scenario "Who read a
      key is answerable"; the architecture page states the deferral, proving scenario
      "The deferral is findable"; on archive, retain the outcome (worked / dead end /
      corrected) into bank `agent-cloud-750a33b9`
