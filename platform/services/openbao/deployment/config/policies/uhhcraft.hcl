# uhhcraft: read access to UhhCraft's own secrets.
#
# STATUS: RESERVED — not consumed at runtime in v1.
#
# UhhCraft uses .env-at-boot: Ansible's tasks/manage-secrets.yml fetches from
# OpenBao using Semaphore's AppRole, templates templates/env.j2 into .env, and
# the Go app reads .env on startup. There is no AppRole for UhhCraft, and the
# Go binary never calls OpenBao at runtime.
#
# This policy exists for two reasons:
#   1. Future-proofing: if/when UhhCraft needs runtime secret rotation, the
#      scope is already documented and can be bound to a new AppRole.
#   2. Documentation: the .hcl serves as the canonical record of which paths
#      UhhCraft's secrets occupy.
#
# To activate: provision an AppRole via tasks/manage-approle.yml with this
# policy attached. Until then, no token uses this policy.

path "secret/data/services/uhhcraft" {
  capabilities = ["read"]
}

path "secret/metadata/services/uhhcraft" {
  capabilities = ["read"]
}

# Sub-paths under secret/data/services/uhhcraft/* — reserved for future
# splits (e.g., secret/data/services/uhhcraft/stripe, .../resend) if/when
# we subdivide for finer-grained rotation.
path "secret/data/services/uhhcraft/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/uhhcraft/*" {
  capabilities = ["read"]
}

# Token self-renewal — needed if the AppRole-issued token approaches its TTL.
path "auth/token/renew-self" {
  capabilities = ["update"]
}
