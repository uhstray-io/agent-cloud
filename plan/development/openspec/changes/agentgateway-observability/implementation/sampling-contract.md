# Implementation plan: gateway trace sampling contract

## Scope

Implement OpenSpec task 2.1 only. Render random trace sampling at 10 percent by
default, keep the existing `(0, 0.1]` bound, and disable parent-based client
sampling with the literal `false`. Make the read-only rendered-runtime inspector
accept that contract and refuse numeric or enabled client sampling. Preserve the
independent OTLP log and trace switches.

## Files and checks

- Update `platform/services/agentgateway/deployment/templates/config.yaml.j2`.
- Update `platform/playbooks/files/inspect-agentgateway-runtime.py` and its
  focused runtime tests.
- Update the agentgateway BATS render assertion for traces-only and explicit
  sampling; retain the existing logs-only and no-traces cases.
- Record the source completion in this change's task and design receipt. Keep
  the live traceparent sampling drill (task 8.3) open until it is run through
  local Semaphore and its result is recorded.

## Acceptance

- An unset sampling value renders `randomSampling: 0.1` and `clientSampling: false`.
- An explicit `0.05` renders `randomSampling: 0.05` and
  `clientSampling: false`.
- The runtime inspector refuses random sampling outside `(0, 0.1]` and refuses
  any client sampling value other than boolean `false`.
- Logs-only continues to omit tracing; traces-only does not enable access-log
  OTLP.
- Focused Python, Ruff, and agentgateway BATS checks pass. No live sampling
  acceptance is claimed by source tests.
