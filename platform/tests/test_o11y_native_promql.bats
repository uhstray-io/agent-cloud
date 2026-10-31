#!/usr/bin/env bats

@test "native PromQL timestamp queries preserve family labels and source times" {
    run python3 "${BATS_TEST_DIRNAME}/o11y_native_promql_runner.py"
    if [ "$status" -ne 0 ]; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$output"
    printf '%s\n' "$output" | grep -q '^PASS: repaired production query preserved all expected families, labels, and raw timestamps$'
    printf '%s\n' "$output" | grep -q '^KILLED: grouped mutant failed during PromQL evaluation '
    printf '%s\n' "$output" | grep -q '^KILLED: plain-or mutant failed during PromQL evaluation '
}
