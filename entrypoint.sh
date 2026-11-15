
#!/bin/bash
set -e -o pipefail

DATA_DIR="/data"
DB_PREFIX="$DATA_DIR/simplex"
# Upstream's -d/--database takes a FILE PREFIX, not a directory.
DB_FILE="${DB_PREFIX}_chat.db"

# ── Resolve PUID/PGID ──────────────────────────────────────────────
PUID="${PUID:-99}"
PGID="${PGID:-100}"

echo "[entrypoint] Using PUID=$PUID PGID=$PGID"

# Validate numeric IDs.
case "$PUID:$PGID" in
    *[!0-9:]*|:*|*:)
        echo "[entrypoint] ERROR: PUID and PGID must be numeric"
        exit 1
        ;;
esac

if [ "$PUID" -eq 0 ]; then
    echo "[entrypoint] ERROR: PUID=0 is not supported. The daemon must run non-root."
    exit 1
fi

# ── Create or adapt simplex user/group ─────────────────────────────
if getent passwd simplex >/dev/null 2>&1; then
    EXISTING_UID=$(id -u simplex 2>/dev/null)
    EXISTING_GID=$(id -g simplex 2>/dev/null)

    if [ "$EXISTING_UID" = "$PUID" ] && [ "$EXISTING_GID" = "$PGID" ]; then
        echo "[entrypoint] simplex user already has PUID=$PUID PGID=$PGID"
    else
        echo "[entrypoint] Recreating simplex user (UID $EXISTING_UID -> $PUID, GID $EXISTING_GID -> $PGID)..."

        if getent group simplex >/dev/null 2>&1; then
            groupdel simplex 2>/dev/null || true
        fi

        if getent passwd simplex >/dev/null 2>&1; then
            userdel simplex 2>/dev/null || true
        fi

        groupadd --system --gid "$PGID" simplex 2>/dev/null || \
            groupadd --system simplex 2>/dev/null

        useradd --system --no-log-init -g simplex -u "$PUID" --create-home simplex
    fi
else
    groupadd --system --gid "$PGID" simplex 2>/dev/null || \
        groupadd --system simplex 2>/dev/null

    useradd --system --no-log-init -g simplex -u "$PUID" --create-home simplex
fi

# Verify that the requested numeric identity is actually usable.
if [ "$(id -u simplex)" != "$PUID" ]; then
    echo "[entrypoint] ERROR: simplex UID does not match PUID=$PUID"
    exit 1
fi

# ── Docker DNS permissions and preflight ───────────────────────────
#
# ROOT CAUSE:
# Docker-generated /etc/resolv.conf and /etc/hosts can have mode 0640
# and ownership root:root.
#
# This prevents the unprivileged daemon user (PUID=99 by default)
# from reading the resolver configuration.
#
# Experimentally confirmed:
#   Before chmod 0644:
#     gosu 99:100 getent smp5.simplex.im -> FAILURE
#
#   After chmod 0644:
#     gosu 99:100 getent smp5.simplex.im -> SUCCESS
#
# Apply this at runtime, because Docker creates these files when
# creating the container. No additional capabilities are required:
# the entrypoint runs as root and owns these files.
#
# Keep this section BEFORE starting simplex-chat through gosu.

echo "[entrypoint] Checking Docker DNS permissions..."

for resolver_file in /etc/resolv.conf /etc/hosts; do
    if [ ! -e "$resolver_file" ]; then
        echo "[entrypoint] ERROR: Missing resolver file: $resolver_file"
        exit 1
    fi

    if ! chmod 0644 "$resolver_file"; then
        echo "[entrypoint] ERROR: Cannot set permissions on $resolver_file"
        ls -la "$resolver_file" 2>&1 || true
        exit 1
    fi
done

# Verify actual read access using the daemon's numeric UID/GID.
for resolver_file in /etc/resolv.conf /etc/hosts /etc/nsswitch.conf; do
    if ! gosu "$PUID:$PGID" test -r "$resolver_file"; then
        echo "[entrypoint] ERROR: $resolver_file is not readable by $PUID:$PGID"
        ls -la "$resolver_file" 2>&1 || true
        exit 1
    fi
done

echo "[entrypoint] DNS resolver files are readable by UID $PUID"

# Verify that getent is available.
if ! command -v getent >/dev/null 2>&1; then
    echo "[entrypoint] ERROR: getent is unavailable (check libc-bin installation)"
    exit 1
fi

# Test actual DNS resolution under the exact daemon identity.
DNS_CHECK_HOST="${SIMPLEX_DNS_CHECK_HOST:-smp5.simplex.im}"
DNS_CHECK_ATTEMPTS=3
DNS_OK=false

echo "[entrypoint] Testing DNS resolution as $PUID:$PGID ($DNS_CHECK_HOST)..."

for attempt in $(seq 1 "$DNS_CHECK_ATTEMPTS"); do
    if DNS_RESULT=$(gosu "$PUID:$PGID" getent ahostsv4 "$DNS_CHECK_HOST" 2>&1); then
        DNS_OK=true
        break
    fi

    echo "[entrypoint] WARNING: DNS attempt $attempt/$DNS_CHECK_ATTEMPTS failed"
    echo "[entrypoint]          $DNS_RESULT"

    if [ "$attempt" -lt "$DNS_CHECK_ATTEMPTS" ]; then
        sleep 2
    fi
done

if [ "$DNS_OK" != true ]; then
    echo ""
    echo "[entrypoint] ================================================"
    echo "[entrypoint] FATAL: DNS RESOLUTION FAILED"
    echo "[entrypoint] ================================================"
    echo "[entrypoint] Host: $DNS_CHECK_HOST"
    echo "[entrypoint] UID:GID: $PUID:$PGID"
    echo "[entrypoint] Resolver files:"
    ls -la /etc/resolv.conf /etc/hosts /etc/nsswitch.conf 2>&1 || true
    echo ""
    echo "[entrypoint] /etc/resolv.conf:"
    cat /etc/resolv.conf 2>&1 || true
    echo ""
    echo "[entrypoint] /etc/nsswitch.conf:"
    cat /etc/nsswitch.conf 2>&1 || true
    echo ""
    echo "[entrypoint] Check Docker DNS, network connectivity and NSS."
    echo "[entrypoint] SimpleX daemon will NOT be started."
    exit 1
fi

echo "[entrypoint] DNS resolution successful:"
echo "$DNS_RESULT" | head -1

# ── Port-readiness helper ──────────────────────────────────────────
# Match a LISTEN socket on an EXACT port.
# Important with network_mode: host, where ss sees host sockets too.
port_listening() {
    local port="$1"

    ss -tlnH "sport = :$port" 2>/dev/null |
        awk -v p=":$port" '$4 ~ p"$" { found=1 } END { exit !found }' &&
        return 0

    ss -tlnH 2>/dev/null |
        awk -v p=":$port" '$4 ~ p"$" { found=1 } END { exit !found }'
}

# ── Graceful shutdown handler ──────────────────────────────────────
shutdown() {
    local signal=$1

    echo "[entrypoint] Received $signal — forwarding to simplex-chat..."

    if [ -n "${DAEMON_PID:-}" ]; then
        kill "-$signal" "$DAEMON_PID" 2>/dev/null || true

        for i in $(seq 1 10); do
            if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
                echo "[entrypoint] simplex-chat exited cleanly"
                break
            fi
            sleep 1
        done
    fi

    if [ -n "${SOCAT_PID:-}" ]; then
        kill "$SOCAT_PID" 2>/dev/null || true
    fi

    if [ -n "${LOG_ROTATOR_PID:-}" ]; then
        kill "$LOG_ROTATOR_PID" 2>/dev/null || true
    fi

    echo "[entrypoint] Goodbye"
    exit 0
}

trap 'shutdown SIGTERM' SIGTERM
trap 'shutdown SIGINT' SIGINT

# ── Timezone ───────────────────────────────────────────────────────
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -sf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# ── Fix data directory ownership ───────────────────────────────────
# Avoid a recursive walk when the top directory already has the
# requested owner.

if [ "$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null)" != "$PUID:$PGID" ]; then
    echo "[entrypoint] Fixing ownership of $DATA_DIR..."
    chown -R "$PUID:$PGID" "$DATA_DIR"
else
    echo "[entrypoint] $DATA_DIR already owned by $PUID:$PGID — skipping chown"
fi

# ── Build extra flags ──────────────────────────────────────────────
FLAGS=(-d "$DATA_DIR/simplex" -p 5225)

# Automatically migrate older database schemas.
FLAGS+=(-y)

if [ ! -f "$DB_FILE" ]; then
    echo "[entrypoint] First run: creating bot profile..."
    FLAGS+=(--create-bot-display-name "$SIMPLEX_DISPLAY_NAME")

    if [ "$SIMPLEX_FILES_ENABLED" = "true" ]; then
        FLAGS+=(--create-bot-allow-files)
    fi
fi

if [ "$SIMPLEX_MARK_READ" = "true" ]; then
    FLAGS+=(-r)
fi

if [ "$SIMPLEX_TOR" = "true" ]; then
    FLAGS+=(-x)
fi

# ── Start simplex-chat daemon as non-root ──────────────────────────
echo "[entrypoint] Starting simplex-chat daemon as UID $PUID..."
echo "[entrypoint]   simplex-chat ${FLAGS[*]}"

# Bound the daemon log.
DAEMON_LOG="/data/daemon.log"
MAX_LOG_BYTES=$((10 * 1024 * 1024))

if [ -f "$DAEMON_LOG" ]; then
    LOG_SIZE=$(stat -c '%s' "$DAEMON_LOG" 2>/dev/null || echo 0)

    if [ "${LOG_SIZE:-0}" -gt "$MAX_LOG_BYTES" ]; then
        echo "[entrypoint] daemon.log is ${LOG_SIZE} bytes — trimming to last 1MB"

        gosu "$PUID:$PGID" sh -c \
            'tail -c 1048576 /data/daemon.log > /data/daemon.log.tmp 2>/dev/null && mv -f /data/daemon.log.tmp /data/daemon.log' || :
    fi
fi

# Redirect stdout/stderr inside the daemon user's shell.
# exec ensures signals reach simplex-chat directly.
gosu "$PUID:$PGID" sh -c \
    'exec simplex-chat "$@" > /data/daemon.log 2>&1' \
    sh "${FLAGS[@]}" &

DAEMON_PID=$!

echo "[entrypoint] Daemon PID: $DAEMON_PID"

# ── Wait for WebSocket readiness ──────────────────────────────────
STARTUP_TIMEOUT="${SIMPLEX_STARTUP_TIMEOUT:-15}"

case "$STARTUP_TIMEOUT" in
    ''|*[!0-9]*)
        echo "[entrypoint] ERROR: SIMPLEX_STARTUP_TIMEOUT must be a positive integer"
        exit 1
        ;;
esac

if [ "$STARTUP_TIMEOUT" -lt 1 ]; then
    echo "[entrypoint] ERROR: SIMPLEX_STARTUP_TIMEOUT must be >= 1"
    exit 1
fi

for i in $(seq 1 "$STARTUP_TIMEOUT"); do
    if port_listening 5225; then
        echo "[entrypoint] WebSocket API ready on port 5225"
        break
    fi

    if [ "$i" -eq "$STARTUP_TIMEOUT" ]; then
        echo "[entrypoint] ERROR: simplex-chat failed to start within ${STARTUP_TIMEOUT}s"
        tail -20 "$DATA_DIR/daemon.log" 2>/dev/null || true
        kill "$DAEMON_PID" 2>/dev/null || true
        exit 1
    fi

    sleep 1
done

# ── First-run bot setup ────────────────────────────────────────────
SETUP_MARKER="$DATA_DIR/.setup-complete"

if [ ! -f "$SETUP_MARKER" ]; then
    sleep 2

    echo "[entrypoint] Setting up bot address..."

    SETUP_LOG="/tmp/setup.log"

    if gosu "$PUID:$PGID" python3 - <<'PYEOF' > "$SETUP_LOG" 2>&1
import asyncio
import json
import os
import sys
import websockets

async def setup():
    async with websockets.connect(
        'ws://127.0.0.1:5225',
        open_timeout=10
    ) as ws:

        # Retrieve the active user.
        await ws.send(json.dumps({
            'corrId': 's1',
            'cmd': '/user'
        }))

        await asyncio.sleep(1)

        # Create the bot contact address.
        await ws.send(json.dumps({
            'corrId': 's2',
            'cmd': '/ad'
        }))

        await asyncio.sleep(2)

        address = None
        user_id = None

        for _ in range(10):
            try:
                evt = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                break

            try:
                data = json.loads(evt)
            except json.JSONDecodeError:
                continue

            resp = data.get('resp', {})
            rtype = resp.get('type')

            if rtype == 'activeUser':
                user = resp.get('user', {})

                if user.get('activeUser'):
                    user_id = user.get('userId')
                elif user_id is None:
                    user_id = user.get('userId')

            elif rtype == 'usersList':
                users = resp.get('users', [])
                active = [u for u in users if u.get('activeUser')]

                if active and user_id is None:
                    user_id = active[0].get('userId')

            elif rtype == 'userContactLinkCreated':
                link = resp.get('connLinkContact', {})

                address = (
                    link.get('connFullLink')
                    or link.get('connShortLink')
                )

                if user_id is None:
                    user_id = (resp.get('user') or {}).get('userId')

            elif rtype == 'chatCmdError':
                print(
                    '[setup] ERROR: SimpleX command failed: '
                    + json.dumps(resp, ensure_ascii=False)
                )

        # Configure auto-accept.
        if os.environ.get('SIMPLEX_AUTO_ACCEPT', 'true') == 'true':

            if user_id is None:
                print(
                    '[setup] WARNING: could not determine userId — '
                    'auto-accept NOT configured'
                )
                print(
                    '[setup] Accept contact requests manually '
                    'in the SimpleX app'
                )

            else:
                settings = json.dumps({
                    'businessAddress': False,
                    'autoAccept': {
                        'acceptIncognito': False
                    }
                })

                await ws.send(json.dumps({
                    'corrId': 's3',
                    'cmd': f'/_address_settings {user_id} {settings}'
                }))

                confirmed = False

                try:
                    # Ignore unrelated events until the matching response.
                    for _ in range(10):
                        evt = await asyncio.wait_for(
                            ws.recv(),
                            timeout=1
                        )

                        try:
                            response = json.loads(evt)
                        except json.JSONDecodeError:
                            continue

                        if response.get('corrId') == 's3':
                            if response.get('resp', {}).get('type') == 'userContactLinkUpdated':
                                confirmed = True
                            break

                except asyncio.TimeoutError:
                    pass

                if confirmed:
                    print('[setup] Auto-accept enabled')
                else:
                    print(
                        '[setup] WARNING: /_address_settings was not '
                        f'confirmed for userId {user_id} — '
                        'auto-accept may not be active'
                    )

        # Save the contact address.
        if address:
            print(f'[setup] Bot address: {address[:80]}...')

            with open('/data/bot_address.txt', 'w') as f:
                f.write(address + '\n')

        else:
            print(
                '[setup] ERROR: no contact link received — '
                'setup will retry on next start'
            )
            sys.exit(1)


asyncio.run(setup())
PYEOF
    then
        sed 's/^/[setup] /' "$SETUP_LOG"

        # Create marker as daemon user.
        gosu "$PUID:$PGID" touch "$SETUP_MARKER"

        echo "[entrypoint] First-run setup completed successfully"

    else
        echo "[entrypoint] WARNING: first-run setup did not complete — will retry on next restart"
        tail -20 "$SETUP_LOG" | sed 's/^/[setup] /'
    fi
fi

# ── Optional SOCAT bridge ──────────────────────────────────────────
if [ -n "${SIMPLEX_SOCAT_PORT:-}" ]; then

    case "$SIMPLEX_SOCAT_PORT" in
        ''|*[!0-9]*)
            echo "[entrypoint] ERROR: SIMPLEX_SOCAT_PORT must be numeric"
            exit 1
            ;;
    esac

    if [ "$SIMPLEX_SOCAT_PORT" -lt 1 ] || [ "$SIMPLEX_SOCAT_PORT" -gt 65535 ]; then
        echo "[entrypoint] ERROR: SIMPLEX_SOCAT_PORT must be between 1 and 65535"
        exit 1
    fi

    if [ "$SIMPLEX_SOCAT_PORT" -eq 5225 ]; then
        echo "[entrypoint] ERROR: SIMPLEX_SOCAT_PORT must NOT be 5225"
        exit 1
    fi

    echo "[entrypoint] *** WARNING: Exposing WebSocket API on 0.0.0.0:$SIMPLEX_SOCAT_PORT ***"
    echo "[entrypoint] *** No authentication — only use on trusted networks ***"

    echo "[entrypoint] Starting socat bridge..."

    socat "TCP-LISTEN:$SIMPLEX_SOCAT_PORT,reuseaddr,fork" \
        TCP:127.0.0.1:5225 &

    SOCAT_PID=$!

    for i in $(seq 1 10); do
        if port_listening "$SIMPLEX_SOCAT_PORT"; then
            echo "[entrypoint] socat bridge listening on port $SIMPLEX_SOCAT_PORT"
            break
        fi

        if ! kill -0 "$SOCAT_PID" 2>/dev/null; then
            echo "[entrypoint] ERROR: socat exited immediately"
            exit 1
        fi

        if [ "$i" -eq 10 ]; then
            echo "[entrypoint] ERROR: socat did not start within 10s"
            exit 1
        fi

        sleep 1
    done
fi

# ── Periodic daemon.log rotation ────────────────────────────────────
# Truncate in place: never replace the inode held open by the daemon.

gosu "$PUID:$PGID" sh -c '
    while true; do
        sleep 3600

        if [ -f /data/daemon.log ]; then
            SIZE=$(stat -c "%s" /data/daemon.log 2>/dev/null || echo 0)

            if [ "${SIZE:-0}" -gt '"$MAX_LOG_BYTES"' ]; then
                truncate -s 0 /data/daemon.log 2>/dev/null || : > /data/daemon.log
                echo "[entrypoint] daemon.log exceeded max size — truncated"
            fi
        fi
    done
' &

LOG_ROTATOR_PID=$!

# ── Ready ──────────────────────────────────────────────────────────
echo ""
echo "=== SimpleX Bridge ready ==="
echo "  Bot name: $SIMPLEX_DISPLAY_NAME"
echo "  Running as: PUID=$PUID PGID=$PGID"
echo "  DNS: OK ($DNS_CHECK_HOST)"

if [ -f "$DATA_DIR/bot_address.txt" ]; then
    echo "  Bot address: $(cat "$DATA_DIR/bot_address.txt")"
fi

echo ""

wait "$DAEMON_PID"
