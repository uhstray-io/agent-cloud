# Architecture Evaluation: Agent Cloud

**Date:** 2026-03-27
**Status:** Proposed
**Author:** Claude (Architecture Review)
**Deciders:** Joe (uhstray.io)

---

## 1. What This Repository Does

Agent Cloud is a dual-agent AI infrastructure platform that deploys two complementary agents across a homelab Proxmox cluster, backed by a shared service layer for secrets, data, automation, and orchestration.

**The two agents serve distinct roles:**

- **NemoClaw** (Engineer / Service Account) runs headless inside NVIDIA's OpenShell sandbox with policy-enforced security. It handles background automation: API integrations, health monitoring, CI/CD tasks, incident response, and scheduled data aggregation. NemoClaw requires Docker (not podman) and authenticates to all services via OpenBao AppRole credential injection.

- **Claude Cowork** (Architect / Researcher) runs on your personal device with browser automation, local file access, and GUI capabilities. It handles research tasks, architecture decisions, document generation, visual verification, and anything requiring interactive context.

**The shared service layer consists of:**

| Service | Purpose | Role in the System |
|---|---|---|
| **OpenBao** | Secrets management | Central credential store. KV v2 for static API tokens, database engine for dynamic credentials, AppRole auth for NemoClaw. All secrets flow through here; nothing is hardcoded. |
| **NocoDB** | Shared data layer | Structured data store accessible by both agents. In Phase 3, becomes the shared task queue for cross-agent coordination. |
| **n8n** | Workflow automation | Event-driven automation with queue-mode workers. Triggers workflows on webhooks, schedules, or API calls. |
| **Semaphore** | Ansible/task automation | Runs Ansible playbooks against the homelab inventory. Manages infrastructure state across the Proxmox cluster. |

**The production topology spans five VMs:**

| VM IP | Services |
|---|---|
| `{{ nocodb_host }}` | NocoDB + OpenBao |
| `{{ n8n_host }}` | n8n |
| `{{ semaphore_host }}` | Semaphore |
| `{{ nemoclaw_host }}` | NemoClaw |
| `{{ netbox_host }}` | NetBox (infrastructure CMDB, not yet integrated) |

All local dev ports are bound to `127.0.0.1`. The NemoClaw network policy (`agent-cloud.yaml`) whitelists production IPs plus `host.docker.internal` fallbacks for local development.

---

## 2. Phase 0 Close-Out: Validation Guide

Phase 0 built the foundation: all containers running, OpenBao initialized with policies and AppRole, placeholder secrets seeded, NemoClaw config staged. Current state is **13 PASS / 0 FAIL / 8 WARN**.

### 2.1 Remaining Items to Close Phase 0

Complete these in order. Each step depends on the previous.

#### Step 1: Identify the Proxmox Host IP

Look at your Proxmox cluster to determine which server hosts the API. Common candidates from your inventory are the physical servers (see site-config inventory). The Proxmox web UI runs on port 8006.

Once identified, update two locations:

```bash
# 1. Update the NemoClaw network policy
# In nemoclaw/agent-cloud.yaml, replace PROXMOX_HOST_PLACEHOLDER with the real IP
sed -i 's/PROXMOX_HOST_PLACEHOLDER/{{ proxmox_host }}/' nemoclaw/agent-cloud.yaml

# 2. Update the OpenBao secret URL
ROOT_TOKEN=$(jq -r '.root_token' secrets/init.json)
podman exec -e "BAO_TOKEN=$ROOT_TOKEN" workflow-openbao bao \
  kv patch secret/services/proxmox url="https://{{ proxmox_host }}:8006"
```

#### Step 2: Create Service API Tokens

Each token must be created manually in the respective service UI, then added to `secrets/service-credentials.env`.

**NocoDB API Token:**
1. Open `http://localhost:8181`
2. Sign up / log in (first run creates admin account)
3. Navigate to Team & Auth > API Tokens
4. Create a token named "nemoclaw"
5. Copy the token value

**n8n API Key:**
1. Open `http://localhost:5678`
2. Complete the initial setup wizard (first run)
3. Go to Settings > API > Create API Key
4. Copy the key

**Semaphore API Token:**
1. Open `http://localhost:3100`
2. Log in with admin credentials from `config/semaphore.env` (look for `SEMAPHORE_ADMIN_PASSWORD`)
3. Go to User Settings (top-right) > API Tokens
4. Create a new token, copy it

**GitHub Fine-Grained PAT:**
1. Go to GitHub > Settings > Developer Settings > Fine-grained Personal Access Tokens
2. Create a token scoped to your target repos
3. Permissions needed: Repository (read), Issues (read/write)
4. Copy the token

**Discord Bot Token:**
1. Go to Discord Developer Portal > Applications > New Application
2. Go to Bot > Reset Token > Copy
3. Under OAuth2 > URL Generator, select `bot` scope with `Send Messages` + `Read Message History` permissions
4. Use the generated URL to invite the bot to your server

**Proxmox API Token:**
1. Log in to Proxmox web UI at `https://<PROXMOX_IP>:8006`
2. Go to Datacenter > Permissions > API Tokens > Add
3. Create a read-only token (uncheck "Privilege Separation" only if needed)
4. Note both the Token ID (e.g., `nemoclaw@pve!nemoclaw`) and the secret value

#### Step 3: Store Credentials in OpenBao

```bash
# First run creates the template file
./scripts/setup-secrets.sh

# Edit the template with real values
nano secrets/service-credentials.env
# Fill in: NOCODB_API_TOKEN, GITHUB_PAT, DISCORD_BOT_TOKEN,
#          PROXMOX_API_TOKEN, PROXMOX_TOKEN_ID, N8N_API_KEY, SEMAPHORE_API_TOKEN

# Second run stores them in OpenBao
./scripts/setup-secrets.sh
```

The script uses `kv patch` to preserve the URL fields already seeded by `setup-openbao.sh`.

#### Step 4: Validate

```bash
./scripts/validate.sh
```

**Expected result with all credentials:** All secret checks should now show PASS instead of WARN. The NocoDB API token live test should also pass.

Target: **15 PASS / 0 FAIL / 0 WARN** (or close to it; NemoClaw CLI check may still WARN if NemoClaw isn't deployed on this machine).

#### Step 5: Migrate NemoClaw Config

Once validation passes:

```bash
cp nemoclaw/sandboxes.json ../nemoclaw-deploy/config/sandboxes.json
cp nemoclaw/agent-cloud.yaml ../nemoclaw-deploy/config/presets/agent-cloud.yaml
```

Then redeploy NemoClaw:

```bash
cd ../nemoclaw-deploy
./deploy.sh --local
```

#### Step 6: NemoClaw Connectivity Smoke Test

After NemoClaw is redeployed with the `agent-cloud` preset, run one read operation per service:

| Service | Test Operation | What Confirms It Works |
|---|---|---|
| NocoDB | Fetch any row via API | HTTP 200 with JSON data |
| GitHub | List issues on a repo | HTTP 200 with issue array |
| Discord | Post a test message to a channel | Message appears in Discord |
| Proxmox | `GET /api2/json/nodes` | HTTP 200 with node status |
| n8n | `GET /api/v1/workflows` | HTTP 200 with workflow list |
| Semaphore | `GET /api/projects` | HTTP 200 with project list |

### 2.2 How to Validate Current Deployment Right Now

Even before completing all credentials, you can validate the infrastructure is healthy:

```bash
# 1. Check all containers are running
podman ps --format "{{.Names}}\t{{.Status}}" | grep workflow-

# 2. Unseal OpenBao if it was restarted
./scripts/unseal.sh

# 3. Verify OpenBao is unsealed and initialized
podman exec workflow-openbao bao status

# 4. Check service HTTP endpoints
curl -s http://localhost:8181/api/v1/health   # NocoDB
curl -s http://localhost:5678/healthz          # n8n
curl -s http://localhost:3100/api/ping         # Semaphore

# 5. Verify OpenBao policies and AppRole exist
ROOT_TOKEN=$(jq -r '.root_token' secrets/init.json)
podman exec -e "BAO_TOKEN=$ROOT_TOKEN" workflow-openbao bao policy list
podman exec -e "BAO_TOKEN=$ROOT_TOKEN" workflow-openbao bao read auth/approle/role/nemoclaw

# 6. List all secrets (check which are still placeholders)
for svc in nocodb github discord proxmox n8n semaphore; do
  echo "--- $svc ---"
  podman exec -e "BAO_TOKEN=$ROOT_TOKEN" workflow-openbao bao kv get secret/services/$svc
done

# 7. Run the full validation suite
./scripts/validate.sh
```

---

## 3. Phase 1: NemoClaw Task Automation — Expanded Plan

Phase 1 transitions NemoClaw from "deployed and connected" to "actively performing useful work." The implementation plan lists six integration targets. Below is an expanded breakdown with concrete steps, dependencies, and acceptance criteria.

### 3.1 Architecture Decisions for Phase 1

Before building individual integrations, establish the patterns everything will follow.

#### ADR-1: NemoClaw Task Dispatch Pattern

**Context:** NemoClaw needs to execute tasks against six different services. Tasks can be triggered by schedule (cron), webhook (n8n), manual invocation, or cross-agent request (Phase 3). We need a consistent pattern for how tasks are defined, dispatched, and logged.

**Decision:** Use NemoClaw's YAML policy preset (`agent-cloud`) as the security boundary, and n8n as the orchestration layer. Each integration gets:
- A NemoClaw "tool" (script callable from the sandbox)
- An n8n workflow that triggers the tool on schedule or webhook
- A NocoDB table row that logs the execution result

**Consequences:**
- n8n becomes the single scheduling and event routing layer (no cron inside NemoClaw)
- NocoDB becomes the audit trail for all automated actions
- Adding a new integration follows the same three-step pattern every time

#### ADR-2: Error Handling and Alerting

**Decision:** All NemoClaw tasks write results to a NocoDB "task_log" table. Failed tasks trigger a Discord notification via n8n webhook. Critical failures (OpenBao unreachable, NemoClaw sandbox crash) escalate to a dedicated Discord channel.

**Consequences:**
- Requires a `task_log` table in NocoDB (created in 3.2)
- Requires a Discord webhook URL for the alerting channel
- n8n monitors the task_log table for failures

### 3.2 NocoDB CRUD Operations

**Goal:** NemoClaw can create, read, update, and delete records in NocoDB tables via the REST API.

**Steps:**

1. **Design the NocoDB schema.** Create the foundational tables that Phase 1 needs:
   - `task_log` — columns: id, timestamp, service, operation, status (success/fail/warn), message, duration_ms, triggered_by
   - `monitored_resources` — columns: id, service, resource_type, resource_id, last_check, status, metadata (JSON)
   - `github_issues_cache` — columns: id, repo, issue_number, title, state, labels (JSON), updated_at, synced_at

2. **Build a NocoDB API client library** (Node.js or Python, depending on NemoClaw's preferred runtime). This library wraps:
   - `GET /api/v1/db/data/noco/{orgId}/{projectId}/{tableId}` — list/filter rows
   - `POST /api/v1/db/data/noco/{orgId}/{projectId}/{tableId}` — create row
   - `PATCH /api/v1/db/data/noco/{orgId}/{projectId}/{tableId}/{rowId}` — update row
   - `DELETE /api/v1/db/data/noco/{orgId}/{projectId}/{tableId}/{rowId}` — delete row
   - Auth header: `xc-token: <api_token>` (injected from OpenBao env var)

3. **Test from NemoClaw sandbox:** Create a row in `task_log`, read it back, update status, delete it. Confirm all four CRUD operations work through the network policy.

4. **Write a `log_task()` helper** that all subsequent integrations call to record their execution results in `task_log`.

**Acceptance Criteria:**
- NemoClaw can perform all four CRUD operations against NocoDB
- `task_log` table exists and receives entries
- API token is read from environment (injected by OpenBao), never from config

### 3.3 GitHub Issue Management

**Goal:** NemoClaw can list, create, update, and comment on GitHub issues.

**Steps:**

1. **Build a GitHub API client** using the REST API (not GraphQL, to keep it simple):
   - `GET /repos/{owner}/{repo}/issues` — list/filter issues
   - `POST /repos/{owner}/{repo}/issues` — create issue
   - `PATCH /repos/{owner}/{repo}/issues/{number}` — update issue (labels, state, assignees)
   - `POST /repos/{owner}/{repo}/issues/{number}/comments` — add comment
   - Auth header: `Authorization: Bearer <pat>` (from OpenBao)

2. **Create an issue sync workflow in n8n:**
   - Runs on schedule (e.g., every 15 minutes)
   - Fetches open issues from target repos
   - Upserts them into `github_issues_cache` in NocoDB
   - Logs the sync to `task_log`

3. **Create an issue creation tool for NemoClaw:**
   - Accepts title, body, labels, assignee
   - Creates the issue on GitHub
   - Logs to `task_log`
   - Posts a Discord notification for new issues created by automation

4. **Implement label-based triage:** Define a label convention (e.g., `priority:high`, `type:bug`, `agent:nemoclaw`) that NemoClaw can use to filter and prioritize.

**Acceptance Criteria:**
- NemoClaw can list issues from a target repo
- NemoClaw can create an issue with title, body, and labels
- Issue sync runs on schedule via n8n and populates NocoDB
- All operations logged to `task_log`

### 3.4 Discord Messaging

**Goal:** NemoClaw can post messages, read channel history, and respond to commands via Discord.

**Steps:**

1. **Build a Discord API client:**
   - `POST /channels/{id}/messages` — send message
   - `GET /channels/{id}/messages` — read recent messages
   - Auth header: `Authorization: Bot <bot_token>` (from OpenBao)

2. **Set up notification channels:**
   - `#agent-alerts` — automated alerts (task failures, health check issues)
   - `#agent-activity` — informational (task completions, sync summaries)
   - Store channel IDs in OpenBao at `secret/services/discord` (add fields: `alert_channel_id`, `activity_channel_id`)

3. **Create an n8n webhook for Discord notifications:**
   - Accepts: severity (info/warn/error), service, message
   - Posts to the appropriate channel based on severity
   - This becomes the universal alerting endpoint for all integrations

4. **Implement a daily digest:** An n8n scheduled workflow that summarizes the day's `task_log` entries and posts a summary to `#agent-activity`.

**Acceptance Criteria:**
- NemoClaw can post a message to a specific Discord channel
- Alert webhook works: `curl -X POST <n8n_webhook_url> -d '{"severity":"error","message":"test"}'`
- Daily digest runs and posts summary

### 3.5 Proxmox Resource Monitoring

**Goal:** NemoClaw monitors Proxmox cluster health and alerts on issues.

**Steps:**

1. **Build a Proxmox API client:**
   - `GET /api2/json/nodes` — list nodes with status
   - `GET /api2/json/nodes/{node}/status` — node resource usage
   - `GET /api2/json/nodes/{node}/qemu` — list VMs on node
   - `GET /api2/json/nodes/{node}/qemu/{vmid}/status/current` — VM status
   - Auth: `PVEAPIToken=<token_id>=<api_token>` header (both from OpenBao)

2. **Create a health check workflow in n8n:**
   - Runs every 5 minutes
   - Checks: node online status, CPU > 90%, memory > 90%, disk > 85%, VM status
   - Writes results to `monitored_resources` in NocoDB
   - Triggers Discord alert on threshold breach

3. **Implement VM inventory sync:**
   - Runs daily
   - Lists all VMs across all nodes
   - Updates `monitored_resources` with current state
   - Compares against `config/inventory.yml` and flags discrepancies

4. **Create a Proxmox status command** that NemoClaw can run on demand:
   - Returns a formatted summary: cluster health, per-node CPU/memory/disk, VM count and status
   - Useful for Claude Cowork to query via NocoDB in Phase 2

**Acceptance Criteria:**
- Health check runs every 5 minutes, results in NocoDB
- Discord alert fires when a threshold is breached
- On-demand status command returns formatted cluster summary

### 3.6 n8n Workflow Triggering

**Goal:** NemoClaw can list, trigger, and monitor n8n workflows.

**Steps:**

1. **Build an n8n API client:**
   - `GET /api/v1/workflows` — list workflows
   - `POST /api/v1/workflows/{id}/activate` — activate a workflow
   - `POST /api/v1/workflows/{id}/deactivate` — deactivate
   - `GET /api/v1/executions` — list recent executions with status
   - Auth: `X-N8N-API-KEY: <api_key>` header (from OpenBao)

2. **Create the core n8n workflows that Phase 1 needs:**
   - GitHub issue sync (see 3.3)
   - Proxmox health check (see 3.5)
   - Discord daily digest (see 3.4)
   - Task failure alerter (watches `task_log` for failures)

3. **Implement workflow health monitoring:**
   - n8n execution failures logged to `task_log`
   - Stale workflows (not executed in expected window) trigger alerts

**Acceptance Criteria:**
- NemoClaw can list active workflows
- NemoClaw can trigger a workflow execution
- Failed workflow executions appear in `task_log` and trigger Discord alerts

### 3.7 Semaphore Playbook Execution

**Goal:** NemoClaw can trigger Ansible playbooks via Semaphore for infrastructure tasks.

**Steps:**

1. **Build a Semaphore API client:**
   - `GET /api/projects` — list projects
   - `GET /api/project/{id}/templates` — list task templates
   - `POST /api/project/{id}/tasks` — run a task template
   - `GET /api/project/{id}/tasks/{task_id}` — check task status
   - Auth: `Authorization: Bearer <api_token>` (from OpenBao)

2. **Set up Semaphore project and templates:**
   - Create a "agent-cloud" project in Semaphore
   - Add the lab inventory (`config/inventory.yml`)
   - Create task templates for common operations:
     - `ping-all` — verify all hosts reachable
     - `update-packages` — apt upgrade on target hosts
     - `restart-service` — restart a specific container/service on a host
     - `collect-facts` — gather system facts for reporting

3. **Implement a scheduled infrastructure check:**
   - Weekly playbook run: `ping-all` + `collect-facts`
   - Results summarized and posted to Discord
   - Facts stored in NocoDB for trending

**Acceptance Criteria:**
- NemoClaw can list Semaphore projects and templates
- NemoClaw can trigger a playbook run and poll for completion
- Weekly infrastructure check runs and reports results

### 3.8 Scheduled Task Execution Framework

**Goal:** A consistent scheduling layer that ties all integrations together.

**Steps:**

1. **Define the schedule registry** in NocoDB:
   - `schedules` table: id, name, service, cron_expression, n8n_workflow_id, enabled, last_run, next_run, description

2. **Create a master scheduler workflow in n8n:**
   - Runs every minute
   - Reads active schedules from NocoDB
   - Triggers the corresponding n8n workflow when cron matches
   - Updates `last_run` and `next_run` in the schedules table

3. **Seed the initial schedules:**

| Schedule | Cron | Description |
|---|---|---|
| GitHub issue sync | `*/15 * * * *` | Sync open issues to NocoDB every 15 min |
| Proxmox health check | `*/5 * * * *` | Check cluster health every 5 min |
| Discord daily digest | `0 9 * * *` | Post yesterday's activity summary at 9 AM |
| Semaphore weekly check | `0 6 * * 1` | Ping all hosts + collect facts Monday 6 AM |
| n8n execution audit | `0 */6 * * *` | Check for stale/failed workflows every 6 hours |

**Acceptance Criteria:**
- Schedules table exists in NocoDB with seeded entries
- Master scheduler triggers workflows on cron match
- Adding a new schedule requires only a NocoDB row insertion

### 3.9 Phase 1 Implementation Order

Dependencies dictate the build order:

```
Step 1: NocoDB CRUD + task_log table          (foundation for everything)
  |
Step 2: Discord messaging + alert webhook      (needed for error reporting)
  |
Step 3: GitHub issue management                 (first real integration)
  |
Step 4: Proxmox monitoring                      (second integration, uses alerting)
  |
Step 5: n8n workflow triggering                 (builds the orchestration workflows)
  |
Step 6: Semaphore playbook execution            (final integration)
  |
Step 7: Scheduled task framework                (ties everything together)
```

Steps 3 and 4 can be parallelized once Steps 1 and 2 are complete.

### 3.10 Phase 1 Validation Checklist

When Phase 1 is complete, the following should all be true:

- [ ] NocoDB has `task_log`, `monitored_resources`, `github_issues_cache`, and `schedules` tables
- [ ] NemoClaw can CRUD all NocoDB tables through the network policy
- [ ] GitHub issues sync to NocoDB every 15 minutes
- [ ] NemoClaw can create GitHub issues programmatically
- [ ] Discord bot posts to `#agent-alerts` and `#agent-activity`
- [ ] Discord alert webhook is operational
- [ ] Proxmox health checks run every 5 minutes with threshold alerting
- [ ] n8n has active workflows for all scheduled tasks
- [ ] Semaphore has a "agent-cloud" project with task templates
- [ ] Weekly infrastructure check runs via Semaphore
- [ ] All task executions are logged to `task_log`
- [ ] Daily digest summarizes activity in Discord
- [ ] No secrets are hardcoded; all credentials flow through OpenBao

---

## 4. Trade-Off Analysis

### n8n as Orchestrator vs. NemoClaw-Native Scheduling

| Dimension | n8n Orchestration | NemoClaw Cron |
|---|---|---|
| Visibility | n8n UI shows execution history, logs, timing | Hidden inside sandbox, log files only |
| Error handling | Built-in retry, webhook on failure | Custom retry logic per script |
| Complexity | Additional service to maintain | Simpler, fewer moving parts |
| Flexibility | Visual workflow builder, branching, conditions | Code-only, full programming flexibility |

**Recommendation:** Use n8n. The visibility and built-in error handling justify the dependency, especially since n8n is already deployed.

### NocoDB as Task Queue vs. Redis/RabbitMQ

| Dimension | NocoDB | Dedicated Queue |
|---|---|---|
| Already deployed | Yes | No, additional service |
| Query flexibility | SQL-like filtering, views, API | Purpose-built queue semantics |
| Performance | Fine for low-volume (<100 tasks/min) | Needed for high-throughput |
| Human visibility | Browsable UI, exportable | Requires monitoring tools |

**Recommendation:** NocoDB is sufficient for Phase 1-3 task volumes. Revisit if task frequency exceeds 100/minute.

---

## 5. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OpenBao sealed after host reboot | High | All services lose credentials | Add `unseal.sh` to host startup; consider auto-unseal for production |
| NocoDB API token expiry/rotation | Medium | NemoClaw CRUD breaks | Monitor token health; OpenBao static role rotation in Phase 2 |
| n8n workflow failure goes unnoticed | Medium | Scheduled tasks silently stop | n8n execution audit workflow (every 6 hours) |
| Discord rate limiting | Low | Alert delivery delayed | Batch notifications; use webhook for critical only |
| Proxmox API token insufficient permissions | Medium | Monitoring returns partial data | Test all required endpoints before committing to read-only scope |

---

## 6. Security Considerations for Phase 1

Phase 0 deferred several security items. Before Phase 1 goes to production:

1. **TLS on OpenBao** — Currently `tls_disable=1`. All credential reads are plaintext on loopback. Acceptable for local dev; must enable TLS before production deployment to `{{ nocodb_host }}`.

2. **AppRole secret_id TTL** — Currently `0` (never expires). Set a TTL (e.g., 24h) and implement rotation via the `nemoclaw-rotate` policy.

3. **NemoClaw network policy audit** — The `agent-cloud.yaml` currently grants `access: full` to all endpoints. Consider restricting to specific HTTP methods and paths where the Proxmox/NocoDB/n8n APIs support it.

4. **Credential scoping** — Ensure each service token has minimum required permissions (read-only for Proxmox, scoped PAT for GitHub, etc.).

---

## 7. Future Phase Reference

### Phase 2: Claude Cowork Workflows
- Browser-based research pulling data from NocoDB
- Document generation from monitoring data and issue summaries
- Visual verification of deployments (screenshot + compare)
- Architecture decision records stored in the repo

### Phase 3: Cross-Agent Coordination
- NocoDB as shared task queue: Claude Cowork creates tasks, NemoClaw executes
- Handoff workflows: NemoClaw gathers data, writes to NocoDB, Claude Cowork generates reports
- Audit logging: all cross-agent actions recorded with provenance
- Human-in-the-loop: Discord notifications when agent actions need approval
