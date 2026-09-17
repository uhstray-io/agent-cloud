# agentgateway deployment

Deploy through Semaphore: template **Deploy agentgateway** (prod) or
`make local-deploy-agentgateway` (local-dev). Destructive rebuild: **Clean Deploy
agentgateway** — the gateway is stateless, so this only re-renders and restarts.

`deploy.sh` is container lifecycle only. Both files the container reads (`.env`,
`config.yaml`) are rendered by `deploy-agentgateway.yml` from OpenBao + inventory and
are gitignored. Operational reference: `../context/architecture.md`.
