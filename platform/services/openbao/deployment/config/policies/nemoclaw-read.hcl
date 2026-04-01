# nemoclaw-read: read-only access to static API tokens and service credentials
# Assigned to the NemoClaw AppRole for all normal operations

path "secret/data/services/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/*" {
  capabilities = ["list", "read"]
}

# Allow token self-renewal
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# Allow AppRole secret-id generation so NemoClaw can re-authenticate after token expiry.
# create/update on this path is required by AppRole auth; it does not grant broader write access.
path "auth/approle/role/nemoclaw/secret-id" {
  capabilities = ["create", "update"]
}
