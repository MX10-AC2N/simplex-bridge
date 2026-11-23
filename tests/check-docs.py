
#!/usr/bin/env python3

import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

readme = (ROOT / "README.md").read_text()
compose = (ROOT / "docker-compose.yml").read_text()
workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
entrypoint = (ROOT / "entrypoint.sh").read_text()
changelog = (ROOT / "CHANGELOG.md").read_text()
dockerfile = (ROOT / "Dockerfile").read_text()
ca_profile = (ROOT / "ca_profile.xml").read_text()
unraid_template = (ROOT / "templates" / "simplex-bridge.xml").read_text()
workflow_dir = ROOT / ".github" / "workflows"
all_workflows = {p: p.read_text() for p in sorted(workflow_dir.glob("*.yml"))}

errors = []

def check(condition, message):
    if condition:
        print(f"PASS: {message}")
    else:
        print(f"FAIL: {message}")
        errors.append(message)

image = "ghcr.io/mx10-ac2n/simplex-bridge"

check(image in compose, "Compose uses fork GHCR image")
check(image in workflow, "Workflow publishes fork GHCR image")
check(image in readme, "README documents fork GHCR image")

check(
    "ghcr.io/libre-7/simplex-bridge" not in compose,
    "Compose does not reference upstream image"
)

check(
    "SIMPLEX_DNS_CHECK_HOST" in entrypoint,
    "Entrypoint supports DNS hostname configuration"
)

check(
    "SIMPLEX_DNS_CHECK_HOST" in readme,
    "README documents DNS hostname configuration"
)

check(
    "SIMPLEX_FILES_DIR" in entrypoint and "--files-folder" in entrypoint,
    "Entrypoint supports SIMPLEX_FILES_DIR (--files-folder)"
)

check(
    "SIMPLEX_FILES_DIR" in readme,
    "README documents SIMPLEX_FILES_DIR"
)

check(
    "DNS" in changelog,
    "Changelog documents DNS changes"
)

check(
    "packages: write" in workflow,
    "Workflow has package publishing permissions"
)

check(
    "linux/amd64" in workflow and "linux/arm64" in workflow,
    "Workflow builds AMD64 and ARM64"
)

check(
    "pull_request" in workflow and "push:" in workflow,
    "Workflow validates pull requests and publishes pushes"
)

# ── F-02: the published image's OCI license label must be forced ──
# docker/metadata-action infers org.opencontainers.image.licenses from the
# repo's own LICENSE (GPL-3.0 here) and OVERRIDES any Dockerfile LABEL with
# it. The image redistributes AGPL-3.0 binaries (simplex-chat, simplexmq),
# so the override below is load-bearing, not cosmetic: remove it and the
# published manifest silently reverts to advertising the wrong license.
check(
    "org.opencontainers.image.licenses=AGPL-3.0" in workflow,
    "Workflow forces the AGPL-3.0 license label (F-02)"
)

# ── F-04: a job must actually BUILD AND RUN the image, waiting for a
# `healthy` Docker healthcheck status, on both published architectures ──
check(
    "linux/arm64" in workflow and re.search(r"State\.Health\.Status", workflow),
    "Workflow has a smoke-test job that waits for a healthy container (F-04)"
)
check(
    not re.search(r"^\s*if:\s*github\.event_name\s*==\s*'push'\s*$", workflow, re.M),
    "Build job is not entirely skipped on pull requests (F-04)"
)

# ── F-05: every third-party Action must be pinned by commit SHA, never a
# mutable tag (@v4, @v5, ...) — docker/login-action and
# docker/build-push-action hold secrets.GITHUB_TOKEN with packages:write ──
floating_action = re.compile(r"uses:\s*[\w.-]+/[\w.-]+@v[0-9][\w.-]*\s*$", re.M)
for path, text in all_workflows.items():
    check(
        not floating_action.search(text),
        f"{path.name}: no mutable @vN action references remain (F-05)"
    )

# ── F-03: the Unraid template, CA profile and OCI labels must point at the
# fork, not the upstream it was copied from. A blanket substring search would
# false-positive on the <Changes> entry that DESCRIBES this bug by name, so
# strip that block first and check only the live reference fields. ──
template_live = re.sub(r"<Changes>.*?</Changes>", "", unraid_template, flags=re.S)
check(
    "libre-7" not in template_live,
    "Unraid template no longer references libre-7 outside its changelog (F-03)"
)
check(
    "libre-7" not in ca_profile,
    "ca_profile.xml does not reference libre-7 (F-03)"
)
check(
    "libre-7" not in dockerfile,
    "Dockerfile OCI labels do not reference libre-7 (F-03)"
)
check(
    "ghcr.io/mx10-ac2n/simplex-bridge" in unraid_template,
    "Unraid template Repository points at the fork's own GHCR image (F-03)"
)

if errors:
    print(f"\n{len(errors)} check(s) failed.")
    sys.exit(1)

print("\nAll documentation checks passed.")
