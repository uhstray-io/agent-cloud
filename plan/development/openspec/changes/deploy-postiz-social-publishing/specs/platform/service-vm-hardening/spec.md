## Purpose

Defines the host-access baseline every platform service VM must meet — credentials held
in the platform secret store, key-only administrative access, and a default-deny host
firewall — together with the ordering guarantees that make reaching that state safe on
a host whose only current access path is a bootstrap password.

## ADDED Requirements

### Requirement: Bootstrap credentials are captured into the secret store before the host is changed

The bootstrap login and privilege-escalation credential for a host SHALL be recorded in
the platform secret store before any hardening step modifies that host, so the secret
store — not an operator's local file — is the authority the automation reads from.

Recording the credential MUST be additive: it may not disturb other material already
held at the same location.

#### Scenario: Credential is recorded before hardening begins

- **WHEN** a host is being onboarded and its bootstrap credential is recorded
- **THEN** the credential is retrievable from the secret store, and the automation that
  later needs privilege escalation on that host reads it from there rather than being
  handed it

#### Scenario: Recording preserves unrelated material

- **GIVEN** the secret store location already holds an administrative key pair
- **WHEN** the bootstrap credential is recorded at that location
- **THEN** the key pair is unchanged

### Requirement: Each service host has its own key, never implicitly rotated

Each service host SHALL be issued its own dedicated key pair, held in the platform
secret store, distinct from the shared administrative key.

Issuing a key MUST be idempotent: where a key for that host already exists it MUST be
left untouched, because replacing a key that is already trusted by a live host would
remove the access it grants.

#### Scenario: Key is issued for a new host

- **WHEN** a key is requested for a host that has none
- **THEN** a key pair is generated and stored, retrievable for later distribution

#### Scenario: Re-running never replaces a live key

- **WHEN** key issuance is run again for a host that already has one
- **THEN** the existing key pair is returned unchanged, and no new key is generated

### Requirement: Key distribution is additive and leaves the existing access path intact

Distributing keys to a host SHALL only add authorized keys. It MUST NOT alter the host's
authentication configuration, and MUST NOT remove or disable the password path that is
currently the only way in.

#### Scenario: Keys are added alongside the working password path

- **WHEN** keys are distributed to a host still reachable only by password
- **THEN** both the shared administrative key and the host's own key are authorized, and
  password authentication still works

#### Scenario: Distribution is repeatable

- **WHEN** distribution is run a second time
- **THEN** the authorized keys are unchanged and no duplicates are introduced

### Requirement: Key authentication is proven from two independent directions before the password path is removed

Password authentication SHALL NOT be disabled on a host until key authentication has
been demonstrated successfully from **both** the orchestrator and an operator
workstation, with password authentication explicitly refused during the demonstration
so that a silent fallback cannot be mistaken for success.

If either demonstration fails, the sequence MUST stop with the password path intact.

#### Scenario: Both proofs succeed, so hardening may proceed

- **WHEN** the orchestrator reaches the host using the key credential, and an operator
  separately reaches it with password authentication explicitly disallowed
- **THEN** key-only access is proven and the hardening step is permitted to run

#### Scenario: A failed proof halts the sequence

- **WHEN** either demonstration fails
- **THEN** hardening does not run, password authentication remains enabled, and the host
  stays reachable

#### Scenario: A silent password fallback cannot be mistaken for success

- **WHEN** the operator's demonstration is performed
- **THEN** password authentication is explicitly refused for that attempt, so success
  can only have come from the key

### Requirement: Privilege escalation is verified before it is depended upon

A host's ability to escalate privilege using the new credential SHALL be exercised
before the step that removes the password path, so that a broken escalation path is
discovered while the password path is still available as a fallback.

#### Scenario: Escalation is exercised while the fallback still exists

- **WHEN** a privileged operation is performed on the host over the key credential,
  before password authentication is removed
- **THEN** its success confirms escalation works, and its failure is recoverable because
  the password path is still open

### Requirement: Hardening removes password authentication and self-verifies

Hardening a host SHALL disable password and interactive authentication, disable direct
administrative login, and configure passwordless privilege escalation for the
service account.

The step MUST verify its own outcome: that password authentication is now refused, and
that key authentication still works. Verification failure MUST be reported, not
silently tolerated.

#### Scenario: Password path is closed and key path confirmed

- **WHEN** hardening completes on a host
- **THEN** an attempt to authenticate by password is refused, and key authentication
  still succeeds

#### Scenario: Privilege escalation configuration is validated before it is installed

- **WHEN** the passwordless-escalation configuration is written
- **THEN** it is validated for correctness before taking effect, so a malformed
  configuration cannot lock the service account out of escalation

### Requirement: The host firewall denies by default and exposes only SSH and the service's own port

A service host SHALL run a default-deny inbound firewall permitting only:
administrative access from designated administrative address ranges, and the service's
own published port from the designated upstream reverse proxy.

Ports belonging to internal supporting components — datastores, caches, workflow
engines — MUST NOT be exposed on the host at all, so no firewall rule is required to
protect them.

Enabling the firewall MUST NOT lock out the orchestrator: administrative allowances are
established before enforcement begins, and connectivity is re-proven afterwards.

#### Scenario: Only the two intended paths are reachable

- **WHEN** the firewall is enforced on a service host
- **THEN** administrative access from a designated range succeeds, the service's port
  from the reverse proxy succeeds, and inbound traffic to any other port or from any
  other source is refused

#### Scenario: Internal components are not exposed

- **WHEN** the host's published ports are enumerated
- **THEN** supporting datastores, caches, and workflow engines publish no host port, and
  no firewall rule names them

#### Scenario: Enforcement does not lock out the orchestrator

- **WHEN** the firewall transitions to enforcing
- **THEN** administrative allowances are already in place, and a fresh connection is
  established afterwards to prove access survived

#### Scenario: The service port is allowed only once it exists

- **WHEN** the firewall is applied before the service is running, and again after
- **THEN** the first application permits only administrative access, the second
  additionally permits the now-published service port, and both runs converge without
  error

### Requirement: An out-of-band recovery path is preserved

Host firewall and authentication hardening SHALL only ever constrain the network path.
An out-of-band administrative console reaching the host independently of its network
configuration MUST remain available, so that no combination of rules can render the host
unrecoverable.

#### Scenario: Recovery after a lockout

- **WHEN** a misconfiguration makes the host unreachable over the network
- **THEN** the out-of-band console still reaches it, and the firewall can be disabled
  from there

### Requirement: Every hardening step is idempotent and re-runnable

Each step in the sequence SHALL converge on re-run rather than erroring or duplicating
state, so that a partially completed onboarding can be resumed by running the sequence
again from the beginning.

#### Scenario: Re-running a completed sequence changes nothing

- **WHEN** the full hardening sequence is run again against an already-hardened host
- **THEN** every step reports no change, and the host's access and firewall state are
  unchanged

#### Scenario: An interrupted sequence is resumable

- **GIVEN** onboarding was interrupted partway
- **WHEN** the sequence is run again from the start
- **THEN** already-completed steps are no-ops and the remaining steps complete
