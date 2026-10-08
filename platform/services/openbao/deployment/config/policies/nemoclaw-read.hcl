# nemoclaw-read: read-only access to static API tokens and service credentials
# Assigned to the NemoClaw AppRole for all normal operations

path "secret/data/services/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/*" {
  capabilities = ["list", "read"]
}

# Never the Semaphore API token. Whoever holds it can launch any template with any extra var,
# and a templated extra var runs code on the Semaphore runner, which holds the controller
# AppRole (plan 01, "Launch permission is runner access"). deny always wins, and these exact
# paths outrank the services/* globs above (openbao.org/docs/concepts/policies).
path "secret/data/services/semaphore" {
  capabilities = ["deny"]
}

path "secret/metadata/services/semaphore" {
  capabilities = ["deny"]
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
