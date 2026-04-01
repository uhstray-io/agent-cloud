# nocodb-write: allows NocoDB VM to write its own credentials to OpenBao
# Scoped to the NocoDB service path only

path "secret/data/services/nocodb" {
  capabilities = ["create", "update", "read", "patch"]
}

path "secret/metadata/services/nocodb" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
