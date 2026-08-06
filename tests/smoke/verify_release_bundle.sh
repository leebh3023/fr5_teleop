#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUNDLE_DIR="${1:-${PROJECT_DIR}/release/vr-teleop-0.2.0rc2}"
VERSION="0.2.0rc2"
VERIFY_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "${VERIFY_DIR}"
}
trap cleanup EXIT

python3 -m venv "${VERIFY_DIR}/venv"
"${VERIFY_DIR}/venv/bin/python" -m pip install \
    --no-index \
    --find-links "${BUNDLE_DIR}/wheelhouse" \
    -r "${BUNDLE_DIR}/requirements-lock.txt"
"${VERIFY_DIR}/venv/bin/python" -m pip install \
    --no-index \
    --no-deps \
    "${BUNDLE_DIR}/vr_teleop-${VERSION}-py3-none-any.whl"
"${VERIFY_DIR}/venv/bin/python" -c \
    "import teleop; assert teleop.__version__ == '${VERSION}'"
echo "offline install: OK"

TELEOP_PYTHON="${VERIFY_DIR}/venv/bin/python" \
TELEOP_SMOKE_PORT=18444 \
TELEOP_SMOKE_LOG="${VERIFY_DIR}/smoke.log" \
    bash "${PROJECT_DIR}/tests/smoke/run_server_smoke.sh"

ARCHIVE="${BUNDLE_DIR}/vr_teleop-${VERSION}.tar.gz"
ARCHIVE_CONTENTS="${VERIFY_DIR}/archive-contents.txt"
tar -tzf "${ARCHIVE}" > "${ARCHIVE_CONTENTS}"
grep -q "docs/FIELD_MANUAL_KO.md" "${ARCHIVE_CONTENTS}"
grep -q "requirements-lock.txt" "${ARCHIVE_CONTENTS}"
if grep -Eq '\.(pem|key)$' "${ARCHIVE_CONTENTS}"; then
    echo "secret-like certificate or key file found in source archive" >&2
    exit 1
fi
echo "archive content and secret scan: OK"
