Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Purpose

Provides the Dispatch Console, the agent-cloud administration UI, in its Phase 1 form. The console is one Dioxus fullstack binary. It signs users in through Authentik, requires a session for every API route, shows the tenant and scope tree declared in the inventory, stores each user's own preferences, and is deployed through a Semaphore template whose verify step checks the running container.

## ADDED Requirements

### Requirement: Sign-in through Authentik

The console MUST sign users in through the Authentik OIDC client `console`, using the authorization code flow with PKCE (S256), a `state` value and a `nonce`. The console MUST verify the ID token and its `nonce` before it stores a user in the session. The console MUST request the `openid`, `profile` and `email` scopes. The `groups` claim is returned with the Authentik profile scope.

#### Scenario: Sign-in redirects to Authentik

- **WHEN** an unauthenticated browser requests `GET /auth/login`
- **THEN** the console answers 302 to the Authentik authorization endpoint, and the PKCE verifier, the `state` and the `nonce` are kept in the session

#### Scenario: Callback stores the verified user

- **WHEN** `GET /auth/callback` receives a valid authorization code and the `state` kept in the session, and the ID token carries the `nonce` kept in the session
- **THEN** the console stores the user's claims in the session and answers 302 to `/`

#### Scenario: Callback with a wrong nonce is refused

- **WHEN** the ID token's `nonce` does not match the `nonce` kept in the session
- **THEN** the console stores no user in the session and refuses the sign-in

### Requirement: Health and the API session gate

The console MUST answer `GET /healthz` with 200 and the text `ok` without a session. Every `/v1/*` route MUST answer 401 with the JSON body `{"error":"unauthenticated"}` when the request carries no session.

#### Scenario: Health answers without a session

- **WHEN** a request without a session calls `GET /healthz`
- **THEN** the response is 200 with the text `ok`, and the `probe` subcommand exits 0 when given the path `/healthz` and the expected status 200

#### Scenario: Unauthenticated API call answers 401

- **WHEN** a request without a session calls `GET /v1/me`
- **THEN** the response is 401 with the JSON body `{"error":"unauthenticated"}`

#### Scenario: Every /v1 route answers 401 without a session

- **WHEN** a request without a session calls any `/v1/*` route, including `GET /v1/tenants`, `GET /v1/tenants/{t}/tree` and `PUT /v1/me`
- **THEN** each response is 401 with the JSON body `{"error":"unauthenticated"}`, and no tenant or preference data is returned

### Requirement: Tenants and scope tree come from the inventory declaration

The console MUST read its tenants, organizations, sites and environments from the scope file, which is rendered from the inventory variable `console_tenants`. In Phase 1 the console MUST NOT read NetBox or Proxmox for this data. The scope file carries an `example` flag. Example data MUST be labelled as example in every response that carries it.

#### Scenario: Tenant list reads the inventory declaration

- **WHEN** a signed-in user calls `GET /v1/tenants`
- **THEN** the response lists each tenant in the scope file with its `id` and `name`, and it reports the `example` flag from that file

#### Scenario: Scope tree for a tenant

- **WHEN** a signed-in user calls `GET /v1/tenants/{t}/tree` for a tenant in the scope file
- **THEN** the response holds the tenant's `id` and `name`, its organizations with their sites, each site's environments, and the `example` flag

#### Scenario: Unknown tenant is not found

- **WHEN** a signed-in user calls `GET /v1/tenants/{t}/tree` for a tenant that is not in the scope file
- **THEN** the response is 404

#### Scenario: Example data is labelled

- **WHEN** the scope file sets `example` to true
- **THEN** every tenant and tree response carries `example` as true, so that no example tenant reads as a live estate

### Requirement: Profile shows OIDC claims read-only and stores preferences

The console MUST show the signed-in user's OIDC claims (`sub`, `email`, `name`, `username` and `groups`) read-only through `GET /v1/me`. The console MUST store each user's preferences (`theme`, `timezone` and `default_scope`) in its SQLite preferences store, keyed by `sub`. The console MUST reject an invalid preference with 422. The `PUT /v1/me` request MUST carry only a `preferences` object, so claims are not writable through the API. The allowed values are `theme` `system`, `light` or `dark` (default `system`); `timezone` of 1 to 64 characters from letters, digits, `_`, `+`, `-` and `/` (default `UTC`); and `default_scope` as null or 1 to 128 characters.

#### Scenario: Profile shows the signed-in claims

- **WHEN** a signed-in user calls `GET /v1/me`
- **THEN** the response holds the `claims` object with `sub`, `email`, `name`, `username` and `groups`, and the `preferences` object with `theme`, `timezone` and `default_scope`, and a user with no saved preferences sees `theme` `system` and `timezone` `UTC`

#### Scenario: Preference update is saved

- **WHEN** a signed-in user calls `PUT /v1/me` with valid preferences
- **THEN** the response has the same shape as `GET /v1/me`, and a later `GET /v1/me` returns the saved values

#### Scenario: Invalid preference is refused

- **WHEN** a signed-in user calls `PUT /v1/me` with a theme other than `system`, `light` or `dark`, or with a timezone that contains characters outside the allowed set
- **THEN** the response is 422 and the stored preferences do not change

### Requirement: Deployment through a Semaphore template with verify

The console MUST be deployed through the Semaphore templates `Deploy console` and `Clean Deploy console`, which run the playbook `deploy-console.yml`. The deployment MUST build the console image on the host from the Containerfile in `platform/services/console/deployment/`. The deployment MUST be idempotent. The verify step MUST pass only when `/healthz` answers 200 and `/v1/me` answers 401 inside the running container.

#### Scenario: Deploy builds the image on the host

- **WHEN** the `Deploy console` template runs
- **THEN** `deploy.sh` builds the console image on the host with the container engine from the Containerfile, and the console image is not pulled from a registry

#### Scenario: Verify checks health and the API gate

- **WHEN** the verify step runs in the deployed container
- **THEN** `/healthz` answers 200 and `/v1/me` answers 401, and the play fails if either answer differs

#### Scenario: Deploy converges on re-run

- **WHEN** the `Deploy console` template runs again with no change to the code or the inventory
- **THEN** the run changes nothing that is already in place, and the verify step still passes
