#!/bin/sh
# https-forward.sh — forward the Mac's privileged 443/80 to local Caddy's high
# ports. Run as root by a LaunchDaemon (see io.uhstray.agent-cloud.https.plist),
# installed idempotently by `make local-https`.
#
# WHY THIS EXISTS: macOS requires root to bind ports <1024 and has no
# net.ipv4.ip_unprivileged_port_start escape hatch; podman-machine's port
# forwarder (gvproxy) runs as your user, so local Caddy can only publish high
# ports (8443/8088). This root-owned forwarder is the one privileged hop that
# makes clean, port-free `https://app.agent-cloud.test` work. TCP is passed through
# verbatim — TLS is still terminated end-to-end by Caddy at the target port, so
# SNI/cert selection is unaffected.
#
# Args: <https_listen> <https_target> <http_listen> <http_target>
#   e.g. 443 8443 80 8088
set -eu

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH

HTTPS_LISTEN="${1:?https listen port}"
HTTPS_TARGET="${2:?https target port}"
HTTP_LISTEN="${3:?http listen port}"
HTTP_TARGET="${4:?http target port}"

command -v socat >/dev/null 2>&1 || { echo "socat not found (brew install socat)" >&2; exit 1; }

# fork = one child per connection; reuseaddr = clean restarts; bind loopback so
# this never exposes anything beyond the Mac itself.
socat "TCP-LISTEN:${HTTPS_LISTEN},fork,reuseaddr,bind=127.0.0.1" "TCP:127.0.0.1:${HTTPS_TARGET}" &
p_https=$!
socat "TCP-LISTEN:${HTTP_LISTEN},fork,reuseaddr,bind=127.0.0.1" "TCP:127.0.0.1:${HTTP_TARGET}" &
p_http=$!

# Exit if EITHER forwarder dies, so launchd (KeepAlive) restarts the whole unit.
# Portable poll instead of `wait -n` (undefined in POSIX sh / macOS bash 3.2).
while kill -0 "$p_https" 2>/dev/null && kill -0 "$p_http" 2>/dev/null; do
  sleep 5
done
