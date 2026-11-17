## Purpose

Provides a self-hosted workflow-automation service: platform automation builds and runs flows that call other platform services. The service is deployed and operated through Semaphore playbooks. Its interactive interface is reachable only by platform administrators through the platform's identity provider, and it holds no privileged containers.

## ADDED Requirements

### Requirement: Encryption key and idempotent re-runs

The service MUST keep its encryption key across redeploys. A deploy MUST NOT regenerate `encryption_key`, because a new key makes every stored connection unreadable. A re-run of the deploy with unchanged inputs MUST report no change.

#### Scenario: A redeploy keeps the encryption key

- **GIVEN** the service holds stored connections that were encrypted with the current encryption key
- **WHEN** the service is redeployed with unchanged inputs
- **THEN** the encryption key in the secret store is unchanged, and the stored connections still decrypt

#### Scenario: A re-run reports no change

- **WHEN** the deploy is run a second time with unchanged inputs
- **THEN** the run reports no changed task

### Requirement: Flows run without privileged containers

The service SHALL execute flow code under the code-only sandbox. A container in the service's compose project MUST NOT be granted privileged mode.

#### Scenario: A scheduled flow runs on the local stack

- **WHEN** a scheduled flow is created on the local-dev instance and its time arrives
- **THEN** the flow runs to completion under the code-only sandbox, and no container in the project is privileged

#### Scenario: A scheduled flow runs in production

- **WHEN** a scheduled flow is created on the production instance and its time arrives
- **THEN** the flow runs to completion under the code-only sandbox, and no container in the project is privileged

### Requirement: Account creation is closed after the owner

In production, before the owner exists, the application port published on the host MUST be reachable only from the service host itself, and after the owner exists only from the reverse proxy and from the service host itself. On local-dev the containers share the local-dev network, so this rule covers the host-published port only. The service SHALL refuse a sign-up for an address that holds no invitation once the owner exists. The service MUST NOT be configured to allow open sign-up.

#### Scenario: The deploy creates the owner before the port is reachable

- **WHEN** the service is deployed for the first time
- **THEN** the owner account exists before the declared bind is rendered, and until then no host other than the service host reached the port

#### Scenario: An uninvited sign-up is refused

- **GIVEN** the owner account exists
- **WHEN** a sign-up is requested for an address that holds no invitation
- **THEN** the request is refused, and no account is created

### Requirement: Only platform-admins reach the application

The service SHALL serve its interface only to members of the `platform-admins` group, who authenticate through the platform's identity provider at the reverse proxy. A request from anyone else MUST be refused before it reaches the application.

#### Scenario: A platform-admin passes the gate

- **WHEN** a member of `platform-admins` opens the service's public address and signs in through the identity provider
- **THEN** the request reaches the application and the member sees the application's interface

#### Scenario: A non-admin is refused at the edge

- **WHEN** a signed-in user outside `platform-admins` opens the service's public address
- **THEN** the reverse proxy refuses the request, and the application receives no request from that user

### Requirement: The application port is reachable from the Caddy host only

The host firewall SHALL admit the application port from the reverse proxy host and from no other remote host; the service host itself reaches it locally. The port MUST NOT be reachable from any other address on the local network.

#### Scenario: A direct connection from another LAN host is refused

- **WHEN** a host on the local network other than the reverse proxy host opens a connection to the application port
- **THEN** the connection fails, and a connection from the reverse proxy host succeeds

### Requirement: Outbound traffic from the service follows a declared allowlist

When the egress mode is enforce, outbound connections from the service host or any container of its compose project that the declared egress allowlist does not name SHALL be refused. Whether the host firewall alone achieves this for the containers is an open question in plan 26 (O11). The in-process SSRF guard MUST NOT be the only egress control, because upstream describes it as best-effort.

#### Scenario: An undeclared outbound connection is refused

- **WHEN** the egress mode is enforce and a process on the service host, or in a container of its compose project, opens a connection to a destination and port the allowlist does not name
- **THEN** the connection is refused, and each declared egress probe still succeeds

### Requirement: Webhook paths are exempted by exact path only

The service's inbound webhook paths SHALL be behind the identity gate unless a flow is approved for one. A webhook exemption MUST name one exact path declared in inventory, which is the `/api/v1/webhooks/<flowId>` path of one flow. The `/sync`, `/draft/sync`, `/draft` and `/test` variants MUST NOT be exempt. An exemption MUST NOT cover a path prefix or a group of paths.

#### Scenario: An undeclared webhook path stays gated

- **WHEN** a request arrives on a webhook path that no inventory entry declares
- **THEN** the identity gate applies to the request, and the request is refused without a valid platform-admin session

### Requirement: The database can be backed up and restored

The service SHALL provide a database dump written into a directory with mode 0700. A restore MUST load the dump into a staging database and MUST swap that copy in only after it has loaded completely. A restore that fails part-way MUST leave the live database unchanged.

#### Scenario: A restore swaps in only a fully loaded copy

- **GIVEN** a valid database dump exists
- **WHEN** a restore is started and fails before the staging copy has loaded completely
- **THEN** the live database is unchanged, and no partial copy replaces it

#### Scenario: A dump restores into a running instance

- **WHEN** a dump taken by the backup playbook is restored
- **THEN** the instance answers its health route, and its flows and connections are present
