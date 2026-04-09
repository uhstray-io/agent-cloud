# OPA Integration Plan: Policy-as-Code for Agent-Cloud

**Date:** 2026-04-07
**Status:** PROPOSED — Pending review
**Context:** The Four-Layer Guardrails Model (CLAUDE.md, Unification Plan) already positions OPA in the Guardrail Layer alongside OpenBao and Kyverno. This plan operationalizes that architecture — defining how OPA deploys, what policies it enforces, how it integrates with the composable automation patterns, and where it fits in the implementation roadmap.

---

## Problem

The current agent-cloud stack has a **policy gap between credential access and action authorization**:

1. **OpenBao governs secrets, not actions** — Once NemoClaw retrieves an API key from `secret/services/nocodb`, nothing constrains which NocoDB endpoints it calls, how often, or under what conditions. OpenBao's HCL policies are path-based and internally scoped — they answer "can this token read this secret path?" not "can this agent perform this action on this service."

2. **Per-service API keys are binary** — A valid API key either works or doesn't. There's no conditional layer: no time-of-day restrictions, no rate limiting by agent identity, no action-level filtering. NemoClaw with a Semaphore API token can trigger any template, including destructive ones.

3. **No unified audit of agent decisions** — OpenBao's audit log captures secret access. n8n logs workflow executions. NocoDB logs task entries. But there's no single decision log that records "NemoClaw attempted action X on service Y and was {allowed|denied} because of policy Z."

4. **Cross-agent coordination has no policy layer** — The NocoDB task queue mediating NemoClaw↔NetClaw coordination relies on application-level checks. There's no infrastructure-level policy preventing one agent from exceeding its scope when writing tasks for another.

5. **The Guardrails Model is incomplete without OPA** — The CLAUDE.md Four-Layer architecture lists OPA explicitly. Kyverno handles Kubernetes admission control (Phase 3+ in the Unification Plan). OPA handles everything else: Docker/Podman environments, agent API authorization, cross-service policy decisions. Without OPA, the Guardrail Layer is only enforcing secrets (OpenBao) and future k8s policies (Kyverno).

---

## Design Principles

These align with the existing platform design principles and composable automation patterns:

1. **OPA is the cross-service policy decision point** — OpenBao owns credentials, Kyverno owns Kubernetes admission, OPA owns everything else. No overlap, no duplication.
2. **Policies are code** — Rego files live in the monorepo under `platform/services/opa/deployment/policies/`, version-controlled, testable with `opa test`.
3. **OPA follows the composable deployment pattern** — Same 4-phase playbook structure as every other service: sparse checkout → manage secrets → deploy containers → verify health.
4. **Credential lifecycle compliance** — OPA's own credentials (if any) follow the Create→Verify→Retire pattern. OPA can also enforce policy on credential access as an Envoy sidecar (Phase 3).
5. **OpenBao remains the single source of truth for secrets** — OPA never stores or manages credentials. It makes authorization decisions. OpenBao issues credentials. They are complementary layers.
6. **Selective integration over completeness** — Deploy the policies the homelab actually needs. Don't write 50 Rego rules for theoretical scenarios. Start with agent action governance and expand based on real incidents or requirements.

---

## Architecture

### Where OPA Sits in the Four-Layer Model

```
┌─────────────────────────────────────────────────────────────┐
│                        AI Layer                              │
│  NemoClaw (.163) · NetClaw (.165) · WisBot · Claude Cowork  │
│  "Can I do X?" ──────────────────────────┐                  │
│                                          ▼                  │
├──────────────────────────────────────────────────────────────┤
│                     Guardrail Layer                          │
│                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐       │
│  │ OpenBao  │    │     OPA      │    │   Kyverno    │       │
│  │ (.164)   │    │   (NEW VM)   │    │  (k8s only)  │       │
│  │          │    │              │    │              │        │
│  │ "Here    │    │ "Are you     │    │ "Is this     │       │
│  │  are your│    │  allowed to  │    │  k8s resource│       │
│  │  creds"  │    │  do this?"   │    │  compliant?" │       │
│  └──────────┘    └──────────────┘    └──────────────┘       │
│       ▲                ▲                    ▲                │
│       │                │                    │                │
│  Credential        Authorization        Admission           │
│  Lifecycle         Decisions            Control             │
│  (AppRole,         (agent actions,      (pod specs,         │
│   rotation,         service calls,       image policies,    │
│   metadata)         time/rate limits)    network policies)  │
├──────────────────────────────────────────────────────────────┤
│                    Automation Layer                           │
│  Semaphore (.117) → Ansible playbooks → deploy.sh           │
├──────────────────────────────────────────────────────────────┤
│                     Platform Layer                           │
│  Docker/Podman · Proxmox VMs · Kubernetes/k0s (future)      │
└──────────────────────────────────────────────────────────────┘
```

### Agent Authorization Flow

The runtime flow for an agent making a service call:

```
Agent (NemoClaw/NetClaw)
  │
  ├─ 1. Query OPA: POST http://opa:8181/v1/data/agentcloud/allow
  │     Input: { agent, action, service, endpoint, context }
  │
  ├─ 2. OPA evaluates Rego policy → { allow: true/false, reason: "..." }
  │     Decision logged to OPA decision log
  │
  ├─ 3. If allowed: Fetch credential from OpenBao
  │     GET http://openbao:8200/v1/secret/data/services/<service>
  │
  ├─ 4. Execute action against target service
  │     POST http://<service>:<port>/<endpoint>
  │
  └─ 5. Log result to NocoDB task_log
        { agent, action, service, opa_decision, result }
```

This is additive to the current flow — agents currently go straight from step 3 to step 4. OPA inserts steps 1-2 as a policy checkpoint without modifying the credential lifecycle.

### Deployment Architecture: Centralized Container

One OPA instance, deployed as a Docker container on a dedicated lightweight VM. All agents and services query it over HTTP.

```
VM: opa-svc (192.168.1.TBD)
  ├── Docker container: openpolicyagent/opa:latest
  │   ├── Port 8181 (REST API — policy decisions)
  │   ├── Port 8282 (diagnostics, optional)
  │   └── /policies (mounted from runtime dir)
  │
  └── Policies loaded from:
      ~/agent-cloud/platform/services/opa/deployment/policies/*.rego
      (sparse checkout, read-only)
```

Resource requirements are minimal — OPA holds all policies and data in-memory, typically consuming ~50–100MB RAM with sub-millisecond evaluation times. A 1-core / 1GB RAM / 20GB disk VM is sufficient.

---

## Repository Structure

Following the established `deployment/ + context/` pattern:

```
platform/services/opa/
  deployment/
    compose.yml                     # OPA container definition
    deploy.sh                       # Container lifecycle (pull, start, wait)
    post-deploy.sh                  # Load initial policies, verify API
    templates/
      opa-env.env.j2               # Environment config (log level, etc.)
    policies/                       # Rego policy files (source of truth)
      agentcloud/
        agent_actions.rego          # Core agent authorization rules
        agent_actions_test.rego     # Unit tests for agent rules
        semaphore_governance.rego   # Semaphore template execution policies
        semaphore_governance_test.rego
        network_operations.rego     # NetClaw-specific network action policies
        network_operations_test.rego
        data.json                   # Static data (agent definitions, service catalog)
      common/
        helpers.rego                # Shared utility functions (time checks, etc.)
  context/
    architecture.md                 # How AI agents interact with OPA
    skills/                         # Agent skill definitions for OPA queries
      check-authorization.md        # NemoClaw/NetClaw skill: query OPA before acting
```

---

## Composable Deployment (Following Automation-Composability Pattern)

### VM Specification

Added to `proxmox/vm-specs.yml`:

```yaml
opa:
  vmid: 270
  hostname: opa-svc
  cores: 1
  memory: 1024         # 1GB — OPA is extremely lightweight
  disk: 20
  ip: "192.168.1.170"  # TBD — confirm against NetBox
  container_engine: docker
  service_name: opa
  monorepo_deploy_path: "platform/services/opa/deployment"
```

### Composable Playbook: `deploy-opa.yml`

Follows the standard 4-phase pattern from AUTOMATION-COMPOSABILITY.md:

```yaml
---
# deploy-opa.yml — Deploy OPA policy engine
# Pattern: Sparse checkout → Secrets → Containers → Verify

- name: "Phase 1: Sparse Checkout"
  hosts: opa_svc
  tasks:
    - include_tasks: tasks/sparse-checkout.yml
      vars:
        _sparse_paths:
          - "platform/services/opa/deployment"
          - "platform/lib"

- name: "Phase 2: Secrets + Runtime Directory"
  hosts: opa_svc
  tasks:
    - include_tasks: tasks/manage-secrets.yml
      vars:
        _secret_definitions:
          - path: "{{ vault_secret_prefix }}/opa"
            fields:
              decision_log_endpoint: "{{ _decision_log_endpoint | default('') }}"
        _env_templates:
          - src: "templates/opa-env.env.j2"
            dest: "env/opa.env"
    - include_tasks: tasks/setup-runtime-dir.yml

- name: "Phase 3: Container Operations"
  hosts: opa_svc
  tasks:
    - include_tasks: tasks/run-deploy.yml

- name: "Phase 4: Verify"
  hosts: opa_svc
  tasks:
    - include_tasks: tasks/verify-health.yml
      vars:
        _health_url: "http://{{ ansible_host }}:8181/health"
        _health_retries: 5
        _health_delay: 3
```

### compose.yml

```yaml
services:
  opa:
    image: openpolicyagent/opa:${OPA_VERSION:-latest-static}
    container_name: opa
    command:
      - "run"
      - "--server"
      - "--addr=0.0.0.0:8181"
      - "--diagnostic-addr=0.0.0.0:8282"
      - "--set=decision_logs.console=true"
      - "--set=status.console=true"
      - "/policies"
    ports:
      - "8181:8181"
      - "8282:8282"
    volumes:
      - "${CLONE_DIR}/platform/services/opa/deployment/policies:/policies:ro"
    env_file:
      - env/opa.env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8181/health"]
      interval: 15s
      timeout: 5s
      retries: 3
```

### deploy.sh

Following the pure container operations pattern — no secret generation, no OpenBao interaction:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLONE_DIR="${CLONE_DIR:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"

source "${CLONE_DIR}/platform/lib/common.sh"

# Step 1: Verify env files (fail if Ansible didn't run)
require_file "env/opa.env" "Run manage-secrets.yml first"

# Step 2: Pull latest OPA image
compose_cmd pull

# Step 3: Start OPA
compose_cmd up -d

# Step 4: Wait for health
wait_for_health "http://localhost:8181/health" 30
log_success "OPA is healthy"
```

### post-deploy.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLONE_DIR="${CLONE_DIR:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"

source "${CLONE_DIR}/platform/lib/common.sh"

# Verify policies loaded
POLICY_COUNT=$(curl -s http://localhost:8181/v1/policies | jq '.result | length')
if [[ "$POLICY_COUNT" -lt 1 ]]; then
  log_error "No policies loaded in OPA"
  exit 1
fi
log_success "OPA loaded ${POLICY_COUNT} policy module(s)"

# Smoke test: query a policy
RESULT=$(curl -s -X POST http://localhost:8181/v1/data/agentcloud/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"agent":"smoke-test","action":"test","service":"opa"}}')

if echo "$RESULT" | jq -e '.result' > /dev/null 2>&1; then
  log_success "OPA policy evaluation working"
else
  log_error "OPA policy evaluation failed: $RESULT"
  exit 1
fi
```

---

## Credential Lifecycle Integration

OPA itself has a minimal credential footprint compared to services like NetBox or NocoDB. However, it must be fully integrated with the credential lifecycle patterns from CREDENTIAL-LIFECYCLE-PLAN.md.

### OPA's Credential Surface

| Credential | Purpose | Storage | TTL |
|---|---|---|---|
| OPA API access token (optional) | Authenticate callers to OPA's REST API | OpenBao `secret/{{ vault_secret_prefix }}/opa:api_token` | 90 days |
| Decision log shipping token | Authenticate to Loki/observability endpoint | OpenBao `secret/{{ vault_secret_prefix }}/opa:log_token` | 90 days |
| Bundle server credentials (Phase 3) | Pull policy bundles from authenticated endpoint | OpenBao `secret/{{ vault_secret_prefix }}/opa:bundle_token` | 90 days |

**Phase 1 (initial deployment):** OPA runs with `--authentication=off` — all agents can query it without tokens. This is acceptable because OPA is on an internal-only network and returns decisions, not secrets. The policy evaluation itself is the security mechanism — an attacker who can query OPA only learns what's allowed, and OPA doesn't execute actions.

**Phase 2 (hardened):** Enable `--authentication=token` and issue per-agent bearer tokens stored in OpenBao. Agents fetch their OPA token from OpenBao alongside their service credentials. Token rotation follows the Create→Verify→Retire pattern via `rotate-credential.yml`.

### AppRole for OPA

OPA's own AppRole follows the `manage-approle.yml` composable task:

```hcl
# opa policy — minimal, OPA only needs its own config secrets
path "secret/data/{{ vault_secret_prefix }}/opa" {
  capabilities = ["read"]
}

path "secret/metadata/{{ vault_secret_prefix }}/opa" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
```

Provisioned via:
```yaml
- include_tasks: tasks/manage-approle.yml
  vars:
    _approle_name: "opa"
    _approle_policy: "{{ lookup('file', _deploy_dir + '/config/policies/opa.hcl') }}"
    _approle_token_ttl: "30m"
```

### Metadata on OPA Secrets

Following the credential metadata standard from Credential-Lifecycle-Plan.md, every secret stored for OPA includes KV v2 custom metadata:

```json
{
  "created_at": "2026-04-07T12:00:00Z",
  "creator": "deploy-opa.yml",
  "site": "uhstray-dc",
  "purpose": "OPA policy engine configuration",
  "rotation_schedule": "90d"
}
```

Written by `write-secret-metadata.yml` as part of the deploy playbook.

### Updating Agent AppRole Policies

Existing agent AppRoles (NemoClaw, NetClaw) do **not** need modification to work with OPA. OPA is queried via unauthenticated HTTP (Phase 1) or with a separate OPA-specific bearer token (Phase 2). The OpenBao AppRole policies control secret access; OPA controls action authorization. They are orthogonal.

However, when per-agent OPA tokens are introduced (Phase 2), agent policies need a single additional path:

```hcl
# Addendum to nemoclaw-read policy
path "secret/data/{{ vault_secret_prefix }}/opa/agents/nemoclaw" {
  capabilities = ["read"]
}
```

This stores NemoClaw's OPA bearer token at a per-agent path, maintaining the least-privilege principle.

---

## Policy Design

### Core Policy: Agent Action Authorization

`platform/services/opa/deployment/policies/agentcloud/agent_actions.rego`:

```rego
package agentcloud

import rego.v1

default allow := false

# ──────────────────────────────────────────────
# Static data: loaded from data.json
# Defines agent identities, service catalog,
# and permission mappings
# ──────────────────────────────────────────────

# NemoClaw: API-only workflow automation
allow if {
    input.agent == "nemoclaw"
    input.service in data.agentcloud.nemoclaw.allowed_services
    input.action in data.agentcloud.nemoclaw.allowed_actions[input.service]
}

# NetClaw: network infrastructure operations
allow if {
    input.agent == "netclaw"
    input.service in data.agentcloud.netclaw.allowed_services
    input.action in data.agentcloud.netclaw.allowed_actions[input.service]
}

# Block all agents from destructive Semaphore templates
deny if {
    input.service == "semaphore"
    input.action == "run_task"
    input.template_name in data.agentcloud.semaphore.destructive_templates
    not input.human_approved
}

# Override: deny always takes precedence
allow if {
    not deny
    # ... (existing allow rules above)
}

# Rate limiting signal (not enforced by OPA, but reported)
rate_warning if {
    input.action_count_last_minute > data.agentcloud.rate_limits[input.agent][input.service]
}

# Decision metadata (returned alongside allow/deny)
decision := {
    "allowed": allow,
    "agent": input.agent,
    "service": input.service,
    "action": input.action,
    "reason": reason,
    "rate_warning": rate_warning,
}

reason := "allowed by agent policy" if { allow; not deny }
reason := "blocked by destructive template policy" if { deny }
reason := "no matching allow rule" if { not allow; not deny }
```

### Static Data File

`platform/services/opa/deployment/policies/agentcloud/data.json`:

```json
{
  "agentcloud": {
    "nemoclaw": {
      "allowed_services": ["nocodb", "github", "discord", "n8n", "semaphore", "netbox"],
      "allowed_actions": {
        "nocodb": ["read", "create", "update"],
        "github": ["list_issues", "create_issue", "update_issue", "add_comment"],
        "discord": ["send_message", "read_messages"],
        "n8n": ["list_workflows", "trigger_workflow", "list_executions"],
        "semaphore": ["list_projects", "list_templates", "run_task", "check_task"],
        "netbox": ["read"]
      }
    },
    "netclaw": {
      "allowed_services": ["netbox", "nocodb", "pfsense", "snmp", "nmap"],
      "allowed_actions": {
        "netbox": ["read", "create", "update"],
        "nocodb": ["read", "create", "update"],
        "pfsense": ["read_config", "read_status"],
        "snmp": ["poll"],
        "nmap": ["scan_subnet"]
      }
    },
    "semaphore": {
      "destructive_templates": [
        "Clean Deploy NetBox",
        "Clean Deploy NocoDB",
        "Clean Deploy n8n",
        "Wipe and Rebuild"
      ]
    },
    "rate_limits": {
      "nemoclaw": { "discord": 5, "semaphore": 2, "github": 10 },
      "netclaw": { "nmap": 1, "pfsense": 5 }
    }
  }
}
```

### Policy Tests

`platform/services/opa/deployment/policies/agentcloud/agent_actions_test.rego`:

```rego
package agentcloud_test

import rego.v1
import data.agentcloud

# NemoClaw can read NocoDB
test_nemoclaw_read_nocodb_allowed if {
    agentcloud.allow with input as {
        "agent": "nemoclaw",
        "action": "read",
        "service": "nocodb"
    }
}

# NemoClaw cannot scan subnets
test_nemoclaw_nmap_denied if {
    not agentcloud.allow with input as {
        "agent": "nemoclaw",
        "action": "scan_subnet",
        "service": "nmap"
    }
}

# NetClaw can poll SNMP
test_netclaw_snmp_allowed if {
    agentcloud.allow with input as {
        "agent": "netclaw",
        "action": "poll",
        "service": "snmp"
    }
}

# No agent can trigger destructive templates without approval
test_destructive_template_blocked if {
    not agentcloud.allow with input as {
        "agent": "nemoclaw",
        "action": "run_task",
        "service": "semaphore",
        "template_name": "Clean Deploy NetBox",
        "human_approved": false
    }
}

# Unknown agent denied by default
test_unknown_agent_denied if {
    not agentcloud.allow with input as {
        "agent": "rogue-agent",
        "action": "read",
        "service": "nocodb"
    }
}
```

Run tests with: `opa test platform/services/opa/deployment/policies/ -v`

### Semaphore Governance Policy

Separate policy file for Semaphore-specific rules, referenced by the Implementation Plan's Phase 1 "Semaphore Playbook Execution" step:

```rego
package agentcloud.semaphore

import rego.v1

default can_trigger := false

# Only non-destructive templates can be triggered by agents
can_trigger if {
    input.agent in data.agentcloud.semaphore.allowed_triggerers
    not input.template_name in data.agentcloud.semaphore.destructive_templates
}

# Destructive templates require human approval
can_trigger if {
    input.template_name in data.agentcloud.semaphore.destructive_templates
    input.human_approved == true
    input.approver != ""
}
```

### Network Operations Policy (NetClaw-Specific)

Enforces the CIDR scoping from the Unification Plan's "AI CAN / AI CANNOT" matrix:

```rego
package agentcloud.network

import rego.v1

default scan_allowed := false

# Only NetClaw can perform network scans
scan_allowed if {
    input.agent == "netclaw"
    input.action == "scan_subnet"
    cidr_in_scope(input.target_cidr)
}

# Scans restricted to defined homelab CIDRs
cidr_in_scope(cidr) if {
    cidr in data.agentcloud.network.allowed_cidrs
}

# Config push requires ITSM gate
default config_push_allowed := false

config_push_allowed if {
    input.agent == "netclaw"
    input.action == "push_config"
    input.itsm_ticket != ""
    input.itsm_status == "approved"
}
```

---

## Semaphore Templates

Added to `platform/semaphore/templates.yml`:

```yaml
- name: "Deploy OPA"
  playbook: "platform/playbooks/deploy-opa.yml"
  description: "Deploy OPA policy engine — sparse checkout, secrets, containers, verify"
  environment: "production"

- name: "Clean Deploy OPA"
  playbook: "platform/playbooks/clean-deploy-opa.yml"
  description: "Wipe and rebuild OPA — destructive"
  environment: "production"

- name: "Test OPA Policies"
  playbook: "platform/playbooks/test-opa-policies.yml"
  description: "Run opa test against all policy files (read-only, no deploy)"
  environment: "production"

- name: "Reload OPA Policies"
  playbook: "platform/playbooks/reload-opa-policies.yml"
  description: "Git pull + restart OPA container to pick up policy changes"
  environment: "production"
```

---

## Integration with Existing Services

### Agent Integration (NemoClaw / NetClaw)

Agents gain a new OPA query step before executing any service call. This is implemented as a shared library function in the agent's tool executor:

```python
# agents/nemoclaw/context/skills/opa_check.py (pseudocode)
import httplib2

OPA_URL = "http://opa-svc:8181/v1/data/agentcloud/decision"

def check_authorization(agent: str, action: str, service: str, **context) -> dict:
    """Query OPA before executing any service action."""
    input_data = {
        "input": {
            "agent": agent,
            "action": action,
            "service": service,
            **context
        }
    }
    response = httplib2.Http().request(OPA_URL, "POST", json.dumps(input_data))
    decision = json.loads(response[1])["result"]

    if not decision["allowed"]:
        raise AuthorizationDenied(
            f"OPA denied: agent={agent} action={action} "
            f"service={service} reason={decision['reason']}"
        )

    return decision
```

### n8n Integration

n8n workflows that trigger agent actions can include an OPA check node (HTTP Request) before the action node. This adds policy enforcement to scheduled workflows without modifying the agent code.

### Observability Integration

OPA's decision log streams to stdout by default (configured with `--set=decision_logs.console=true`). In the compose environment, Docker collects these logs. When the observability stack (Loki) is deployed, logs route through the standard pipeline:

```
OPA container stdout → Docker log driver → Loki → Grafana dashboards
```

This provides the missing "unified audit of agent decisions" identified in the Problem section. A Grafana dashboard can show: decisions per agent, deny rate, policy evaluation latency, and rate warning triggers.

---

## Implementation Phases

This plan slots into the existing implementation roadmap. OPA is a **Guardrail Layer** service and does not block any current Phase 0.75/1 work. It can be deployed in parallel.

### Phase 1: Foundation (Week 1-2)

**Goal:** OPA running, core agent policies loaded, smoke tests passing.

**Tasks:**
1. Provision OPA VM via `provision-vm.yml` (1 core / 1GB / 20GB)
2. Create `platform/services/opa/deployment/` directory structure
3. Write `compose.yml`, `deploy.sh`, `post-deploy.sh`
4. Write `agent_actions.rego` + `data.json` with NemoClaw and NetClaw rules
5. Write `agent_actions_test.rego` — all tests passing via `opa test`
6. Create `deploy-opa.yml` composable playbook (4-phase pattern)
7. Add OPA to `platform/semaphore/templates.yml` + run `setup-templates.yml`
8. Create OPA AppRole via `manage-approle.yml` (minimal — read own config)
9. Deploy via Semaphore "Deploy OPA" template
10. Verify: `curl http://opa-svc:8181/v1/data/agentcloud/allow` returns decisions

**Acceptance criteria:**
- OPA container healthy on port 8181
- All Rego unit tests pass
- Smoke test queries return correct allow/deny decisions
- OPA appears in OpenBao as AppRole with metadata
- Semaphore templates registered

**Does NOT require:** Agent code changes, n8n workflow modifications, or Loki deployment.

### Phase 2: Agent Integration (Week 3-4)

**Goal:** NemoClaw and NetClaw query OPA before executing actions.

**Tasks:**
1. Add `check_authorization()` function to NemoClaw's tool executor
2. Add OPA query to NetClaw's network action pipeline
3. Update NemoClaw sandbox network policy to allow HTTP to OPA (port 8181)
4. Update NetClaw sandbox network policy to allow HTTP to OPA (port 8181)
5. Write `semaphore_governance.rego` — enforce template execution policies
6. Write `network_operations.rego` — CIDR scoping and ITSM gating for NetClaw
7. Add OPA decision results to NocoDB `task_log` entries (new column: `opa_decision`)
8. Update `data.json` with Semaphore destructive template list from actual templates.yml

**Acceptance criteria:**
- NemoClaw queries OPA before every service API call
- NetClaw queries OPA before network operations
- Denied actions appear in NocoDB `task_log` with reason
- Destructive Semaphore templates blocked without human approval
- NetClaw subnet scans restricted to allowed CIDRs

**Depends on:** Phase 1 complete, NemoClaw Phase 1 integration (Implementation Plan Step 6) in progress or complete.

### Phase 3: Hardening + Observability (Week 5-6)

**Goal:** OPA authenticated, decision logs flowing to observability stack, credential rotation integrated.

**Tasks:**
1. Enable `--authentication=token` on OPA
2. Generate per-agent OPA bearer tokens, store in OpenBao at `secret/{{ vault_secret_prefix }}/opa/agents/<agent>`
3. Update agent OpenBao policies with OPA token read path
4. Add OPA token to agent `manage-secrets.yml` env templates
5. Write `rotate-opa-tokens.yml` following Create→Verify→Retire pattern
6. Add OPA rotation to credential lifecycle schedule (90-day rotation)
7. Configure OPA decision log shipping to Loki (when available)
8. Create Grafana dashboard: decisions/minute, deny rate, per-agent activity
9. Add OPA to `audit-credentials.yml` scope (weekly credential inventory)

**Acceptance criteria:**
- Unauthenticated OPA queries rejected
- Each agent authenticates to OPA with its own bearer token
- OPA tokens rotate on 90-day schedule via Semaphore
- Decision logs visible in Grafana (if Loki deployed)
- OPA credentials appear in weekly audit report

**Depends on:** Phase 2 complete. Loki deployment is optional — decision logs fall back to Docker stdout.

### Phase 4: Advanced Policies (Ongoing)

**Goal:** Expand policy coverage based on operational needs.

**Potential additions (implement as needed, not speculatively):**
- Time-based restrictions (maintenance windows for NetClaw config changes)
- Cross-agent delegation policies (NemoClaw requesting NetClaw actions via NocoDB queue)
- Rate limiting enforcement (currently advisory — make it blocking)
- Envoy sidecar in front of OpenBao for enriched vault access policies (replaces missing Sentinel)
- Policy bundle server (nginx serving bundles, replacing volume mount) for multi-site

---

## Dependency Map

```
Phase 1 (OPA Foundation)
  ├── Requires: Proxmox provisioning (Phase 0 ✅), manage-approle.yml (✅)
  ├── Requires: sparse-checkout.yml, setup-runtime-dir.yml (planned tasks)
  └── Parallel with: NemoClaw Phase 1 integration work

Phase 2 (Agent Integration)
  ├── Requires: Phase 1 complete
  ├── Requires: NemoClaw deployed and functional
  └── Requires: NetClaw VM provisioned (if integrating NetClaw)

Phase 3 (Hardening)
  ├── Requires: Phase 2 complete
  ├── Optional: Loki deployment (for decision log shipping)
  └── Integrates with: Credential Lifecycle Plan Phase 2 (metadata) + Phase 5 (audit)

Phase 4 (Advanced)
  ├── Driven by operational needs, not scheduled
  └── Envoy sidecar requires: OPA + OpenBao both stable
```

---

## Open Questions

1. **VM IP allocation** — `.170` is proposed for OPA. Confirm against NetBox that this is available and does not conflict with existing reservations.
2. **OPA authentication in Phase 1** — Running unauthenticated is pragmatic for internal-only homelab. Should we skip straight to token auth if deployment is quick? The complexity delta is low.
3. **Policy change workflow** — Currently policies are volume-mounted from the sparse checkout. A `git pull + container restart` reloads policies. Is this sufficient, or should we implement the OPA bundle API for hot-reload without restart?
4. **n8n OPA integration depth** — Should n8n workflows query OPA directly (HTTP Request node), or should this be handled at the agent level only? Adding OPA checks in n8n provides defense-in-depth but adds latency to every workflow execution.
5. **Rate limiting enforcement** — OPA can report rate warnings, but actual rate limiting requires a stateful component (OPA is stateless). Should rate limiting be delegated to an API gateway (Kong/Traefik, Phase 3 of Unification Plan) with OPA providing the policy?

---

## Anti-Patterns to Avoid

- **Don't duplicate OpenBao's job** — OPA does not store secrets, manage credentials, or handle authentication. If you're writing a Rego rule about "can this agent read this vault path," reconsider — that's OpenBao's HCL policy domain.
- **Don't write speculative policies** — Start with the agent action rules that match real NemoClaw/NetClaw integrations from the Implementation Plan. Expand based on incidents or new agent capabilities, not theoretical attack vectors.
- **Don't make OPA a hard dependency on day one** — Agents should gracefully handle OPA being unreachable (log a warning, continue with caution). Make OPA a hard gate only after Phase 2 stability is proven.
- **Don't bypass OPA with direct API calls** — If an agent has a valid API key, nothing technically prevents skipping the OPA check. The enforcement mechanism is the agent code itself (and eventually, an Envoy proxy that mandates OPA checks). Agent code reviews must verify OPA integration.
- **Don't put mutable state in OPA** — OPA's `data.json` should be static configuration, not a live database. Dynamic context (current time, action counts) comes from the `input` document, supplied by the caller.

---

## Cross-Reference

| Document | How OPA Integrates |
|---|---|
| **CLAUDE.md** | OPA fills the "OPA (policy)" slot in the Four-Layer Guardrails Model |
| **AUTOMATION-COMPOSABILITY.md** | OPA follows the 4-phase composable playbook pattern; `deploy-opa.yml` uses the standard task library |
| **CREDENTIAL-LIFECYCLE-PLAN.md** | OPA credentials (bearer tokens) follow Create→Verify→Retire rotation; OPA included in `audit-credentials.yml` scope; metadata written via `write-secret-metadata.yml` |
| **IMPLEMENTATION_PLAN.md** | OPA enables policy enforcement for Phase 1 integrations (Semaphore template governance, NocoDB CRUD scoping); does not block existing phases |
| **NETCLAW-INTEGRATION-PLAN.md** | OPA enforces CIDR scoping and ITSM gating for NetClaw network operations; NetClaw queries OPA before any scan or config push |
| **UNIFICATION-PLAN.md** | OPA is listed as P0 priority alongside Kyverno in the "Recommended Additions" section; Governance Agent (P1) wraps OPA + NeMo Guardrails |
