Author: Joseph A. Wisneski IV <stray@uhstray.io>

Task ids are permanent Plane keys (R3). To supersede a task, strike its id through, as in `~~1.2~~`, and the sync skips it as superseded. To add a task, give it a new id with a suffix, such as `1.2a`.

## 1. Plan, service and local-dev

- [x] 1.1 Write plan 26 and this change. (AC-769)
- [ ] 1.2 Service definition under platform/services/activepieces/deployment/. (AC-770)
      Starts after the O8 spike (task 1.5 line). compose (app, worker, Postgres, Redis; restart always; only app publishes, on the declared bind), compose.local.yml, deploy.sh (reuses compose_up_if_changed and wait_for_healthy from platform/lib/common.sh, with a compose_staged_up hook as platform/services/plane/deployment/deploy.sh does), templates/env.j2, CLAUDE.md. Spike O1: an app healthcheck that proves migrations, so the worker can wait on service_healthy (platform/services/postiz/deployment/compose.yml lines 116 to 122); otherwise the labelled stopgap. Spikes O2 and O4. Pin image digests.
- [ ] 1.3 Secrets and deploy automation. (AC-771)
      secret/services/activepieces (D8) via manage-secrets. Add the hex type only if spike O3 shows Activepieces refuses a 32-character alphanumeric key. deploy-activepieces.yml and clean-deploy-activepieces.yml (the clean one imports tasks/clean-service.yml). One platform/semaphore/templates.yml entry per playbook with dev_variant: true, and no hand-written (Dev) copies. OPA service_deploy_templates entry; validate-all play.
- [ ] 1.4 Close account creation after the owner. (AC-776)
      Deploy creates the owner over loopback (D10b), with the email from the platform-admins roster key named by activepieces_owner. AP_ALLOW_OPEN_SIGN_UP refused by the template. Verify that an uninvited sign-up is refused. Spikes O5 and O6. The code ships in the task 1.3 pull request where it can; its live proof is the task 1.5 gate.
- [ ] 1.5 Local-dev bring-up. (AC-772)
      O8 spike, run once before task 1.2, or cite AC-796's recorded result: see plan 26, Risk: no API key on Community edition.
      make local-deploy-activepieces; owner exists; health 200; one scheduled flow runs. Validation gate: scenarios "A scheduled flow runs on the local stack", "The deploy creates the owner before the port is reachable", "An uninvited sign-up is refused", "A re-run reports no change", "A redeploy keeps the encryption key" and "A restore swaps in only a fully loaded copy" hold locally.

## 2. Production

- [ ] 2.1 Declare the VM in site-config. (AC-773)
      vm-specs entry, activepieces_svc inventory, NetBox address reservation; activepieces_publish_bind, activepieces_owner (a platform-admins roster key) and the Caddy group as instance values.
- [ ] 2.2 Provision and harden the VM. (AC-774)
      Provision VM, service SSH key + site-config backup, Distribute SSH Keys, Harden SSH, Apply Firewall with default-deny inbound (SSH from the controller only).
- [ ] 2.3 Production deploy and firewall. (AC-778)
      Deploy Activepieces (Dev), dry run then real run, in this order: compose up on the loopback bind; owner created over loopback if missing; declared activepieces_publish_bind rendered and compose up again. Apply Firewall with firewall_detect_ports false, admitting the app port from the Caddy group (firewall_allow_groups). Record the ordering evidence in the Semaphore task output. Run Probe Reachability (Dev) twice: from the Caddy group expecting the app port open, and from another LAN group expecting it closed. No route yet.
- [ ] 2.4 Authentik forward-auth gate. (AC-775)
      app-catalog entry tier: admin (platform-admins only), with blueprint activepieces-forward-auth.yaml reading the external host with !Env; Deploy Authentik. Open item O9 (the prod_required filter at platform/playbooks/deploy-authentik.yml line 243) is owned here.
- [ ] 2.5 Edge. (AC-777)
      Caddy route with forward_auth (site-config caddy_managed_sites), DNS record in platform/infra/cloudflare/dns.tf, opened only after 2.3 and 2.4. Webhook exposure decision (D12, O7).
- [ ] 2.6 Boot persistence, health and host observability. (AC-779)
      Ensure/Verify Service Persistence, Verify Service Health, Instrument Host Observability.
- [ ] 2.7 Back Up and Restore Activepieces DB playbooks. (AC-780)
      Extract the dump and staged-swap steps of backup-plane-db.yml and restore-plane-db.yml into shared tasks, parameterised by group, container, database, user, data services and a post-restore hook. Build the Activepieces playbooks on them. Needs task 2.3 (a running database), not the edge. Encryption key backed up with Back Up Credentials to site-config.
- [ ] 2.8 Validation gate. (AC-781)
      Scenarios "A platform-admin passes the gate", "A non-admin is refused at the edge", "A scheduled flow runs in production", "An undeclared webhook path stays gated", "A redeploy keeps the encryption key" (read the encryption_key KV version before and after the re-run), "A re-run reports no change", "An uninvited sign-up is refused" and "A restore swaps in only a fully loaded copy" (one real dump-and-restore from 2.7) hold in production. Cites task 2.3 for "The deploy creates the owner before the port is reachable" and "A direct connection from another LAN host is refused".
