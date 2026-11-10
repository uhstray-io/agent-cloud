# Receiver OTLP mTLS implementation plan

Scope: implement the o11y-owned source portion of existing task 6.1. This is not a
new OpenSpec proposal and does not close live acceptance. Agentgateway client TLS,
client issuance and gateway deployment remain with the gateway owner.

## Contract

- Add `o11y_otlp_tls_enabled`, default `false`, and require the exact
  `o11y_otlp_tls_server_name` when enabled. The name must match the existing private
  lowercase internal DNS SAN policy; do not guess a default.
- Keep the existing OTLP gRPC listener on its current bind and port. When disabled,
  rendered receiver and journal configuration and the seven-container stack must be
  unchanged. Preserve HTTP 4318 and the internal Alloy-to-Tempo hop.
- The receiver uses the fixed `o11y-otlp-server` server leaf and trusts only the
  authoritative existing step-ca root/intermediate bundle. The same-host journal
  collector uses its required fixed `o11y-journal` client leaf and verifies the
  declared receiver name. No gateway key is copied to o11y.
- Use Alloy's pinned native TLS reload interval and mount the staged `current`
  directories read-only so atomic certificate swaps are visible. Do not hash
  certificate bytes or restart the receiver stack for renewal. Existing connections
  are not immediately reauthenticated by a file reload.
- A trusted client CA proves CA membership, not gateway SAN identity. Keep the
  existing source-scoped firewall boundary; do not claim per-service identity.

## Changes

1. Add only the receiver TLS block to the pinned Alloy v1.5.1 gRPC receiver template,
   conditional on the switch. Keep the plaintext rendering byte-for-byte otherwise.
2. Add an Alloy-only receiver Compose overlay mounting the fixed server leaf parent
   and CA bundle read-only. Add a separate journal-only overlay mounting only its
   own client leaf and the bundle.
3. Add an authenticated TLS client branch to the existing journal exporter. Keep the
   existing plaintext branch unchanged and use the same current endpoint.
4. Add one o11y-specific shared enabled-mode preflight for both non-destructive
   deploys. Validate declarations, fixed host/path/profile/SAN, staged key ownership
   and mode, key/cert match, validity, EKU and CA before config rendering or container
   changes. Distribute the authoritative CA bundle through the existing task only
   after declaration checks. Reuse existing issuance and certificate validation
   primitives; do not create a generic certificate framework.
5. Update the existing journal provider, candidate validation and apply path to select
   the fixed TLS overlay and prove exact read-only runtime mounts while preserving
   v3 cursor, positions and Loki delivery checks. Keep all seven receiver containers
   unchanged during journal apply.
6. Refuse TLS-enabled Clean Deploy before teardown because it removes the issued
   certificate tree. Extend the existing renewal classifier with only the fixed
   receiver server and journal client classes, both using native reload. Prove the
   served server serial and journal client acceptance with bounded local TLS probes;
   no-client and foreign-CA clients must fail through post-handshake I/O.
7. Ignore the exact o11y certificate tree. Add regression coverage to existing BATS,
   strict-provider, synthetic-CA and actual-Ansible test modules. Document source
   ownership, lifecycle and the still-deferred coordinated activation in existing
   OpenSpec and o11y docs. Inventory examples remain placeholders only.

## Ordering and gates

Declaration, containment, host, profile, SAN and path checks precede CA writes.
Authoritative trust distribution and staged leaf checks precede configuration or
container changes. Both the journal producer and receiver must be ready before any
TLS activation. If current-runtime compatibility checks create a first-enable
deadlock, stop for an architecture decision; do not add a readiness waiver or
plaintext fallback.

Validate focused source behavior with the repository's existing render, provider,
renewal and task-sequence harnesses, plus playbook syntax, configured lint and strict
OpenSpec validation. Pinned-image validation is attempted only if the exact image is
already cached; never pull or start local services for this work. No production task,
private inventory, credential read, deploy, clean deploy or certificate issuance is
in scope. Source completion does not check task 6.1 or live task 6.5 complete; those
remain open until their stated external and live evidence exists.
