#!/bin/bash
# SIMPLEX_FILES_DIR verification harness — tests the REAL validation /
# creation / chown block for the dedicated attachments volume, extracted
# from entrypoint.sh at runtime (no copy that can drift).
#
# This block was added on top of the F-01/F-07 fixes to let Home Assistant
# mount a read-only attachments volume without reaching the SimpleX DB/keys
# (`--files-folder`, verified against upstream `Options.hs`). It ships with
# no coverage upstream of this harness, so every branch below is new.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$HERE/../entrypoint.sh"
EXTRACT="$(mktemp)"
MOCKDIR=""
WORKDIR=""
cleanup() { rm -rf "$MOCKDIR" "$EXTRACT" "$WORKDIR"; }
trap cleanup EXIT

# ── Extract the shipped block verbatim ─────────────────────────────
awk '/^# ── Incoming files directory/,/^$/' "$ENTRYPOINT" > "$EXTRACT"
if ! grep -q "SIMPLEX_FILES_DIR must be an absolute path" "$EXTRACT"; then
    echo "FATAL: could not extract the SIMPLEX_FILES_DIR block from $ENTRYPOINT" >&2
    echo "extracted:" >&2; cat "$EXTRACT" >&2
    exit 2
fi
echo "=== extracted from entrypoint.sh ==="
sed 's/^/    /' "$EXTRACT"
echo

# ── Mocks ────────────────────────────────────────────────────────────
# chown is mocked (not just permission-skipped) so the test is portable to
# a non-root CI runner, and so we can assert exactly what it was called
# with. stat is mocked so "already owned" vs "needs chown" is deterministic
# across hosts/CI users instead of depending on who actually ran the test.
MOCKDIR=$(mktemp -d)
CHOWN_LOG="$MOCKDIR/chown.log"
: > "$CHOWN_LOG"

cat > "$MOCKDIR/chown" <<MOCK
#!/bin/bash
echo "\$@" >> "$CHOWN_LOG"
exit 0
MOCK
chmod +x "$MOCKDIR/chown"

cat > "$MOCKDIR/stat" <<'MOCK'
#!/bin/bash
# Emulate `stat -c '%u:%g' <path>`.
printf '%s\n' "${STAT_MOCK_OUTPUT:-0:0}"
MOCK
chmod +x "$MOCKDIR/stat"
export PATH="$MOCKDIR:$PATH"

WORKDIR=$(mktemp -d)

PASS=0; FAIL=0

print_indented() {
    while IFS= read -r line; do printf '      %s\n' "$line"; done <<< "$1"
}

# Runs the extracted block in a SUBSHELL (not sourced into this script),
# because the block calls `exit 1` on invalid input — sourcing it here
# would kill the whole test harness instead of just that one case.
run_case() {
    local files_dir="$1" data_dir="$2" stat_output="$3"
    : > "$CHOWN_LOG"
    SIMPLEX_FILES_DIR="$files_dir" DATA_DIR="$data_dir" PUID=99 PGID=100 \
        STAT_MOCK_OUTPUT="$stat_output" \
        bash -c '. "$1" && echo "[case] reached end of block"' _ "$EXTRACT" 2>&1
}

check_ok() {
    local desc="$1" expected_dir="$2" out="$3" rc="$4"
    if [ "$rc" -eq 0 ] && [ -d "$expected_dir" ] && grep -q "reached end of block" <<< "$out"; then
        echo "  ✓ $desc"; PASS=$((PASS+1))
    else
        echo "  ✗ $desc (rc=$rc)"; print_indented "$out"; FAIL=$((FAIL+1))
    fi
}

check_fail() {
    local desc="$1" expected_dir="$2" needle="$3" out="$4" rc="$5"
    if [ "$rc" -ne 0 ] && [ ! -e "$expected_dir" ] && grep -q "$needle" <<< "$out"; then
        echo "  ✓ $desc"; PASS=$((PASS+1))
    else
        echo "  ✗ $desc (rc=$rc)"; print_indented "$out"; FAIL=$((FAIL+1))
    fi
}

check_chown_skipped() {
    local desc_yes="$1" desc_no="$2"
    if [ -s "$CHOWN_LOG" ]; then
        echo "  ✗ $desc_no"; cat "$CHOWN_LOG"; FAIL=$((FAIL+1))
    else
        echo "  ✓ $desc_yes"; PASS=$((PASS+1))
    fi
}

echo "=== A. default (unset) — falls back to \$DATA_DIR/received_files ==="
DATA_DIR="$WORKDIR/a"; mkdir -p "$DATA_DIR"
OUT=$(run_case "" "$DATA_DIR" "0:0"); RC=$?
check_ok "directory created and chown invoked (not yet owned)" "$DATA_DIR/received_files" "$OUT" "$RC"
if grep -q -- "-R 99:100 $DATA_DIR/received_files" "$CHOWN_LOG"; then
    echo "  ✓ chown -R 99:100 on the right path"; PASS=$((PASS+1))
else
    echo "  ✗ chown was not called with the expected args"; cat "$CHOWN_LOG"; FAIL=$((FAIL+1))
fi

echo
echo "=== B. empty string behaves the same as unset ==="
DATA_DIR="$WORKDIR/b"; mkdir -p "$DATA_DIR"
OUT=$(run_case "" "$DATA_DIR" "99:100"); RC=$?
check_ok "default path used, chown skipped (already owned)" "$DATA_DIR/received_files" "$OUT" "$RC"
check_chown_skipped "chown skipped when already owned" "chown should have been skipped"

echo
echo "=== C. relative path is REJECTED (must be absolute) ==="
DATA_DIR="$WORKDIR/c"; mkdir -p "$DATA_DIR"
OUT=$(run_case "relative/subdir" "$DATA_DIR" "0:0"); RC=$?
check_fail "relative path rejected, nothing created" "$WORKDIR/c/relative/subdir" \
    "must be an absolute path" "$OUT" "$RC"

echo
echo "=== D. bare / is REJECTED (would chown -R the whole filesystem) ==="
DATA_DIR="$WORKDIR/d"; mkdir -p "$DATA_DIR"
OUT=$(run_case "/" "$DATA_DIR" "0:0"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "must not be /" <<< "$OUT"; then
    echo "  ✓ / rejected before mkdir/chown could run"; PASS=$((PASS+1))
else
    echo "  ✗ / was not rejected (rc=$RC)"; print_indented "$OUT"; FAIL=$((FAIL+1))
fi
check_chown_skipped "chown never invoked for /" "chown must never be called for /"

echo
echo "=== E. explicit absolute path outside \$DATA_DIR is honored ==="
DATA_DIR="$WORKDIR/e-data"; mkdir -p "$DATA_DIR"
CUSTOM="$WORKDIR/e-custom/inbox"
OUT=$(run_case "$CUSTOM" "$DATA_DIR" "99:100"); RC=$?
check_ok "custom path created, already owned so chown skipped" "$CUSTOM" "$OUT" "$RC"
check_chown_skipped "chown skipped when already owned" "chown should have been skipped"

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
