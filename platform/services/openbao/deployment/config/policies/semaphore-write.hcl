# semaphore-write: allows Semaphore VM to write its own credentials to OpenBao
# Scoped to the Semaphore service path only

path "secret/data/services/semaphore" {
  capabilities = ["create", "update", "read", "patch"]
}

path "secret/metadata/services/semaphore" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
