# launch-gate — container deployment

The launch gate is the only identity that may ask Semaphore to launch a task on behalf of an agent. It is a
stdlib Python HTTPS service in `app/launch_gate.py`. An agent (skynet, later the Dispatch Console) calls
`POST /v1/launch` with its caller bearer token. The gate checks the token, that the caller may act as the
requested agent, and that the template is on the allowlist. It then asks OPA for a decision and creates the
Semaphore task only on an allow. Full design and the check order: `plan/development/openspec/changes/skynet-launch-containment/design.md`.

## Status: deploy playbooks and templates; not deployed

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

No environment runs the gate yet. The open tasks of the `skynet-launch-containment` change
(`plan/development/openspec/changes/skynet-launch-containment/tasks.md`) are:

- the local-dev inventory and bootstrap entries (3.4). Until local-dev declares the `launch_gate_svc` group, `Deploy launch-gate (Local)`
  refuses at its pre-flight,
- the renewal proof for the server leaf (3.5),
- skynet's side: the shared read of `caller_skynet_token` and the route to the gate (3.6),
- the production host, firewall rules, the `svc-launch-gate` team member, and the rollout (5.1-5.5).

## Files

| File | Role |
|------|------|
| `compose.yml` | The base: one container, `restart: always`, the HTTPS port on `${LAUNCH_GATE_BIND:-127.0.0.1}:${LAUNCH_GATE_PORT:-8443}`, read-only mounts of `./app`, `./config.json` and `./certs`, `read_only`, `cap_drop: ALL`, `no-new-privileges`. |
| `compose.prod.yml` | Production overlay, loaded by `deploy.sh` outside `LOCAL_MODE`: `user: "1000:1000"` and `userns_mode: keep-id:uid=1000,gid=1000`, so the non-root process reads the 0600 TLS key. |
| `compose.local.yml` | Local-dev overlay (`LOCAL_MODE=true`, appended by `platform/lib/common.sh`): memory cap, `label=disable`, the `local-dev` network. It does not republish ports. |
| `deploy.sh` | Container lifecycle only. Checks the inputs exist, pulls, starts with `compose_up_if_changed app config.json certs/step-ca-bundle.crt`, waits for the container to be running. It generates no secret. |
| `templates/env.j2` | Renders `.env`: the image, bind and port, the Semaphore token, one caller token per caller. |
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

## Allowlist

`launch_gate_allowlist(catalog_doc, templates_doc, agents)` renders the list at deploy time:

1. For each agent, it takes `catalog.<agent>.allowed_templates` and `catalog.<agent>.service_deploy_templates`
   from OPA's `policies/agentcloud/data.json`. The gate's list only narrows what reaches OPA. It adds nothing
   OPA does not name.
2. It removes every name in `catalog.semaphore.destructive_templates` and every name that starts with
   `Clean Deploy`.
3. For each remaining name, it adds `<name> (Dev)` when `platform/semaphore/templates.yml` sets
   `dev_variant: true` for it.

It fails closed: it raises `AnsibleFilterError` on an empty agent list, an agent not in the catalog, an agent with no
`allowed_templates` list, a remaining name that `templates.yml` does not declare (catalog drift), or a template
name declared twice. The deploy playbook calls it in Phase 1 without a rescue, so the error stops the run before the
repo is placed.

## Do not

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
