
#!/usr/bin/env bash
#
# SimpleX Bridge - Complete regression test runner
# MX10-AC2N
#

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

FAIL=0
SKIP=0

step() {
    printf '\n========================================\n'
    printf ' %s\n' "$1"
    printf '========================================\n'
}

run_test() {
    local label="$1"
    shift

    echo
    echo ">>> $label"

    if "$@"; then
        echo "PASS: $label"
    else
        echo "FAIL: $label"
        FAIL=$((FAIL + 1))
    fi
}

skip_test() {
    local label="$1"
    echo "SKIP: $label"
    SKIP=$((SKIP + 1))
}

# --------------------------------------------------
# 1. Shell syntax
# --------------------------------------------------

step "Shell syntax validation"

SHELL_FILES=(
    "entrypoint.sh"
    "install-websockets.sh"
)

for file in "${SHELL_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        run_test "Bash syntax: $file" bash -n "$file"
    else
        echo "WARNING: $file not found"
    fi
done

for file in tests/*.sh; do
    [[ -f "$file" ]] || continue
    run_test "Bash syntax: $file" bash -n "$file"
done

# --------------------------------------------------
# 2. ShellCheck
# --------------------------------------------------

step "ShellCheck"

if command -v shellcheck >/dev/null 2>&1; then
    run_test \
        "ShellCheck" \
        shellcheck entrypoint.sh install-websockets.sh tests/*.sh
else
    skip_test "ShellCheck not installed"
fi

# --------------------------------------------------
# 3. Python compilation
# --------------------------------------------------

step "Python compilation"

PYTHON_FILES=()

for file in \
    healthcheck.py \
    tests/test-setup-userid.py \
    tests/ss_from_proc.py \
    tests/check-docs.py
do
    if [[ -f "$file" ]]; then
        PYTHON_FILES+=("$file")
    fi
done

if (( ${#PYTHON_FILES[@]} > 0 )); then
    run_test \
        "Python compilation" \
        python3 -m py_compile "${PYTHON_FILES[@]}"
else
    skip_test "No Python files found"
fi

# --------------------------------------------------
# 4. YAML validation
# --------------------------------------------------

step "YAML validation"

if python3 -c "import yaml" >/dev/null 2>&1; then

    YAML_FILES=()

    for file in \
        docker-compose.yml \
        compose.yml \
        .github/workflows/docker-publish.yml \
        .github/workflows/base-refresh.yml
    do
        [[ -f "$file" ]] && YAML_FILES+=("$file")
    done

    if (( ${#YAML_FILES[@]} > 0 )); then
        run_test \
            "YAML parsing" \
            python3 -c '
import sys
import yaml

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as f:
        yaml.safe_load(f)
    print("PASS:", path)
' "${YAML_FILES[@]}"
    else
        skip_test "No YAML files found"
    fi

else
    skip_test "PyYAML not installed"
fi

# --------------------------------------------------
# 5. XML validation
# --------------------------------------------------

step "XML validation"

XML_FILES=()

for file in templates/simplex-bridge.xml ca_profile.xml; do
    [[ -f "$file" ]] && XML_FILES+=("$file")
done

if (( ${#XML_FILES[@]} > 0 )); then
    run_test \
        "XML parsing" \
        python3 -c '
import sys
import xml.dom.minidom

for path in sys.argv[1:]:
    xml.dom.minidom.parse(path)
    print("PASS:", path)
' "${XML_FILES[@]}"
else
    skip_test "No XML files found"
fi

# --------------------------------------------------
# 6. DNS preflight regression
# --------------------------------------------------

step "DNS preflight regression"

if [[ -f tests/test-dns-preflight.sh ]]; then
    run_test \
        "DNS permissions and preflight" \
        bash tests/test-dns-preflight.sh
else
    skip_test "tests/test-dns-preflight.sh not found"
fi

# --------------------------------------------------
# 7. Port readiness regression
# --------------------------------------------------

step "Port readiness regression"

if [[ -f tests/test-port-gate.sh ]]; then
    run_test \
        "Port readiness" \
        bash tests/test-port-gate.sh
else
    skip_test "tests/test-port-gate.sh not found"
fi

# --------------------------------------------------
# 8. Installer regression
# --------------------------------------------------

step "Installer regression"

if [[ -f tests/test-installer-interpreter.sh ]]; then

    set +e
    bash tests/test-installer-interpreter.sh
    RC=$?
    set -e

    case "$RC" in
        0)
            echo "PASS: Installer regression"
            ;;
        77)
            skip_test "Installer test requires Hermes environment"
            ;;
        *)
            echo "FAIL: Installer regression (exit $RC)"
            FAIL=$((FAIL + 1))
            ;;
    esac

else
    skip_test "Installer test not found"
fi

# --------------------------------------------------
# 9. First-run setup regression
# --------------------------------------------------

step "First-run setup regression"

if [[ -f tests/test-setup-userid.py ]]; then
    run_test \
        "Setup userId" \
        python3 tests/test-setup-userid.py
else
    skip_test "Setup userId test not found"
fi

# --------------------------------------------------
# 10. Documentation consistency
# --------------------------------------------------

step "Documentation consistency"

if [[ -f tests/check-docs.py ]]; then
    run_test \
        "README, Compose, CHANGELOG and workflow" \
        python3 tests/check-docs.py
else
    skip_test "Documentation consistency test not found"
fi

# --------------------------------------------------
# 11. Final summary
# --------------------------------------------------

step "Test summary"

echo "Failed tests : $FAIL"
echo "Skipped tests: $SKIP"

if (( FAIL > 0 )); then
    echo
    echo "REGRESSION TESTS FAILED"
    exit 1
fi

echo
echo "ALL AVAILABLE TESTS PASSED"

exit 0
