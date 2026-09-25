## Purpose

Runs NetBox on local-dev so the workflow's inventory, address and tracking steps can be
proven locally, with discovery confined to local-dev targets so a laptop never writes the
production network into its IPAM.

## ADDED Requirements

### Requirement: NetBox runs on local-dev through the local controller
NetBox SHALL deploy on local-dev through the local Semaphore under podman, with its secrets
from the local OpenBao, and MUST answer through the local Caddy behind Authentik.

#### Scenario: Local deploy is repeatable
- **WHEN** the local NetBox deploy runs twice
- **THEN** the second run reports no change and NetBox answers its health check

### Requirement: Discovery is confined to local-dev targets
Local NetBox discovery SHALL scan only targets declared in the local inventory, and every
declared target MUST fall inside a network the deploy reads from the local container engine
at deploy time. A target outside those networks, or an empty target list, MUST stop the
deploy before any discovery agent starts. The allowlist is read from the live engine rather
than declared, because the production ranges live only in site-config, which the local
controller does not hold.

#### Scenario: Target outside local-dev is refused
- **WHEN** a declared discovery target is outside every local container network
- **THEN** the deploy fails before any discovery agent starts, naming the target

#### Scenario: No targets means no discovery
- **WHEN** the local inventory declares no discovery targets
- **THEN** NetBox deploys with discovery disabled and reports that it did so

#### Scenario: Local targets are discovered
- **WHEN** discovery runs with only local-dev targets declared
- **THEN** discovered records appear in local NetBox and every address lies inside a local
  container network
