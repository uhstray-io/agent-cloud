# Semaphore read policy — allows reading all service secrets
# Used by Semaphore playbooks to fetch PVE tokens, SSH keys, etc.
path "secret/data/services/*" {
  capabilities = ["read", "list"]
}

path "secret/metadata/services/*" {
  capabilities = ["read", "list"]
}
