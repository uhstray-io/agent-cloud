# NocoDB — RETIRED

NocoDB is retired. The decision was made on 2026-09-01: tududi replaced it as the
to-do/tracking app, and tududi has itself since been replaced by Plane (see
`plan/development/16-plane-work-management.md`, which scopes tududi's own
teardown). This is recorded in `AGENTS.md` under "In Progress" ("NocoDB
decommission") and in `plan/development/09-service-migrations-tooling.md`.

The service still runs through the legacy `deploy.sh` path in `deployment/`
(`deploy.sh`, `compose.yml`) — this directory is not evidence of a supported
composable deploy. Taking it down (containers, volumes, its hand-maintained Caddy
route, and `generate_nocodb_env()`) is a separate pending change scoped as its
own work; do NOT execute the old migration plan in
`plan/development/09-service-migrations-tooling.md` against it. Do not deploy,
extend, or depend on this service for new work.
