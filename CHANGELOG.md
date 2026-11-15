
# Changelog

All notable changes to MX10-AC2N SimpleX Bridge are documented here.

## [Unreleased]

### Fixed

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
