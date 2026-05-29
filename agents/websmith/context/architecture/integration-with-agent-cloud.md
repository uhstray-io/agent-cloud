# WebSmith ↔ agent-cloud integration

> **Status:** Skeleton. Filled in by Phase 11 of `plan/development/WEBSMITH-INTEGRATION-PLAN.md` once the UhhCraft integration has proven the pattern end-to-end.

This document will describe how a signed `SPEC.md` produced by a WebSmith session is converted into a `platform/services/<sitename>/` service in agent-cloud.

## Planned contents (Phase 11)

- **Spec → service shape.** Standard layout of `platform/services/<sitename>/` derived from the SPEC.
- **Spec → playbook.** Standard `platform/playbooks/deploy-<sitename>.yml` derived from the SPEC's tooling phase.
- **Spec → Caddy fragment.** Standard `templates/caddy-site.j2` derived from the SPEC's domain.
- **Spec → OpenBao secret layout.** Standard `secret/services/<sitename>/...` derived from the SPEC's integrations.
- **Spec → Semaphore template.** Standard `Deploy <Sitename>` entry in `platform/semaphore/templates.yml`.
- **Spec → CI path filters.** Standard `.github/workflows/lint-and-test.yml` additions per stack.
- **Deviations register.** The `## Deviations from Spec` section every service must keep in its `context/spec/SPEC.md`.

## Until then

Use [`platform/services/uhhcraft/`](../../../../platform/services/uhhcraft/) as the reference shape once Phase 2 lands. Reading its `deployment/` + `context/spec/SPEC.md` side-by-side shows the spec-to-service mapping in full.
