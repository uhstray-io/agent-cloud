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
