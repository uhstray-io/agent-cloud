# agentgateway deployment

Deploy through Semaphore: template **Deploy agentgateway** (prod) or
`make local-deploy-agentgateway` (local-dev). Destructive rebuild: **Clean Deploy
agentgateway** — removes both containers (gateway + its own Postgres) and the budget
volume, then redeploys. Keys come back from OpenBao unchanged; the only state lost is
every identity's current token-budget window.

`deploy.sh` is container lifecycle only. Both files the container reads (`.env`,
`config.yaml`) are rendered by `deploy-agentgateway.yml` from OpenBao + inventory and
are gitignored. Operational reference: `../context/architecture.md`.

## The operator UI is read-only

The rendered `.env` sets `UI_READ_ONLY=true` whether or not the UI is enabled. In agentgateway
v1.5.0 that switches the config store to read-only (`crates/agentgateway/src/config.rs:390-392`):
the five UI write handlers answer 403 and the UI shows its read-only banner, while logs, budgets
and the playground keep working. `config.yaml` is rendered by the deploy and mounted `:ro`, so
it is the only source of gateway configuration; change it through inventory and a deploy.
