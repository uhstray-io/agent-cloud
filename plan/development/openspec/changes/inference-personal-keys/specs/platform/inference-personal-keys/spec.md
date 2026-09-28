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
rotate due keys, remove expired previous keys and revoke the keys of anyone no longer
eligible. Every run MUST import the gateway deploy, whether or not it wrote a record,
and that deploy MUST restart the gateway only when the rendering differs from the one
the running gateway started with or when no gateway container is running. It MUST fail
before any write when the membership listing fails or the group is not found. On a
listing that succeeded, an empty group is a valid answer; the run MUST refuse, unless
explicitly allowed, a plan that revokes more than half of the existing records, unless
the plan revokes a single record. Each run SHALL push one result line to Loki, and the
platform's shared scheduled-job-silent alert MUST fire when none has arrived for three
hours. It MUST NOT print a key value.

#### Scenario: Unchanged membership does not restart the gateway
- WHEN the reconcile runs with no member added or removed, no key due to rotate or
  expire, and the running gateway started from the current rendering
- THEN no OpenBao record changes, the deploy runs, and the gateway container is not
  recreated

#### Scenario: A large drop does not revoke everyone
- WHEN the membership listing succeeds but the plan would revoke more than half of the
  existing records and more than one record, and `allow_mass_revoke` is not set
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
- THEN the next run's deploy finds the running gateway started from an older rendering,
  brings up a gateway on the current one, and a request with the revoked key gets 401

#### Scenario: Expiry converges without a record change
- WHEN a key's recorded expiry passes and no record is due for any write
- THEN the next run's deploy renders without the expired key, brings up a gateway on
  that rendering, and a request with that key gets 401

#### Scenario: A missing gateway container is redeployed
- WHEN the gateway container is missing or stopped on its host and no record is due
  for any write
- THEN the next run deploys the gateway, and a request with a current personal key is
  served

#### Scenario: A stopped reconcile is detected
- WHEN no reconcile result line reaches Loki for three hours
- THEN the scheduled-job-silent alert fires naming the reconcile job

#### Scenario: A non-conforming username is refused by name
- WHEN a member's username does not match `^[a-z0-9][a-z0-9-]*$`
- THEN that member gets no key, the run reports the username as refused, and every
  other member is still reconciled

### Requirement: Keys expire within a month with an enforced overlap
When a key change restarts the gateway, the reconcile SHALL rotate all keys together on a
declared fixed day of each calendar month, so that rotation restarts the gateway a fixed
number of times per cycle whatever the number of users, and every key SHALL carry an
expiry no later than the next cohort day plus the grace days (at most 34 days after
issue). When the gateway applies key changes without a restart, every key SHALL carry an
expiry no later than 30 days after issue and the reconcile SHALL mint each key's
successor when that key is 27 days old. In both cases the old key SHALL stay valid until its recorded expiry, at
most three days after its successor is minted. The gateway deploy MUST render a key only
while its recorded expiry is in the future and MUST fail on a record whose expiry is
missing or unparseable, so that expiry is enforced by code on every deploy.

#### Scenario: Rotation overlaps
- WHEN the reconcile runs on a rotation day (the cohort day, or a key's day 27 when key
  changes need no restart)
- THEN each due key gets a new key, the old one is kept as the previous key with an
  expiry no later than three days later, the gateway accepts both, and on the cohort
  branch the whole cohort's rotation recreates the gateway at most once in that run

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
