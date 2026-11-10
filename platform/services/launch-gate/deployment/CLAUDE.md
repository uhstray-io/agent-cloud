# launch-gate — container deployment

The launch gate is the only identity that may ask Semaphore to launch a task on behalf of an agent. It is a
stdlib Python HTTPS service in `app/launch_gate.py`. An agent calls `POST /v1/launch` with its caller bearer
token. The callers are skynet and the Dispatch Console (`console`), each with its own token (see "Callers"). The
gate checks the token, that the caller may act as the requested agent, and that the template is on the allowlist.
It then asks OPA for a decision and creates the Semaphore task only on an allow. `POST /v1/decide` runs the same
checks and answers the decision without creating a task (see "The decision route"). Full design and the check order:
`plan/development/openspec/changes/skynet-launch-containment/design.md`.

## Status: deployed and validated in local-dev; not deployed in production

The container files are in this directory. This directory does not deploy anything by itself. The playbooks that
deploy it are in `platform/playbooks/`, and their Semaphore templates are declared in `platform/semaphore/templates.yml`
(with generated `(Dev)` variants) and `platform/semaphore/templates-local.yml`:

- `deploy-launch-gate.yml`, the deploy (template `Deploy launch-gate`). It refuses bad inputs before any change, places
  the repo, renders the allowlist, manages the tokens, issues the server leaf, runs `deploy.sh`, and verifies.
- `clean-deploy-launch-gate.yml`, the destructive redeploy (template `Clean Deploy launch-gate`). It runs the teardown
  first, then the deploy.
- `provision-launch-gate-semaphore-access.yml`, the identity `svc-launch-gate` at `task_runner` (template
  `Provision launch-gate Semaphore Access`). Run it before the deploy: the deploy reads the token it stores and stops
  when that token is absent.

The playbook headers hold the behaviour, the refusals and the order of the steps.

Local-dev ran the gate on 2026-11-04: bootstrapped from dev, deployed and validated (local Semaphore tasks 11594
and 11596; the 3.8 scenarios passed against the live gate), then stopped again with its volumes kept. Production
does not run it yet. The decision route and the `console` caller are task 7.2 of `add-dispatch-console`
(`plan/development/openspec/changes/add-dispatch-console/tasks.md`; decisions D49 and D51 in
`plan/development/17-dispatch-console.md`). That change deploys nothing. The console's own deploy, which shared-reads
`caller_console_token`, is built in `deploy-console.yml` (read when `console_launch_gate_url` is declared). The open tasks of the `skynet-launch-containment` change
(`plan/development/openspec/changes/skynet-launch-containment/tasks.md`) are:

- skynet's side: the shared read of `caller_skynet_token` and the route to the gate (3.6),
- the production host, firewall rules, the `svc-launch-gate` team member, and the rollout (5.1-5.5).

## Files

| File | Role |
|------|------|
| `compose.yml` | The base: one container, `restart: always`, the HTTPS port on `${LAUNCH_GATE_BIND:-127.0.0.1}:${LAUNCH_GATE_PORT:-8443}`, read-only mounts of `./app`, `./config.json` and `./certs`, `read_only`, `cap_drop: ALL`, `no-new-privileges`, and `init: true` (an init process as PID 1, so a stop delivers SIGTERM; production hosts need catatonit, which the deploy checks). |
| `compose.prod.yml` | Production overlay, loaded by `deploy.sh` outside `LOCAL_MODE`: `user: "1000:1000"` and `userns_mode: keep-id:uid=1000,gid=1000`, so the non-root process reads the 0600 TLS key; `extra_hosts` maps the OPA host name to `launch_gate_opa_ip` (see "Production inventory"). |
| `compose.local.yml` | Local-dev overlay (`LOCAL_MODE=true`, appended by `platform/lib/common.sh`): memory cap, `label=disable`, the `local-dev` network. It does not republish ports. |
| `deploy.sh` | Container lifecycle only. Checks the inputs exist, pulls, starts with `compose_up_if_changed app config.json certs/step-ca-bundle.crt`, waits for the container to be running. It generates no secret. |
| `templates/env.j2` | Renders `.env`: the image, bind and port, the Semaphore token, one caller token per caller, and outside local-dev the OPA name map (`LAUNCH_GATE_OPA_HOSTNAME`, `LAUNCH_GATE_OPA_IP`) that `compose.prod.yml` reads. |
| `templates/config.json.j2` | Renders `config.json`: URLs, allowlist, caller-to-agent map, listener, certificate paths. It holds no token. |
| `.gitignore` | `.env`, `config.json`, `certs/`. The rendered and issued files are never committed. |
| `AGENTS.md` | Symlink to this file. |

The allowlist filter lives outside this directory: `platform/playbooks/filter_plugins/launch_gate.py`
(`launch_gate_allowlist`). The list is the union over every agent the callers act as, base names beside their
(Dev) variants, so it can admit a name OPA then refuses for that agent or branch. That is deliberate:
per-agent and per-branch permission is OPA's decision on every launch; the allowlist keeps anything outside
the catalog, and every destructive template, from reaching OPA at all.

## Image

The image is `${LAUNCH_GATE_IMAGE:?set launch_gate_image in the inventory}`. The design calls for an official
Python image pinned by digest. Nothing in this directory chooses it. The app's annotations (`str | None`) are
evaluated at import, so the image must be Python 3.10 or later. The deploy does not assert a Python version. Its
Phase 3 probes run the gate under the pinned image: `python` must start and import `ssl` inside the container, and
`/healthz` must answer 200 over TLS from the host.

## Config contract (`config.json`)

`load_config` in `app/launch_gate.py` reads exactly these keys and refuses any other key:

- required: `semaphore_url`, `semaphore_project`, `opa_url`, `allowlist`, `callers`, `listen_host`,
  `listen_port`, `tls_cert`, `tls_key`
- optional: `opa_ca_bundle`, `local_mode`

`semaphore_url` and `opa_url` must be `https://` unless `local_mode` is true. `callers` maps a caller name to
the list of agents it may act as. A caller name is 1 to 32 characters: a lowercase letter, then lowercase
letters, digits or hyphens.

The template renders `listen_host` as `0.0.0.0` and `listen_port` as `8443`, which is the container side of
the port mapping in `compose.yml`. It renders `opa_ca_bundle` as `/certs/step-ca-bundle.crt`.

## Environment contract (`.env`)

- `LAUNCH_GATE_SEMAPHORE_TOKEN`: the Semaphore token.
- `LAUNCH_GATE_CALLER_TOKEN_<NAME>`: one per caller in `callers`. `<NAME>` is the caller name upper-cased with
  `-` replaced by `_`.

Each token must be at least 32 characters. Two callers may not share a token. The gate refuses to start on
either failure, and it never prints a token. `env.j2` renders one `LAUNCH_GATE_CALLER_TOKEN_*` line per
caller, in sorted order, from `secrets['caller_<name>_token']`.

## Callers

`callers` maps each caller to the agents it may act as. The deploy's default (`_launch_gate_callers` in
`deploy-launch-gate.yml`, overridable by inventory `launch_gate_callers`) names two callers, `skynet` and `console`,
and both act as the five role agents in `_launch_gate_role_agents`: infra-, security-, o11y-, service- and
network-agent. The Dispatch Console launches as its own caller, not as skynet (plan 17, D49).

Each caller has one token. The deploy generates `caller_<name>_token` into `secret/services/launch-gate` once and
reuses it (`_secret_definitions`), and renders it as `LAUNCH_GATE_CALLER_TOKEN_<NAME>`. A caller needs an entry in
`_launch_gate_token_callers`, which the deploy checks, and a matching `caller_<name>_token` secret definition, which
the bats suite checks against that list.

The deploy's Phase 3 probes present skynet's token: the probe caller is `skynet` whenever skynet is a caller, and the
first sorted caller name otherwise (`deploy-launch-gate.yml`, "Hold the caller and template the verify play probes with").

The task message is `<caller> <workflow_id> <step>` (`task_message`). The tag D49 specifies for a console launch is
`console <proposal id> <step>`; the gate's message form is unchanged.

## The decision route (`POST /v1/decide`)

The console previews a proposal here, and the launch at approval decides again (plan 17, D51). The request is the
same bearer token and the same body as `/v1/launch`, with the same keys, including `dry_run`. The route runs the same
checks in the same order, up to and including OPA: authenticate, parse the request, authorise the agent and template,
merge the survey (declared fields, defaults, types), pin the branch, apply the check-mode version rule when `dry_run`
is true, then ask OPA. Both routes call one method (`Gate._judge`), so they cannot drift.

The decision then stops. It never reads the template's running tasks, never takes the template lock and never creates
a task. Its log event is `"event": "decide"`, with the launch's fields and no `task_id`.

Answers:

- `200 {"decision": {"allowed": true, "reason": "<OPA's reason>"}}` when OPA allows.
- `200 {"decision": {"allowed": false, "reason": "<OPA's reason>"}}` when OPA denies. An OPA deny is the answer, not a
  refusal, so this body has no `error`.
- The refusals a launch gives before OPA, with the same status, code and detail: `401 unauthorized`; `400 bad_request`,
  `undeclared_field`, `missing_required`, `bad_value`, `branch_mismatch`, `dry_run_unverified`; `403
  agent_not_permitted`, `template_not_allowed`; `502` with `semaphore_unreadable`, `template_not_found`,
  `template_ambiguous`, `branch_ambiguous`, `bad_default` or `unsupported_field_type`; `503 opa_unavailable` when OPA is
  unreachable or its answer is unusable.
- `405` for any method other than POST, and `404` for any other path.

A decision is a preview. `/v1/launch` decides again at approval and is the authoritative answer. The tests in
`platform/tests/test_launch_gate_flow.py` (the decision route section) pin that each pre-OPA refusal matches the
launch's, that the OPA input matches the launch's, and that the decision makes no Semaphore write, takes no lock and
reads no running task.

## Certificates and renewal

- The server leaf is `certs/<leaf>/current/cert.pem` and `certs/<leaf>/current/key.pem`, issued from the
  internal CA. `certs/` is mounted as a directory, not as a file. Renewal swaps the `current` symlink, and a
  single-file mount would keep the old inode.
- The gate re-reads the leaf on every connection. It fingerprints the resolved files and rebuilds its TLS
  context when they change. A failed reload keeps the last good context. A renewal therefore needs no restart.
- `deploy.sh` hashes `app/`, `config.json` and `certs/step-ca-bundle.crt`. It does not hash the leaf
  directory. A renewal must not recreate the container. A changed bundle does recreate it, because the OPA
  client reads its CA bundle once, when it is constructed, at start.
- The bundle is the only `certs/` file in the digest. Hashing all of `certs/` would make every renewal a restart.

## Production inventory

Outside local-dev the deploy refuses to run unless these are declared on `launch_gate_svc:vars` in site-config's inventory:

- `launch_gate_opa_url`: the OPA decision API, `https://`. Set it to the same URL as `skynet_opa_url`: skynet's OPA call
  already reaches OPA under that host name (`deploy-skynet.yml`, its OPA decision probe).
- `launch_gate_opa_ip`: one IPv4 address, the OPA host's. The OPA leaf is signed for a name with no production DNS
  record, so `compose.prod.yml` maps that name to this address inside the container. `0.0.0.0` is refused. The URL's
  host must be a DNS name, not an IP address, because the mapping needs a name.

Phase 3 proves the mapping: `GET <launch_gate_opa_url>/health` from inside the container must answer 200 through the
gate's own trust bundle (`opa-health 200`). A failure names the status or the exception class, never the body.

OPA's host firewall must admit the gate's host before the first production deploy: declare `launch_gate_svc` in
`opa_svc`'s `firewall_allow_groups` and run `Apply Firewall` on `opa_svc`. Otherwise Phase 2 has already started the
new container when the Phase 3 OPA probe fails, and the run ends red with the gate running.

## Local-dev

Declared in `platform/playbooks/bootstrap-local-dev.yml` (`[launch_gate_svc]`, the local Semaphore's inventory) and
mirrored in `platform/inventory/local-dev.yml.example`. `Deploy launch-gate (Local)` and
`Provision launch-gate Semaphore Access (Local)` are in `platform/semaphore/templates-local.yml`.

- Deploy OPA (Local) first. Phase 3 asks OPA for `/health` from inside the gate container at `http://opa:8181`, so a
  local deploy fails there while OPA is not running on the `local-dev` network.
- The published port is 18443 on 127.0.0.1, because Caddy holds 8443 on the Mac. The container still listens on 8443.
- The TLS name is `launch-gate.local.agent-cloud.test` (`dns_site` local, `dns_zone` the local zone). `compose.local.yml`
  makes it a network alias on `local-dev`. The Phase 3 probes run inside the Semaphore container and dial that name on
  8443 with no `--resolve`: the published port is on the Mac's loopback, which that container cannot reach. Production
  keeps `--resolve` to the declared bind.
- The local process runs as uid 1001 gid 0 (`compose.local.yml`), the uid of the Semaphore container, which writes
  the leaf's 0600 key. `cap_drop: ALL` would keep root from reading it.
- The local Semaphore gets openssl only after a bootstrap re-run. `tasks/issue-internal-leaf.yml` makes the key and
  request with openssl on the consumer, which for this group is that container. The image has none (measured in a
  throwaway container from `docker.io/semaphoreui/semaphore:v2.19.11`).
- The leaf is declared on `launch_gate_svc:vars`. The deploy's assert and its mint read it on the consumer.

**Local renewal is done by the deploy.** Local-dev has no `Renew Internal Certs` run, and the renewal planner reads
`hostvars[_ca_host].internal_leaves` (the CA host's, not this group's), so it never sees this leaf. A local Clean Deploy
keeps `certs/` (clean-service removes the checkout only in production). The leaf lives 720h (`stepca_leaf_dur`
default). So the deploy itself checks it: in local-dev, `openssl x509 -checkend 604800` on `current/cert.pem`, and a
leaf with under 7 days left, or none yet, is issued again (`_mint_reissue`). Re-run `Deploy launch-gate (Local)` at
least weekly, or after a long break, to keep it current. Production keeps an issued leaf; Renew Internal Certs renews it.

**The working-tree branch.** On both routes (`/v1/launch` and `/v1/decide`, through `Gate._judge`) the branch the
gate pins and asks OPA about is the template's repository record's, read from Semaphore and never from the caller.
Local-dev's working-tree record declares `HEAD` (whatever the developer has checked out), and OPA allows agent
launches only from `main` or `dev`, so in local mode only the gate reads that `HEAD` as `dev` (`policy_branch`), as
skynet's local catalog already does. Outside local mode `HEAD` reaches OPA unchanged and is refused. Found by the
local skynet launch probe on 2026-11-09 (local task 14161): every agent launch through the local gate was denied "an
agent task may run only from main or dev".

## Allowlist

`launch_gate_allowlist(catalog_doc, templates_doc, agents, local_templates_doc=None)` renders the list at deploy
time:

1. For each agent, it takes `catalog.<agent>.allowed_templates` and `catalog.<agent>.service_deploy_templates`
   from OPA's `policies/agentcloud/data.json`. The gate's list only narrows what reaches OPA. It adds nothing
   OPA does not name.
2. It removes every name in `catalog.semaphore.destructive_templates` and every name that starts with
   `Clean Deploy`.
3. For each remaining name, it adds `<name> (Dev)` when `platform/semaphore/templates.yml` sets
   `dev_variant: true` for it.
4. Under local mode only, the deploy also passes `platform/semaphore/templates-local.yml`, and the filter adds
   `<name> (Local)` for each remaining name that file declares. skynet's local catalog launches the ` (Local)`
   variants, and OPA trims that suffix (`agent_actions.rego`, `_base_template`). A production deploy passes
   nothing, so its list carries no ` (Local)` name.

It fails closed: it raises `AnsibleFilterError` on an empty agent list, an agent not in the catalog, an agent with no
`allowed_templates` list, a remaining name that `templates.yml` does not declare (catalog drift), a template
name declared twice, or a local document without a `templates` list. The deploy playbook calls it in Phase 1 without a rescue, so the error stops the run before the
repo is placed.

## Do not

- Do not let the console call OPA itself. Its preview goes through `POST /v1/decide`: one OPA caller and one input
  builder, so the preview cannot disagree with the launch (plan 17, D51).
- Do not put a token in `config.json`, in a template outside the `secrets` namespace, or in any committed file.
  Templates use `secrets.*` and `_`-prefixed variables only. `test_credential_leaks.bats` is meant to check
  that, but its regex does not match `{{ name` written with a space. CI's GNU grep shows the same miss: run
  38731550214 (head 66c18f5b) passes that test while `n8n.env.j2` at that head has `{{ n8n_bind |`.
  `test_launch_gate_allowlist.py` therefore checks the parsed templates instead.
- Do not add a template to the gate by hand. The list comes from OPA's catalog, and a template the catalog
  does not name never reaches the gate.
- Do not mount a leaf file, or any single file under `certs/` that renewal replaces.
- Do not declare `command` in an overlay. The base command stands.
- Re-render after changing a list in an overlay: `podman-compose -f compose.yml -f <overlay> config`. Under
  podman-compose 1.0.6 and 1.6.0 the overlay's `security_opt` is appended to the base list (checked in
  `compose.local.yml`). OPA's `compose.tls.yml` records that 1.0.6 replaces `command` when an overlay declares
  it; that claim is not re-verified here, and this overlay does not declare `command`.

## Verification

- `platform/tests/test_service_launch_gate.bats`: the compose shape, the hardening, the mounts, the deploy
  script's input hashing, the overlays, the templates' namespaces, and this file's symlink.
- `platform/tests/test_launch_gate_allowlist.py`: the filter on synthetic and on the repository's own catalog and
  templates, and both templates rendered with the real `to_nice_json`. The rendered config is accepted by the
  app's own `load_config`.
- `platform/tests/test_launch_gate_playbooks.bats`: the order of the guards and the plays in the three playbooks, their
  refusals, the secrets they declare, the Phase 3 probes, and one behavioural run that refuses a non-digest image
  before the repo is placed.
- `platform/tests/test_launch_gate*.py` (existing): the app.
- `platform/tests/test_restart_policy.bats`, `test_compose_image_qualified.py`, `test_no_bind_mount_dir_delete.py`:
  repository-wide guards that also cover this directory.
- `platform/playbooks/probe-launch-gate.yml` (template `Probe launch-gate (Dev)`): the allow-path and duplicate-refusal
  probe, run from inside the gate's container; `platform/tests/test_probe_launch_gate.py` runs it against a fake gate.
