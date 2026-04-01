# nemoclaw-rotate: access to dynamic database credentials
# Used when NemoClaw needs short-lived, auto-rotating DB creds

path "database/creds/nemoclaw-role" {
  capabilities = ["read"]
}

# Allow lease renewal for dynamic creds
path "sys/leases/renew" {
  capabilities = ["update"]
}

# Allow revocation of own leases
path "sys/leases/revoke" {
  capabilities = ["update"]
}
