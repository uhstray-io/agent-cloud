# launch-gate — container deployment

The launch gate is the only identity that may ask Semaphore to launch a task on behalf of an agent. It is a
stdlib Python HTTPS service in `app/launch_gate.py`. An agent (skynet, later the Dispatch Console) calls
`POST /v1/launch` with its caller bearer token. The gate checks the token, that the caller may act as the
requested agent, and that the template is on the allowlist. It then asks OPA for a decision and creates the
Semaphore task only on an allow. Full design and the check order: `plan/development/openspec/changes/skynet-launch-containment/design.md`.

## Status: container files only

This directory holds the container files. It does not deploy anything by itself. Not in this directory yet:

- the deploy playbook (`deploy-launch-gate.yml`), its clean-deploy and Semaphore-access playbooks,
- the `templates.yml` entries,
- the `svc-launch-gate` Semaphore identity,
- the server leaf issuance and the renewal proof,
- the host firewall rules that admit the callers.

These are the later tasks of the `skynet-launch-containment` change (tasks 3.2 and 3.3). Until they land,
nothing here runs in any environment.

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
(`launch_gate_allowlist`).

## Image

The image is `${LAUNCH_GATE_IMAGE:?set launch_gate_image in the inventory}`. The design calls for an official
Python image pinned by digest. Nothing in this directory chooses it. The app's annotations (`str | None`) are
evaluated at import, so the image must be Python 3.10 or later. Whether the chosen image meets that, and has
`ssl` and `python` on its PATH, is unverified here. The playbook change must prove it.

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

It fails closed: it raises `ValueError` on an empty agent list, an agent not in the catalog, an agent with no
`allowed_templates` list, a remaining name that `templates.yml` does not declare (catalog drift), or a template
name declared twice. The playbook that calls it is a later change and must not catch the error.

## Do not

- Do not put a token in `config.json`, in a template outside the `secrets` namespace, or in any committed file.
  Templates use `secrets.*` and `_`-prefixed variables only. `test_credential_leaks.bats` is meant to check
  that, but its regex does not match `{{ name` written with a space, so `test_launch_gate_allowlist.py` checks
  the parsed templates instead.
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
- `platform/tests/test_launch_gate*.py` (existing): the app.
- `platform/tests/test_restart_policy.bats`, `test_compose_image_qualified.py`, `test_no_bind_mount_dir_delete.py`:
  repository-wide guards that also cover this directory.
