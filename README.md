
# SimpleX Bridge — MX10-AC2N

A Dockerized SimpleX Chat bot daemon exposing the SimpleX CLI WebSocket API.

This repository is a maintained fork of
[libre-7/simplex-bridge](https://github.com/libre-7/simplex-bridge).

Docker images are published to GitHub Container Registry:

`ghcr.io/mx10-ac2n/simplex-bridge`

[![WebSocket Validation](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml/badge.svg)](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml)

## Features

- SimpleX Chat daemon running in WebSocket mode.
- Automatic bot profile and contact address creation.
- Persistent database and bot identity.
- Unprivileged daemon execution using configurable PUID and PGID.
- Docker DNS permission correction at startup.
- DNS preflight executed using the actual daemon UID/GID.
- Docker security hardening.
- Multi-architecture images (AMD64 and ARM64).

## Quick Start

Clone the repository:

```bash
git clone https://github.com/MX10-AC2N/simplex-bridge.git
cd simplex-bridge
```

Build and start:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f simplex-bridge
```

Retrieve the bot contact address:

```bash
docker compose exec simplex-bridge cat /data/bot_address.txt
```

The named Docker volume `simplex-data` stores the SimpleX database and bot identity.
Incoming attachments are stored separately in `simplex-inbox`; the optional shared
outbox is `simplex-outbox`. Back up `simplex-data` regularly.

## Using the Published Image

To use the image published by this fork instead of building locally:

```bash
docker compose pull
docker compose up -d
```

Image:

```text
ghcr.io/mx10-ac2n/simplex-bridge:latest
```

For production deployments, prefer a specific commit or version tag
instead of `latest`.

## DNS Permission Fix

### Problem

Docker generates `/etc/resolv.conf` and `/etc/hosts` when a container starts.

On some systems, these files can have restrictive permissions such as:

```text
-rw-r----- root root /etc/resolv.conf
-rw-r----- root root /etc/hosts
```

The SimpleX daemon runs as an unprivileged user.

If this user cannot read these files, DNS resolution fails even when
the Docker host itself can resolve the same domains.

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

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SIMPLEX_DISPLAY_NAME` | SimpleX Bridge | Bot display name |
| `SIMPLEX_AUTO_ACCEPT` | true | Automatic contact acceptance |
| `SIMPLEX_FILES_ENABLED` | true | Enable file transfers |
| `SIMPLEX_MARK_READ` | true | Automatically mark messages as read |
| `SIMPLEX_TOR` | false | Enable the SimpleX Tor option |
| `SIMPLEX_STARTUP_TIMEOUT` | 15 | WebSocket startup timeout |
| `SIMPLEX_FILES_DIR` | `/data/received_files` | Directory used by the CLI for received attachments (`--files-folder`) |
| `SIMPLEX_DNS_CHECK_HOST` | smp5.simplex.im | DNS preflight hostname |
| `SIMPLEX_SOCAT_PORT` | unset | Optional socat listener port |
| `SIMPLEX_BOT_COMMAND` | unset | Commandes bot au format SimpleX |
| `PUID` | 99 | Daemon user ID |
| `PGID` | 100 | Daemon group ID |
| `TZ` | UTC | Container timezone |

PUID must be non-zero. The daemon is intentionally not run as root.

### Shared attachment volumes with Home Assistant

The Compose file gives the attachment volumes stable Docker names:
`simplex-inbox` and `simplex-outbox`. A separate Home Assistant Compose project
can mount those exact volumes as `external: true`. Keep `simplex-data` private to
this bridge container; it contains the SimpleX database and identity keys.

## Network and Security

The WebSocket API listens on:

```text
127.0.0.1:5225
```

The API does not provide authentication by default.

Host networking is recommended when Hermes Agent runs on the same machine.

For the MX10-AC2N Home Assistant integration on the same Linux host, configure
**both** `homeassistant` and `simplex-bridge` with `network_mode: host`. In the
integration, use `ws://127.0.0.1:5225` (this is also its default URL). No Docker
port mapping and no SOCAT proxy are needed for this topology.

The host's TCP port 5225 must be free before the bridge starts. The entrypoint
now fails closed if another process already listens on that port, instead of
mistaking an unrelated listener for the SimpleX daemon.

The container drops all Linux capabilities except:

- CHOWN
- SETUID
- SETGID

These are required by the root entrypoint to prepare data ownership
and drop privileges.

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

Tu peux exposer un menu de commandes aux utilisateurs avec :

```yaml
SIMPLEX_BOT_COMMANDS: "'Help':/help,'Status':/status,'Info':/info"
```
Syntaxe complète (menus imbriqués possibles) :
'Label':/keyword,'Menu':{'Sub':/sub <param>}
Voir la doc officielle.
https://github.com/simplex-chat/simplex-chat/blob/stable/bots%2FREADME.md

### Hermes Agent Integration

Configure Hermes Agent to connect to:

```bash
SIMPLEX_WS_URL=ws://127.0.0.1:5225
SIMPLEX_ALLOW_ALL_USERS=true
SIMPLEX_HOME_CHANNEL=1
```

Both containers must share the host network namespace.

The repository also provides:

```bash
bash install-websockets.sh
```

This helper installs the WebSocket dependency and checks the Hermes
integration environment.

## Local Build

Build directly:

```bash
docker build -t simplex-bridge:local .
```

Or use Docker Compose:

```bash
docker compose up -d --build
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

## Image Publishing

Images are published automatically to:

```text
ghcr.io/mx10-ac2n/simplex-bridge
```

## License

The project source is licensed under GPL-3.0.
See the LICENSE file.

Third-party components retain their respective licenses.
