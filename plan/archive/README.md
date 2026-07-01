# plan/archive/ — consolidated plan originals
This directory holds the **pre-consolidation** plan documents. On 2026-06-26 the
`plan/development/` (35 docs) and `plan/architecture/` (16 docs) sets were
consolidated into dependency-ordered, numbered documents (`00-`, `01-`, …) that
merge the originals **verbatim** (under `<!-- source: -->` dividers) — so all
detail is preserved. These originals are kept here for provenance/history.

`plan/architecture/skills-recommendation.md` was left in place (standalone meta-doc).

## plan/development/ → consolidated
- `development/LOCAL-DEV-DEPLOYMENT.md` → **00-foundation-local-dev.md**
- `development/LOCAL-DEV-12A-IMPLEMENTATION.md` → **00-foundation-local-dev.md**
- `development/LOCAL-DEV-TLS-TRUST.md` → **00-foundation-local-dev.md**
- `development/INTERNAL-CA-DEPLOYMENT.md` → **00-foundation-local-dev.md**
- `development/DNS-SERVER-DEPLOYMENT.md` → **00-foundation-local-dev.md**
- `development/OPENBAO-HA-DEPLOYMENT.md` → **01-secrets-credentials.md**
- `development/OPENBAO-KV-MOUNT-PARAMETERIZATION.md` → **01-secrets-credentials.md**
- `development/CREDENTIAL-LIFECYCLE-IMPLEMENTATION.md` → **01-secrets-credentials.md**
- `development/APPROLE-TTL-ENFORCEMENT-PLAN.md` → **01-secrets-credentials.md**
- `development/ANSIBLE-CREDENTIAL-REDACTION-PLAN.md` → **01-secrets-credentials.md**
- `development/AUTH-SSO-DEPLOYMENT.md` → **02-sso-auth.md**
- `development/PROD-SSO-ROLLOUT-PLAN.md` → **02-sso-auth.md**
- `development/OPA-INTEGRATION-PLAN.md` → **03-guardrails-governance.md**
- `development/MAIN-BRANCH-PROTECTION-PLAN.md` → **03-guardrails-governance.md**
- `development/SOURCE-OF-TRUTH.md` → **03-guardrails-governance.md**
- `development/NETBOX-DISCOVERY-EXPANSION.md` → **04-netbox-discovery.md**
- `development/NETBOX-LOCAL-ENGINE.md` → **04-netbox-discovery.md**
- `development/SNMPV3-UPGRADE-PLAN.md` → **04-netbox-discovery.md**
- `development/O11Y-DEPLOYMENT.md` → **05-observability.md**
- `development/SKYNET-REPLACEMENT-PLAN.md` → **06-inference-skynet.md**
- `development/WISAI-TO-SKYNET-MIGRATION-PLAN.md` → **06-inference-skynet.md**
- `development/NETCLAW-INTEGRATION-PLAN.md` → **06-inference-skynet.md**
- `development/WEBSMITH-INTEGRATION-PLAN.md` → **07-websmith-uhhcraft.md**
- `development/UHHCRAFT-GO-LIVE-PLAN.md` → **07-websmith-uhhcraft.md**
- `development/UHHCRAFT-GO-LIVE-WALKTHROUGH.md` → **07-websmith-uhhcraft.md**
- `development/UHHCRAFT-GPU-PASSTHROUGH.md` → **07-websmith-uhhcraft.md**
- `development/ERPNEXT-DEPLOYMENT.md` → **08-erpnext.md**
- `development/nocodb-n8n-composable-migration.md` → **09-service-migrations-tooling.md**
- `development/PODMAN-UPGRADE-PLAN.md` → **09-service-migrations-tooling.md**
- `development/SPARSE-CHECKOUT-MIGRATION.md` → **09-service-migrations-tooling.md**
- `development/DEV-PROXMOX-CLUSTER-PLAN.md` → **10-infra-resilience.md**
- `development/DISASTER-RECOVERY-PLAN.md` → **10-infra-resilience.md**
- `development/IMPLEMENTATION_PLAN.md` → **(archived — legacy/reference, not in an active doc)**
- `development/ARCHITECTURE-REVIEW-FOLLOWUP.md` → **(archived — legacy/reference, not in an active doc)**
- `development/OPENSSF-SCORECARD-PLAN.md` → **architecture/03-testing-ci-quality.md**

## plan/architecture/ → consolidated
- `architecture/architecture-reference.md` → **plan/architecture/00-foundation-standards.md**
- `architecture/AUTOMATION-COMPOSABILITY.md` → **plan/architecture/01-automation-model.md**
- `architecture/AUTOMATION-DECLARATIVE-VS-IMPERATIVE.md` → **plan/architecture/01-automation-model.md**
- `architecture/SERVICE-INTEGRATION-PLAN.md` → **plan/architecture/02-service-onboarding.md**
- `architecture/TESTING-AND-LINTING-PLAN.md` → **plan/architecture/03-testing-ci-quality.md**
- `architecture/CI-TESTING-SPECIFICATION.md` → **plan/architecture/03-testing-ci-quality.md**
- `architecture/LINTING-AND-TESTING.md` → **plan/architecture/03-testing-ci-quality.md**
- `architecture/SECURITY-TESTING-STANDARDS.md` → **plan/architecture/03-testing-ci-quality.md**
- `architecture/BRANCH-TESTING-WORKFLOW.md` → **plan/architecture/03-testing-ci-quality.md**
- `architecture/CREDENTIAL-LIFECYCLE-PLAN.md` → **plan/architecture/04-credentials-access.md**
- `architecture/ACCESS-BOUNDARIES.md` → **plan/architecture/04-credentials-access.md**
- `architecture/CADDY-REVERSE-PROXY.md` → **plan/architecture/05-platform-infra.md**
- `architecture/PODMAN-VS-DOCKER-COMPOSE.md` → **plan/architecture/05-platform-infra.md**
- `architecture/OBSERVABILITY-INSTRUMENTATION.md` → **plan/architecture/06-observability-instrumentation.md**
- `architecture/WEBSITE-BUILDING-AGENT.md` → **plan/architecture/07-website-building-agent.md**
