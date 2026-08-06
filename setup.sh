#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv-linux"
CERT_DIR="${SCRIPT_DIR}/certs"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This setup script targets Ubuntu 22.04." >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))'; then
    echo "Python 3.10 is required for the bundled Fairino SDK." >&2
    exit 1
fi

echo "[1/4] Creating Python 3.10 virtual environment"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${SCRIPT_DIR}/requirements-dev.txt"
"${VENV_DIR}/bin/python" -m pip install --no-deps -e "${SCRIPT_DIR}"

echo "[2/4] Preparing local TLS certificate"
mkdir -p "${CERT_DIR}"
LOCAL_IP="$(hostname -I | awk '{print $1}')"
if [[ ! -f "${CERT_DIR}/cert.pem" || ! -f "${CERT_DIR}/key.pem" ]]; then
    if [[ -z "${LOCAL_IP}" ]]; then
        echo "Unable to determine a local IPv4 address." >&2
        exit 1
    fi
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CERT_DIR}/key.pem" \
        -out "${CERT_DIR}/cert.pem" \
        -days 365 -nodes \
        -subj "/CN=${LOCAL_IP}" \
        -addext "subjectAltName=IP:${LOCAL_IP},IP:127.0.0.1"
    chmod 600 "${CERT_DIR}/key.pem"
elif ! openssl x509 -in "${CERT_DIR}/cert.pem" -noout -ext subjectAltName \
    | grep -Fq "IP Address:${LOCAL_IP}"; then
    echo "WARNING: existing certificate does not cover ${LOCAL_IP}." >&2
    echo "Remove certs/cert.pem and certs/key.pem to regenerate it for this host." >&2
fi

echo "[3/4] Running non-hardware tests"
"${VENV_DIR}/bin/python" -m pytest

echo "[4/4] Running dry-run server smoke test"
TELEOP_PYTHON="${VENV_DIR}/bin/python" \
    bash "${SCRIPT_DIR}/tests/smoke/run_server_smoke.sh"

echo
echo "Setup complete."
echo "Dry-run: ${VENV_DIR}/bin/python -m teleop"
echo "Quest URL: https://${LOCAL_IP}:8443"
echo "Real robot mode always requires both --robot and --confirm-hardware."
