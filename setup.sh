#!/bin/bash
# VR Teleop 환경 설정 스크립트
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"

echo "=== VR Teleop for FR5 Setup ==="

# 1. SSL 인증서 생성 (WebXR은 HTTPS 필수)
if [ ! -f "$CERT_DIR/cert.pem" ]; then
    echo "[1/2] SSL 자체 서명 인증서 생성 중..."
    # PC의 IP를 SAN에 포함 (Quest 브라우저 호환)
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$CERT_DIR/key.pem" \
        -out "$CERT_DIR/cert.pem" \
        -days 365 -nodes \
        -subj "/CN=$LOCAL_IP" \
        -addext "subjectAltName=IP:$LOCAL_IP,IP:127.0.0.1" \
        2>/dev/null
    echo "    인증서 생성 완료: $CERT_DIR/"
    echo "    PC IP: $LOCAL_IP"
else
    echo "[1/2] SSL 인증서 이미 존재합니다."
fi

# 2. Python 패키지 설치
echo "[2/2] Python 패키지 설치 중..."
pip install -r "$SCRIPT_DIR/requirements.txt" -q

echo ""
echo "=== 설정 완료 ==="
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo "서버 시작: python $SCRIPT_DIR/server.py"
echo "Quest 3에서 접속: https://$LOCAL_IP:8443"
