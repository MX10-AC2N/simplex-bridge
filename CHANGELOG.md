
# Changelog

All notable changes to MX10-AC2N SimpleX Bridge are documented here.

## [Unreleased]

### Fixed

- **F-01**: `/ad` was called unconditionally on first-run setup, but it is
  not idempotent (it fails with `chatCmdError`/`duplicateContactLink` when
  an address already exists). Any crash between address creation and the
  `.setup-complete` marker being written left the container unable to ever
  start again. The setup block now queries `/show_address` first and only
  creates the address when none exists, matching upstream's own bot
  initialization pattern (`Bot.hs:55-63`).
- **F-07**: a bot-command menu rejected by the daemon
  (`/set bot commands` → `chatCmdError`) was logged as both a warning and
  "Bot commands configured" in the same run. A rejected menu is now
  reported as an explicit error, and the setup run exits non-zero so it is
  retried on the next restart instead of being silently accepted.
- Fixed Docker-generated DNS resolver file permissions.
- Ensured the unprivileged SimpleX daemon can read `/etc/resolv.conf`
  and `/etc/hosts`.
- Added DNS resolution verification using the daemon UID/GID.
- Added retry handling for DNS preflight failures.
- Added diagnostic output when DNS resolution fails.

### Added

- Added `SIMPLEX_DNS_CHECK_HOST` environment variable.
- Added DNS preflight regression tests.
- Added documentation for DNS troubleshooting.
- Added fork-specific GHCR image publishing.
- Added multi-architecture image builds.
- Added documentation consistency tests.

### Changed

- Updated Docker Compose to build and deploy the MX10-AC2N fork.
- Updated README deployment instructions.
- Updated image registry references to GHCR.
- Preserved Docker capability restrictions and no-new-privileges.

## Upstream

This fork originates from:

https://github.com/libre-7/simplex-bridge

See the upstream changelog for historical changes.
