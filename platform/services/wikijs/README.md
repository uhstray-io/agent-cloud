# Wiki.js — placeholder (no deployment)

This service has no agent-cloud deployment: `deployment/` contains only a
`.gitkeep`, so there is no deploy script, compose file, playbook, or Semaphore
template. `plan/architecture/service-architecture.md` lists it among the
directories that "exist with nothing deployable".

`context/` retains legacy application notes (a historical UI-customization and
YouTube-embed walkthrough with its screenshots). Those notes are explicitly
historical reference, not a supported procedure: the former direct Docker
command enabled app-managed TLS and bypassed Semaphore/OpenBao, so any real
integration must deploy through Semaphore and terminate public TLS at Caddy.
