//! The static tier of the agentgateway MCP edge in the Cloudflare OpenTofu root (change
//! agentgateway-mcp, task 5.4; plan 24, Phase 4).
//!
//! Verifies the Cloudflare half of the MCP edge in `platform/infra/cloudflare`: the `mcp` DNS
//! record, the challenge skip and the one rate-limit rule, all scoped to the MCP host's exact
//! `/mcp` path, and the inference rules left as they were. Structural only: file reads, no tofu,
//! no Cloudflare call.
//!
//! Replaces the first six tests of `platform/tests/test_cloudflare_mcp_edge.bats`, which is
//! deleted. Its seventh test, `tofu fmt -check`, is dropped rather than ported (see the table).
//!
//! Each BATS `grep`, `sed`, `cut` and `awk` pipeline is reproduced in the helper it names: `rule`
//! is the `_rule` awk, `sed_range` is `sed -n '/a/,/b/p'`, and `refs` is `grep | cut -d'"' -f2`.
//! The `[ "$(...)" = ... ]` comparisons are exact string comparisons here too.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `cloudflare mcp: ONE definition names the MCP host and the exact /mcp path, and only those` | `mcp_endpoint_names_the_mcp_host_and_exact_path_only` |
//! | `cloudflare mcp: the challenge skip is the MCP expression only, appended last` | `mcp_challenge_skip_is_the_mcp_expression_only_and_appended_last` |
//! | `cloudflare mcp: the Plane MCP skip names its host and exact machine paths only, appended last` | `plane_mcp_skip_names_its_host_and_machine_paths_only_and_is_appended_last` |
//! | `cloudflare mcp: the inference skip rule and rate-limit rule are not widened to MCP` | `inference_rules_are_not_widened_to_mcp` |
//! | `cloudflare mcp: exactly one MCP rate-limit rule, log-first, its own bucket, two rules on the zone (Pro cap)` | `exactly_one_mcp_rate_limit_rule_log_first_with_its_own_bucket` |
//! | `cloudflare mcp: the mcp record is a proxied platform subdomain` | `mcp_record_is_a_proxied_platform_subdomain` |
//! | `cloudflare mcp: the HCL is formatted (tofu fmt -check)` | dropped: never ran anywhere (BATS skipped it without tofu; no tofu in CI or on workstations); CI follow-up AC-953 |

use regex::Regex;

use agent_cloud_tests::text::{any_line, read};

const WAF: &str = "platform/infra/cloudflare/waf.tf";
const RATELIMIT: &str = "platform/infra/cloudflare/ratelimit.tf";
const DNS: &str = "platform/infra/cloudflare/dns.tf";

/// `grep -E pattern`: the matching lines of `text`, in order. Split at each `\n` as grep does, so a
/// `\r` before it stays part of its line (agent_cloud_tests::text).
fn matching_lines<'a>(text: &'a str, pattern: &str) -> Vec<&'a str> {
    let re = Regex::new(pattern).expect("a valid pattern");
    text.split_terminator('\n')
        .filter(|line| re.is_match(line))
        .collect()
}

/// `sed -n '/start/,/end/p'`: each range runs from a line `start` accepts through the next line
/// `end` accepts (the start line is not tested against `end`). The lines are joined with `\n`.
fn sed_range(text: &str, start: impl Fn(&str) -> bool, end: impl Fn(&str) -> bool) -> String {
    let mut printed = Vec::new();
    let mut on = false;
    for line in text.lines() {
        if on {
            printed.push(line);
            if end(line) {
                on = false;
            }
        } else if start(line) {
            printed.push(line);
            on = true;
        }
    }
    printed.join("\n")
}

/// The attributes of one rule, as the BATS `_rule` awk printed them: from the line holding
/// `ref         = "<reference>"` through the first line that is exactly the rule's closing brace
/// (four spaces, optional comma). Empty when the reference is absent, as awk printed nothing.
fn rule(rel: &str, reference: &str) -> String {
    let needle = format!("ref         = \"{reference}\"");
    let text = read(rel);
    let mut printed = Vec::new();
    let mut on = false;
    for line in text.lines() {
        if line.contains(needle.as_str()) {
            on = true;
        }
        if on {
            printed.push(line);
            if line == "    }" || line == "    }," {
                break;
            }
        }
    }
    printed.join("\n")
}

/// `grep -E '^      ref +=' <file> | cut -d'"' -f2`: the rule references of `rel`, in file order.
fn refs(rel: &str) -> Vec<String> {
    let text = read(rel);
    matching_lines(&text, r"^      ref +=")
        .into_iter()
        .map(|line| line.split('"').nth(1).unwrap_or("").to_owned())
        .collect()
}

#[test]
fn mcp_endpoint_names_the_mcp_host_and_exact_path_only() {
    let ratelimit = read(RATELIMIT);
    let endpoint = matching_lines(&ratelimit, r"^  mcp_endpoint = ").join("\n");
    assert!(
        !endpoint.is_empty(),
        "{RATELIMIT}: no `mcp_endpoint` definition"
    );
    assert!(endpoint.contains(r#"http.host eq \"mcp.${var.zone_name}\""#));
    assert!(endpoint.contains(r#"http.request.uri.path eq \"/mcp\""#));
    // Exact match: no prefix operator that would also take /mcp/x, /sse or /v1.
    assert!(!endpoint.contains("starts_with"));
    assert!(!endpoint.contains("contains"));
    assert!(!endpoint.contains("inference"));
}

#[test]
fn mcp_challenge_skip_is_the_mcp_expression_only_and_appended_last() {
    let skip = rule(WAF, "mcp-api-bypass-challenge");
    assert!(!skip.is_empty(), "{WAF}: no mcp-api-bypass-challenge rule");
    assert!(any_line(
        &skip,
        r#"^[[:space:]]*action[[:space:]]+= "skip"$"#
    ));
    assert!(any_line(
        &skip,
        r"^[[:space:]]*expression[[:space:]]+= local\.mcp_endpoint$"
    ));
    assert!(skip.contains(r#"phases   = ["http_request_sbfm"]"#));
    // Appended after every earlier rule, so none of them moved (a list reorder is a plan diff): the
    // only rule after it is the Plane MCP skip, which was appended later still.
    let refs = refs(WAF);
    let after: Vec<&str> = refs
        .windows(2)
        .filter(|w| w[0].as_str() == "mcp-api-bypass-challenge")
        .map(|w| w[1].as_str())
        .collect();
    assert_eq!(after.join("\n"), "plane-mcp-bypass-challenge");
}

#[test]
fn plane_mcp_skip_names_its_host_and_machine_paths_only_and_is_appended_last() {
    let plane = rule(WAF, "plane-mcp-bypass-challenge");
    assert!(
        !plane.is_empty(),
        "{WAF}: no plane-mcp-bypass-challenge rule"
    );
    assert!(any_line(
        &plane,
        r#"^[[:space:]]*action[[:space:]]+= "skip"$"#
    ));
    assert!(plane.contains(r#"phases   = ["http_request_sbfm"]"#));
    let expression = matching_lines(&plane, r"^[[:space:]]*expression[[:space:]]+=").join("\n");
    assert!(expression.contains(r#"http.host eq \"plane-mcp.uhstray.io\" and ("#));
    // The machine paths, exact: both MCP endpoints and the token and registration endpoints; the
    // protected-resource and authorization-server documents by prefix, so the SDK's path-inserted
    // lookups reach the origin (PR 866 review).
    assert!(expression.contains(
        r#"http.request.uri.path in {\"/http/mcp\" \"/http/api-key/mcp\" \"/token\" \"/register\" \"/http/token\" \"/http/register\"}"#
    ));
    assert!(expression.contains(
        r#"starts_with(http.request.uri.path, \"/.well-known/oauth-protected-resource\")"#
    ));
    assert!(expression.contains(
        r#"starts_with(http.request.uri.path, \"/.well-known/oauth-authorization-server\")"#
    ));
    // Claude Code resolves the endpoints and documents relative to the issuer `/http` (2026-11-07).
    assert!(expression.contains(r#"starts_with(http.request.uri.path, \"/http/.well-known/\")"#));
    // The browser step stays challenged, and no broader prefix opens the host.
    assert!(!expression.contains("/authorize"));
    assert_eq!(expression.matches("starts_with").count(), 3);
    assert!(!expression.contains("contains("));
    // Appended, so no existing rule moved: the rules up to and including this one are exactly the
    // ones that predate it. Mirrors `sed -n '1,/^plane-mcp-bypass-challenge$/p' | tr '\n' ' '`.
    let refs = refs(WAF);
    let end = refs
        .iter()
        .skip(1)
        .position(|r| r == "plane-mcp-bypass-challenge")
        .map_or(refs.len(), |i| i + 2);
    let prefix: String = refs[..end].iter().map(|r| format!("{r} ")).collect();
    assert_eq!(
        prefix,
        "18ca3c2e79b845c59acb2148f295c07e 882488a5afd543eebc0037d22fcb1754 [CF AI Audit] \
         48c8a0a15cc64245a27285d298db0f32 semaphore-api-bypass-challenge o11y-api-bypass-challenge \
         inference-api-bypass-challenge authentik-oidc-bypass-challenge mcp-api-bypass-challenge \
         plane-mcp-bypass-challenge "
    );
}

#[test]
fn inference_rules_are_not_widened_to_mcp() {
    let skip = rule(WAF, "inference-api-bypass-challenge");
    assert!(
        !skip.is_empty(),
        "{WAF}: no inference-api-bypass-challenge rule"
    );
    assert!(skip.contains(
        r#"http.host eq \"inference.uhstray.io\" and (starts_with(http.request.uri.path, \"/v1/\") or http.request.uri.path eq \"/health\")"#
    ));
    assert!(!any_line(&skip, "(?i)mcp"));
    let limit = rule(RATELIMIT, "inference-api-ratelimit-block");
    assert!(
        !limit.is_empty(),
        "{RATELIMIT}: no inference-api-ratelimit-block rule"
    );
    assert!(any_line(
        &limit,
        r"^[[:space:]]*expression[[:space:]]+= local\.inference_v1$"
    ));
    assert!(any_line(
        &limit,
        r"^[[:space:]]*ratelimit[[:space:]]+= local\.inference_bucket$"
    ));
    assert!(!any_line(&limit, "(?i)mcp"));
    // The inference host set and its bucket, as locals, name no MCP host.
    let ratelimit = read(RATELIMIT);
    let locals = matching_lines(
        &ratelimit,
        r"^  (inference_host|inference_admin_host|inference_v1) ",
    );
    assert_eq!(locals.len(), 3, "the inference locals: {locals:?}");
    assert!(
        !locals
            .iter()
            .any(|line| line.to_lowercase().contains("mcp"))
    );
}

#[test]
fn exactly_one_mcp_rate_limit_rule_log_first_with_its_own_bucket() {
    let limit = rule(RATELIMIT, "mcp-api-ratelimit-log");
    assert!(
        !limit.is_empty(),
        "{RATELIMIT}: no mcp-api-ratelimit-log rule"
    );
    // Observe only until agentgateway-mcp task 6.6 measures a real handshake: the bucket is
    // provisional.
    assert!(any_line(
        &limit,
        r#"^[[:space:]]*action[[:space:]]+= "log"$"#
    ));
    assert!(!any_line(&limit, r#""block""#));
    assert!(any_line(
        &limit,
        r"^[[:space:]]*expression[[:space:]]+= local\.mcp_endpoint$"
    ));
    assert!(any_line(
        &limit,
        r"^[[:space:]]*ratelimit[[:space:]]+= local\.mcp_bucket$"
    ));
    // The API accepts `logging` only on skip rules (error 20018); a rate-limit rule must not carry
    // it.
    assert!(!any_line(&limit, "logging"));
    let ratelimit = read(RATELIMIT);
    assert_eq!(
        matching_lines(&ratelimit, r#"ref *= "mcp-api-ratelimit"#).len(),
        1
    );
    // The zone is on the Pro tier: two http_ratelimit rules at most. A third would fail at apply.
    assert_eq!(refs(RATELIMIT).len(), 2);
    // A bucket of its own, with the IP characteristic the plan allows and a legal period.
    let bucket = sed_range(
        &ratelimit,
        |line| line.starts_with("  mcp_bucket = {"),
        |line| line.starts_with("  }"),
    );
    assert!(!bucket.is_empty(), "{RATELIMIT}: no mcp_bucket definition");
    assert!(bucket.contains(r#"characteristics     = ["cf.colo.id", "ip.src"]"#));
    assert!(any_line(
        &bucket,
        r"^[[:space:]]*period[[:space:]]+= (10|60)$"
    ));
}

#[test]
fn mcp_record_is_a_proxied_platform_subdomain() {
    let dns = read(DNS);
    let subdomains = sed_range(
        &dns,
        |line| line.contains("platform_subdomains = ["),
        |line| line.starts_with("  ]"),
    );
    assert!(!subdomains.is_empty(), "{DNS}: no platform_subdomains list");
    assert!(any_line(&subdomains, r#"^[[:space:]]*"mcp","#));
    // The record resource builds name = <label>.<zone> and proxies every platform name.
    let record = sed_range(
        &dns,
        |line| line.contains(r#"resource "cloudflare_dns_record" "platform""#),
        |line| line.starts_with('}'),
    );
    assert!(any_line(&record, r"^[[:space:]]*proxied = true$"));
    assert!(record.contains(r#"name    = "${each.key}.${var.zone_name}""#));
}
