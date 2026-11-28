# SimpleX Bridge — MX10-AC2N

**English** | [Français](README.fr.md)

A Dockerized SimpleX Chat bot daemon exposing the SimpleX CLI WebSocket API.

This repository is a maintained fork of
[libre-7/simplex-bridge](https://github.com/libre-7/simplex-bridge), extended with
a Home Assistant-oriented validation suite and hardened startup.

Docker images are published to GitHub Container Registry:

`ghcr.io/mx10-ac2n/simplex-bridge`

[![WebSocket Validation](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml/badge.svg)](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml)

## Features

- SimpleX Chat daemon running in WebSocket mode.
- Automatic bot profile and contact address creation (idempotent — safe across restarts).
- Persistent database and bot identity.
- Unprivileged daemon execution using configurable PUID and PGID.
- Docker DNS permission correction at startup, verified under the daemon identity.
- Received attachments stored in a dedicated volume, made world-readable so Home
  Assistant can mount it read-only.
- Docker security hardening (dropped capabilities, no-new-privileges).
- Multi-architecture images (AMD64 and ARM64).
- Automated WebSocket validation suite (T01–T13) certifying everything the
  Home Assistant integration relies on.

## Quick Start

Clone the repository:

```bash
git clone https://github.com/MX10-AC2N/simplex-bridge.git
cd simplex-bridge
```

Build and start:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

View logs:

```bash
docker compose logs -f simplex-bridge
```

Retrieve the bot contact address:

```bash
docker compose exec simplex-bridge cat /data/bot_address.txt
```

The named Docker volume `simplex-data` stores the SimpleX database and bot
identity. Incoming attachments are stored separately in `simplex-inbox`; the
optional shared outbox is `simplex-outbox`. Back up `simplex-data` regularly.

## Using the Published Image

`docker-compose.yml` runs the image published by this fork, pinned by digest
(v1.5.1), so no build is involved:

```bash
docker compose pull
docker compose up -d
```

To upgrade, read the new release's digest and replace the one in
`docker-compose.yml`:

```bash
docker buildx imagetools inspect ghcr.io/mx10-ac2n/simplex-bridge:<tag>
```

Use the top-level `Digest:` line, not a per-platform one. Avoid `latest`: it
moves with `main`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SIMPLEX_DISPLAY_NAME` | SimpleX Bridge | Bot display name |
| `SIMPLEX_AUTO_ACCEPT` | true | Automatic contact acceptance |
| `SIMPLEX_FILES_ENABLED` | true | Enable file transfers |
| `SIMPLEX_MARK_READ` | true | Automatically mark messages as read |
| `SIMPLEX_TOR` | false | Enable the SimpleX Tor option |
| `SIMPLEX_STARTUP_TIMEOUT` | 15 | WebSocket startup timeout (seconds) |
| `SIMPLEX_FILES_DIR` | `/data/received_files` | Directory used by the CLI for received attachments (`--files-folder`) |
| `SIMPLEX_DNS_CHECK_HOST` | smp5.simplex.im | DNS preflight hostname |
| `SIMPLEX_SOCAT_PORT` | unset | Optional socat listener port |
| `SIMPLEX_BOT_COMMANDS` | unset | Bot command menu in SimpleX syntax |
| `PUID` | 99 | Daemon user ID |
| `PGID` | 100 | Daemon group ID |
| `TZ` | UTC | Container timezone |

PUID must be non-zero. The daemon is intentionally not run as root.

### Shared attachment volumes with Home Assistant

The Compose file gives the attachment volumes stable Docker names:
`simplex-inbox` and `simplex-outbox`. A separate Home Assistant Compose project
can mount those exact volumes as `external: true`. Files landing in the inbox
are created group/world-readable by design (`umask 0022`), so the integration
can serve them without permission issues. Keep `simplex-data` private to this
bridge container; it contains the SimpleX database and identity keys.

## DNS Permission Fix

### Problem

Docker generates `/etc/resolv.conf` and `/etc/hosts` when a container starts.

On some systems, these files can have restrictive permissions such as:

```text
-rw-r----- root root /etc/resolv.conf
-rw-r----- root root /etc/hosts
```

The SimpleX daemon runs as an unprivileged user.

If this user cannot read these files, DNS resolution fails even when the
Docker host itself can resolve the same domains.

### Solution

The entrypoint corrects these permissions at container startup:

```bash
chmod 0644 /etc/resolv.conf
chmod 0644 /etc/hosts
```

It then verifies that the daemon user can read:

- `/etc/resolv.conf`
- `/etc/hosts`
- `/etc/nsswitch.conf`

Finally, it checks DNS resolution using the same UID/GID as the daemon:

```bash
gosu "$PUID:$PGID" getent ahostsv4 smp5.simplex.im
```

The DNS check is retried three times.

If resolution still fails, startup stops with diagnostic information.

This correction is performed at runtime because Docker creates or mounts
these files when the container starts. Changing their permissions in the
Dockerfile would not solve the problem.

### DNS Configuration

The hostname used by the DNS preflight can be changed:

```yaml
environment:
  SIMPLEX_DNS_CHECK_HOST: "smp5.simplex.im"
```

The default is `smp5.simplex.im`.

### DNS Troubleshooting

Check container logs:

```bash
docker compose logs --tail=150 simplex-bridge
```

Check resolver permissions:

```bash
docker compose exec simplex-bridge ls -l \
  /etc/resolv.conf \
  /etc/hosts \
  /etc/nsswitch.conf
```

Test DNS as the daemon user:

```bash
docker compose exec simplex-bridge \
  gosu 99:100 getent ahostsv4 smp5.simplex.im
```

If this fails, inspect Docker networking, DNS forwarding and host firewall
rules.

## Network and Security

The WebSocket API listens on:

```text
127.0.0.1:5225
```

The API does not provide authentication by default.

Host networking is recommended when clients (Home Assistant, Hermes Agent)
run on the same machine.

For the [MX10-AC2N Home Assistant integration](https://github.com/MX10-AC2N/SimpleX-Chat_HA)
on the same Linux host, configure **both** `homeassistant` and
`simplex-bridge` with `network_mode: host`. In the integration, use
`ws://127.0.0.1:5225` (this is also its default URL). No Docker port mapping
and no SOCAT proxy are needed for this topology.

The host's TCP port 5225 must be free before the bridge starts. The entrypoint
fails closed if another process already listens on that port, instead of
mistaking an unrelated listener for the SimpleX daemon.

The container drops all Linux capabilities except:

- CHOWN
- SETUID
- SETGID

These are required by the root entrypoint to prepare data ownership and drop
privileges.

The following protection remains enabled:

```yaml
security_opt:
  - no-new-privileges:true
```

### SOCAT Warning

If `SIMPLEX_SOCAT_PORT` is enabled, socat can expose an unauthenticated
listener on the network.

Only enable this feature on trusted networks.

Do not expose the WebSocket API directly to the Internet.

### Bot Commands

You can expose a command menu to users:

```yaml
SIMPLEX_BOT_COMMANDS: "'Help':/help,'Status':/status,'Info':/info"
```

Full syntax (nested menus supported):

```text
'Label':/keyword,'Menu':{'Sub':/sub <param>}
```

See the official documentation:
https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md

## WebSocket Validation Suite

`tests/ws-validate.py` (run by
[`.github/workflows/ws-validation.yml`](.github/workflows/ws-validation.yml))
validates the complete gateway contract for the Home Assistant integration:

| Interaction HA | Test(s) |
|---|---|
| `binary_sensor.simplex_chat_connection` + reconnect backoff | T01, T07 |
| RPC protocol `{"corrId","cmd"}` (string mode), correlation | T02, T03, T04 |
| Resilience to malformed frames, concurrent connections | T05, T06 |
| `list_contacts` / `create_invite` / `accept_invite` / `send_message` | T10, T11, T12 |
| Long-run stability under real traffic | T08 |
| Incoming event flux: alias, direction, item_id, text (incl. image/video/file) | T09 |
| HA chat-item mapping replay (regression guard on CLI serialization) | T13 |

Run locally:

```bash
python3 tests/ws-validate.py --url ws://127.0.0.1:5225
```

### Interactive pairing (single run)

The workflow is a `workflow_dispatch` with adjustable parameters (contact wait,
reply window). During the run:

1. T12 publishes a fresh invitation link in the logs and in the run Summary
   (line `INVITE LINK: …`);
2. open that link in the SimpleX app on your phone while the run waits;
3. the bot sends a validation ping to the new contact;
4. reply from your phone within the reply window (default 90 s, countdown
   displayed). Detection keeps running during T08, and T09 evaluates the
   whole run — a slightly late reply is still counted.

Each CI run creates a brand-new bot identity (the volume is destroyed by
`down -v`), so always reply in the thread that just received the ping — not
in an older "SimpleX Bridge" thread from a previous run.

### Certified baseline

**Baseline: [12 PASS / 1 WARN / 0 FAIL](docs/validation-baseline.md)** — run
dated 2026-11-23, SimpleX CLI 7.1.4.0 / simplexmq 7.1.0.18, `websockets` 17.4.

Immutable reference run:
https://github.com/MX10-AC2N/simplex-bridge/actions/runs/45246557568

The badge reflects the **latest** run (it turns red if a future change breaks
the contract); the frozen baseline lives in
[`docs/validation-baseline.md`](docs/validation-baseline.md).

## Other Integrations (fork heritage)

The upstream fork ships a Hermes Agent integration helper:

```bash
bash install-websockets.sh
```

This helper installs the WebSocket dependency and checks the Hermes Agent
environment (`SIMPLEX_WS_URL`, `SIMPLEX_ALLOW_ALL_USERS`,
`SIMPLEX_HOME_CHANNEL`). Both containers must share the host network
namespace.

## Local Build

Build directly:

```bash
docker build -t simplex-bridge:local .
```

Or use Docker Compose with the build override:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## Tests

Run the regression tests:

```bash
bash tests/run-all.sh
```

The test suite checks:

- Shell syntax.
- Python compilation.
- Docker Compose and workflow YAML.
- XML templates.
- DNS preflight.
- Port readiness.
- Installer behavior.
- First-run setup.
- Documentation consistency.

The WebSocket contract itself is validated by the dedicated suite — see
[WebSocket Validation Suite](#websocket-validation-suite).

## Image Publishing

Images are published automatically to:

```text
ghcr.io/mx10-ac2n/simplex-bridge
```

## License

The project source is licensed under GPL-3.0.
See the LICENSE file.

Third-party components retain their respective licenses.
