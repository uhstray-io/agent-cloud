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
- **`check-discovery.yml` mutates state.** It writes site coordinates and
  tolerates query errors; do not advertise it as a read-only recovery gate.
- **Preserve build and configuration boundaries.** Keep the upstream
  `netbox-docker/` clone untouched. The Diode target, plugin username and mounted
  client secret must agree; policy `scope` is a sibling of `config`, device roles
  come from `discovery/roles.yaml`, and OID mappings from `discovery/snmp-extensions/`.

Source details and the deployment sequence are linked from [README.md](README.md).
