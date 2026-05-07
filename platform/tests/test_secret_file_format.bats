#!/usr/bin/env bats
# test_secret_file_format.bats — Validate secret files have clean formatting
#
# Secret files (*.txt) must not contain trailing newlines, carriage returns,
# or control characters (null, tab, escape, etc.). Special characters like
# !@#$%^&*() are allowed for password complexity. Control characters cause
# JSON parsing failures when secrets are passed to OpenBao or Semaphore APIs.

SITE_CONFIG="${SITE_CONFIG_DIR:-/Users/stray/Documents/GitHub/site-config}"
SECRETS_DIR="${SITE_CONFIG}/secrets"

skip_if_no_site_config() {
  if [ ! -d "$SECRETS_DIR" ]; then
    skip "site-config not available at $SITE_CONFIG"
  fi
}

@test "secret files exist in site-config" {
  skip_if_no_site_config
  local count
  count=$(find "$SECRETS_DIR" -name '*.txt' -type f | wc -l)
  [ "$count" -gt 0 ]
}

@test "no secret files have trailing newlines" {
  skip_if_no_site_config
  local bad_files=""
  while IFS= read -r f; do
    if [ -s "$f" ]; then
      local last_byte
      last_byte=$(tail -c 1 "$f" | xxd -p)
      if [ "$last_byte" = "0a" ]; then
        bad_files="${bad_files}  $(basename "$f")\n"
      fi
    fi
  done < <(find "$SECRETS_DIR" -name '*.txt' -type f)
  if [ -n "$bad_files" ]; then
    echo "Files with trailing newlines:"
    printf "%b" "$bad_files"
    return 1
  fi
}

@test "no secret files have carriage returns" {
  skip_if_no_site_config
  local bad_files=""
  while IFS= read -r f; do
    if grep -qP '\r' "$f" 2>/dev/null; then
      bad_files="${bad_files}  $(basename "$f")\n"
    fi
  done < <(find "$SECRETS_DIR" -name '*.txt' -type f)
  if [ -n "$bad_files" ]; then
    echo "Files with carriage returns:"
    printf "%b" "$bad_files"
    return 1
  fi
}

@test "no secret files contain shell/JSON-breaking control characters" {
  # Allow printable ASCII (0x20-0x7E) plus special chars (!@#$%^&* etc.)
  # Reject: null bytes (0x00), control chars (0x01-0x1F except 0x0A newline), DEL (0x7F)
  skip_if_no_site_config
  local bad_files=""
  while IFS= read -r f; do
    if LC_ALL=C grep -qP '[\x00-\x09\x0B-\x1F\x7F]' "$f" 2>/dev/null; then
      bad_files="${bad_files}  $(basename "$f")\n"
    fi
  done < <(find "$SECRETS_DIR" -name '*.txt' -type f)
  if [ -n "$bad_files" ]; then
    echo "Files with control characters (null, tab, escape, etc.):"
    printf "%b" "$bad_files"
    return 1
  fi
}

@test "no secret files are empty" {
  skip_if_no_site_config
  local bad_files=""
  while IFS= read -r f; do
    if [ ! -s "$f" ]; then
      bad_files="${bad_files}  $(basename "$f")\n"
    fi
  done < <(find "$SECRETS_DIR" -name '*.txt' -type f)
  if [ -n "$bad_files" ]; then
    echo "Empty secret files:"
    printf "%b" "$bad_files"
    return 1
  fi
}
