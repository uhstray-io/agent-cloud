# Semaphore orchestrator policy — full platform management
# Used by Semaphore playbooks to:
#   - Fetch credentials for SSH, Proxmox API, service tokens (read)
#   - Store deploy-generated secrets back to OpenBao (write)
#   - Provision AppRoles for services (orb-agent, netclaw, etc.)
# Semaphore is the deployment orchestrator — it manages the full lifecycle.

# Service secrets (read + write for all services)
path "secret/data/services/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
}

path "secret/metadata/services/*" {
  capabilities = ["read", "list"]
}

# Fixed o11y isolated-restore coordination record. This is the only non-service
# path needed by the cooperating mutator guard and coordination workflow.
path "secret/data/coordination/o11y-restore-target" {
  capabilities = ["create", "read", "update"]
}

path "secret/metadata/coordination/o11y-restore-target" {
  capabilities = ["read"]
}

# AppRole management (create/update roles and policies for services)
path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "delete"]
}

path "auth/approle/role/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "auth/approle/role/+/role-id" {
  capabilities = ["read"]
}

path "auth/approle/role/+/secret-id" {
  capabilities = ["create", "update"]
}
