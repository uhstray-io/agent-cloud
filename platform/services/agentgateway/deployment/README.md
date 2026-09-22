# agentgateway deployment

Deploy through Semaphore: template **Deploy agentgateway** (prod) or
`make local-deploy-agentgateway` (local-dev). Destructive rebuild: **Clean Deploy
agentgateway** — removes both containers (gateway + its own Postgres) and the budget
volume, then redeploys. Keys come back from OpenBao unchanged; the only state lost is
every identity's current token-budget window.

`deploy.sh` is container lifecycle only. Both files the container reads (`.env`,
`config.yaml`) are rendered by `deploy-agentgateway.yml` from OpenBao + inventory and
are gitignored. Operational reference: `../context/architecture.md`.
