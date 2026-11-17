Author: Joseph A. Wisneski IV <stray@uhstray.io>

Task ids are permanent Plane keys (R3). To supersede a task, strike its id through, as in `~~1.2~~`, and the sync skips it as superseded. To add a task, give it a new id with a suffix, such as `1.2a`.

## 1. Plan, service and local-dev

- [x] 1.1 Write plan 26 and this change. (AC-769)
- [ ] 1.2 Service definition under platform/services/activepieces/deployment/. (AC-770)
      compose (app, worker, Postgres, Redis; restart always; only app publishes), compose.local.yml, deploy.sh, templates/env.j2, CLAUDE.md. Spikes O1 readiness, O2 worker token, O4 pgvector; pin image digests.
- [ ] 1.3 Secrets and deploy automation. (AC-771)
      secret/services/activepieces (D8) via manage-secrets with a new hex type (O3); deploy-activepieces.yml, clean-deploy-activepieces.yml; Semaphore templates with dev variants; OPA service_deploy_templates entry; validate-all play.
- [ ] 1.4 Close account creation after the owner. (AC-776)
      Deploy creates the owner (D10a); AP_ALLOW_OPEN_SIGN_UP refused by the template; verify that an uninvited sign-up is refused; spikes O5, O6.
- [ ] 1.5 Local-dev bring-up. (AC-772)
      Early spike O8: prove or refute one authenticated API call on Community at 0.93.0 (F27); a refuted result goes to Joseph, no workaround. Plan 28 orders the same spike first under AC-796; this task references it, it does not duplicate it.
      make local-deploy-activepieces; owner exists; health 200; one scheduled flow runs. Validation gate: scenarios "A scheduled flow runs on the local stack", "The deploy creates the owner before the port is reachable", "An uninvited sign-up is refused" and "A re-run reports no change" hold locally.

## 2. Production

- [ ] 2.1 Declare the VM in site-config. (AC-773)
      vm-specs entry, activepieces_svc inventory, NetBox address reservation; activepieces_owner_email and AP_FRONTEND_URL host as instance values.
- [ ] 2.2 Provision and harden the VM. (AC-774)
      Provision VM, service SSH key + site-config backup, Distribute SSH Keys, Harden SSH, Apply Firewall (SSH from the controller only).
- [ ] 2.3 Production deploy and firewall. (AC-778)
      Deploy Activepieces (Dev) dry run then real run; owner created; firewall admits the app port from the Caddy host only. No route yet.
- [ ] 2.4 Authentik forward-auth gate. (AC-775)
      Proxy-provider blueprint + app-catalog entry bound to platform-admins (precedent netbox-forward-auth.yaml); Deploy Authentik.
- [ ] 2.5 Edge. (AC-777)
      Caddy route with forward_auth (site-config caddy_managed_sites), DNS record in platform/infra/cloudflare/dns.tf, opened only after 2.3 and 2.4. Webhook exposure decision (D12, O7).
- [ ] 2.6 Boot persistence, health and host observability. (AC-779)
      Ensure/Verify Service Persistence, Verify Service Health, Instrument Host Observability.
- [ ] 2.7 Back Up and Restore Activepieces DB playbooks. (AC-780)
      Plane pattern: pg_dump into a 0700 directory; staging restore with swap; encryption key backed up with Back Up Credentials to site-config.
- [ ] 2.8 Validation gate. (AC-781)
      Scenarios "A platform-admin passes the gate", "A non-admin is refused at the edge", "A scheduled flow runs in production", "The deploy creates the owner before the port is reachable", "An uninvited sign-up is refused", "A direct connection from another LAN host is refused", "An undeclared webhook path stays gated", "A restore swaps in only a fully loaded copy", "A redeploy keeps the encryption key" and "A re-run reports no change" hold in production.
