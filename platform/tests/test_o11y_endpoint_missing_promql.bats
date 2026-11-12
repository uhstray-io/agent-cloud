#!/usr/bin/env bats

@test "endpoint missing-data rule is proven by native PromQL evaluation over synthetic series" {
    run python3 "${BATS_TEST_DIRNAME}/o11y_endpoint_missing_promql_runner.py"
    if [ "$status" -ne 0 ]; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$output"
    printf '%s\n' "$output" | grep -q '^PASS: rendered missing-data rule fires only for absent series not explained by a down scrape, including when up is absent$'
    printf '%s\n' "$output" | grep -q '^KILLED: no-scrape-suppression mutant failed during PromQL evaluation (expected_result_mismatch)$'
    printf '%s\n' "$output" | grep -q '^KILLED: instant-absent mutant failed during PromQL evaluation (expected_result_mismatch)$'
}
