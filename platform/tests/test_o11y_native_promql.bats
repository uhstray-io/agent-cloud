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

@test "the KV pool trial stop-line rule is proven by native PromQL evaluation over one day of synthetic vLLM series" {
    run python3 "${BATS_TEST_DIRNAME}/o11y_kv_pool_trial_promql_runner.py"
    if [ "$status" -ne 0 ]; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$output"
    printf '%s\n' "$output" | grep -q '^PASS: rendered KV pool trial stop line fires on each branch alone and on a zero hit ratio on a busy day, and stays silent when healthy and on a quiet day$'
    printf '%s\n' "$output" | grep -q '^KILLED: no-count-wrap mutant failed during PromQL evaluation (expected_result_mismatch)$'
    printf '%s\n' "$output" | grep -q '^KILLED: no-prompt-token-condition mutant failed during PromQL evaluation (expected_result_mismatch)$'
}
