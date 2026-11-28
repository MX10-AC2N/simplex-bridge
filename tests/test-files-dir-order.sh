#!/usr/bin/env bash
# Ordering regression: the attachments directory must be created BEFORE
# /data is chowned to PUID:PGID.
#
# Why: the compose file runs with cap_drop: ALL, which removes
# CAP_DAC_OVERRIDE. Once /data belongs to PUID:PGID (mode 0755), root can no
# longer mkdir inside it, so creating received_files AFTER the chown fails
# on a fresh volume ("Permission denied") and the container never starts.
# A textual "line A before line B" grep would pass on a renamed variable, so
# this runs the real block with mkdir/chown mocks that model that capability
# loss instead.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$HERE/../entrypoint.sh"
EXTRACT="$(mktemp)"
MOCKDIR="$(mktemp -d)"
WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$EXTRACT" "$MOCKDIR" "$WORKDIR"; }
trap cleanup EXIT

awk '/^# ── Incoming files directory/,/^umask 0022$/' "$ENTRYPOINT" > "$EXTRACT"
if ! grep -qF "mkdir -p \"\$SIMPLEX_FILES_DIR\"" "$EXTRACT" \
    || ! grep -qF "chown -R \"\$PUID:\$PGID\" \"\$DATA_DIR\"" "$EXTRACT" \
    || ! grep -q '^umask 0022$' "$EXTRACT"; then
    echo "FATAL: could not extract the files-dir + ownership span from $ENTRYPOINT" >&2
    cat "$EXTRACT" >&2
    exit 2
fi

REAL_MKDIR="$(command -v mkdir)"
STATE="$WORKDIR/state"; mkdir -p "$STATE"

# chown: record that DATA_DIR changed owner (root loses write access to it).
cat > "$MOCKDIR/chown" <<MOCK
#!/usr/bin/env bash
case "\$*" in *"\$DATA_DIR"*) : > "$STATE/data-chowned" ;; esac
exit 0
MOCK
# mkdir: after DATA_DIR was chowned, creating anything inside it is denied,
# exactly what cap_drop: ALL does to root.
cat > "$MOCKDIR/mkdir" <<MOCK
#!/usr/bin/env bash
if [ -e "$STATE/data-chowned" ]; then
    for a in "\$@"; do
        case "\$a" in "\$DATA_DIR"/*) echo "mkdir: cannot create directory '\$a': Permission denied" >&2; exit 1 ;; esac
    done
fi
exec "$REAL_MKDIR" "\$@"
MOCK
# stat: report a root-owned tree so the chown branch is taken.
printf '#!/usr/bin/env bash\necho 0:0\n' > "$MOCKDIR/stat"
# gosu: drop the user switch, run the command (chmod -R a+rX is harmless here).
printf '#!/usr/bin/env bash\nshift\nexec "$@"\n' > "$MOCKDIR/gosu"
chmod +x "$MOCKDIR"/*

DATA_DIR="$WORKDIR/data"; "$REAL_MKDIR" -p "$DATA_DIR"
OUT=$(PATH="$MOCKDIR:$PATH" DATA_DIR="$DATA_DIR" PUID=99 PGID=100 SIMPLEX_FILES_DIR="" \
      bash -c '. "$1" && echo "[case] reached end"' _ "$EXTRACT" 2>&1)
RC=$?

PASS=0; FAIL=0
if [ "$RC" -eq 0 ] && grep -q "reached end" <<< "$OUT" && [ -d "$DATA_DIR/received_files" ]; then
    echo "  ✓ received_files created while root can still write to /data"; PASS=1
else
    echo "  ✗ received_files could not be created (rc=$RC)"
    while IFS= read -r l; do printf '      %s\n' "$l"; done <<< "$OUT"
    FAIL=1
fi
if [ -e "$STATE/data-chowned" ]; then
    echo "  ✓ /data chown still happens"; PASS=$((PASS+1))
else
    echo "  ✗ /data chown never ran"; FAIL=$((FAIL+1))
fi
echo "=== RESULT: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
