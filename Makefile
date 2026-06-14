# agent-cloud — local-dev entry points (plan/development/LOCAL-DEV-DEPLOYMENT.md)
# "make bootstraps, Semaphore operates."

LOCAL_DEV := scripts/local-dev.sh

.PHONY: help local-preflight local-init local-bootstrap local-validate local-dns local-dns-resolver local-https local-https-down local-tls-trust local-tls-untrust local-clean promote
.PHONY: local-deploy-%

help: ## Show available targets
	@grep -E '^[a-zA-Z%-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

local-preflight: ## Verify toolchain (Brewfile) + podman machine
	@$(LOCAL_DEV) preflight

local-init: ## Create the gitignored working inventory from the committed example
	@$(LOCAL_DEV) init

local-bootstrap: ## Stand up local OpenBao + Semaphore + templates (idempotent)
	@$(LOCAL_DEV) bootstrap

local-deploy-%: ## Deploy a service through the LOCAL Semaphore (e.g. make local-deploy-uhhcraft)
	@$(LOCAL_DEV) deploy $*

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
