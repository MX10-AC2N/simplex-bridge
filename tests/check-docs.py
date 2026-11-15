
#!/usr/bin/env python3

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

readme = (ROOT / "README.md").read_text()
compose = (ROOT / "docker-compose.yml").read_text()
workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
entrypoint = (ROOT / "entrypoint.sh").read_text()
changelog = (ROOT / "CHANGELOG.md").read_text()

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

if errors:
    print(f"\n{len(errors)} check(s) failed.")
    sys.exit(1)

print("\nAll documentation checks passed.")
