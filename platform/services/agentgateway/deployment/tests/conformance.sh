#!/usr/bin/env bash
# conformance.sh — send the same requests to the inference gateway and to vLLM directly, and
# compare what comes back (change inference-gateway-agentgateway, tasks 2.1 and 2.2).
#
# Run on the gateway VM by platform/playbooks/run-agw-conformance.yml (Semaphore template
# "Run agentgateway Conformance"), which places this file and the two key files in a 0700
# temporary directory, runs it, reads the results and removes the directory.
#
#   conformance.sh run                 send every case to both targets; write results.jsonl
#   conformance.sh diff <results.jsonl> [<model-map.json>]
#                                      compare the two targets case by case; print one JSON report
#
# Cases (spec "Client-visible contract is unchanged through the gateway"):
#   models              GET /models
#   chat-thinking-off   chat completion with chat_template_kwargs {"enable_thinking": false}
#   effort-<value>      one chat completion per reasoning_effort value: none minimal low medium
#                       high xhigh max (the seven values vLLM's request schema advertises; the
#                       dgx-spark endpoint maps high/max to xhigh and minimal to low)
#   chat-template-kwargs chat completion with chat_template_kwargs {"reasoning_effort": "low"}
#   tool-call           chat completion offering one function tool
#   stream-xhigh        streamed chat completion at reasoning_effort xhigh: time to first token
#                       and the gaps between chunks
#   responses           one Responses API request (POST /responses, reasoning.effort nested)
#
# Each case goes to the gateway, then straight to vLLM, from the same host, so the two differ only
# by the gateway hop. A result line records the status, a sha256 of the body with identifiers and
# timestamps removed and keys sorted, a sha256 of its shape (every leaf path and its JSON type), a
# few semantic fields (model, finish reason, whether content/reasoning/tool calls came back) and
# timings. It never records a body, a header or a key.
#
# Keys never reach a command line. They are read from files into this shell, and each curl call
# gets its Authorization header from `-H @<(printf ...)`: printf is a shell builtin, so the value
# exists only in this process and in a pipe curl reads, never in any process's argv.
#
# Environment (run):
#   AGW_CONF_OUT              existing directory for results.jsonl (required)
#   AGW_CONF_GATEWAY_URL      gateway base URL including /v1 (required)
#   AGW_CONF_GATEWAY_KEY_FILE file holding one enrolled client key (required)
#   AGW_CONF_GATEWAY_RESOLVE  optional curl --resolve entry name:port:address, so the URL can
#                             name the server leaf's SAN without a hosts line
#   AGW_CONF_GATEWAY_CERT, AGW_CONF_GATEWAY_CERT_KEY, AGW_CONF_GATEWAY_CA
#                             listener TLS: the client leaf, its key and the trust bundle; all
#                             three or none
#   AGW_CONF_DIRECT_URL       vLLM base URL including /v1 (required)
#   AGW_CONF_DIRECT_KEY_FILE  optional file holding the upstream key; unset or empty means the
#                             upstream takes none
#   AGW_CONF_MODEL            model name sent to the gateway (required)
#   AGW_CONF_DIRECT_MODEL     model id sent to vLLM (default AGW_CONF_MODEL)
#   AGW_CONF_TIMEOUT          per-request ceiling in seconds (default 600)
#   AGW_CONF_MAX_TOKENS       max_tokens for non-streamed cases (default 512)
#   AGW_CONF_STREAM_MAX_TOKENS max_tokens for the streamed case (default 2048)
#
# Needs bash, curl (7.55 or later, for -H @file), jq (1.6 or later) and sha256sum or shasum.
set -euo pipefail

EFFORTS="none minimal low medium high xhigh max"
PROMPT='What is 12*17? Answer with the number only.'

die() {
	printf 'conformance: %s\n' "$*" >&2
	exit 2
}

sha256() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum | cut -d' ' -f1
	else
		shasum -a 256 | cut -d' ' -f1
	fi
}

# Shared jq definitions. Volatile keys are dropped wherever they occur; keys are sorted so a
# gateway that re-serialises JSON in another order still hashes the same.
JQ_LIB='
def canon: walk(if type == "object"
  then with_entries(select(.key | IN("id", "created", "created_at", "completed_at",
         "system_fingerprint", "request_id", "response_id", "item_id") | not))
       | to_entries | sort_by(.key) | from_entries
  else . end);
def shape: [paths(if type == "object" or type == "array" then length == 0 else true end) as $p
  | ($p | map(if type == "number" then "[]" else tostring end) | join(".")) + ":" + (getpath($p) | type)]
  | unique;
def nonempty: type == "string" and length > 0;
def r3: if . == null then null else (. * 1000 | round) / 1000 end;
def err_semantic: {
  error_type: ((try .error.type catch null) // (try .type catch null)),
  error_code: ((try .error.code catch null) // (try .code catch null))};
def semantic_for($kind):
  if $kind == "models" then {object, ids: ([.data[]?.id] | sort)}
  elif $kind == "responses" then {object, status, model,
    output_types: ([.output[]?.type] | unique),
    has_output_text: ([.output[]? | select(.type == "message") | .content[]?
      | select(.type == "output_text") | .text | nonempty] | any)}
  else {object, model, finish_reason: .choices[0].finish_reason,
    has_content: (.choices[0].message.content | nonempty),
    has_reasoning: ((.choices[0].message.reasoning | nonempty)
      or (.choices[0].message.reasoning_content | nonempty)),
    reasoning_tokens_zero: (.usage.completion_tokens_details.reasoning_tokens
      | if . == null then null else . == 0 end),
    tool_calls: ([.choices[0].message.tool_calls[]?.function.name] | sort)}
  end;
def summarise_body($kind; $status):
  . as $raw | (try fromjson catch null) as $j
  | if $j == null then {norm: $raw, shape: ["nonjson"], semantic: {json: false}}
    else {norm: ($j | canon | tojson), shape: ($j | shape),
      semantic: (if $status >= 200 and $status < 300 then ($j | semantic_for($kind))
                 else ($j | err_semantic) end)}
    end;
def stats: sort as $s | ($s | length) as $n
  | if $n == 0 then {count: 0, max_s: null, p50_s: null, p95_s: null, mean_s: null}
    else {count: $n, max_s: ($s[-1] | r3), p50_s: ($s[(($n - 1) * 0.5) | floor] | r3),
      p95_s: ($s[(($n - 1) * 0.95) | floor] | r3), mean_s: (($s | add) / $n | r3)} end;
def token_event: any(.e.choices[]?; (.delta.content | nonempty) or (.delta.reasoning | nonempty)
  or (.delta.reasoning_content | nonempty));
'

# ── Request bodies ─────────────────────────────────────────────────────────────
# Deterministic where the server allows it (temperature 0, fixed seed), so the body comparison
# has a chance to be exact; the verdict rests on status, shape and semantics, not the hash.
body_for() { # case model -> JSON on stdout
	local case="$1" model="$2"
	local base
	base=$(jq -nc --arg m "$model" --arg p "$PROMPT" --argjson mt "$MAX_TOKENS" \
		'{model: $m, temperature: 0, seed: 7, max_tokens: $mt, messages: [{role: "user", content: $p}]}')
	case "$case" in
	chat-thinking-off) jq -nc --argjson b "$base" '$b + {chat_template_kwargs: {enable_thinking: false}}' ;;
	effort-*) jq -nc --argjson b "$base" --arg e "${case#effort-}" '$b + {reasoning_effort: $e}' ;;
	chat-template-kwargs) jq -nc --argjson b "$base" '$b + {chat_template_kwargs: {reasoning_effort: "low"}}' ;;
	tool-call)
		jq -nc --argjson b "$base" '$b + {reasoning_effort: "low", tool_choice: "auto",
			messages: [{role: "user", content: "What is the weather in Newark, NJ? Use the tool."}],
			tools: [{type: "function", function: {name: "get_weather",
				description: "Current weather for a city",
				parameters: {type: "object", required: ["city"], properties: {
					city: {type: "string"}, unit: {type: "string", enum: ["c", "f"]}}}}}]}'
		;;
	stream-xhigh)
		jq -nc --argjson b "$base" --argjson mt "$STREAM_MAX_TOKENS" '$b + {stream: true, reasoning_effort: "xhigh", max_tokens: $mt}'
		;;
	responses)
		jq -nc --arg m "$model" --arg p "$PROMPT" --argjson mt "$MAX_TOKENS" \
			'{model: $m, input: $p, reasoning: {effort: "low"}, max_output_tokens: $mt, temperature: 0}'
		;;
	*) die "no body for case $case" ;;
	esac
}

# ── HTTP ───────────────────────────────────────────────────────────────────────
# curl_target <target> <curl args...>: the target's base options and, when it has a key, the
# Authorization header from a process substitution (see the header comment).
curl_target() {
	local target="$1"
	shift
	local key
	if [ "$target" = gateway ]; then
		key="$GATEWAY_KEY"
		curl ${GW_OPTS[@]+"${GW_OPTS[@]}"} -H @<(printf 'Authorization: Bearer %s\n' "$key") "$@"
	elif [ -n "$DIRECT_KEY" ]; then
		key="$DIRECT_KEY"
		curl -H @<(printf 'Authorization: Bearer %s\n' "$key") "$@"
	else
		curl "$@"
	fi
}

base_url() { if [ "$1" = gateway ]; then printf '%s' "$GATEWAY_URL"; else printf '%s' "$DIRECT_URL"; fi; }
model_for() { if [ "$1" = gateway ]; then printf '%s' "$MODEL"; else printf '%s' "$DIRECT_MODEL"; fi; }

# emit <case> <target> <method> <path> <status> <curl_exit> <summary-file> <timing-json>
# The summary travels as a file: a streamed body's normalised form can exceed the size one
# command-line argument may have, and nothing body-sized ever goes on a command line.
emit() {
	local norm_sha shape_sha
	norm_sha=$(jq -r '.norm' "$7" | sha256)
	shape_sha=$(jq -c '.shape' "$7" | sha256)
	jq -c --arg case "$1" --arg target "$2" --arg method "$3" --arg path "$4" \
		--argjson status "$5" --argjson curl_exit "$6" --arg body "$norm_sha" --arg shape "$shape_sha" \
		--argjson timing "$8" \
		'{case: $case, target: $target, method: $method, path: $path, status: $status,
		  curl_exit: $curl_exit, body_sha256: $body, shape_sha256: $shape,
		  semantic: .semantic, timing: $timing}' "$7" >>"$RESULTS"
	printf '%-22s %-8s %s\n' "$1" "$2" "$5"
}

run_plain() { # case kind method path target
	local case="$1" kind="$2" method="$3" path="$4" target="$5"
	local body="$WORK/$case.$target.body" req="$WORK/$case.$target.req" w rc=0
	local -a data=()
	if [ "$method" = POST ]; then
		body_for "$case" "$(model_for "$target")" >"$req"
		data=(-H 'Content-Type: application/json' --data-binary "@$req")
	fi
	: >"$body"
	w=$(curl_target "$target" -sS -X "$method" --max-time "$TIMEOUT" -o "$body" \
		-w '%{http_code} %{time_starttransfer} %{time_total}' \
		${data[@]+"${data[@]}"} "$(base_url "$target")$path" 2>>"$WORK/curl.err") || rc=$?
	# -w prints "<code> <starttransfer> <total>", also when the connection failed (code 000).
	[ -n "$w" ] || w="000 0 0"
	local status="${w%% *}" rest="${w#* }"
	local ttfb="${rest%% *}" total="${rest#* }"
	status=$((10#${status:-0}))
	local timing
	jq -Rs -c --arg kind "$kind" --argjson status "$status" \
		"$JQ_LIB summarise_body(\$kind; \$status)" "$body" >"$body.sum"
	timing=$(jq -nc --argjson a "${ttfb:-0}" --argjson b "${total:-0}" "$JQ_LIB"'{ttfb_s: ($a | r3), total_s: ($b | r3)}')
	emit "$case" "$target" "$method" "$path" "$status" "$rc" "$body.sum" "$timing"
}

run_stream() { # case target
	local case="$1" target="$2"
	local req="$WORK/$case.$target.req" raw="$WORK/$case.$target.raw" hdr="$WORK/$case.$target.hdr"
	local t0 rc_curl rc_jq
	body_for "$case" "$(model_for "$target")" >"$req"
	: >"$raw"
	: >"$hdr"
	# One long-lived jq stamps every line as it arrives (`now`, microsecond resolution); curl -N
	# passes each chunk on unbuffered. Measured with a stub emitting every 0.3 s: the stamps
	# reproduce the cadence (platform/tests/test_agw_conformance.py).
	t0=$(jq -n now)
	set +e +o pipefail
	curl_target "$target" -sS -N --max-time "$TIMEOUT" -D "$hdr" -H 'Content-Type: application/json' \
		--data-binary "@$req" "$(base_url "$target")/chat/completions" 2>>"$WORK/curl.err" |
		jq -R -c --unbuffered '[now, .]' >"$raw"
	local -a st=("${PIPESTATUS[@]}")
	set -e -o pipefail
	rc_curl="${st[0]}"
	rc_jq="${st[1]}"
	[ "$rc_jq" -eq 0 ] || die "jq failed while reading the $target stream"
	local status
	status=$(tr -d '\r' <"$hdr" | awk '/^HTTP\//{s=$2} END{print s+0}')
	jq -s -c --argjson t0 "$t0" --argjson status "$status" "$JQ_LIB"'
		map(.[1] |= rtrimstr("\r")) as $lines
		| [$lines[] | select(.[1] | startswith("data:")) | {t: .[0], d: (.[1][5:] | ltrimstr(" "))}] as $data
		| ([$lines[] | select(.[1] | startswith(":"))] | length) as $ka
		| (($lines | last | .[0]) // $t0) as $tend
		| if ($status < 200 or $status >= 300) or ($data | length) == 0 then
			{summary: ($lines | map(.[1]) | join("\n") | summarise_body("stream"; $status)),
			 timing: {ttft_s: null, total_s: ($tend - $t0 | r3), keepalives: $ka, gaps: ([] | stats)}}
		  else
			[$data[] | select(.d != "[DONE]") | {t, e: (.d | try fromjson catch {_unparsed: true})}] as $ev
			| any($data[]; .d == "[DONE]") as $done
			| ([$ev[] | select(token_event) | .t] | first) as $ft
			| (if $ft == null then [] else [$ev[] | select(.t >= $ft) | .t] end) as $ts
			| {summary: {
				norm: (([$ev[].e | canon]) + (if $done then ["[DONE]"] else [] end) | tojson),
				shape: ([$ev[].e | shape] | add // [] | unique),
				semantic: {
					finish_reason: ([$ev[].e.choices[]?.finish_reason | select(. != null)] | last),
					has_content: any($ev[].e.choices[]?; .delta.content | nonempty),
					has_reasoning: any($ev[].e.choices[]?; (.delta.reasoning | nonempty)
						or (.delta.reasoning_content | nonempty)),
					done: $done,
					error_event: any($ev[]; .e.error != null)}},
			   timing: {
				ttft_s: (if $ft == null then null else ($ft - $t0 | r3) end),
				total_s: ($tend - $t0 | r3),
				keepalives: $ka,
				gaps: ([range(1; $ts | length) as $i | $ts[$i] - $ts[$i - 1]] | stats)}}
		  end' "$raw" >"$raw.out"
	jq -c '.summary' "$raw.out" >"$raw.sum"
	emit "$case" "$target" POST /chat/completions "$status" "$rc_curl" "$raw.sum" "$(jq -c '.timing' "$raw.out")"
}

read_key() { # file -> value on stdout (first line, no newline); never echoed elsewhere
	local v=""
	[ -r "$1" ] || die "key file $1 is not readable"
	IFS= read -r v <"$1" || [ -n "$v" ] || true
	printf '%s' "$v"
}

positive_int() { case "$2" in '' | *[!0-9]* | 0) die "$1 must be a positive integer" ;; esac }

cmd_run() {
	: "${AGW_CONF_OUT:?AGW_CONF_OUT is required}"
	: "${AGW_CONF_GATEWAY_URL:?AGW_CONF_GATEWAY_URL is required}"
	: "${AGW_CONF_GATEWAY_KEY_FILE:?AGW_CONF_GATEWAY_KEY_FILE is required}"
	: "${AGW_CONF_DIRECT_URL:?AGW_CONF_DIRECT_URL is required}"
	: "${AGW_CONF_MODEL:?AGW_CONF_MODEL is required}"
	[ -d "$AGW_CONF_OUT" ] || die "AGW_CONF_OUT $AGW_CONF_OUT is not a directory"
	TIMEOUT="${AGW_CONF_TIMEOUT:-600}"
	MAX_TOKENS="${AGW_CONF_MAX_TOKENS:-512}"
	STREAM_MAX_TOKENS="${AGW_CONF_STREAM_MAX_TOKENS:-2048}"
	positive_int AGW_CONF_TIMEOUT "$TIMEOUT"
	positive_int AGW_CONF_MAX_TOKENS "$MAX_TOKENS"
	positive_int AGW_CONF_STREAM_MAX_TOKENS "$STREAM_MAX_TOKENS"
	GATEWAY_URL="${AGW_CONF_GATEWAY_URL%/}"
	DIRECT_URL="${AGW_CONF_DIRECT_URL%/}"
	MODEL="$AGW_CONF_MODEL"
	DIRECT_MODEL="${AGW_CONF_DIRECT_MODEL:-$MODEL}"

	GW_OPTS=()
	[ -z "${AGW_CONF_GATEWAY_RESOLVE:-}" ] || GW_OPTS+=(--resolve "$AGW_CONF_GATEWAY_RESOLVE")
	local tls_set=0
	[ -z "${AGW_CONF_GATEWAY_CERT:-}" ] || tls_set=$((tls_set + 1))
	[ -z "${AGW_CONF_GATEWAY_CERT_KEY:-}" ] || tls_set=$((tls_set + 1))
	[ -z "${AGW_CONF_GATEWAY_CA:-}" ] || tls_set=$((tls_set + 1))
	case "$tls_set" in
	0) ;;
	3) GW_OPTS+=(--cert "$AGW_CONF_GATEWAY_CERT" --key "$AGW_CONF_GATEWAY_CERT_KEY" --cacert "$AGW_CONF_GATEWAY_CA") ;;
	*) die "AGW_CONF_GATEWAY_CERT, AGW_CONF_GATEWAY_CERT_KEY and AGW_CONF_GATEWAY_CA go together" ;;
	esac

	GATEWAY_KEY=$(read_key "$AGW_CONF_GATEWAY_KEY_FILE")
	[ -n "$GATEWAY_KEY" ] || die "the gateway key file is empty"
	DIRECT_KEY=""
	[ -z "${AGW_CONF_DIRECT_KEY_FILE:-}" ] || DIRECT_KEY=$(read_key "$AGW_CONF_DIRECT_KEY_FILE")

	RESULTS="$AGW_CONF_OUT/results.jsonl"
	: >"$RESULTS"
	umask 077
	WORK=$(mktemp -d "$AGW_CONF_OUT/work.XXXXXX")
	# Request and response bodies live only here, and only for the run, whatever ends it.
	trap 'rm -rf "$WORK"' EXIT

	local t e
	for t in gateway direct; do run_plain models models GET /models "$t"; done
	for t in gateway direct; do run_plain chat-thinking-off chat POST /chat/completions "$t"; done
	for e in $EFFORTS; do
		for t in gateway direct; do run_plain "effort-$e" chat POST /chat/completions "$t"; done
	done
	for t in gateway direct; do run_plain chat-template-kwargs chat POST /chat/completions "$t"; done
	for t in gateway direct; do run_plain tool-call chat POST /chat/completions "$t"; done
	for t in gateway direct; do run_stream stream-xhigh "$t"; done
	for t in gateway direct; do run_plain responses responses POST /responses "$t"; done
}

# ── 2.2: the comparison ────────────────────────────────────────────────────────
# A case MATCHES only when both targets succeeded (curl exit 0 and a 2xx status) and gave the same
# status, the same body shape and the same semantic fields. Either side failing is an "error"
# whose `failure` names the status (or the curl exit) on each side: two identical 401s are a
# failed run, not a conforming one (PR 409 review). The normalised-body hash is reported as
# exact_body_match but is not part of the verdict: generation is not guaranteed bit-identical
# between two requests even at temperature 0 with a seed. Timing deltas are gateway minus direct.
#
# Model names. The gateway serves each agw_models `name`, and the inventory's `upstream_model`
# may remap it to a different id at vLLM (local-dev: gpt-oss-20b -> openai/gpt-oss-20b). The
# optional second argument is that mapping as a JSON object, {"<gateway name>": "<upstream id>"},
# written by the playbook from inventory. Before comparing, a model name the gateway reports is
# translated through it, and the direct models list is narrowed to the declared upstream ids (vLLM
# may serve ids the gateway deliberately does not expose). Without a mapping, names compare as-is.
cmd_diff() {
	[ -r "${1:-}" ] || die "usage: conformance.sh diff <results.jsonl> [<model-map.json>]"
	local map='{}'
	if [ -n "${2:-}" ]; then
		[ -r "$2" ] || die "model map $2 is not readable"
		jq -e 'type == "object" and all(.[]; type == "string")' "$2" >/dev/null ||
			die "model map $2 must be a JSON object of strings"
		map=$(jq -c . "$2")
	fi
	jq -s -c --argjson map "$map" "$JQ_LIB"'
		def d($a; $b): if $a == null or $b == null then null else ($a - $b | r3) end;
		def up: . as $v | if ($v | type) == "string" and ($map | has($v)) then $map[$v] else $v end;
		($map | [.[]] | unique) as $declared
		| def gw_norm: if has("ids") then .ids |= (map(up) | sort) else . end
			| if has("model") then .model |= up else . end;
		def direct_norm: if has("ids") and ($declared | length) > 0
			then .ids |= map(select(. as $i | $declared | index($i))) else . end;
		def ok: .curl_exit == 0 and .status >= 200 and .status < 300;
		def how: if .curl_exit != 0 then "curl exit \(.curl_exit)" else "HTTP \(.status)" end;
		group_by(.case)
		| map(
			(map(select(.target == "gateway")) | first) as $g
			| (map(select(.target == "direct")) | first) as $d
			| {case: .[0].case}
			+ if $g == null or $d == null then {verdict: "incomplete"}
			  else ($g.semantic | gw_norm) as $gs | ($d.semantic | direct_norm) as $ds
			  | {
				status: {gateway: $g.status, direct: $d.status},
				status_match: ($g.status == $d.status),
				shape_match: ($g.shape_sha256 == $d.shape_sha256),
				semantic_match: ($gs == $ds),
				exact_body_match: ($g.body_sha256 == $d.body_sha256),
				semantic_diff: [($gs + $ds) | keys[] as $k
					| select($gs[$k] != $ds[$k])
					| {key: $k, gateway: $gs[$k], direct: $ds[$k]}],
				timing_delta: {
					total_s: d($g.timing.total_s; $d.timing.total_s),
					ttft_s: d($g.timing.ttft_s; $d.timing.ttft_s),
					gap_p95_s: d($g.timing.gaps.p95_s; $d.timing.gaps.p95_s),
					gap_max_s: d($g.timing.gaps.max_s; $d.timing.gaps.max_s)}}
				| if ($g | ok) and ($d | ok) then
					.verdict = (if .status_match and .shape_match and .semantic_match then "match" else "differ" end)
				  else
					.verdict = "error" | .failure = "gateway \($g | how), direct \($d | how)"
				  end
			  end)
		| {verdict: (if length > 0 and all(.[]; .verdict == "match") then "pass" else "fail" end),
		   matched: ([.[] | select(.verdict == "match")] | length),
		   total: length,
		   not_matched: [.[] | select(.verdict != "match") | .case],
		   failures: [.[] | select(.failure != null) | "\(.case): \(.failure)"],
		   cases: .}' "$1"
}

case "${1:-}" in
run) cmd_run ;;
diff) cmd_diff "${2:-}" "${3:-}" ;;
*) die "usage: conformance.sh run | conformance.sh diff <results.jsonl> [<model-map.json>]" ;;
esac
