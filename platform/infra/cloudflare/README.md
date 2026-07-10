# Cloudflare — OpenTofu (platform IaC)

OpenTofu root that manages Cloudflare **as code**, run via Semaphore. This is the
platform standard for Cloudflare changes going forward (WAF rulesets, then DNS,
then zone settings) — superseding manual API pokes and, once Phase 2 lands, the
legacy `platform/playbooks/manage-cloudflare-records.yml`.

Plan + phasing: `plan/development/13-cloudflare-iac.md`.

## What manages what (ownership boundary)

OpenTofu manages ONLY resources declared here. Everything else in the zone is
left untouched (tofu never deletes what it doesn't declare). Deliberately **out
of scope** (external/vendor/ephemeral — do NOT import):

- Email: ProtonMail MX/SPF/DKIM/DMARC.
- Vendor verification/records: Microsoft enrollment, Shopify, Teams/Lync SRV,
  `_domainconnect`, Cloudflare Access (`cloud`).
- External hosts: `teleport`, `rustdesk`, `press` (WordPress.com), `redash`.
- `_acme-challenge.*` TXT — **created/deleted dynamically by Caddy's DNS-01**.
  tofu must never own these or it will fight Caddy.

In scope: the `http_request_firewall_custom` ruleset, and (Phase 2) the platform
service A-records that point at the Caddy origin.

## Secrets / config (never committed)

| What | Source | How it reaches tofu |
|------|--------|---------------------|
| CF API token (Zone Read + WAF/Rulesets Edit + DNS Edit) | OpenBao `secret/services/cloudflare:api_token` | `CLOUDFLARE_API_TOKEN` env (Semaphore) |
| R2 state keys | OpenBao `secret/services/cloudflare:r2_access_key_id` / `:r2_secret_access_key` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env |
| R2 endpoint + bucket (embeds account id) | site-config | `-backend-config` at init |
| zone id | site-config | `TF_VAR_zone_id` env |

## Bootstrap (one-time — adopt existing resources with zero-diff)

Everything already exists in Cloudflare, so we ADOPT, not create. Run in a tofu
runtime (local `tofu`, or the Semaphore tofu template):

```sh
# 1. init against the R2 backend (partial config supplies account-specific bits)
tofu init \
  -backend-config="bucket=<r2-bucket>" \
  -backend-config="key=cloudflare/terraform.tfstate" \
  -backend-config="endpoints={s3=\"https://<account-id>.r2.cloudflarestorage.com\"}"

# 2. import + auto-generate matching HCL for the existing WAF ruleset
#    (add DNS import blocks in Phase 2). See imports.tf for the import blocks.
tofu plan -generate-config-out=generated.tf

# 3. move the generated resource blocks into waf.tf / dns.tf, remove the import
#    blocks, then:
tofu plan     # MUST report "No changes" — proves faithful adoption, zero risk
```

**Gate:** never `apply` until `tofu plan` shows **No changes** for the imported
resources. Zero-diff = tofu now manages them without altering anything live.

## Steady state (via Semaphore)

Semaphore `tofu` app template "Apply Cloudflare Infra (OpenTofu)":
- repository = agent-cloud, subdirectory = `platform/infra/cloudflare`
- environment = the CF token + R2 keys + `TF_VAR_zone_id` (from OpenBao)
- plan on PR, gated apply on merge to `main`.
