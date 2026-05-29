# UhhCraft — Config Files

These files contain **non-secret** configuration loaded by the Go app at startup.
Secrets (API keys, passwords) live in environment variables — never in these files.

## Files

| File | Purpose |
|------|---------|
| `materials.toml` | All sticker and 3D print material/cut options with pricing modifiers. **Edit this to add new materials.** |
| `printify.toml` | Printify integration — sticker overflow fulfillment. |
| `fulfillment_3d.toml` | Hubs (Protolabs Network) integration — 3D print overflow fulfillment. |
| `usps.toml` | USPS v3 API — real-time shipping rate calculation. |

## Required environment variables

```env
# Stripe
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_TAX_ENABLED=true

# Resend (email)
RESEND_API_KEY=re_...

# Discord
DISCORD_ORDERS_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_OPS_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Postgres  (example format only — real value is templated from OpenBao)
DATABASE_URL=postgres://USER:PASSWORD@HOST:5432/uhhcraft  # trufflehog:ignore

# Redis
REDIS_URL=redis://localhost:6379

# MinIO / S3-compatible storage
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET=uhhcraft
MINIO_USE_SSL=false

# Session
SESSION_SECRET=<32-byte random hex>

# Printify
PRINTIFY_API_KEY=...
PRINTIFY_SHOP_ID=...

# Hubs / Protolabs Network (3D print overflow)
HUBS_API_KEY=...

# USPS API (v3)
USPS_CLIENT_ID=...
USPS_CLIENT_SECRET=...

# AI service endpoints (internal network — not secret, but env-configurable).
# Use the sidecar VMs' internal hostnames/IPs; real values live in site-config.
AI_IMAGE_SERVICE_URL=http://inference-comfyui.internal:8189
AI_3D_SERVICE_URL=http://inference-hunyuan3d.internal:8001

# App
APP_ENV=production
APP_PORT=3000
APP_BASE_URL=https://uhhcraft.uhstray.io
```

## Adding a new material

1. Open `materials.toml`.
2. Copy an existing `[[sticker.materials]]` or `[[print.materials]]` block.
3. Give it a unique `id` (lowercase, hyphens only).
4. Set the `display_name`, `description_customer`, and `price_modifier_usd`.
5. Set `available = true`.
6. Redeploy the Go app — the new material appears automatically in all radial selectors.

No database migration required — materials are loaded from config at startup.
