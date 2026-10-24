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
