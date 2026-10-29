# platform/agentgateway-mcp

An authenticated MCP endpoint on the agentgateway, one gateway identity per declared
platform admin, its own edge hostname, and secret-free configuration for the Claude Code
and pi harnesses.

## ADDED Requirements

### Requirement: The MCP endpoint is gated by an enrolled key
The gateway SHALL serve MCP only on its `default` listener, only when the inventory flag
`agw_mcp_enabled` is true, and SHALL authenticate every `/mcp` request with a strict API-key
policy built from the same identity list and the same rendering as the `llm` key policy. A
request with no key or an unknown key MUST be answered 401 by the gateway before any target
is contacted. The legacy shared key and the internal self-call identity MUST NOT be
admitted on `/mcp`. With the flag false the rendered configuration SHALL carry no MCP
block.

#### Scenario: Unauthenticated MCP request is refused
- WHEN a client sends an MCP `initialize` request to `/mcp` with no `Authorization` header
- THEN the gateway answers 401 and no target receives a request

#### Scenario: An unknown key is refused
- WHEN a client sends an MCP `initialize` request with a Bearer value that is not an
  enrolled identity's key
- THEN the gateway answers 401

#### Scenario: An enrolled identity lists tools
- WHEN a client sends `initialize` and then `tools/list` to `/mcp` with an enrolled
  identity's key
- THEN the gateway answers `initialize` with a session and `tools/list` with the tools of
  every declared target

#### Scenario: The legacy shared key is not admitted
- WHEN a client presents the legacy shared key to `/mcp` while that key is still admitted
  on `/v1`
- THEN the gateway answers 401 on `/mcp`

#### Scenario: The internal identity is refused on the MCP endpoint
- WHEN a client presents the internal self-call identity's key to `/mcp`
- THEN the gateway answers 401, and the same key is still honoured on the `/v1` request the
  self-call makes

#### Scenario: Disabling MCP removes the endpoint
- WHEN the deploy runs with `agw_mcp_enabled` false
- THEN the rendered configuration contains no `mcp` block and `/mcp` is not served

### Requirement: The deploy verifies the MCP surface
The gateway deploy SHALL, when MCP is enabled, prove an unauthenticated `/mcp` request is
refused and an authenticated `initialize` succeeds, from a request made by the verify step
itself and never through any process's argument list, and SHALL fail the deploy when either
check does not hold. When MCP is disabled the deploy SHALL assert `/mcp` is not served. The
MCP-enabled rendering MUST be validated by the pinned image before any container is
recreated.

#### Scenario: Deploy fails when /mcp answers without a key
- WHEN the verify step finds `/mcp` answering a request that carries no key with anything
  other than 401
- THEN the deploy fails and names the check, with no key value in its output

#### Scenario: Deploy proves an authenticated initialize
- WHEN the verify step sends `initialize` to `/mcp` with one enrolled identity's key
- THEN it records a successful MCP session and the deploy passes the check

### Requirement: Declared admins hold gateway identities
A single declared admin list SHALL drive both the Authentik membership of platform admins
and the gateway's personal identities. The gateway deploy SHALL derive one identity per
list entry, named by the entry's explicit gateway name or else its Authentik username,
merge them with the service identities into one list, and use that list for the secret
declarations, the name check, the `llm` key policy and the `mcp` key policy. A name that
appears twice in the list, equals a reserved internal identity, or falls outside the
permitted charset MUST fail the deploy before any restart; a name present in both the list
and the service identities SHALL be merged once and reported by name as an overlap. An
identity that already has a stored key SHALL keep it. The Authentik verify SHALL assert the
members of `platform-admins` equal the declared admin list plus a declared exceptions list.

#### Scenario: A declared admin gets an identity on the next deploy
- WHEN a new admin is added to the declared list and the gateway deploy runs
- THEN a `client_<name>` field exists for that admin in the service's secret path, the
  rendered configuration enrols its hash on both the `llm` and `mcp` key policies, and the
  admin's key is retrievable through the credential handout template

#### Scenario: A name collision is refused
- WHEN the same name appears twice in the declared admin list, or a list entry's name is
  `mcp-self` or `legacy-shared`
- THEN the deploy fails before any container is recreated and names the colliding entry

#### Scenario: An existing identity keeps its key
- WHEN an admin whose identity already has a stored key is declared in the list under the
  same name, first while the name is still in the service identities and then after it is
  removed from them
- THEN each deploy reuses the stored key, mints nothing, the rendered key-hash count does
  not drop, and the key the admin already holds still authenticates

#### Scenario: Removing an admin ends their access
- WHEN an admin is removed from the declared list and the gateway deploy runs
- THEN the admin's key is answered 401 on both `/v1` and `/mcp`

#### Scenario: Admin membership and the declared list agree
- WHEN a user is added to `platform-admins` outside the declared list and exceptions list,
  or a declared admin is missing from the group
- THEN the Authentik deploy's verify fails naming the account

#### Scenario: The rendered admin accounts match the fixed blocks
- WHEN the Authentik admin entries are rendered from the declared list for the inputs the
  fixed blocks use today
- THEN the rendered entries equal the fixed blocks' username, name, email, active state,
  path and group membership, and the Authentik deploy's read-back verify passes

### Requirement: MCP has its own hostname and edge rules
MCP SHALL be reachable from outside the platform only through its own hostname
(`agw_mcp_hostname`), served by a dedicated Caddy managed site that proxies only `/mcp`
to the gateway over the same upstream mutual-TLS transport the operator-UI host uses,
answers a missing or malformed Bearer header 401 at the edge, and answers every other path
404. The Cloudflare record, challenge skip and rate-limit rule for that hostname SHALL be
declared in `platform/infra/cloudflare` and applied through `apply-cloudflare-tofu.yml`.
The inference hostname's allowlist MUST NOT change.

#### Scenario: The MCP hostname is not reachable without a key
- WHEN a client sends `initialize` to the MCP hostname with no key, and again with a
  malformed one
- THEN each is answered 401 and no `cf-mitigated` challenge header is present

#### Scenario: Only the MCP path reaches the gateway on the MCP hostname
- WHEN a client with a valid key requests `/v1/models`, `/sse` and `/metrics` on the MCP
  hostname
- THEN each is answered 404 at Caddy

#### Scenario: The inference allowlist is unchanged
- WHEN a client requests `/mcp` on the inference hostname, with and without a key
- THEN it is answered 404 at Caddy, and `/v1/*` and `/health` on that hostname behave as
  before this change

### Requirement: Model requests through MCP use a dedicated internal identity
The first MCP target SHALL be an OpenAPI target over the gateway's own OpenAI-compatible
`/v1`, defined by a schema file in the repository, authenticating with a dedicated internal
identity that has its own token budget, whose key is held in OpenBao, supplied to the
gateway by environment reference, never rendered into the configuration and never handed
out.

#### Scenario: A model request through MCP is charged to the internal identity
- WHEN an enrolled identity calls the chat-completion tool through `/mcp`
- THEN the model answers, and the gateway's access log and metrics attribute the `/v1`
  request to the internal identity

#### Scenario: The internal identity is never handed out
- WHEN the rendered configuration, the committed templates and the operator documentation
  are inspected
- THEN the internal key's value appears in none of them, and no documented handout names
  its field

### Requirement: The gateway's self-call stays inside the container
The gateway's own call for the first MCP target SHALL go to a loopback listener that is
attached to the LLM surface only, is restricted to requests from the container's own
loopback address, and is never published; and the deploy SHALL render only OpenAPI
targets whose host is that listener, so no target can address the gateway's external
listener or `/mcp`. The loopback listener and its LLM attachment SHALL be rendered only
while MCP is enabled, and the external listener's client-certificate allowlist SHALL NOT
be widened for the self-call.

#### Scenario: A target cannot reach the gateway's own external listener
- WHEN a target is declared as an `mcp` or `sse` kind, or with a host other than the
  loopback listener's address and port (including one aimed at the external listener or
  its `/mcp`)
- THEN the deploy refuses it, naming the entry, before any render or restart; and where
  such a call is made anyway, the external listener refuses it (a client leaf outside the
  allowlist is 403, the internal identity is 401 on `/mcp`) and no request loop starts

#### Scenario: The loopback listener refuses other containers
- WHEN a client other than the gateway process, such as another container on the same
  network, sends a request to the loopback listener's port, with or without a valid key
- THEN the listener answers 403 `authorization failed`, and the port is absent from every
  compose file's published ports

### Requirement: The administrative interface is not an MCP tool
The gateway's configuration dump and admin API SHALL NOT be reachable as an MCP target or
tool until the configuration dump's handling of environment-expanded secrets has been read
and proven; the admin listener MUST remain bound to the container loopback.

#### Scenario: No tool exposes the configuration dump
- WHEN an enrolled identity lists tools and calls every tool whose name or description
  mentions configuration, admin or dump
- THEN none returns gateway configuration, and the admin port is still refused from a LAN
  host

### Requirement: Harness client configuration carries no secret
The repository SHALL ship Claude Code and pi MCP client templates and one idempotent
installer that merges the agentgateway server entry into each harness's user-level
configuration (through the harness's own CLI where it has one), taking the MCP hostname as
an argument and leaving the template URL a placeholder, and the harness SHALL read the key
at connect time by a command that prints the authorization header from the handed-out
site-config file or the macOS Keychain, with no key exported or written to any file. No template,
installer output or repository file
SHALL contain a key value, and the repository SHALL NOT contain a live repo-root
`.mcp.json`.

#### Scenario: Client templates contain no secret
- WHEN the client templates, the installer and the repository root are scanned for a
  Bearer value, a key-shaped string and a `.mcp.json`
- THEN none is found

#### Scenario: Installing client configuration preserves existing servers
- WHEN the installer runs against a user configuration that already holds other MCP servers
- THEN every other server entry is unchanged as parsed JSON (entry-equal) afterwards and
  only the agentgateway entry is added or updated

#### Scenario: Installing client configuration twice changes nothing
- WHEN the installer runs a second time with the same inputs
- THEN no file changes and the run reports names only

### Requirement: Operators have one documented path for each MCP change
The service's operator documentation SHALL state, for the MCP surface, how to add and
remove an admin identity, set the MCP hostname, enable and disable MCP, hand out a key,
install and remove client configuration, and roll the change back, with the Semaphore
template or command for each.

#### Scenario: The operator documentation covers every MCP change
- WHEN `architecture.md` and `agent-practices.md` for the service are read after this
  change lands
- THEN each of the named operations has a row naming its template or command, and no row
  names a template or variable that does not exist in the repository
