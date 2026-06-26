# AppRole TTL Enforcement Plan

**Date:** 2026-05-06
**Status:** PROPOSED
**Context:** The `manage-approle.yml` task hardcodes `secret_id_ttl: 0` and `token_num_uses: 0`, meaning AppRole credentials never expire and tokens have unlimited uses. This contradicts the 90-day TTL requirement established in CREDENTIAL-LIFECYCLE-PLAN.md and the secure defaults documented in AUTOMATION-COMPOSABILITY.md.

**References:**
- [CREDENTIAL-LIFECYCLE-PLAN.md](../architecture/CREDENTIAL-LIFECYCLE-PLAN.md) -- Defines 90-day secret_id TTL, 25 token_num_uses
- [AUTOMATION-COMPOSABILITY.md](../architecture/AUTOMATION-COMPOSABILITY.md) -- Documents intended defaults and security rationale
- [ACCESS-BOUNDARIES.md](../architecture/ACCESS-BOUNDARIES.md) -- AppRole scope boundaries and blast radius

---

## Problem

### Current Code (manage-approle.yml, lines 59-61)

```yaml
body:
  token_policies:
    - "{{ _approle_name }}"
  token_ttl: "{{ _approle_token_ttl | default('30m') }}"
  token_max_ttl: "{{ _approle_token_max_ttl | default('2h') }}"
  secret_id_ttl: 0
  token_num_uses: 0
```

### What the Documentation Says

CREDENTIAL-LIFECYCLE-PLAN.md specifies:

| Setting | Current Value | Required Value | Risk of Current |
|---------|--------------|----------------|-----------------|
| `secret_id_ttl` | `0` (never expires) | `2160h` (90 days) | A leaked secret_id grants indefinite access to the AppRole's scope |
| `token_num_uses` | `0` (unlimited) | `25` | An intercepted token can make unlimited API calls |

AUTOMATION-COMPOSABILITY.md section "AppRole Management (Composable)" documents these as the intended defaults:
- `_approle_secret_id_ttl` default: `"2160h"` (90 days)
- `_approle_token_num_uses` default: `25`

But the actual task body ignores these variables and hardcodes `0` for both.

### Impact

- **orb-agent AppRole** -- secret_id never expires; a compromised agent credential grants permanent read access to NetBox Diode and SNMP secrets
- **Any future AppRole** created via `manage-approle.yml` -- inherits the same no-expiry behavior
- **Semaphore orchestrator** -- intentionally unlimited (documented exception), but this should be explicit, not a side effect of the hardcoded defaults

---

## Required Changes

### Step 1: Update manage-approle.yml to Use Variables with Secure Defaults

Replace the hardcoded values with variable references that default to the documented secure values:

```yaml
# Before (current)
secret_id_ttl: 0
token_num_uses: 0

# After (proposed)
secret_id_ttl: "{{ _approle_secret_id_ttl | default('2160h') }}"
token_num_uses: "{{ _approle_token_num_uses | default(25) }}"
```

This is a two-line change. Existing callers that do not set `_approle_secret_id_ttl` or `_approle_token_num_uses` will automatically get the secure defaults (90-day TTL, 25 uses).

### Step 2: Explicitly Override for Semaphore Orchestrator

The Semaphore orchestrator AppRole is the documented exception. Its callers must explicitly pass the unlimited values:

```yaml
# In deploy-openbao.yml or wherever Semaphore's AppRole is provisioned
- include_tasks: tasks/manage-approle.yml
  vars:
    _approle_name: "semaphore"
    _approle_secret_id_ttl: "0"    # Orchestrator exception: unlimited
    _approle_token_num_uses: 0      # Orchestrator exception: unlimited
    _approle_policy: "{{ semaphore_policy }}"
```

### Step 3: Add secret_id Rotation Playbook

With a 90-day TTL, secret_ids will expire. A rotation playbook must be created and scheduled in Semaphore to run before expiry (e.g., every 60 days):

```yaml
# rotate-approle-secrets.yml (new playbook)
# For each AppRole with bounded TTL:
#   1. Generate new secret_id via manage-approle.yml
#   2. Update the stored credentials in OpenBao
#   3. Verify the new secret_id authenticates successfully
#   4. Old secret_id expires naturally via TTL (no manual revocation needed)
```

### Step 4: Add Semaphore Template

Add a new template to `platform/semaphore/templates.yml`:

```yaml
- name: Rotate AppRole Secrets
  playbook: platform/playbooks/rotate-approle-secrets.yml
  schedule: "0 3 1 */2 *"  # 3 AM on the 1st of every other month (60-day cycle)
```

---

## Testing Approach

### Unit Testing (Before Merge)

1. **Verify variable substitution** -- Run `ansible-playbook --check` with debug to confirm the `default()` filter produces `2160h` and `25` when no overrides are passed
2. **Verify Semaphore override** -- Confirm that passing `_approle_secret_id_ttl: "0"` produces `0` in the API call body
3. **Lint** -- Ensure `ansible-lint` passes on the modified task file

### Integration Testing (After Merge, Before Production)

1. **Create a test AppRole** with the new defaults:
   ```
   Semaphore -> manage-approle.yml with _approle_name: "test-ttl-enforcement"
   ```
2. **Verify the AppRole configuration** via OpenBao API:
   ```
   GET /v1/auth/approle/role/test-ttl-enforcement
   -> secret_id_ttl should be 7776000 (2160h in seconds)
   -> token_num_uses should be 25
   ```
3. **Verify secret_id expiry** (accelerated test with short TTL):
   ```
   Create AppRole with _approle_secret_id_ttl: "5m"
   Authenticate successfully with the secret_id
   Wait 6 minutes
   Attempt authentication -> should fail with "secret_id expired"
   ```
4. **Verify token_num_uses** enforcement:
   ```
   Create AppRole with _approle_token_num_uses: 3
   Authenticate and make 3 API calls -> succeed
   Make a 4th API call -> should fail with "token has been used too many times"
   ```
5. **Clean up test AppRole** after testing

### Regression Testing

1. **Deploy NetBox** via Semaphore after the change -- verify full 5-phase deploy succeeds
2. **Deploy Orb Agent** -- verify agent starts and authenticates to OpenBao at runtime
3. **Run validate-all.yml** -- confirm all services remain healthy

---

## Rollout Strategy

### Phase 1: Update Code (Low Risk)

1. Modify `manage-approle.yml` (the two-line change)
2. Update any callers that need the Semaphore exception
3. PR, CI checks, CodeRabbit review
4. Merge to main

**Risk:** None. Existing AppRoles are not retroactively affected. The change only applies when `manage-approle.yml` is next invoked for a given AppRole.

### Phase 2: Apply to New AppRoles First

1. Create any new per-service AppRoles (e.g., `netbox-deploy`, `nocodb-deploy`) with the new defaults
2. These AppRoles will have 90-day TTL from creation
3. Validate that deployments work correctly with bounded TTLs

### Phase 3: Rotate Existing AppRoles

Apply the new settings to existing AppRoles one at a time, in order of increasing blast radius:

| Order | AppRole | Action | Verification |
|-------|---------|--------|-------------|
| 1 | `orb-agent` | Re-run `manage-approle.yml` (gets new defaults) | Verify orb-agent authenticates, discovery works |
| 2 | Future per-service AppRoles | Created with defaults | Verify deploy playbook succeeds |
| 3 | `semaphore` | Re-run with explicit `_approle_secret_id_ttl: "0"` override | Verify all Semaphore templates still execute |

**Note:** The Semaphore orchestrator AppRole is the LAST to be touched, and it retains `secret_id_ttl: 0` intentionally. The rotation for Semaphore is a re-application of the explicit override to confirm the code path works, not a TTL change.

### Phase 4: Schedule Rotation

1. Create `rotate-approle-secrets.yml` playbook
2. Add Semaphore template with 60-day cron schedule
3. Run manually once to verify
4. Enable scheduled execution

---

## AppRoles Affected

| AppRole | Current TTL | Target TTL | Exception? | Notes |
|---------|------------|------------|------------|-------|
| `semaphore` | 0 (unlimited) | 0 (unlimited) | Yes -- orchestrator | Broad scope requires unlimited; compensated by Semaphore runner isolation |
| `orb-agent` | 0 (unlimited) | 2160h (90 days) | No | Runtime agent; should have bounded lifetime |
| Future per-service | N/A | 2160h (90 days) | No | Created with secure defaults from day one |

---

## Success Criteria

- [ ] `manage-approle.yml` uses `default('2160h')` for `secret_id_ttl` and `default(25)` for `token_num_uses`
- [ ] Semaphore orchestrator AppRole explicitly overrides to `0` (documented exception)
- [ ] New AppRoles created via the task get 90-day TTL without callers needing to specify it
- [ ] orb-agent AppRole re-provisioned with 90-day TTL and continues to function
- [ ] Rotation playbook exists and is scheduled in Semaphore
- [ ] All existing CI tests pass after the change
