# inference-hunyuan3d: read access to Hunyuan3D sidecar's own secrets.
#
# STATUS: RESERVED — not consumed at runtime in v1.
#
# inference-hunyuan3d uses .env-at-boot. The FastAPI wrapper reads model
# path, MinIO creds, and generation defaults from .env templated by Ansible
# from OpenBao. No AppRole is provisioned for this service yet.
#
# See platform/services/openbao/deployment/config/policies/uhhcraft.hcl for
# the rationale (this policy mirrors that pattern).

path "secret/data/services/inference-hunyuan3d" {
  capabilities = ["read"]
}

path "secret/metadata/services/inference-hunyuan3d" {
  capabilities = ["read"]
}

path "secret/data/services/inference-hunyuan3d/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/inference-hunyuan3d/*" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
