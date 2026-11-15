
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$ROOT/entrypoint.sh"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "PASS: $*"
}

grep -Fq 'chmod 0644 "$resolver_file"' "$ENTRYPOINT" ||
    fail "DNS resolver permissions fix missing"

grep -Fq 'gosu "$PUID:$PGID" test -r "$resolver_file"' "$ENTRYPOINT" ||
    fail "Resolver readability check missing"

grep -Fq 'gosu "$PUID:$PGID" getent ahostsv4 "$DNS_CHECK_HOST"' "$ENTRYPOINT" ||
    fail "DNS lookup is not performed as daemon user"

grep -Fq 'SIMPLEX_DNS_CHECK_HOST' "$ENTRYPOINT" ||
    fail "DNS hostname configuration missing"

grep -Fq 'DNS_CHECK_ATTEMPTS=3' "$ENTRYPOINT" ||
    fail "DNS retry configuration missing"

grep -Fq 'FATAL: DNS RESOLUTION FAILED' "$ENTRYPOINT" ||
    fail "DNS fatal diagnostic missing"

dns_line="$(grep -nF 'Checking Docker DNS permissions' "$ENTRYPOINT" | head -1 | cut -d: -f1)"
daemon_line="$(grep -nF 'Starting simplex-chat daemon' "$ENTRYPOINT" | head -1 | cut -d: -f1)"

[[ -n "$dns_line" ]] || fail "DNS preflight location not found"
[[ -n "$daemon_line" ]] || fail "Daemon startup location not found"

(( dns_line < daemon_line )) ||
    fail "DNS preflight must run before daemon startup"

pass "DNS preflight is correctly configured"
