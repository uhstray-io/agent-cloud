# platform/inference-personal-keys

Personal, self-service, expiring inference keys for Authentik users, delivered through
OpenBao and enforced at the inference gateway.

## ADDED Requirements

### Requirement: Eligibility is membership of one Authentik group
The platform SHALL grant a personal inference key only to active members of the
`inference-users` Authentik group, declared as code in the platform groups blueprint
with its memberships declared in the platform users blueprint, and MUST treat a
deactivated account as not a member.

#### Scenario: A declared member becomes eligible
- WHEN a user is added to `inference-users` in the users blueprint and the Authentik
  deploy and the reconcile run
- THEN a key record exists for that user and the gateway enrols its hash under
  identity `user-<username>`

#### Scenario: Other tiers do not imply eligibility
- WHEN a member of `platform-developers` or `platform-business` who is not in
  `inference-users` runs the reconcile's listing
- THEN no key record is created for them

### Requirement: Each key is stored at the owner's own path and readable only by the owner
Each personal key SHALL be stored in OpenBao at `secret/users/<username>/inference`
with its identity, issue time and expiry, written only by the controller AppRole.
A user logged in through the inference OIDC mount MUST be able to read their own
record and MUST NOT be able to read or list any other user's record or any path under
`secret/services/`. Because the path is keyed on the username, users MUST NOT be able
to change their own username in Authentik; the Authentik deploy SHALL enforce that
setting and fail its verification when it is not in force.

#### Scenario: Users cannot rename themselves
- WHEN the Authentik deploy completes
- THEN its verification reads the tenant's user username-change setting as disabled,
  and a deploy that finds it enabled sets it back and fails if the read-back still
  shows it enabled

#### Scenario: A user reads their own key
- WHEN a member logs in to OpenBao through Authentik on the inference OIDC mount and
  reads their own record
- THEN the read succeeds and returns the current key and its expiry

#### Scenario: A user cannot read another user's key
- WHEN the same user requests another user's record, lists `secret/metadata/users/`,
  or reads `secret/data/services/agentgateway`
- THEN each request is denied with 403

### Requirement: The inference login cannot reach administrative policy
The inference OIDC login SHALL be a separate OpenBao auth mount with its own Authentik
application and client, whose only role binds the `groups` claim to
`inference-users` and grants only the self-read policy. The administrative OIDC role
MUST bind the `groups` claim to `platform-admins`, so that neither role depends on
the Authentik gate alone.

#### Scenario: An inference user cannot request the admin role
- WHEN a member of `inference-users` who is not a platform admin attempts an OIDC
  login naming the administrative role, on either mount
- THEN OpenBao refuses the login and issues no token

#### Scenario: A non-member cannot use the inference login
- WHEN an Authentik user outside `inference-users` starts the inference OIDC login
- THEN Authentik refuses authorization, and a token forged past that step would fail
  the role's bound claim

### Requirement: Keys are reconciled from group membership on a schedule
A scheduled Semaphore template, declared in `templates.yml`, SHALL converge the key
records to the group's active membership at least hourly: mint for new members,
rotate aged keys, remove expired previous keys and revoke the keys of anyone no longer
eligible. On every run it MUST compare the set of unexpired user keys it would enrol
with the set the gateway has enrolled, read back from the gateway host, and MUST deploy
the gateway when they differ, when the running gateway predates its rendered
configuration, or when no gateway container is running, and only then; whether this run
wrote a record MUST NOT decide it. It MUST fail before any write when the membership
listing fails or the group is not found. On a listing that succeeded, an empty group is a
valid answer; the run MUST refuse, unless explicitly allowed, only a plan that revokes at
least the declared minimum count and more than the declared fraction of existing records.
It MUST NOT print a key value.

#### Scenario: Unchanged membership does not restart the gateway
- WHEN the reconcile runs with no member added or removed, no key due to rotate or
  expire, and the gateway's enrolled key set equal to the desired set
- THEN no OpenBao record changes and the gateway container is not recreated

#### Scenario: A large drop does not revoke everyone
- WHEN the membership listing succeeds but the plan would revoke two or more records
  and more than half of the existing records, and `allow_mass_revoke` is not set
- THEN the run fails naming the guard and the counts, and no record is deleted

#### Scenario: The last member's removal revokes normally
- WHEN the listing succeeds, the group exists with no active members, and exactly one
  key record exists
- THEN that record is deleted without `allow_mass_revoke`, the gateway is deployed, and
  a request with that key gets 401

#### Scenario: A failed listing changes nothing
- WHEN the listing script exits non-zero, prints output that does not parse, or
  reports the group as not found
- THEN the run fails before any OpenBao write or gateway deploy, even with
  `allow_mass_revoke` set

#### Scenario: A revoked key stays refused after a failed deploy
- WHEN a run deletes a removed member's record and its gateway deploy fails, and the
  next scheduled run finds no membership change
- THEN the next run finds the enrolled key set still holding that key's hash, deploys
  the gateway, and a request with the revoked key gets 401

#### Scenario: Expiry converges without a record change
- WHEN a key's recorded expiry passes and no record is due for any write
- THEN the next run finds the expired key's hash in the enrolled set but not in the
  desired set, deploys the gateway, and a request with that key gets 401

#### Scenario: A missing gateway container is redeployed
- WHEN the gateway container is missing or stopped on its host and no record is due
  for any write
- THEN the next run deploys the gateway, and a request with a current personal key is
  served

#### Scenario: A non-conforming username is refused by name
- WHEN a member's username does not match `^[a-z0-9][a-z0-9-]*$`
- THEN that member gets no key, the run reports the username as refused, and every
  other member is still reconciled

### Requirement: Keys expire after 30 days with an enforced overlap
Every key SHALL carry an expiry 30 days after issue. The reconcile SHALL mint a
successor when the current key is 27 days old, and both keys SHALL be valid until the
older key's expiry. The gateway deploy MUST render a key only while its recorded
expiry is in the future and MUST fail on a record whose expiry is missing or
unparseable, so that expiry is enforced by code on every deploy.

#### Scenario: Rotation overlaps
- WHEN the reconcile runs on day 27 of a key's life
- THEN a new key is stored, the old one is kept as the previous key with its original
  expiry, and the gateway accepts both

#### Scenario: The old key stops working at its expiry
- WHEN the reconcile runs after the previous key's expiry
- THEN the previous key is removed from the record, the gateway is re-rendered, and a
  request with the old key gets 401

#### Scenario: A damaged record cannot become a permanent key
- WHEN a user record has no parseable `expires_at`
- THEN the gateway deploy fails naming the identity and renders nothing from that
  record

### Requirement: Leaving the group ends access
When a user is removed from `inference-users` or deactivated in Authentik, the next
reconcile SHALL delete that user's key record with all its versions and re-render the
gateway, so the key is rejected within one scheduled interval.

#### Scenario: Offboarding revokes the key
- WHEN a member is removed from the group in the users blueprint, the Authentik deploy
  runs, and the next scheduled reconcile completes
- THEN the user's record and its version history are gone and a request with their
  key gets 401 at the gateway

### Requirement: Personal keys are gateway identities with their own budget
The gateway SHALL enrol each valid personal key as a sha256 hash with identity
`user-<username>`, a per-user hourly token budget and, when declared, a per-user model
allow-list, and MUST never render a personal key in plaintext in any environment. An
inventory client name beginning with `user-` MUST be refused by the deploy.

#### Scenario: A personal key has its own budget
- WHEN one user exhausts their hourly token budget
- THEN their further requests are blocked while another user's key is served

#### Scenario: Inventory cannot shadow a person
- WHEN `agw_clients` contains a name beginning with `user-`
- THEN the gateway deploy fails before rendering

### Requirement: Keys are managed only as code, never in the gateway UI
The gateway's operator UI SHALL run read-only, with the rendered configuration mounted
read-only, so that no key can be created, edited or revoked from the UI.

#### Scenario: The UI cannot change keys
- WHEN an admin logged in to the gateway UI attempts to add or edit an API key
- THEN the change is refused and the rendered configuration is unchanged

### Requirement: Usage and key reads are attributable to a person
Gateway metrics and access-log lines SHALL carry the personal identity, the inference
dashboard MUST show per-user requests, tokens and budget blocks, and OpenBao SHALL
record every read of a personal key record in an audit device configured as code,
without writing key values in clear.

#### Scenario: Per-user usage on the dashboard
- WHEN a user sends requests with their personal key for an hour
- THEN the dashboard's per-user row shows their request and token counts under
  `user-<username>`

#### Scenario: Who read a key is answerable
- WHEN an operator queries the OpenBao audit log for reads of one user's record
- THEN each read is listed with its time and the reading identity, and no entry
  contains the key value in clear

### Requirement: Direct JWT authentication at the gateway is a recorded deferral
The platform SHALL record, in this change's design and in the gateway's architecture
page, that accepting Authentik JWTs at the gateway was considered and deferred, with
the reasons (no personal budget for JWT callers at v1.5.0, the Cloudflare skip rule
for JWKS retrieval, static-key SDK clients) and the conditions for revisiting.

#### Scenario: The deferral is findable
- WHEN a reader opens the gateway's architecture page
- THEN it states that JWT authentication at the gateway is deferred, why, and what
  would reopen it
