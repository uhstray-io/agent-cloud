## Why

The current o11y rollout proves a receiver and a few named producers, but it does not establish logs, metrics, and traces across the services and infrastructure Agent Cloud supports. Expanding collection without a coverage inventory and measured resource budget could leave blind spots or exhaust the single o11y VM. This follow-on begins only after the current stack upgrades, dashboards, and production acceptance gates are complete.

## What Changes

- Establish a declared, reviewable coverage inventory for every supported service, agent, VM, and managed infrastructure target, distinguishing deployed from planned or retired components and recording each signal's owner, collection method, expected endpoint, and verification receipt.
- Extend the existing Alloy, Prometheus, Loki, Tempo, Grafana, and Semaphore paths for each target. Prefer native metrics and existing log discovery; use supported OpenTelemetry zero-code instrumentation for request-serving runtimes after compatibility and security checks. Add small manual spans only where automatic instrumentation cannot show a critical operation.
- Make cross-signal identity, bounded cardinality, trace sampling, retention, redaction, private ingress, and missing-telemetry alerts part of the reusable onboarding and verification contract.
- Add a receiver-host metric and log source first, then measure the production receiver's ingestion, CPU, memory, disk, query load, and headroom before each rollout wave. If the measured forecast exceeds the declared budget, review a private VM-spec change and converge it through Semaphore; disk growth additionally requires an idempotent guest-filesystem workflow before adding that wave.
- Roll out in reversible waves through reviewed `dev` code, private `site-config` declarations, and Dev-bound Semaphore automation. Preserve existing telemetry volumes during ordinary redeploy and validation.

## Capabilities

### New Capabilities

- `platform/estate-instrumentation`: complete, verifiable, bounded observability coverage for supported workloads and infrastructure.

### Modified Capabilities

None. The current store has no ratified observability capability spec; this change builds on the still-open `observability-estate` and `inference-telemetry-production` work rather than claiming those proposals are already complete.

## Impact

- Public repo: service/agent deployment declarations, shared collection and verification automation, o11y configuration, provisioned dashboards and alerts, and onboarding guidance.
- Private `site-config`: actual endpoint identities, network allowlists, per-host instrumentation settings, retention and VM sizing declarations. No production addresses or credentials enter this public change.
- Runtime: supported application containers; Proxmox VMs and hypervisor; network/DNS/CA/identity/secrets/automation services; container hosts; DGX inference; and external managed devices only where a safe, authorized exporter/API path exists. The first phase resolves deployed versus scaffolded entries before asserting coverage.
- Prerequisite: complete and verify the current o11y upgrade/dashboard/agentgateway work and its production receipt gates. This proposal does not deploy or resize anything.

## Rollback Plan

An operator reverts a target's declared instrumentation or scrape/OTLP route through the reviewed branch workflow and redeploys it through Semaphore; receiver configuration reverts without deleting volumes. Preserve prior VM sizing declarations when increasing resources; do not attempt disk shrink. For a failed wave, restore the last reviewed `dev` configuration and verify the previous signal and alert baseline before resuming.
