# agent-cloud — local-dev entry points (plan/development/LOCAL-DEV-DEPLOYMENT.md)
# "make bootstraps, Semaphore operates."

LOCAL_DEV := scripts/local-dev.sh

.PHONY: help local-preflight local-init local-bootstrap local-up local-validate local-dns local-dns-resolver local-https local-https-down local-tls-trust local-tls-untrust local-clean promote
.PHONY: local-deploy-% local-clean-deploy-%

help: ## Show available targets
	@grep -E '^[a-zA-Z%-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

local-preflight: ## Verify toolchain (Brewfile) + podman machine
	@$(LOCAL_DEV) preflight

local-init: ## Create the gitignored working inventory from the committed example
	@$(LOCAL_DEV) init

local-bootstrap: ## Stand up local OpenBao + Semaphore + templates (idempotent)
	@$(LOCAL_DEV) bootstrap

# Full-stack bring-up in DEPENDENCY ORDER (all idempotent — safe to re-run):
#   Tier 0  control plane : bootstrap (OpenBao + Semaphore) — everything else
#                           deploys THROUGH Semaphore, so it must exist first.
#   Tier 1  foundation    : DNS (name resolution) → step-ca (internal CA).
#   Tier 2  ingress + IdP : Caddy (needs step-ca's CA to mint the wildcard) →
#                           Authentik (IdP behind Caddy; SSO consumers need it).
#   Tier 3  services      : o11y, OPA, ERPNext, NetBox, n8n — each needs Caddy
#                           routing + Authentik for SSO, so they come last.
# n8n is best-effort (leading '-'): its image registry can rate-limit pulls;
# a miss there must not abort the rest of the stack. Host-only steps that need
# sudo (resolver / TLS-trust / :443 forwarder) are deliberately NOT here — run
# `make local-dns-resolver local-tls-trust local-https` once, separately.
local-up: ## Bring the FULL local stack up in dependency order (idempotent)
	@$(MAKE) --no-print-directory local-bootstrap
	@$(MAKE) --no-print-directory local-deploy-dns
	@$(MAKE) --no-print-directory local-deploy-step-ca
	@$(MAKE) --no-print-directory local-deploy-caddy
	@$(MAKE) --no-print-directory local-deploy-authentik
	@$(MAKE) --no-print-directory local-deploy-o11y
	@$(MAKE) --no-print-directory local-deploy-opa
	@$(MAKE) --no-print-directory local-deploy-erpnext
	@$(MAKE) --no-print-directory local-netbox
	-@$(MAKE) --no-print-directory local-deploy-n8n
	@echo "[local-up] full stack brought up in dependency order."

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
