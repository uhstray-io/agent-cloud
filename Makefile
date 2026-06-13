# agent-cloud — local-dev entry points (plan/development/LOCAL-DEV-DEPLOYMENT.md)
# "make bootstraps, Semaphore operates."

LOCAL_DEV := scripts/local-dev.sh

.PHONY: help local-preflight local-init local-bootstrap local-validate local-clean promote
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

local-clean: ## Remove the local control plane (containers, volume, state)
	@$(LOCAL_DEV) clean

promote: ## Fast checks, push feature branch, open PR into dev
	@$(LOCAL_DEV) promote
