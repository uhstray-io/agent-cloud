# Nextcloud — placeholder (no deployment)

This service has no agent-cloud deployment: `deployment/` contains only a
`.gitkeep`, so there is no deploy script, compose file, playbook, or Semaphore
template. `plan/architecture/service-architecture.md` lists it among the
directories that "exist with nothing deployable".

`context/README.md` retains upstream reference material only. The former direct
Docker recipe bypassed the platform (Semaphore, OpenBao, Caddy) and is not an
installation procedure; a supported deployment needs a Semaphore playbook, an
OpenBao credential flow, and Caddy routing before rollout, designed against the
upstream Nextcloud AIO documentation.
