# n8n-write: allows n8n VM to write its own credentials to OpenBao
# Scoped to the n8n service path only

path "secret/data/services/n8n" {
  capabilities = ["create", "update", "read", "patch"]
}

path "secret/metadata/services/n8n" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
