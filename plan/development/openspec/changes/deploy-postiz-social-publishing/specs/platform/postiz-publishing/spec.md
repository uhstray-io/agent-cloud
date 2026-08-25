## Purpose

Provides a self-hosted social-media publishing plane: platform members compose and
schedule posts to connected social accounts through a single-sign-on web interface,
and platform automation drives the same publishing surface programmatically over an
API-key-authenticated endpoint.

## ADDED Requirements

### Requirement: Single-sign-on through the platform identity provider

The publishing service SHALL authenticate interactive users through the platform's
central identity provider, and SHALL NOT serve the application to an unauthenticated
visitor.

The canonical public URL the service advertises MUST be identical to the URL a browser
uses to reach it, because the authentication redirect is derived from that URL; a
mismatch MUST be treated as a misconfiguration rather than tolerated.

#### Scenario: Member signs in with a platform identity

- **WHEN** a platform member with publishing entitlement opens the service's public URL
  and chooses to sign in with the platform identity provider
- **THEN** they are redirected to the identity provider, and on approval returned to
  the service with an authenticated session

#### Scenario: Unauthenticated visitor is refused

- **WHEN** an unauthenticated visitor requests the application
- **THEN** the service does not render publishing functionality and offers only the
  sign-in path

#### Scenario: Identity provider does not release an email claim

- **WHEN** the identity provider returns a profile without the email claim the service
  identifies accounts by
- **THEN** sign-in fails with an error surfaced to the operator, and no partial or
  anonymous account is created

#### Scenario: Advertised URL does not match the browser URL

- **WHEN** the service is configured with a public URL that differs from the address a
  browser actually reaches it on
- **THEN** the sign-in redirect is rejected by the identity provider as an unregistered
  destination, and the failure is visible rather than silent

### Requirement: Registration closes after the first account exists

The service SHALL support a closed-registration state in which no new account can be
created, and the transition into that state MUST be expressed as configuration so it
is reproducible and survives a redeployment.

#### Scenario: Operator closes registration after first sign-in

- **WHEN** the first account has been established and the operator sets registration to
  closed, then redeploys
- **THEN** the service continues to serve the existing account, and a second, unknown
  identity attempting to register is refused

#### Scenario: Closed state survives redeployment

- **WHEN** the service is redeployed while registration is configured closed
- **THEN** registration remains closed without any manual step on the host

### Requirement: Scheduled posts publish without operator intervention

The service SHALL publish a scheduled post to its connected social accounts at the
requested time, with no operator action between scheduling and publication.

A scheduling backlog MUST survive a restart of the service: a post scheduled before a
restart and due after it MUST still publish.

#### Scenario: Post publishes at its scheduled time

- **WHEN** a member schedules a post to a connected account for a future time and that
  time arrives
- **THEN** the post is published to that account and its state reflects publication

#### Scenario: Schedule survives a service restart

- **GIVEN** a post is scheduled for a time in the future
- **WHEN** the service is restarted before that time arrives
- **THEN** the post still publishes at its scheduled time

#### Scenario: Publication failure is attributable

- **WHEN** publication to a connected account fails because the account's authorization
  has expired or been revoked
- **THEN** the failure is recorded against that post and is distinguishable from a post
  that was never attempted

### Requirement: Programmatic publishing for platform automation

The service SHALL expose an automation endpoint that authenticates by API key,
independent of the interactive sign-in flow, so non-interactive callers can create,
upload media for, and schedule posts.

Interactive single-sign-on MUST NOT be enforced in front of this endpoint — a
browser-oriented authentication gate placed ahead of it would make the endpoint
unusable by a non-interactive caller.

The service MUST enforce a configurable ceiling on automated post-creation requests,
and that ceiling MUST be discoverable by the callers expected to respect it.

#### Scenario: Automation creates a scheduled post

- **WHEN** an automation caller presents a valid API key and requests creation of a
  scheduled post
- **THEN** the post is accepted and appears in the schedule alongside posts created
  interactively

#### Scenario: Missing or invalid key is refused

- **WHEN** a caller requests the automation endpoint without a valid API key
- **THEN** the request is rejected as unauthorized, and no post is created

#### Scenario: Automation endpoint is reachable without an interactive session

- **WHEN** an automation caller with a valid API key and no browser session calls the
  endpoint over the service's public URL
- **THEN** the request reaches the service and is served, without being redirected to
  an interactive sign-in flow

#### Scenario: Rate ceiling is enforced

- **WHEN** an automation caller exceeds the configured post-creation ceiling within the
  ceiling's window
- **THEN** further creation requests are refused until the window resets, and the
  refusal is distinguishable from an authentication failure

### Requirement: All credentials are sourced from the platform secret store

Every credential the service needs — its own signing and datastore secrets, and the
per-platform social application credentials — SHALL be sourced from the platform secret
store at deployment time. No credential may be committed to the repository, baked into
an image, or persist only on the host as the source of truth.

Credentials whose rotation would destroy state — the session-signing secret and the
datastore password — MUST be generated once and reused on every subsequent deployment.

#### Scenario: Repository contains no credential values

- **WHEN** the service's committed configuration is inspected
- **THEN** it contains no credential values, only references resolved at deployment time

#### Scenario: Sessions and API keys survive redeployment

- **GIVEN** a member has an active session and automation holds a valid API key
- **WHEN** the service is redeployed
- **THEN** both remain valid, because the signing secret was reused rather than
  regenerated

#### Scenario: Existing data remains readable after redeployment

- **WHEN** the service is redeployed against its existing datastore
- **THEN** it authenticates to that datastore successfully, because the password was
  reused rather than regenerated

#### Scenario: A social platform credential is added later

- **WHEN** an operator adds a credential for a social platform that was previously
  unconfigured, into the secret store, and redeploys
- **THEN** that platform becomes connectable with no change to committed code

### Requirement: Public reachability over TLS at a single canonical host

The service SHALL be reachable by members and automation over TLS at one canonical
public hostname, served through the platform's central reverse proxy.

The service MUST NOT require its own publicly reachable port: all external traffic
arrives via the reverse proxy, and the interactive interface, the automation endpoint,
and uploaded media MUST all be served under that one hostname.

#### Scenario: Interface and automation share one hostname

- **WHEN** a member loads the interface and automation calls the endpoint
- **THEN** both are served over TLS under the same canonical hostname

#### Scenario: Uploaded media is retrievable

- **WHEN** a member uploads media and it is attached to a post
- **THEN** that media is retrievable under the same canonical hostname, and remains
  retrievable after the service restarts
