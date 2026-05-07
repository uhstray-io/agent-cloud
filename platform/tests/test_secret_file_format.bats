#!/usr/bin/env bats
# test_secret_file_format.bats — Validate secret files have clean formatting
#
# Secret files (*.txt) must not contain trailing newlines, carriage returns,
# or non-printable characters. These cause JSON parsing failures when secrets
# are passed to OpenBao or Semaphore APIs.

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

@test "no secret files contain non-printable characters (except newline)" {
  skip_if_no_site_config
  local bad_files=""
  while IFS= read -r f; do
    if LC_ALL=C grep -qP '[^\x20-\x7E\n]' "$f" 2>/dev/null; then
      bad_files="${bad_files}  $(basename "$f")\n"
    fi
  done < <(find "$SECRETS_DIR" -name '*.txt' -type f)
  if [ -n "$bad_files" ]; then
    echo "Files with non-printable characters:"
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
