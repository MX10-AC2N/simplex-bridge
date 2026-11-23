
# Changelog

All notable changes to MX10-AC2N SimpleX Bridge are documented here.

## [Unreleased]

## [v1.4.0] - 2026-11-23

### Fixed

- **F-03**: the Unraid template (`templates/simplex-bridge.xml`), CA profile
  (`ca_profile.xml`) and Dockerfile OCI labels (`vendor`, `url`, `source`,
  `documentation`) all pointed at `libre-7/simplex-bridge` — the upstream
  this project forks from — rather than at this fork. In particular the
  template's `Repository` field named `ghcr.io/libre-7/simplex-bridge`, so
  installing this template on Unraid pulled the **upstream's own image**,
  not this fork's. All references now point at `MX10-AC2N/simplex-bridge`.
  `tests/check-docs.py` now asserts no live reference to `libre-7` remains
  in these three files (a `<Changes>` entry that names the old bug by way
  of explanation is exempted from that check).
- **F-14**: there was no git tag and no release in this fork; the Unraid
  template claimed to pin `v1.3.1`, a version that was never actually cut
  here (it was copied from upstream's own template unchanged), and
  `docker-compose.yml` floated on `:latest`. `v1.4.0` is this fork's first
  real tagged release. `docker-compose.yml` and the Unraid template now
  both pin `v1.4.0` instead of `:latest`/`v1.3.1`.
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
- **F-02**: the published GHCR image advertised
  `org.opencontainers.image.licenses = GPL-3.0` (this repo's own LICENSE),
  even though it redistributes AGPL-3.0 binaries (`simplex-chat`,
  `simplexmq`). `docker/metadata-action` overrides the Dockerfile's
  `LABEL` with the repo-inferred license; the workflow now forces
  `org.opencontainers.image.licenses=AGPL-3.0` explicitly.
- **F-05**: CI actions (`actions/checkout`, `actions/setup-python`,
  `actions/upload-artifact`, `docker/setup-qemu-action`,
  `docker/setup-buildx-action`, `docker/login-action`,
  `docker/metadata-action`, `docker/build-push-action`) were pinned to
  mutable `@vN` tags despite `SECURITY.md` claiming SHA pinning. All are
  now pinned by commit SHA, at current versions.
- **F-04**: restored a `smoke` CI job that actually builds and runs the
  image (with the same `cap_drop`/`cap_add`/`no-new-privileges` set as
  `docker-compose.yml`) on both `linux/amd64` and `linux/arm64`, waiting
  for a `healthy` Docker healthcheck status before the build/publish job
  runs. The publish job no longer skips entirely on pull requests — it now
  always builds (smoke-tested first); only the actual registry push is
  gated to non-PR events.
- Fixed Docker-generated DNS resolver file permissions.
- Ensured the unprivileged SimpleX daemon can read `/etc/resolv.conf`
  and `/etc/hosts`.
- Added DNS resolution verification using the daemon UID/GID.
- Added retry handling for DNS preflight failures.
- Added diagnostic output when DNS resolution fails.

### Added

- Added `SIMPLEX_FILES_DIR` (default `/data/received_files`) and passed it
  to the daemon via `--files-folder` (confirmed against upstream
  `Options.hs`), so received attachments live in their own volume instead
  of inside `/data`. This lets a separate Home Assistant Compose project
  mount that volume read-only without any access to the SimpleX database
  or identity keys in `simplex-data`. The value is validated at startup —
  rejected if relative, or if it is bare `/` (which would otherwise
  `chown -R` the whole filesystem) — and chowned to `$PUID:$PGID` only
  when not already owned, mirroring the existing `$DATA_DIR` pattern.
  Covered by `tests/test-files-dir.sh`, which exercises the real block
  extracted from `entrypoint.sh`.
- Added a fail-closed startup guard: with `network_mode: host` (required
  for the Home Assistant integration), an unrelated process can already own
  `127.0.0.1:5225` when the container starts. The entrypoint now refuses to
  start rather than risk later readiness checks mistaking that process for
  its own daemon. Covered by `tests/test-port-preflight.sh`.
- Added named Docker volumes `simplex-inbox` and `simplex-outbox` so a
  separate Home Assistant Compose project can reference them as
  `external: true`.
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

### Pending (F-14 follow-up)

`docker-compose.yml` and the Unraid template currently pin the `v1.4.0`
**tag**, not yet a `@sha256:` **digest**. The real digest cannot be read
back before CI has actually built and published this tag. Once the
`v1.4.0` tag is pushed and `docker-publish.yml` has run:

```bash
docker buildx imagetools inspect ghcr.io/mx10-ac2n/simplex-bridge:v1.4.0
```

Replace `image: ghcr.io/mx10-ac2n/simplex-bridge:v1.4.0` in
`docker-compose.yml` with `ghcr.io/mx10-ac2n/simplex-bridge@sha256:<digest>`
and update the matching comment in `templates/simplex-bridge.xml`.

## Upstream

This fork originates from:

https://github.com/libre-7/simplex-bridge

See the upstream changelog for historical changes.
