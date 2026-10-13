# NetBox deployment — agent guidance

Read [README.md](README.md) with the root [AGENTS.md](../../../../AGENTS.md).
`AGENTS.md` in this directory links to this file; maintain one copy of the rules.

## Platform boundaries

- Deploy and operate through Semaphore. `deploy-netbox.yml` owns the five-phase
  service deployment; `deploy-orb-agent.yml` owns the separate discovery agent.
- OpenBao owns credentials. Ansible renders env/config files; `deploy.sh` does
  not generate them. Do not restore the removed `lib/generate-secrets.sh` flow.
- Keep real addresses, upstream credentials and discovery scope in private
  site-config/OpenBao. Never publish rendered files or credential-bearing logs.
- Preserve volumes for routine updates. Clean deployment is destructive and
  needs explicit authorization.

## Runtime and validation

The production path defaults to Docker in `lib/common.sh`. An explicit
`CONTAINER_ENGINE` is accepted, but container names still use a hardcoded hyphen
separator there; this is not automatic runtime detection or proof that every
Podman path works. Do not describe NetBox itself as universally incompatible
with Podman.

A local app-only Podman script exists. The deployable local platform does not
currently have NetBox configured on Docker; production is the current validation
target. Local app readiness cannot prove the privileged, host-network discovery
path or production ingestion. This documentation establishes no recovery result.

## Change-aware deploy

`deploy.sh` recreates the stack only when its inputs changed (`lib/common.sh`,
`netbox_inputs_digest` and `netbox_recreate_reason`; NetBox keeps its own compose
wrapper, so it does not use `compose_up_if_changed` from `platform/lib/`). An
unchanged re-run must stop at the `DEPLOY_CHANGED=false` line before anything is
stopped, built or removed: the standalone orb-agent is not a compose service,
`stop_orb_agent` removes it, and nothing in this workflow starts it again.

- A new bind-mounted config file, a new environment-read compose variable, or a
  new image build input belongs in the digest. One left out changes without the
  containers being recreated.
- Do not add the orb-agent-only `.env` keys to the digest. `ORB_AGENT_CLIENT_ID` and
  `ORB_AGENT_CLIENT_SECRET` are rewritten by the credential refresh after a changed
  deploy, so the next run would look changed. `SNMP_COMMUNITY` is not rewritten by it;
  it is left out because no compose service reads it (only the standalone orb-agent
  start does), so a change would recreate the stack for nothing.
- Every `compose up` after the decision carries `.deploy-inputs-label.yml` through
  `COMPOSE_OVERLAYS`; one started without it is recreated by the next run.
- `netbox_force_rebuild` / `deploy_force_recreate` (literal `true`) are the operator
  levers. A changed deploy stops the orb-agent; the operator runs Deploy Orb Agent.
  Either branch of `deploy.sh` prints `ORB_AGENT_RUNNING=false` when no orb-agent is
  running, and `deploy-netbox.yml` shows it as a warning.
- Never delete or replace a bind-mounted directory under a running stack
  (`platform/tests/test_no_bind_mount_dir_delete.py` covers this service).
  `deploy.sh` forces a recreate when it replaces the `netbox-docker/` clone.

## Responsibilities that must remain separate

| Entry point | Owns |
|---|---|
| `deploy.sh` | Rendered-file checks, upstream base/image, staged infrastructure startup and readiness |
| `post-deploy.sh` | Application migrations, superuser and OAuth2 setup, discovery-service restart and checks |
| Ansible Diode credential task | Create/verify credentials and persist them in OpenBao |
| `deploy-orb-agent.yml` | Agent template, scoped AppRole and privileged container with workers |

The current compose bridge includes an on-disk Diode client-secret mount under
`secrets/`. It is generated from OpenBao, not an independent secret store. Its
task label promises cleanup that is not implemented; do not claim that the
mount source is deleted after startup.

## Common mistakes

- **A green login check is not recovered discovery.** Verify collection,
  Diode ingestion/reconciliation and resulting records with freshness evidence.
- **Vault reads are path-scoped.** One successful read does not exclude an ACL
  failure at another discovery path; "missing credential" needs evidence.
- **The deployed template controls schedules and workers.** `agent.yaml.j2`
  uses runtime `vault://` references and schedules pfSense/Proxmox workers.
  Legacy helpers in `lib/common.sh` do not describe this deployment path.
- **Never pair `fast_mode` with `ports` in a `network_discovery` scope.** `fast_mode`
  adds nmap `-F`, which nmap refuses beside an explicit port list, so the scan exits 1
  on every run and NetBox stays empty (`tests/test_discovery_scan_flags.py`).
- **Verify the checker revision.** The current source replaces GPS writes with
  bounded read-only evidence and refuses recovery acceptance. An older installed
  revision may still mutate coordinates. Aggregate counts and submission messages
  cannot establish complete collection or reconciliation.
- **Preserve build and configuration boundaries.** Keep the upstream
  `netbox-docker/` clone untouched. The Diode target, plugin username and mounted
  client secret must agree; policy `scope` is a sibling of `config`, device roles
  come from `discovery/roles.yaml`, and OID mappings from `discovery/snmp-extensions/`.

Source details and the deployment sequence are linked from [README.md](README.md).
