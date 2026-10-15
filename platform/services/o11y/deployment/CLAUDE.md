# CLAUDE.md — platform/services/o11y/deployment

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

o11y is the platform's observability receiver: one rootless-Podman compose stack on its
own VM in production (`o11y_svc`), and on the local-dev VM. `README.md` in this directory
is the full operating record (dated receipts, drills, gates); this file is the short
orientation an agent needs before editing here.

## What runs

`compose.yml` defines seven containers, each image pinned by an env-overridable default:
`o11y-prometheus`, `o11y-loki`, `o11y-alloy`, `o11y-tempo`, `o11y-pyroscope`,
`o11y-grafana` and `o11y-node-exporter` (the receiver host's own exporter).
`deploy.sh` is container lifecycle only: it reads the rendered `.env`, pulls, starts the
stack and waits for Grafana to report healthy. Outside local mode it adds
`compose.prod.yml`, which gives Grafana a route to Authentik through the LAN Caddy host.

## How it is deployed

- **Only through Semaphore:** `platform/playbooks/deploy-o11y.yml` (production runs the
  Dev-bound `Deploy o11y (Dev)` template). When a candidate SHA is required it refuses any
  other checkout, renders the Alloy and Prometheus configs and the
  Grafana alert provisioning, then runs `deploy.sh` and reads the live state back
  (health endpoints, datasources, three named dashboards, the `o11y_` alert rules and
  the Discord contact point).
- **Secrets** come from OpenBao `secret/services/o11y` through `tasks/manage-secrets.yml`:
  the Grafana admin password, and `alert_discord_webhook_url` (read only when alerts are
  enabled). The synthetic probe's key is a shared read rendered to its own 0600
  `probe/inference-probe.env`, never to `.env`, which Grafana loads.
- **Private values** (addresses, binds, enable flags) live in site-config inventory. The
  production Semaphore inventory is a copy of it; sync it before a deploy relies on a new
  value (README, "production Semaphore inventory").
- **Receiver-host journal pilot:** `Deploy o11y Journal Collector (Dev)` runs independently
  of `Deploy o11y`. Run its `survey` action first: it checks only `/var/log/journal` and
  `/run/log/journal`, requiring a recent exact-name record and file readability by the
  cached receiver Alloy v1.5.1 image as a rootless UID `0:0` file-read probe with
  `--pull=never`; it does not validate collector config or pull an image. If that image
  is absent, the survey reports `probe_unavailable`. It reports one viable path,
  `ambiguous`, or `none`. Apply uses the separate pinned Alloy v1.9.2 collector image
  for `alloy validate`. Private
  `o11y_svc` inventory must then declare the reviewed `o11y_journal_directory` as that
  actual directory.
  The playbook checks that directory and a recent exact `CONTAINER_NAME=o11y-alloy` entry,
  then mounts only that directory read-only. It sends `service=o11y/alloy`,
  `signal=container`, and `target_id=service:o11y` through the existing private OTLP
  receiver. It does not read the engine socket or alter receiver volumes. The
  Compose service has no dependencies;
  after apply, the playbook hashes IDs and states for all seven existing receiver
  containers and refuses success if any changed. The collector state lives in the
  fresh Compose named `journal-collector-state-v2` volume mounted at `/alloy-state`
  with long-syntax `volume.nocopy: true`; Alloy's
  `--storage.path` and exact live-mount/rollback checks use that same root-level path.
  This avoids traversing the image's `/var/lib/alloy` directory, which the pinned
  [Alloy v1.9.2 Dockerfile](https://github.com/grafana/alloy/blob/v1.9.2/Dockerfile)
  creates as UID/GID 473 with mode `770`, while this pilot keeps UID `0:0` and drops
  all capabilities. Apply observes health for up to
  three minutes, matching the Compose startup grace/retry window. A failed apply or
  explicit `stop` force-removes only the exact pilot container without `-v`, then
  verifies it is absent from Podman's reboot restart set and that its positions
  volume remains. Failure output includes only container state, health state,
  failing streak, health-check exit codes, and fixed mount categories found on
  permission-denied log lines (`journal`, `positions`, `config`); it excludes raw
  logs, arbitrary check output, and paths outside those fixed categories.
  The playbook
  passes `--no-deps` too, while the dependency-free service and receiver readback keep
  older podman-compose releases safe if they ignore that flag.
  The explicit `survey` action resolves the positions volume from the receiver
  Compose project and `journal-collector-state-v2` identity, including when the pilot
  container is absent. New volumes carry the explicit `com.docker.compose.volume`
  label; an existing volume remains compatible when that label is absent only if
  its exact expected name and project label match. Conflicting labels or names refuse.
  The legacy `journal-collector-state` volume remains untouched for later independent
  forensic/disposition review; all journal helper actions resolve only v2. First
  bootstrap requires v2 to be absent. Unexpected existing v2 state without a valid
  cursor refuses the start gate. A fresh start can replay only Alloy's supported
  `max_age` window, not older journal entries. Use the explicit Dev-bound `stage`
  action to place the exact `expected_repository_sha` through the shared
  `place-monorepo.yml` task; it refuses check mode and does not start/recreate a
  container or inspect, mount, or change either positions volume. The required
  order is `stage` -> read-only `survey` -> separate `apply`. Survey never places
  the checkout. Stage, survey, and apply first require installed target `HEAD` to
  equal `expected_repository_sha`, then run the installed `podman-compose config`
  parser under `no_log` and require the resolved v2 source,
  `/alloy-state` destination, and `nocopy: true`; this does not prove the runtime
  applied the option. Check mode skips provider runtime reads and reports
  `check_mode_unverified`; stage refuses check mode. Survey reads bounded owner/mode
  and direct-child metadata in the rootless Podman
  namespace; it does not mount or initialize the volume and never reads
  positions-file content. Review this survey before choosing
  `repair-positions`, which is a separate action. Both repair paths require an
  unused unique local volume, no ACL or mount ambiguity, only direct regular
  single-link files owned by collector identity `0:0`, a root owner mismatch,
  and owner mode bits sufficient for access after the root-only change. Free-byte
  and free-inode counters are independent threshold checks; changes above threshold
  do not invalidate stable root, child, ACL, or mount evidence. Initialized-volume
  repair rechecks evidence, pins the root directory by file descriptor, changes
  only that directory's owner/group with rootless `podman unshare`, verifies the
  result, and runs the restricted cached Alloy create/write/rename/delete probe.
  Its failure path may restore the original owner only after fresh identity and
  metadata checks. The narrow pending repair applies only to an empty volume with
  `NeedsChown=false` and `NeedsCopyUp=true`. If group/other write bits are the
  only failed guard, it may first reduce the pinned root mode with
  `original_mode & ~0o022`, preserving read/search bits; it refuses special bits,
  incomplete owner RWX, malformed mode or unrelated failed checks. A separate
  full discovery/readback must pass before the owner/group change. Each mutation
  pins the same verified root device/inode through a no-follow descriptor. It
  performs no probe mount and never restores mode or ownership after mutation
  because first-mount history cannot be proven. A failed pending repair reports
  uncertain and preserves the volume; a converged rerun reports already-correct.
  It never recurses or removes/recreates the volume. Survey, repair,
  and apply report the positions-specific result as `check_mode_unverified` in
  check mode. Apply accepts `bootstrap_allowed` only at the pre-apply and
  immediately-before-start gates. For a missing volume, bounded volume inventory
  must prove the exact expected project name is unused and an all-container query
  must prove the named collector is absent, including stopped containers. Only
  a proven-missing v2 volume can bootstrap. `podman-compose up` is the only
  create/initialization action. Any existing v2 volume requires strict identity,
  access, layout, and a valid parsed cursor before it can start; empty or
  initialization-pending state, absent cursor, and invalid, unreadable, or unknown
  cursor evidence refuse. There
  is no cursorless pre-start retry. The immediate pre-start check is outside the
  rollback block, so a refusal before startup cannot remove an already-running
  collector. After startup, the playbook calls separate `verify-live`, which
  must return strict `ready`, including the parsed expected journal cursor in
  `positions.yml`; the cursor is mandatory for post-start success. Runtime checks also prove the actual RW volume source/destination,
  UID mapping, effective write/rename access, health, unchanged receiver containers,
  and exact Loki delivery. The bounded live layout permits optional `alloy_seed.json`
  and the source's `loki.source.journal.o11y_alloy/` directory; post-start acceptance
  still requires its valid cursor evidence. The separate repair action still refuses
  directory children.
  Health, seven-container preservation, Loki receipt, and rollback gates still apply.
  Neither survey nor repair proves collector health or Loki delivery. OpenSpec task
  1.2 stays unchecked until the reviewed Semaphore run records the exact production
  Loki receipt and the other live mount, access, health, and receiver-preservation
  checks. Check mode reports unverified and does not mutate or mount the volume.
  An unexpected survey exception returns the bounded unavailable receipt with fixed
  reason `survey_failed` so the journal-source survey can continue safely.

## Switches that change what renders

All default off, so declaring a value alone never starts a scrape or pages anyone:

| Variable | Effect |
|---|---|
| `dgx_spark_scrape_enabled` | Renders `config/scrape.d/dgx-spark.yml` and the `inference-failing`, `telemetry-missing`, `memory-thermal` and `benchmark-gate` alert groups; off lists those rules under `deleteRules` |
| `o11y_inference_probe_enabled` | Installs the five-minute probe timer and two probe rules; off removes only the probe artefacts. The shared textfile collector remains active for daily capacity samples |
| `o11y_inference_probe_client_leaf` | With the probe enabled, sends it straight to the gateway's mutual-TLS listener as a gateway client identity, presenting this declared internal-CA client leaf; places the CA bundle beside the leaf and maps the gateway's name on this host (README, "Gateway client identity") |
| `o11y_alerts_enabled` | Unpauses the rendered rules and routes them to the Discord contact point, except the `benchmark-gate` placeholder, which stays paused and unrouted (`templates/alerts.yml.j2:340-349,428-432`) |
| `o11y_expected_metrics_targets_reviewed` | Defaults false; enables production per-target missing-telemetry rules only with alerts enabled and a reviewed bounded five-label target declaration. See README, “Expected metrics target contract.” |
| `o11y_gateway_span_logs_enabled` | Also writes one Loki line per gateway span |
| `o11y_inference_queue_waiting_warn` | Threshold (default 6) for the `inference_queue_depth_high` warning: requests waiting on vLLM for 10 minutes. A number, not a switch; the rule renders only with `dgx_spark_scrape_enabled` |

## Conventions specific to this service

- Binds default to loopback (`templates/env.j2`). Grafana, Loki and the OTLP receiver
  (`o11y_otlp_bind`, ports 4317/4318) are bound to the VM address in production by
  inventory, each admitted by a source-scoped firewall rule; a production deploy
  refuses a loopback or non-IPv4 OTLP bind when gateway tracing is enabled.
- Retention defaults are Prometheus `15d`, Loki `7d`, Tempo `168h`; the Prometheus size
  cap defaults to `1GB` in local mode and `0B` (no cap) otherwise (`templates/env.j2:26-30`).
  In production, any retention other than that baseline is refused unless the run has a
  nonzero Prometheus cap and numeric `o11y_capacity_receipt_id` and
  `o11y_backup_restore_receipt_id` references (`deploy-o11y.yml:134-150`), the named
  volumes resolve with at least 30% headroom (`:169-180`), and the capacity readback
  reports an acceptable state (`:182-188`). The backup/restore check validates only that
  inventory contains a positive numeric reference; it does not inspect independent receipt
  evidence or prove a live restore. Tempo-derived metrics use the same common receipt
  variable (`:312-323`). These reference checks do not authorize retention expansion.
- `config/scrape.d/host-<group>.yml` (rendered from `templates/scrape-host-node.yml.j2`) is
  written by `instrument-host-o11y.yml`, the service deployment workflow's instrument-host
  step, never by `deploy-o11y.yml`, which neither writes nor removes it.
- Every `vllm:` and `node_` metric name used by a dashboard or alert rule must appear in
  the vendored fixtures under `platform/tests/fixtures/`; the o11y BATS suite enforces it.
- The deploy readback requires the live rules in the provisioned folders and groups to
  equal the rendered alert file, so it covers the `inference_` rules as well as the `o11y_`
  ones; the drills check only `o11y_` rules (README, "Inference dashboards and alerts").

## Tests

`platform/tests/test_service_o11y.bats` (render, dashboards, alert rules, metric names)
and `platform/tests/test_inference_probe.py` (probe script).
