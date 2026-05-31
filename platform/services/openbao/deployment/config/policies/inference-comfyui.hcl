# inference-comfyui: read access to ComfyUI sidecar's own secrets.
#
# STATUS: RESERVED — not consumed at runtime in v1.
#
# inference-comfyui uses .env-at-boot. The FastAPI wrapper reads its config
# (COMFYUI_URL, MinIO creds, generation defaults) from .env templated by
# Ansible from OpenBao. No AppRole is provisioned for this service yet.
#
# See platform/services/openbao/deployment/config/policies/uhhcraft.hcl for
# the rationale (this policy mirrors that pattern).

path "secret/data/services/inference-comfyui" {
  capabilities = ["read"]
}

path "secret/metadata/services/inference-comfyui" {
  capabilities = ["read"]
}

path "secret/data/services/inference-comfyui/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/inference-comfyui/*" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
