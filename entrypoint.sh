#!/usr/bin/env bash
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


def store_error_type(resp):
    """Pull the StoreError tag out of a chatCmdError envelope, if present.

    On failure the daemon replies (apps/simplex-chat/Server.hs, ObjChatCmdError):
      {"type": "chatCmdError",
       "chatError": {"type": "errorStore", "storeError": {"type": "<tag>"}}}
    A chatCmdError that wraps something other than a store error (agent,
    database, ...) returns None here, and callers treat that as an
    unexpected failure rather than guessing a storeError tag for it.
    """
    chat_error = resp.get('chatError', {})
    if chat_error.get('type') == 'errorStore':
        return chat_error.get('storeError', {}).get('type')
    return None


def extract_link(contact_link):
    """connLinkContact carries a full link, a short link, or both."""
    return contact_link.get('connFullLink') or contact_link.get('connShortLink')


async def send_and_await(ws, corr_id, cmd, attempts=10, per_attempt_timeout=1):
    """Send one command and wait for the reply carrying the same corrId.

    Matching on corrId (not on response type or substring) is what lets this
    survive unrelated traffic arriving on the same socket — a contact
    connecting, another event firing — between the send and the reply we
    actually asked for.
    """
    await ws.send(json.dumps({'corrId': corr_id, 'cmd': cmd}))

    for _ in range(attempts):
        try:
            evt = await asyncio.wait_for(ws.recv(), timeout=per_attempt_timeout)
        except asyncio.TimeoutError:
            break

        try:
            data = json.loads(evt)
        except json.JSONDecodeError:
            continue

        if data.get('corrId') == corr_id:
            return data.get('resp', {})

    return None


async def setup():
    async with websockets.connect(
        'ws://127.0.0.1:5225',
        open_timeout=10
    ) as ws:

        # ── Resolve the active user ───────────────────────────────────
        # Needed below for /_address_settings, which takes a literal
        # userId (Library/Commands.hs:7027) rather than acting on
        # "the active user" implicitly.
        user_id = None

        user_resp = await send_and_await(ws, 's1', '/user')
        if user_resp:
            rtype = user_resp.get('type')
            if rtype == 'activeUser':
                user_id = user_resp.get('user', {}).get('userId')
            elif rtype == 'usersList':
                active = [u for u in user_resp.get('users', []) if u.get('activeUser')]
                if active:
                    user_id = active[0].get('userId')

        # ── Resolve the bot's contact address ─────────────────────────
        #
        # /ad (APICreateMyAddress) is NOT idempotent: it fails with
        # chatCmdError/duplicateContactLink when an address already exists
        # (Library/Commands.hs:2487-2491: getUserAddress Right _ ->
        # throwError SEDuplicateContactLink). Calling it unconditionally on
        # every first-run attempt means any crash between address creation
        # and writing the .setup-complete marker below — a WS timeout,
        # ConnectionClosed, PayloadTooBig, a plain `docker kill` — leaves the
        # container unable to ever start again: /ad fails forever on an
        # address that already exists, and restart: unless-stopped turns
        # that into an infinite crash loop around a perfectly valid identity.
        #
        # Upstream's own bot init (Bot.hs:55-63) never hits this because it
        # checks first. Mirror that: ask ShowMyAddress, and only create when
        # the store reports there is truly no address yet.
        address = None

        show_resp = await send_and_await(ws, 's2', '/show_address')

        if show_resp and show_resp.get('type') == 'userContactLink':
            contact_link = show_resp.get('contactLink', {})
            address = extract_link(contact_link.get('connLinkContact', {}))
            if user_id is None:
                user_id = (show_resp.get('user') or {}).get('userId')

        elif show_resp and store_error_type(show_resp) == 'userContactLinkNotFound':
            create_resp = await send_and_await(ws, 's2b', '/ad')

            if create_resp and create_resp.get('type') == 'userContactLinkCreated':
                address = extract_link(create_resp.get('connLinkContact', {}))
                if user_id is None:
                    user_id = (create_resp.get('user') or {}).get('userId')

            elif create_resp and store_error_type(create_resp) == 'duplicateContactLink':
                # Lost a race: something created the address between our
                # /show_address and this /ad. Don't fail the run over it —
                # ask again instead, same as a cold start would.
                retry_resp = await send_and_await(ws, 's2c', '/show_address')

                if retry_resp and retry_resp.get('type') == 'userContactLink':
                    contact_link = retry_resp.get('contactLink', {})
                    address = extract_link(contact_link.get('connLinkContact', {}))
                    if user_id is None:
                        user_id = (retry_resp.get('user') or {}).get('userId')
                else:
                    print(
                        '[setup] ERROR: /ad reported duplicateContactLink but the '
                        'address could not be re-read: '
                        + json.dumps(retry_resp, ensure_ascii=False)
                    )

            else:
                print(
                    '[setup] ERROR: /ad failed: '
                    + json.dumps(create_resp, ensure_ascii=False)
                )

        else:
            print(
                '[setup] ERROR: /show_address returned an unexpected response: '
                + json.dumps(show_resp, ensure_ascii=False)
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

                addr_settings_resp = await send_and_await(
                    ws, 's3', f'/_address_settings {user_id} {settings}'
                )

                if addr_settings_resp and addr_settings_resp.get('type') == 'userContactLinkUpdated':
                    print('[setup] Auto-accept enabled')
                else:
                    print(
                        '[setup] WARNING: /_address_settings was not '
                        f'confirmed for userId {user_id} — '
                        'auto-accept may not be active: '
                        + json.dumps(addr_settings_resp, ensure_ascii=False)
                    )

        # Configure bot commands if provided.
        bot_commands = os.environ.get('SIMPLEX_BOT_COMMANDS', '').strip()
        bot_commands_confirmed = True  # vacuously true: none were requested

        if bot_commands and user_id is not None:
            cmd = f'/set bot commands {bot_commands}'
            bot_cmd_resp = await send_and_await(ws, 's4', cmd)
            rtype = bot_cmd_resp.get('type') if bot_cmd_resp else None

            if rtype in ('userProfileUpdated', 'cmdOk'):
                bot_commands_confirmed = True
                print(f'[setup] Bot commands configured: {bot_commands[:80]}...')

            elif rtype == 'chatCmdError':
                # A rejected menu (bad grammar — see botCommandsP,
                # Library/Commands.hs:7169-7185) must NOT be reported as
                # configured. The previous code set confirmed=True on
                # chatCmdError too, so the same run logged both
                # "WARNING: bot commands may have failed" and
                # "Bot commands configured" for an invalid menu.
                bot_commands_confirmed = False
                print(
                    '[setup] ERROR: bot commands rejected by daemon: '
                    + json.dumps(bot_cmd_resp, ensure_ascii=False)
                )

            else:
                bot_commands_confirmed = False
                print(
                    '[setup] WARNING: /set bot commands was not confirmed: '
                    + json.dumps(bot_cmd_resp, ensure_ascii=False)
                )

        elif bot_commands and user_id is None:
            bot_commands_confirmed = False
            print('[setup] WARNING: cannot set bot commands — userId unknown')

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

        if not bot_commands_confirmed:
            # No .setup-complete marker on this path either: SIMPLEX_BOT_COMMANDS
            # is operator-controlled, so surface the failure loudly and retry on
            # the next restart rather than silently run with a half-applied (or
            # falsely "confirmed") command menu. /show_address above is
            # idempotent, so retrying the whole block costs nothing once the
            # value is fixed.
            print(
                '[setup] ERROR: bot command configuration failed — '
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

# shellcheck disable=SC2016
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
