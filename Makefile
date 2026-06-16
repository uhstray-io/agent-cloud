# agent-cloud — local-dev entry points (plan/development/LOCAL-DEV-DEPLOYMENT.md)
# "make bootstraps, Semaphore operates."

LOCAL_DEV := scripts/local-dev.sh

.PHONY: help local-preflight local-init local-bootstrap local-up local-all local-creds local-validate local-dns local-dns-resolver local-https local-https-down local-tls-trust local-tls-untrust local-clean promote
.PHONY: local-deploy-% local-clean-deploy-%

help: ## Show available targets
	@grep -E '^[a-zA-Z%-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

local-preflight: ## Verify toolchain (Brewfile) + podman machine
	@$(LOCAL_DEV) preflight

local-init: ## Create the gitignored working inventory from the committed example
	@$(LOCAL_DEV) init

local-bootstrap: ## Genesis: OpenBao + secure foundation (dns,step-ca,caddy,authentik) + OIDC-secured Semaphore (idempotent)
	@$(LOCAL_DEV) bootstrap

# Full-stack bring-up (all idempotent — safe to re-run):
#   GENESIS (make local-bootstrap, §12A): OpenBao → the secure foundation
#     (dns → step-ca → caddy → authentik, Mac-direct) → Semaphore LAST, already
#     OIDC-secured. Everything genesis stands up directly (it's the sanctioned
#     Rule #1 bootstrap exemption — a service can't deploy through an
#     orchestrator that isn't up yet).
#   TIER 3 (here): o11y, OPA, ERPNext, NetBox, n8n — deployed THROUGH Semaphore,
#     each needs Caddy routing + Authentik for SSO, so they come after genesis.
# n8n is best-effort (leading '-'): its image registry can rate-limit pulls;
# a miss there must not abort the rest of the stack. Host-only steps that need
# sudo (resolver / TLS-trust / :443 forwarder) are NOT here so local-up stays
# sudo-free for CI/scripts — `make local-all` chains them after the stack.
local-up: ## Full stack: genesis (bootstrap) then Tier-3 services through Semaphore (idempotent)
	@$(MAKE) --no-print-directory local-bootstrap
	@$(MAKE) --no-print-directory local-deploy-o11y
	@$(MAKE) --no-print-directory local-deploy-opa
	@$(MAKE) --no-print-directory local-deploy-erpnext
	@$(MAKE) --no-print-directory local-netbox
	-@$(MAKE) --no-print-directory local-deploy-n8n
	@echo "[local-up] full stack up: foundation via genesis, Tier-3 via Semaphore."

# One-shot in dependency order: the full stack PLUS the host-side wiring that
# needs sudo (macOS DNS resolver + internal-CA trust). local-up stays sudo-free;
# local-all is the human "bring everything up and make *.agent-cloud.test work in
# my browser" command. Order matters: the stack (genesis deploys dns + step-ca +
# caddy) must exist before the resolver points at local DNS and before the CA root
# can be trusted. --yes skips the per-step y/N (sudo still prompts for a password
# once); both host steps are idempotent — no-op when already correct, and re-trust
# the CURRENT step-ca root after a cold rebuild minted a new one. The persistent
# :443 forwarder (make local-https) stays opt-in — :8443 works without it.
local-all: ## EVERYTHING in dependency order: full stack + macOS DNS resolver + internal-CA trust (asks for sudo)
	@$(MAKE) --no-print-directory local-up
	@$(LOCAL_DEV) resolver --yes
	@$(LOCAL_DEV) tls-trust --yes
	@echo "[local-all] stack up + host wiring done — browse https://<svc>.agent-cloud.test:8443 (port-free :443: make local-https)."
	@$(LOCAL_DEV) creds

local-creds: ## Show the Authentik admin login (read live from OpenBao) for browser SSO testing
	@$(LOCAL_DEV) creds

local-deploy-%: ## Deploy a service through the LOCAL Semaphore (e.g. make local-deploy-uhhcraft)
	@$(LOCAL_DEV) deploy $*

local-clean-deploy-%: ## DESTRUCTIVE: wipe a service's containers+volumes, then redeploy (e.g. make local-clean-deploy-dns)
	@$(LOCAL_DEV) clean-deploy $*

local-validate: ## Run Validate All through the LOCAL Semaphore
	@$(LOCAL_DEV) validate

local-smoke: ## Smoke-test the live local stack (control plane, DNS, Caddy/TLS); --full adds lint+BATS
	@bash scripts/local-smoke.sh $(ARGS)

local-netbox: ## Bring up the NetBox app tier under podman (NETBOX-LOCAL-ENGINE; idempotent)
	@bash scripts/local-netbox-up.sh

local-netbox-discover: ## Discover the running agent-cloud containers into NetBox (idempotent)
	@bash scripts/local-netbox-discover.sh

local-dns: ## Bring local DNS fully online: deploy hickory + wire macOS resolver (idempotent)
	@$(LOCAL_DEV) deploy dns
	@$(LOCAL_DEV) resolver

local-dns-resolver: ## Point macOS /etc/resolver/<zone> at the local DNS (sudo; idempotent, re-runnable)
	@$(LOCAL_DEV) resolver

local-https: ## Clean port-free https://app.agent-cloud.test via a persistent root forwarder (sudo; idempotent)
	@$(LOCAL_DEV) https

local-https-down: ## Remove the privileged-port forwarder (sudo)
	@$(LOCAL_DEV) https-down

local-tls-trust: ## Trust Caddy's local CA so *.agent-cloud.test has no cert warning (sudo; idempotent)
	@$(LOCAL_DEV) tls-trust

local-tls-untrust: ## Remove the trusted Caddy root CA (sudo)
	@$(LOCAL_DEV) tls-untrust

local-clean: ## Remove the local control plane (containers, volume, state)
	@$(LOCAL_DEV) clean

promote: ## Fast checks, push feature branch, open PR into dev
	@$(LOCAL_DEV) promote
