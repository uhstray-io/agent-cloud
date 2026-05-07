# Ansible Credential Redaction Plan

**Date:** 2026-05-06
**Status:** PLANNING
**Depends on:** Callback plugin implementation, testing in non-production Semaphore run

---

## Problem Statement

The codebase uses `no_log: true` on 41 Ansible tasks to prevent secrets from appearing in terminal or Semaphore output. This approach has critical drawbacks:

1. **Debugging is impossible** -- when a `no_log` task fails, the entire output is replaced with `CENSORED`, including the error message, HTTP status code, and response body
2. **Manual annotation is error-prone** -- every new task that touches secrets must remember to add `no_log: true`, and forgetting leaves secrets exposed
3. **No partial redaction** -- `no_log` is all-or-nothing; you cannot show the task name and error while hiding just the secret value
4. **No audit trail** -- there is no indication in logs that redaction occurred or what was redacted

## Solution: Custom Callback Plugin

A callback plugin named `redact_secrets` will intercept all Ansible output events, scan for values that match known sensitive variable patterns, and replace them with `***REDACTED***`. The actual values remain in Ansible memory for use by subsequent tasks.

## Design

### Plugin Location

```
platform/playbooks/callback_plugins/redact_secrets.py
```

### Plugin Class

```python
"""Ansible callback plugin that redacts sensitive values from output.

Replaces no_log: true across all playbooks. Values matching sensitive
variable name patterns are replaced with ***REDACTED*** in displayed
output while remaining available to subsequent tasks.
"""

from ansible.plugins.callback.default import CallbackDefault

DOCUMENTATION = """
    name: redact_secrets
    type: stdout
    short_description: Redact sensitive values from task output
    description:
        - Replaces values of known-sensitive variables with ***REDACTED***
        - Preserves task names, status, and error messages
        - Actual values remain in memory for task execution
    requirements:
        - Set as stdout_callback in ansible.cfg
"""

# Variable name patterns that contain secrets.
# Matching is case-insensitive and uses fnmatch-style globs.
SENSITIVE_PATTERNS = [
    # OpenBao authentication
    "_bao_auth",
    "_bao_existing",
    "_admin_auth",
    "client_token",
    "X-Vault-Token",

    # Resolved/generated secrets
    "_resolved",
    "_existing",

    # AppRole credentials
    "_bao_role_id",
    "_bao_secret_id",
    "_new_role_id",
    "_new_secret_id",
    "role_id",
    "secret_id",

    # Diode credentials
    "_orb_client_secret",
    "_new_creds",
    "_orb_approle_resp",

    # SSH keys
    "_key_content",
    "_ssh_private_key",
    "_ssh_public_key",

    # Generic patterns (fnmatch)
    "*_password",
    "*_secret*",
    "*_token",
    "*_key",
    "*_credential*",
]

# Value patterns that indicate a secret even if the variable name
# does not match (e.g., inline vault tokens in URIs).
VALUE_PATTERNS = [
    r"hvs\.[A-Za-z0-9]{20,}",          # Vault/OpenBao tokens
    r"s\.[A-Za-z0-9]{20,}",            # Legacy vault tokens
    r"ghp_[A-Za-z0-9]{36,}",           # GitHub PATs
    r"BEGIN .* PRIVATE KEY",            # PEM key material
]


class CallbackModule(CallbackDefault):
    """Stdout callback that redacts sensitive variable values."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "redact_secrets"

    def __init__(self):
        super().__init__()

    def _redact_dict(self, data, depth=0):
        """Recursively redact sensitive values in a dict."""
        if depth > 10:  # prevent infinite recursion
            return data
        if isinstance(data, dict):
            redacted = {}
            for key, value in data.items():
                if self._is_sensitive_key(key):
                    redacted[key] = "***REDACTED***"
                elif isinstance(value, (dict, list)):
                    redacted[key] = self._redact_dict(value, depth + 1)
                elif isinstance(value, str) and self._is_sensitive_value(value):
                    redacted[key] = "***REDACTED***"
                else:
                    redacted[key] = value
            return redacted
        elif isinstance(data, list):
            return [self._redact_dict(item, depth + 1) for item in data]
        return data

    def _is_sensitive_key(self, key):
        """Check if a key matches sensitive patterns."""
        import fnmatch
        key_lower = str(key).lower()
        for pattern in SENSITIVE_PATTERNS:
            if fnmatch.fnmatch(key_lower, pattern.lower()):
                return True
        return False

    def _is_sensitive_value(self, value):
        """Check if a value matches known secret formats."""
        import re
        for pattern in VALUE_PATTERNS:
            if re.search(pattern, str(value)):
                return True
        return False

    def _redact_result(self, result):
        """Create a redacted copy of a task result for display."""
        result_copy = result._result.copy()
        return self._redact_dict(result_copy)

    def v2_runner_on_ok(self, result):
        """Intercept successful task results and redact secrets."""
        result._result = self._redact_result(result)
        super().v2_runner_on_ok(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        """Intercept failed task results and redact secrets."""
        result._result = self._redact_result(result)
        super().v2_runner_on_failed(result, ignore_errors)

    def v2_runner_on_skipped(self, result):
        """Pass through skipped tasks unchanged."""
        super().v2_runner_on_skipped(result)
```

### Configuration

Add to `ansible.cfg` (or create one at the playbook root):

```ini
[defaults]
# Use the redact_secrets callback plugin for all output
stdout_callback = redact_secrets
callback_plugins = ./callback_plugins

# Ensure callbacks are whitelisted
callback_whitelist = redact_secrets
```

For Semaphore, the `ansible.cfg` must be present in the playbook directory that Semaphore clones. Since Semaphore clones agent-cloud and runs playbooks from `platform/playbooks/`, the config file goes at:

```
platform/playbooks/ansible.cfg
```

### Variable Name Patterns to Redact

These patterns are derived from the 41 `no_log: true` instances currently in the codebase:

| Source File | Variables Registered/Set | Pattern |
|-------------|------------------------|---------|
| `tasks/manage-secrets.yml` | `_bao_auth`, `_bao_existing`, `_resolved` | Exact match |
| `tasks/manage-approle.yml` | `_admin_auth`, `_new_role_id`, `_new_secret_id`, `_secret_id_resp` | Exact match |
| `tasks/manage-diode-credentials.yml` | `_bao_auth`, `_orb_client_secret`, `_new_creds` | Exact match |
| `tasks/seed-discovery-credential.yml` | `_bao_auth`, credential fields | Exact match |
| `tasks/update-vault-field.yml` | `_bao_auth`, field values | Exact match |
| `tasks/apply-openbao-policy.yml` | `_bao_auth` | Exact match |
| `deploy-orb-agent.yml` | `_bao_auth`, `_orb_approle_resp` | Exact match |
| `deploy-netbox.yml` | `_bao_auth` (via include_tasks) | Exact match |
| `check-secrets.yml` | `_bao_auth`, `_bao_data` | Exact match |
| `validate-secrets.yml` | `_bao_auth`, `_bao_data` | Exact match |
| `sync-secrets-to-openbao.yml` | `_bao_auth`, secret data | Exact match |
| `distribute-ssh-keys.yml` | `_key_file` content | Exact match |
| `harden-ssh.yml` | `_key_file` content | Exact match |
| `update-proxmox-token.yml` | `_bao_auth` | Exact match |
| `apply-openbao-policies.yml` | `_bao_auth` | Exact match |
| `provision-template.yml` | SSH key content | Exact match |
| `seed-discovery-credentials.yml` | `_bao_auth` | Exact match |
| All URI tasks | `body.role_id`, `body.secret_id`, `headers.X-Vault-Token` | Nested key match |

### What Gets Redacted vs. What Stays Visible

**Visible (preserved in output):**
- Task name (e.g., "Authenticate to OpenBao")
- Task status (ok, failed, changed, skipped)
- Error messages (e.g., "Status code was 403, expected 200")
- Non-sensitive fields (e.g., `status_code`, `url`, `method`)
- Loop labels (already designed to show safe values)

**Redacted (replaced with `***REDACTED***`):**
- `client_token` values in auth responses
- `role_id` and `secret_id` in request bodies
- `X-Vault-Token` header values
- Full `_resolved` dictionary contents
- SSH key file contents
- Any string matching vault token patterns (`hvs.xxx`, `s.xxx`)

### Example Output Comparison

**Before (with `no_log: true`):**
```
TASK [Authenticate to OpenBao] ****
ok: [netbox-vm] => {"censored": "the output has been hidden due to the fact that 'no_log: true' was set"}
```

**After (with callback plugin):**
```
TASK [Authenticate to OpenBao] ****
ok: [netbox-vm] => {
    "json": {
        "auth": {
            "client_token": "***REDACTED***",
            "policies": ["semaphore-read", "default"],
            "token_type": "service"
        }
    },
    "status": 200,
    "url": "https://openbao.example.com:8200/v1/auth/approle/login"
}
```

**Before (with `no_log: true`, task fails):**
```
TASK [Store secrets in OpenBao] ****
fatal: [netbox-vm]: FAILED! => {"censored": "the output has been hidden due to the fact that 'no_log: true' was set"}
```

**After (with callback plugin, task fails):**
```
TASK [Store secrets in OpenBao] ****
fatal: [netbox-vm]: FAILED! => {
    "msg": "Status code was 403, not one of [200]",
    "status": 403,
    "url": "https://openbao.example.com:8200/v1/secret/data/services/netbox",
    "json": {
        "errors": ["permission denied"]
    }
}
```

The error message, status code, and URL are all visible for debugging. Only the token in the request header was redacted.

## Testing Approach

### Unit Tests

Create `platform/playbooks/callback_plugins/tests/test_redact_secrets.py`:

```python
"""Tests for the redact_secrets callback plugin."""

import pytest
from callback_plugins.redact_secrets import CallbackModule

@pytest.fixture
def plugin():
    return CallbackModule()

class TestRedactDict:
    def test_redacts_exact_match_key(self, plugin):
        data = {"client_token": "hvs.abc123", "status": 200}
        result = plugin._redact_dict(data)
        assert result["client_token"] == "***REDACTED***"
        assert result["status"] == 200

    def test_redacts_glob_pattern_key(self, plugin):
        data = {"db_password": "secret123", "db_host": "localhost"}
        result = plugin._redact_dict(data)
        assert result["db_password"] == "***REDACTED***"
        assert result["db_host"] == "localhost"

    def test_redacts_nested_dict(self, plugin):
        data = {"auth": {"client_token": "hvs.abc", "policies": ["default"]}}
        result = plugin._redact_dict(data)
        assert result["auth"]["client_token"] == "***REDACTED***"
        assert result["auth"]["policies"] == ["default"]

    def test_redacts_value_pattern(self, plugin):
        data = {"response": "token is hvs.ABCDEFghijklmnopqrst"}
        result = plugin._redact_dict(data)
        assert result["response"] == "***REDACTED***"

    def test_preserves_non_sensitive(self, plugin):
        data = {"status": 200, "url": "https://example.com", "method": "POST"}
        result = plugin._redact_dict(data)
        assert result == data

    def test_handles_empty_dict(self, plugin):
        assert plugin._redact_dict({}) == {}

    def test_handles_list_of_dicts(self, plugin):
        data = [{"secret_id": "abc"}, {"name": "test"}]
        result = plugin._redact_dict(data)
        assert result[0]["secret_id"] == "***REDACTED***"
        assert result[1]["name"] == "test"

    def test_depth_limit(self, plugin):
        # Build deeply nested dict
        data = {"a": "value"}
        current = data
        for _ in range(15):
            current["nested"] = {"a": "value"}
            current = current["nested"]
        current["secret_id"] = "should-not-crash"
        result = plugin._redact_dict(data)
        assert result is not None

class TestIsSensitiveKey:
    @pytest.mark.parametrize("key", [
        "_bao_auth", "_bao_existing", "_admin_auth", "_resolved",
        "_new_role_id", "_new_secret_id", "client_token",
        "X-Vault-Token", "db_password", "redis_password",
        "secret_key", "_orb_client_secret",
    ])
    def test_sensitive_keys(self, plugin, key):
        assert plugin._is_sensitive_key(key)

    @pytest.mark.parametrize("key", [
        "status", "url", "method", "msg", "policies",
        "token_type", "changed", "failed",
    ])
    def test_non_sensitive_keys(self, plugin, key):
        assert not plugin._is_sensitive_key(key)
```

### Integration Test

Run a test playbook that exercises vault interactions with verbose output and verify:

1. Task names are visible
2. Error messages are visible when tasks fail
3. Secret values are replaced with `***REDACTED***`
4. Subsequent tasks can still use the actual secret values

```yaml
# test-redaction.yml -- manual integration test
---
- name: Test credential redaction
  hosts: localhost
  gather_facts: false
  vars:
    test_password: "SuperSecret123"
    test_token: "hvs.TestTokenValue12345678901234"
  tasks:
    - name: "Set a fact with sensitive data"
      ansible.builtin.set_fact:
        _bao_auth:
          json:
            auth:
              client_token: "{{ test_token }}"
              policies: ["default"]

    - name: "Display the auth result (should be redacted)"
      ansible.builtin.debug:
        var: _bao_auth

    - name: "Use the token in a subsequent task (should work)"
      ansible.builtin.debug:
        msg: "Token length: {{ _bao_auth.json.auth.client_token | length }}"
```

## Migration Plan

### Phase 1: Deploy Plugin (non-breaking)

1. Create `platform/playbooks/callback_plugins/redact_secrets.py`
2. Create `platform/playbooks/ansible.cfg` with `stdout_callback = redact_secrets`
3. Run unit tests
4. Test in a single Semaphore run (e.g., `check-secrets.yml` which is read-only)
5. Verify output shows redacted values, not `CENSORED`

### Phase 2: Remove no_log (per-file)

Remove `no_log: true` from each file, one at a time, testing after each removal:

| Order | File | Instances | Risk |
|-------|------|-----------|------|
| 1 | `check-secrets.yml` | 2 | Low (read-only) |
| 2 | `validate-secrets.yml` | 5 | Low (read-only) |
| 3 | `tasks/manage-secrets.yml` | 5 | Medium (write ops) |
| 4 | `tasks/manage-approle.yml` | 4 | Medium (write ops) |
| 5 | `tasks/manage-diode-credentials.yml` | 4 | Medium (write ops) |
| 6 | `tasks/seed-discovery-credential.yml` | 4 | Medium (write ops) |
| 7 | `tasks/update-vault-field.yml` | 2 | Low |
| 8 | `tasks/apply-openbao-policy.yml` | 1 | Low |
| 9 | `deploy-orb-agent.yml` | 3 | Medium |
| 10 | `deploy-netbox.yml` | 1 | Medium |
| 11 | `distribute-ssh-keys.yml` | 1 | Low |
| 12 | `harden-ssh.yml` | 1 | Low |
| 13 | `sync-secrets-to-openbao.yml` | 4 | Medium |
| 14 | `update-proxmox-token.yml` | 1 | Low |
| 15 | `apply-openbao-policies.yml` | 1 | Low |
| 16 | `seed-discovery-credentials.yml` | 1 | Low |
| 17 | `provision-template.yml` | 1 | Low |

After each removal, run the affected playbook in Semaphore and verify the output is properly redacted.

### Phase 3: Enforce Ban

Add a CI lint step that fails if any `no_log: true` is found:

```yaml
- name: Enforce no_log ban
  run: |
    FOUND=$(grep -rn 'no_log:\s*true' platform/playbooks/ --include='*.yml' || true)
    if [ -n "$FOUND" ]; then
      echo "ERROR: no_log: true is banned. Use the redact_secrets callback plugin."
      echo "$FOUND"
      exit 1
    fi
```

Update `platform/playbooks/README.md` to remove the `no_log: true` convention and document the callback plugin.

### Phase 4: Ongoing Maintenance

- New sensitive variable patterns are added to `SENSITIVE_PATTERNS` in the plugin
- Unit tests are updated to cover new patterns
- Plugin is version-controlled alongside playbooks
- No task in `platform/playbooks/` may use `no_log: true`

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Plugin fails to load | Ansible falls back to default callback; secrets may appear in output. Add health check in CI. |
| Pattern misses a new secret variable | Pattern list uses globs (`*_password`, `*_secret*`); most new secrets match existing patterns. Review new tasks for novel patterns. |
| False positive redaction | Non-sensitive values like `token_type` or `key_name` could match glob patterns. Use exact matches for ambiguous names. Test thoroughly. |
| Performance overhead | Redaction adds a dict walk per task result. For typical playbook runs (< 100 tasks), overhead is negligible. |
| Semaphore callback compatibility | Semaphore uses Ansible's default stdout callback. Replacing it with `redact_secrets` (which extends `CallbackDefault`) maintains compatibility. |

## Files to Create

| File | Purpose |
|------|---------|
| `platform/playbooks/callback_plugins/redact_secrets.py` | The callback plugin |
| `platform/playbooks/callback_plugins/__init__.py` | Package marker |
| `platform/playbooks/callback_plugins/tests/test_redact_secrets.py` | Unit tests |
| `platform/playbooks/ansible.cfg` | Plugin configuration |

## Files to Modify

| File | Change |
|------|--------|
| All 17 playbook/task files listed above | Remove `no_log: true` |
| `platform/playbooks/README.md` | Update credential handling docs |
| `.github/workflows/lint-and-test.yml` | Add `no_log` ban enforcement step |
| `CLAUDE.md` | Document callback plugin convention |

## Revision History

| Date | Change |
|------|--------|
| 2026-05-06 | Initial plan -- plugin design, migration order, testing approach |
