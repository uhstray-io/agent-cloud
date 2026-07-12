# versions.tf — OpenTofu root for Cloudflare (WAF rulesets + platform DNS).
#
# Cloudflare is managed here as code (OpenTofu, run via Semaphore) — the platform
# standard for all future Cloudflare changes (DNS, WAF, zone settings). Manual API
# pokes and the legacy manage-cloudflare-records.yml are superseded once Phase 2
# (DNS migration) lands; see plan/development/13-cloudflare-iac.md.
#
# STATE: Cloudflare R2 via the S3-compatible backend. State holds secrets, so it
# is NEVER committed — it lives in the R2 bucket. The bucket + endpoint (which
# embeds the account id) are supplied at `init` time via PARTIAL backend config
# (-backend-config=...), NOT hardcoded here, so no account id reaches this PUBLIC
# repo. R2 access/secret keys come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# env (the R2 S3 keys in site-config), injected by the Semaphore environment.
#
# PROVIDER AUTH: CLOUDFLARE_API_TOKEN env (a Zone:Read + Zone WAF/Rulesets:Edit +
# Zone DNS:Edit token), sourced from OpenBao secret/services/cloudflare:api_token
# and injected by Semaphore — never in a .tf/.tfvars file.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  # Partial config: bucket / endpoints.s3 (account-id-bearing) / key are provided
  # at init via -backend-config (see README.md + the Semaphore tofu template), so
  # nothing environment-specific is committed to this public repo.
  backend "s3" {
    region                      = "auto"
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
    use_path_style              = true
  }
}

provider "cloudflare" {
  # Reads CLOUDFLARE_API_TOKEN from the environment (Semaphore-injected from
  # OpenBao). No api_token argument here — keeps the token out of code + state diff.
}
