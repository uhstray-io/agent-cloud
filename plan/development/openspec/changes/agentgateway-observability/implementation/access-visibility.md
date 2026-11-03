# Read-only access-log configuration visibility

## Scope

Extend the existing Dev-bound `Verify agentgateway Runtime (Dev)` receipt to report
bounded metadata from the rendered, gitignored gateway config. This reports whether
`frontendPolicies.accessLog.otlp` is structurally present and whether its fixed
`service` and `signal` CEL fields match the reviewed access-log values. It does not
prove transport, receiver ingestion, Loki records, or which source revision rendered
the config. The receipt remains a sampling/runtime-input proof; it must not describe
access records as accepted from these metadata alone.

## Output contract

Add one fixed-shape `access_log_otlp` object:

```json
{
  "state": "present|absent|unknown",
  "service_field_matches": true,
  "signal_field_matches": false
}
```

The field-match values are booleans when the relevant structure is unambiguous, and
`null` when its structure is unknown. A clear absence reports `state: absent` and
both booleans `false`, without changing the existing sampling receipt's successful
status. Ambiguous structure reports `state: unknown`; it never becomes absence by
falling through a parser miss. The report includes no endpoint, identity expression,
raw config, request data, or secret.

When the checkout SHA differs from the requested SHA, the existing runtime playbook
may emit only the observed and expected 40-character SHAs plus a fixed comparison
category. That output is marked `observed_unverified`; it does not make the mismatch
acceptable and the exact-checkout assertion still fails. Invalid observed stdout is
reported as `unknown`, never echoed.

## Implemented source follow-up: runtime comparison refusal reasons

The read-only runtime-input guard now reports a fixed safe reason for its comparison,
without exposing the existing human-readable `RECREATE_REASON`, which can contain a
container name.

`deploy.sh` assigns a closed machine reason alongside the human reason for each current
comparison branch: missing/unavailable gateway state, stopped gateway, rendered-input
mismatch, project enumeration failure, running image mismatch, and readiness failure.
In verify-only mode it emits one `runtime-inputs-reason` marker with that fixed value
before the existing pass/refused marker. Comparison ordering, checks, exit status, and
lifecycle behavior stay unchanged. Earlier verification failures and older scripts
have no machine reason.

The controller-side Python command-boundary adapter is invoked with the fixed
`bash deploy.sh --verify-only` argv, playbook-selected working directory, and the
existing deployment environment. It bounds capture, duration, and child cleanup;
raw child output and exceptions never leave the adapter. Its closed JSON result carries
the return code, pass/refused classification, and an allowlisted reason. Acceptance
continues to require return code zero and the existing literal pass marker. Missing,
old, duplicate, malformed, or unknown reason markers become `diagnostic-unavailable`
for reporting, but do not reject a historically successful pass. Adapter failures,
timeouts, and output overflow cannot pass.

The visible playbook guard validates the adapter's closed result and includes only
the normalized fixed reason in a refusal message. It retains the existing read-only,
check-mode, and sampling-receipt gates; raw output, exception text, names, paths,
environment, and receipt metadata stay out of refusals. The existing fake-engine
deployment fixture exercises each comparison branch and proves the refusal path does
not pull or recreate containers. Adapter tests cover malformed reason markers,
legacy-script success, exit/marker contradictions, timeout, overflow, and output
non-disclosure; playbook projection checks the strict JSON shape and safe failure
summary.

Focused source checks passed on 2026-10-24: 71 pytest cases across the adapter, runtime
proof, and shared deploy-environment guard, plus all 20 change-aware BATS cases. This
improves future source-side diagnosis only. It cannot reconstruct the cause of a
historical refusal or prove deployed telemetry behavior; observability-estate task 5.7
remains open until its independent runtime checks and retention assessment pass.

## Parser and guardrails

Use the existing inspector's standard-library-only, exact-indentation approach. Scope
parsing to the single `frontendPolicies` block and recognize only the canonical
`accessLog` → `otlp` → `fields` → `add` mapping structure. Compare only the reviewed
fixed service and signal scalars. Duplicate keys, aliases, inline/scalar substitutions,
malformed values, wrong indentation, or multiple relevant blocks produce fixed
unknown metadata, not guessed absence. Preserve the image pin, sampling bounds,
reviewed checkout, clean-source, stable config digest, input-label, readiness, and
running-image checks unchanged.

## Verification

Extend `platform/tests/test_agentgateway_runtime_proof.py` with real Jinja renders for
the clear absent and present cases, fixed expected-field matches, mismatched field
values, duplicate and malformed/ambiguous block cases, and a non-disclosure assertion
for arbitrary config values. Keep the existing read-only playbook assertions and add
checks that the final receipt includes only the new fixed-shape metadata while both
protected inspector reads remain `no_log` and the before/after stability check stays
in place. Update the deployment README to distinguish config presence from record
delivery.
