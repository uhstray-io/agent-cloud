# platform/internal-ca

A production internal certificate authority that issues, distributes, renews, monitors
and backs up certificates for internal names, reached only through the orchestrator.

## ADDED Requirements

### Requirement: The production CA runs on its own host and is deployed through Semaphore
The platform SHALL run step-ca in production on a dedicated VM declared in site-config,
brought up through the service deployment workflow's steps and deployed only by a
Semaphore template from the committed service files, with the key password sourced from
OpenBao and the CA's keys kept encrypted in its volume. A redeploy MUST keep the existing
root, and a destructive reset MUST refuse unless the launch names the CA host explicitly.

#### Scenario: Deploy converges and keeps the root
- WHEN the step-ca deploy template runs twice against the production inventory
- THEN the CA's health check passes after each run and the root certificate's
  fingerprint after the second run equals the fingerprint after the first

#### Scenario: Reset without confirmation is refused
- WHEN the clean-deploy playbook is launched against the CA host without the confirmation
  variable naming that host
- THEN it fails before touching any container or volume, and the root fingerprint is
  unchanged

### Requirement: The CA is reachable only through the orchestrator
The CA's API MUST NOT be reachable from any other host; it SHALL listen on the CA host's
loopback only, and the CA host's firewall SHALL admit only SSH from the controller and the
declared operator ranges. Issuance SHALL run inside the CA container over the
orchestrator's SSH connection.

#### Scenario: A LAN host cannot reach the CA API
- WHEN a LAN host that is not the CA host connects to the CA's port on the CA host
- THEN the connection is refused or dropped

#### Scenario: Only the declared sources reach SSH
- WHEN a host outside the controller address and the declared operator ranges connects to
  the CA host's SSH port
- THEN the connection is dropped

### Requirement: Leaves are issued from consumer-held keys for declared names only
The issuance task SHALL generate each leaf's private key on the consumer host and send
only a certificate request to the CA, so that no private key is ever written on the CA
host or the controller. It MUST refuse a name that is not declared for that consumer in
inventory or that lies outside the internal zone, and it SHALL be the same task in
local-dev and production.

#### Scenario: The private key never leaves the consumer
- WHEN a leaf is issued for a consumer on a host other than the CA host
- THEN the key exists only in the consumer's certificate directory with owner-only
  permissions, and no file containing that key exists on the CA host or in the
  controller's working directory after the run

#### Scenario: An undeclared name is refused
- WHEN the issuance task is asked for a name that is not in the consumer's declared leaf
- THEN it fails before any request reaches the CA, naming the refused name

#### Scenario: Local-dev issuance is unchanged in effect
- WHEN the local Caddy deploy runs with the evolved task, the CA host being the same
  machine
- THEN Caddy serves a wildcard leaf for the local zone that verifies against the local
  step-ca bundle, as before the change

### Requirement: Server and client certificates are distinct profiles
A leaf issued with the server profile MUST NOT be usable for client authentication, and a
leaf issued with the client profile MUST NOT be usable as a server certificate. Production
issuance SHALL use provisioners separate from the bootstrap provisioner, each limited to
the production leaf lifetime. Because every client-profile leaf chains to the same root,
the gateway MUST also admit a request only when the presented client certificate carries,
among its subject alternative names, the name of a client leaf on the gateway's declared
allowlist. The allowlist SHALL name only declared client-profile leaves, and MUST default
to Caddy's leaf alone.

#### Scenario: Another client leaf is refused at the gateway
- WHEN a client presents a client-profile leaf from the production CA whose subject
  alternative names include no name on the gateway's allowlist
- THEN the gateway refuses the request, although the TLS handshake completes

#### Scenario: An allowlisted non-Caddy client is served
- WHEN the gateway's own verification probe presents the declared verifier client leaf
  from the gateway host, with the verifier on the allowlist
- THEN the handshake completes and the request reaches the gateway's key check

#### Scenario: An undeclared allowlist entry is refused at render
- WHEN the gateway's allowlist names a leaf that is not a declared client-profile leaf
- THEN the gateway deploy fails before restarting the gateway and names the entry

#### Scenario: A server leaf is refused as a client
- WHEN a client presents a server-profile leaf from the production CA to a listener that
  requires client certificates chained to that CA
- THEN the TLS handshake fails

#### Scenario: A client leaf authenticates
- WHEN Caddy presents its client-profile leaf to the gateway listener
- THEN the handshake completes and the request is processed

### Requirement: Every consumer trusts the same bundle, read from the CA
The platform SHALL distribute one public trust bundle, root plus intermediate, read from
the CA host on each run, to every declared consumer, and MUST NOT commit the production
root to the public repository.

#### Scenario: Bundles match across consumers
- WHEN the root-distribution task has run for Caddy and for the gateway
- THEN the bundle files on both hosts are byte-identical and their root fingerprint equals
  the CA's

### Requirement: Leaves are renewed on a schedule and the new certificate is served
A Semaphore template on a schedule declared as code SHALL re-issue every declared leaf
whose remaining lifetime is below one third of its total, replace it atomically in a
mounted directory, run the leaf's declared reload action, and MUST fail the run unless
the new certificate is proven in use on the TLS path its profile serves. A server leaf is
proven on the consumer's serving listener, which MUST present the new serial. A client
leaf is proven on the peer that verifies it: a request sent through the client after
the reload action MUST complete its mutual TLS handshake with that peer, and the
certificate the peer records for that request MUST match the fingerprint of the new
leaf read from the client host's file; the client's own listening port is never the
check. A leaf outside its renewal window SHALL be left unchanged.

#### Scenario: A server leaf inside its window is renewed and served
- WHEN the renewal template runs while a server leaf has less than a third of its
  lifetime left
- THEN a new certificate with a new serial is written, the consumer's serving listener
  presents that serial, and requests through the consumer succeed throughout

#### Scenario: Caddy's client leaf is renewed and presented to the gateway
- WHEN the renewal template runs while Caddy's client leaf has less than a third of its
  lifetime left
- THEN a new certificate is written on the Caddy host, a probe request through Caddy
  to the gateway completes after Caddy's reload, the client certificate the gateway
  records for that probe matches the new leaf's fingerprint, and requests through
  Caddy succeed throughout

#### Scenario: A fresh leaf is left alone
- WHEN the renewal template runs while every leaf has more than a third of its lifetime
  left
- THEN no certificate or key changes and the run reports each leaf as current

### Requirement: Expiry is alerted before it causes an outage
The renewal run SHALL publish each leaf's and the intermediate's expiry to the o11y stack,
and Grafana MUST alert when any leaf has fewer than seven days left, when the intermediate
has fewer than ninety days left, or when no renewal result has arrived for thirty-six
hours.

#### Scenario: A leaf near expiry raises an alert
- WHEN a declared leaf's remaining lifetime falls below seven days
- THEN the expiry alert fires to the platform's alert contact point, naming the leaf

#### Scenario: A silent renewal job raises an alert
- WHEN no renewal result reaches Loki for thirty-six hours
- THEN the renewal-silent alert fires

### Requirement: The CA material is backed up and restorable to the same root
The platform SHALL back up the CA's certificates, encrypted keys and configuration from
its volume into site-config on a new branch per run without printing any key material,
and the key password through the existing credential backup. A restore from that backup
MUST produce a CA with the same root.

#### Scenario: Restore keeps the root
- WHEN the backed-up material and password are restored into a fresh volume in local-dev
  and the CA is started
- THEN its health check passes and its root fingerprint equals the backed-up root's

#### Scenario: Backup output carries no key material
- WHEN the backup template runs
- THEN its task output lists file names, the branch and counts only

### Requirement: Internal TLS on a consumer can be rolled back by inventory
Each consumer's use of internal TLS SHALL be switched by inventory values, so that a
redeploy through Semaphore returns that consumer to its previous plain internal transport
without editing committed files.

#### Scenario: Caddy returns to a plain upstream
- WHEN the inference blocks' internal-TLS values are removed from Caddy's inventory and
  Caddy is redeployed through Semaphore
- THEN the blocks proxy to the gateway over plain HTTP and the public path still answers

### Requirement: Platform documentation uses the current Caddy directives
The platform's architecture documents MUST NOT recommend `tls_trusted_ca_certs`; they SHALL
show `tls_trust_pool file` for backend trust and `tls_client_auth` with `tls_server_name`
for mutual TLS.

#### Scenario: No deprecated directive remains
- WHEN `plan/architecture/` is searched for `tls_trusted_ca_certs`
- THEN no line recommends it, and the backend-policy table shows the trust-pool and
  mutual-TLS forms
