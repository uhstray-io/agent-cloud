# agentgateway deployment

Deploy through Semaphore: template **Deploy agentgateway** (prod) or
`make local-deploy-agentgateway` (local-dev). Destructive rebuild: **Clean Deploy
agentgateway** — removes both containers (gateway + its own Postgres) and the budget
volume, then redeploys. Keys come back from OpenBao unchanged; the only state lost is
every identity's current token-budget window.

Every real deploy checks that rendered `AGW_IMAGE` equals the reviewed
`cr.agentgateway.dev/agentgateway:v1.5.0` pin, then asks `deploy.sh --pull-only`
to resolve the effective Compose images. It captures the resulting local image
ID and runs that ID with `--validate-only -f /config.yaml` before `deploy.sh`
can recreate containers. The validator uses `--pull=never`, and the actual
lifecycle uses `deploy.sh --no-pull`; a second image-ID check immediately before
deployment refuses a moved tag. The pull-only mode uses the same Compose
file/overlay selection as deployment and exits before any container decision or
change. The validator receives `.env` by
file path and has no network. In local mode and when listener TLS is enabled it
also receives the same read-only `/certs` mount and trust settings as Compose;
the production TLS check uses the same keep-id mapping. Its output is suppressed
because parser errors may quote configuration. The invocation follows the
[standalone validation command](https://agentgateway.dev/docs/standalone/latest/documentation/setup/update/)
and the image comes from the [v1.5.0 release](https://github.com/agentgateway/agentgateway/releases/tag/v1.5.0).
`Verify agentgateway Runtime (Dev)` is a separate,
read-only receipt for a clean reviewed checkout SHA, the running input label and
rendered files matching, readiness, the actual running image ID matching the
resolved v1.5.0 image ID, and the two sampling values read from the rendered file.
It reports only metadata; hashes and rendered file contents are not displayed.
It does not claim callback-marker absence from stored logs or spans. That runtime
correlation gate remains incomplete until stdout and OTLP records and an exact
callback span can be joined to each bounded synthetic request. Validator success
is necessary config evidence, not proof of runtime callback behavior. OTLP access
record separation with `signal: "access-log"` remains pending in gateway-owned
configuration; this source-side verifier does not claim that field has landed.

`deploy.sh` is container lifecycle only. Both files the container reads (`.env`,
`config.yaml`) are rendered by `deploy-agentgateway.yml` from OpenBao + inventory and
are gitignored. Operational reference: `../context/architecture.md`. When a deploy's render changes `config.yaml`, the file it replaced is kept
beside it as `config.yaml.previous` (an unchanged render leaves that copy alone; never under
local-dev's `agw_plaintext_keys`), which `rollback-inference-route.yml` gateway-config mode puts back. The
rollback refuses, before any write, a kept copy that enrols an identity the live config does not
(a rotated or revoked key) or `legacy-shared` past `legacy_shared_expires`, naming the identities
only; and `manage-agentgateway-client-key.yml` removes the copy once its deploy succeeds, so a
rotation or revocation leaves no rollback path to the old key.

## A deploy recreates the gateway only when its inputs changed

A recreate drops every in-flight stream, and scheduled or imported runs call this deploy, so
`deploy.sh` leaves a running gateway alone unless something it runs on changed (gateway task
1.12, design decision 11). It hashes `config.yaml`, `.env`, the compose files in use (the
base, `compose.local.yml` in local-dev, every `COMPOSE_OVERLAYS` entry such as
`compose.tls.yml`) and every file under `./certs`, and compares that digest with the
`io.agent-cloud.inputs-sha256` label the running container was started with. It recreates
both containers (`up -d --force-recreate`) when the digest differs, when no gateway
container exists or it is stopped, when any of the project's containers (the gateway and its
Postgres, both on tags a pull can move) runs a different image than its tag now names, or when
readiness does not answer; otherwise it runs no `compose up` at all. An input it cannot list
or read fails the deploy by name rather than hashing what happened to be readable.
The label, not this run's render, is the record, so a run that fails after rendering is
converged by the next one. The last output line is `deploy-result: recreated (<reason>)` or
`deploy-result: unchanged`, and the playbook reports a change only for the first.

## The operator UI is read-only

The rendered `.env` sets `UI_READ_ONLY=true` whether or not the UI is enabled. In agentgateway
v1.5.0 that switches the config store to read-only (`crates/agentgateway/src/config.rs:390-392`):
the five UI write handlers answer 403 and the UI shows its read-only banner, while logs, budgets
and the playground keep working. `config.yaml` is rendered by the deploy and mounted `:ro`, so
it is the only source of gateway configuration; change it through inventory and a deploy.
