# variables.tf — non-secret inputs. The zone id is not a secret, but it is
# environment-specific, so it is supplied at plan/apply time via TF_VAR_zone_id
# (Semaphore environment, from site-config) rather than hardcoded in this public
# repo. Secrets (API token, R2 keys) are NEVER variables here — they come from
# provider/backend env (CLOUDFLARE_API_TOKEN, AWS_* ), see versions.tf.

variable "zone_id" {
  type        = string
  description = "Cloudflare zone id for uhstray.io (TF_VAR_zone_id; from site-config, Semaphore-injected)."

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.zone_id))
    error_message = "zone_id must be a 32-char hex Cloudflare zone id."
  }
}

variable "zone_name" {
  type        = string
  description = "Apex zone name."
  default     = "uhstray.io"
}
