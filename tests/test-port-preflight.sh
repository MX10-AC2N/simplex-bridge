#!/bin/bash
# Host-listener preflight harness — tests the REAL "refuse to attach to an
# unrelated host listener" guard, extracted from entrypoint.sh at runtime
# (no copy that can drift).
#
# With network_mode: host (required for the Home Assistant integration),
# a leftover or unrelated process can already own 127.0.0.1:5225 when this
# container starts. Without this guard the entrypoint would carry on and
# eventually mistake that unrelated process for its own daemon in later
# readiness checks. port_listening() itself already has its own harness
# (test-port-gate.sh); this one only covers the fail-closed decision built
# on top of it, which ships with no coverage of its own.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$HERE/../entrypoint.sh"
EXTRACT="$(mktemp)"
cleanup() { rm -f "$EXTRACT"; }
trap cleanup EXIT

# ── Extract the shipped block verbatim ─────────────────────────────
awk '/^# ── Refuse to attach to an unrelated host listener/,/^fi$/' "$ENTRYPOINT" > "$EXTRACT"
if ! grep -q "already listening in the host network namespace" "$EXTRACT"; then
    echo "FATAL: could not extract the port-preflight block from $ENTRYPOINT" >&2
    echo "extracted:" >&2; cat "$EXTRACT" >&2
    exit 2
fi
echo "=== extracted from entrypoint.sh ==="
sed 's/^/    /' "$EXTRACT"
echo

PASS=0; FAIL=0

print_indented() {
    while IFS= read -r line; do printf '      %s\n' "$line"; done <<< "$1"
}

# Runs the extracted block in a SUBSHELL, with port_listening() stubbed to
# the fixture's answer. The block itself calls `exit 1`, so sourcing it
# directly into this script would kill the whole harness.
run_case() {
    local listening_rc="$1"
    bash -c '
        port_listening() { return '"$listening_rc"'; }
        . "$1"
        echo "[case] reached end of block"
    ' _ "$EXTRACT" 2>&1
}

echo "=== A. port already listening — must fail closed ==="
OUT=$(run_case 0); RC=$?
if [ "$RC" -ne 0 ] \
    && grep -q "already listening in the host network namespace" <<< "$OUT" \
    && ! grep -q "reached end of block" <<< "$OUT"; then
    echo "  ✓ startup refused, block does not fall through"; PASS=$((PASS+1))
else
    echo "  ✗ did not fail closed (rc=$RC)"; print_indented "$OUT"; FAIL=$((FAIL+1))
fi

echo
echo "=== B. port free — must fall through and continue startup ==="
OUT=$(run_case 1); RC=$?
if [ "$RC" -eq 0 ] \
    && ! grep -q "already listening" <<< "$OUT" \
    && grep -q "reached end of block" <<< "$OUT"; then
    echo "  ✓ startup continues when the port is free"; PASS=$((PASS+1))
else
    echo "  ✗ did not continue when free (rc=$RC)"; print_indented "$OUT"; FAIL=$((FAIL+1))
fi

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
