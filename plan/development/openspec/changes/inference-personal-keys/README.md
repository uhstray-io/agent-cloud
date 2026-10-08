# inference-personal-keys

Personal inference access through Authentik SSO: one gateway API key per eligible user, kept in OpenBao, readable only by its owner, rotated monthly (as one cohort on a fixed calendar day while a key change restarts the gateway, so a key lives at most 34 days; 30 days with hot reload) and revoked on offboarding by a scheduled reconcile
