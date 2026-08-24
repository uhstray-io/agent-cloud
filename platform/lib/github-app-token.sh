#!/usr/bin/env bash
# github-app-token.sh — Mint short-lived GitHub credentials from an organisation App.
#
# Uses openssl + curl + jq only, matching bao-client.sh: no new runtime dependency on
# the orchestrator image, and nothing to install on a service VM.
#
# Why an App rather than a token: a self-hosted runner's registration credential lives
# ONE HOUR, so registration is automated or it is not repeatable. That forces the
# question of what the automation holds long-term. An App's derived tokens expire on
# their own and its permission can be narrowed to self-hosted runners alone, so a leak
# is bounded in both time and reach — and the whole plane is revocable in one action,
# independently of every host.
#
# The credential chain, each step shorter-lived than the last:
#
#   App private key (OpenBao, never on a runner host)
#     -> RS256 JWT            (<=10 min, this file signs it)
#        issuer = the App's CLIENT ID (upstream's recommended value) or its App ID;
#        both are accepted verbatim in `iss`. The App's OAuth client SECRET plays no
#        part in App authentication and is not used anywhere here — only the private
#        key can sign.
#       -> installation token (~1 hour, from the App installation)
#         -> registration token (1 hour, single-use for one runner config)
#
# Every function prints ONLY its credential on stdout, so a caller captures it into a
# variable and never has to filter log noise. Nothing here echoes the key or a token,
# and no credential is ever passed as a command-line argument (argv is world-readable
# via /proc on a multi-user host) — the key is read from a file or stdin, and tokens
# travel in headers.
#
# Source guard: safe to source multiple times
[ -n "${_GITHUB_APP_TOKEN_SH_LOADED:-}" ] && return 0
_GITHUB_APP_TOKEN_SH_LOADED=1

GITHUB_API="${GITHUB_API:-https://api.github.com}"

# ── Internal helpers ──────────────────────────────────────────────────────────

# base64url with padding stripped, as JWT requires. `openssl base64 -A` keeps the
# output on one line; the default wraps at 64 columns and would corrupt the token.
_gh_b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# _gh_app_jwt <issuer> <pem_path>
# Signs the App assertion. `issuer` is the App's client id (recommended) or its app id.
# Reads the key from a PATH (or `-` for stdin) — never argv.
_gh_app_jwt() {
  local app_id="$1" pem="$2"
  local now header payload signing_input keyfile staged="" sigfile="" sig_b64 rc

  [ -n "$app_id" ] || { echo "ERROR: issuer is required (the App's client id, or its app id)" >&2; return 2; }
  [ -n "$pem" ] || { echo "ERROR: private key path is required ('-' for stdin)" >&2; return 2; }

  # Stdin support lets a caller pipe the key straight from the secret store without it
  # ever touching disk in a location the caller does not control. When it must be
  # staged, stage it at 0600 and remove it on every exit path.
  #
  # Cleanup is explicit rather than a `trap ... RETURN`. Bash fires a RETURN trap on
  # return from NESTED function calls too, so a trap set here was sprung by the very
  # first base64 helper call and deleted the key before openssl ever read it.
  if [ "$pem" = "-" ]; then
    staged="$(mktemp)" || { echo "ERROR: could not create a temp file for the key" >&2; return 1; }
    chmod 600 "$staged"
    cat > "$staged"
    keyfile="$staged"
  else
    [ -r "$pem" ] || { echo "ERROR: private key not readable: $pem" >&2; return 1; }
    keyfile="$pem"
  fi

  now=$(date +%s)
  # iat is backdated 60s to absorb clock skew against the forge, which rejects an
  # assertion issued in its future. exp is 9 minutes: the documented ceiling is 10, and
  # sitting on the boundary turns skew into an intermittent auth failure.
  header=$(printf '{"alg":"RS256","typ":"JWT"}' | _gh_b64url)
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | _gh_b64url)
  signing_input="${header}.${payload}"

  # The signature goes to a FILE, never a variable. An RSA signature is arbitrary
  # bytes, and command substitution cannot carry NUL — capturing it in a variable
  # silently truncates it, yielding a well-formed token whose signature is garbage.
  # That fails nowhere locally and only at the forge, during registration, on a host
  # that is already built.
  sigfile="$(mktemp)" || { rm -f "$staged"; echo "ERROR: could not create a temp file for the signature" >&2; return 1; }
  chmod 600 "$sigfile"
  printf '%s' "$signing_input" | openssl dgst -sha256 -sign "$keyfile" -binary > "$sigfile"
  rc=$?
  # openssl is the LAST command in that pipeline, so $? is its status and not the
  # printf's — the masking failure in docs/MISTAKES.md §1.3 does not apply here, but
  # the status is still checked on its own line rather than folded into a pipeline.
  if [ "$rc" -ne 0 ]; then
    rm -f "$staged" "$sigfile"
    echo "ERROR: signing failed (openssl rc=$rc) — is the key a valid RSA private key?" >&2
    return 1
  fi

  sig_b64=$(_gh_b64url < "$sigfile")
  rm -f "$staged" "$sigfile"

  [ -n "$sig_b64" ] || { echo "ERROR: signature encoded to nothing" >&2; return 1; }
  printf '%s.%s' "$signing_input" "$sig_b64"
}

# ── Public API ────────────────────────────────────────────────────────────────

# gh_app_installation_id <issuer> <pem_path|-> <org>
# Prints the numeric installation id for <org> on stdout.
#
# Discovered rather than configured. An installation id is not a secret and not a
# decision — it is a fact the forge already knows, derivable from the App credential we
# necessarily hold. Making an operator read it off a URL and copy it into inventory adds
# a hand-transcribed value that can be wrong, and gives a confusing failure when the App
# is reinstalled and the id changes.
gh_app_installation_id() {
  local issuer="$1" pem="$2" org="$3"
  local jwt response id count

  [ -n "$org" ] || { echo "ERROR: organisation is required" >&2; return 2; }

  # The key may arrive on stdin, which can only be read once — so read it here and pass
  # it on as a staged file rather than letting two callees both try to consume stdin.
  local staged=""
  if [ "$pem" = "-" ]; then
    staged="$(mktemp)" || { echo "ERROR: could not stage the key" >&2; return 1; }
    chmod 600 "$staged"; cat > "$staged"; pem="$staged"
  fi

  jwt=$(_gh_app_jwt "$issuer" "$pem") || { rm -f "$staged"; return 1; }
  rm -f "$staged"

  response=$(curl -sf \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API}/app/installations") || {
      echo "ERROR: could not list installations — the assertion was rejected. Check the issuer (client id or app id) and that the private key belongs to this App." >&2
      return 1
    }

  count=$(printf '%s' "$response" | jq -r 'length')
  if [ "$count" = "0" ]; then
    echo "ERROR: this App has no installations. It exists, and its key authenticates, but it has not been INSTALLED on any account yet — install it on '${org}' before a runner can be registered." >&2
    return 1
  fi

  id=$(printf '%s' "$response" | jq -r --arg org "$org" '.[] | select(.account.login == $org) | .id' | head -1)
  [ -n "$id" ] || {
    echo "ERROR: this App is installed, but not on '${org}'. Installed on: $(printf '%s' "$response" | jq -r '[.[].account.login] | join(", ")')" >&2
    return 1
  }
  printf '%s' "$id"
}

# gh_app_installation_token <app_id> <installation_id> <pem_path|->
# Prints an installation token (~1h) on stdout.
gh_app_installation_token() {
  local app_id="$1" installation_id="$2" pem="$3"
  local jwt response token

  [ -n "$installation_id" ] || { echo "ERROR: installation id is required" >&2; return 2; }

  jwt=$(_gh_app_jwt "$app_id" "$pem") || return 1

  response=$(curl -sf -X POST \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API}/app/installations/${installation_id}/access_tokens") || {
      echo "ERROR: installation token exchange failed — check the app id, installation id, and that the App is installed on the organisation" >&2
      return 1
    }

  token=$(printf '%s' "$response" | jq -r '.token // empty')
  [ -n "$token" ] || { echo "ERROR: no token in the installation response" >&2; return 1; }
  printf '%s' "$token"
}

# gh_runner_registration_token <org> <installation_token>
# Prints a runner registration token (1h, single use) on stdout.
gh_runner_registration_token() {
  _gh_runner_token "registration-token" "$1" "$2"
}

# gh_runner_remove_token <org> <installation_token>
# Prints a runner REMOVE token on stdout — de-registration's counterpart, so a runner
# can be withdrawn without destroying its host.
gh_runner_remove_token() {
  _gh_runner_token "remove-token" "$1" "$2"
}

_gh_runner_token() {
  local kind="$1" org="$2" itoken="$3"
  local response token

  [ -n "$org" ] || { echo "ERROR: organisation is required" >&2; return 2; }
  [ -n "$itoken" ] || { echo "ERROR: installation token is required" >&2; return 2; }

  response=$(curl -sf -X POST \
    -H "Authorization: Bearer ${itoken}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API}/orgs/${org}/actions/runners/${kind}") || {
      echo "ERROR: could not obtain a runner ${kind} for org '${org}' — does the App grant organisation self-hosted-runners write?" >&2
      return 1
    }

  token=$(printf '%s' "$response" | jq -r '.token // empty')
  [ -n "$token" ] || { echo "ERROR: no token in the ${kind} response" >&2; return 1; }
  printf '%s' "$token"
}
