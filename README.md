# VR Teleop Bridge for Fairino FR5

Meta Quest 3의 WebXR controller pose를 받아 Fairino FR5의 Cartesian servo target으로 변환하는 Ubuntu 22.04용 teleoperation bridge다.

## Architecture

```text
Quest WebXR (JavaScript)
    -> HTTPS WebSocket
Python aiohttp process
    -> latest-pose shared mailbox + control IPC
Python RobotWorker process
    -> Fairino Python SDK
FR5 controller
```

Fairino SDK는 동기 호출과 내부 network thread를 사용하므로 별도 process에 격리한다. 기본 실행은 실제 robot에 연결하지 않는 fake dry-run이다.

## Requirements

- Ubuntu 22.04 x86-64
- Python 3.10
- `python3.10-venv`
- OpenSSL
- Meta Quest가 접근할 수 있는 LAN

## Setup

```bash
./setup.sh
```

수동 설치:

```bash
python3 -m venv .venv-linux
source .venv-linux/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -e .
python -m pytest
```

## Dry-run

기본 설정은 저장소의 `config.yaml`이며 실제 robot에 연결하지 않는
`runtime.dry_run: true`다. TLS를 사용하는 Quest 경로:

```bash
python -m teleop
```

localhost smoke test:

```bash
python -m teleop --no-tls --host 127.0.0.1
```

## Configuration

IP, port, scale, SDK/Web/TLS 경로와 timing 제한은 `config.yaml`에서
수정한다. 상대 경로는 config 파일이 있는 디렉터리를 기준으로 해석된다.
알 수 없는 키나 잘못된 타입은 시작 단계에서 거부된다.

```yaml
runtime:
  dry_run: true
server:
  host: 0.0.0.0
  port: 8443
robot:
  ip: 192.168.58.2
  sdk_path: fairino-python-sdk-main/linux
motion:
  position_scale: 500.0
```

다른 설정 파일은 `--config`로 선택한다. `--host`, `--port`, `--scale`,
`--sdk-path`, `--pose-timeout-ms`, `--no-tls`는 YAML 값을 일시적으로
override한다.

```bash
python -m teleop --config /etc/vr-teleop/config.yaml
```

Health endpoints:

```text
GET /health/live
GET /health/ready
GET /ws
```

## Real robot

실제 robot 모드는 YAML의 `runtime.dry_run: false`와 `robot.ip`를
설정한 뒤에도 매 실행마다 명시적인 확인 옵션을 요구한다.

```bash
python -m teleop --config /etc/vr-teleop/config.yaml --confirm-hardware
```

CLI만으로 일시 override할 때는
`python -m teleop --robot 192.168.58.2 --confirm-hardware`를 사용할 수
있다. `--dry-run`은 실제 로봇 설정 파일도 fake mode로 강제한다.

실행 전 다음을 사람이 확인한다.

- SDK와 controller firmware 호환성
- 물리 E-stop
- controller 통신 단절 정지 설정
- 낮은 scale과 보수적인 workspace
- robot 주변 안전과 observer

vendor SDK의 `example/` 파일은 실제 motion command를 top-level에서 실행할 수 있으므로 자동 실행하지 않는다.

## Grip sleep behavior

grip 해제는 WebXR session이나 controller lease를 종료하지 않는다.
worker는 `ServoMoveEnd()`를 한 번 호출하고 `SLEEPING` 상태가 된다.
같은 WebXR session에서 grip을 다시 누르면 현재 VR/TCP 원점을 새로
잡고 servo를 재개한다. WebXR 종료, WebSocket 끊김, 명시적 control
release는 session을 실제로 해제하고 `IDLE`로 돌아간다.

## WSL

WSL Ubuntu 22.04는 import, unit, process, WebSocket smoke test에 사용할 수 있다. `/mnt/c`에서 측정한 timing은 native Ubuntu 성능으로 해석하지 않으며 WSL에서 실제 robot motion test를 수행하지 않는다.

## Deployment

초기 운영 방식은 native virtualenv와 `systemd`다. 예제 unit은 dry-run으로 시작한다.

```bash
sudo install -d -o vr-teleop -g vr-teleop /opt/vr-teleop
sudo install -d -o vr-teleop -g vr-teleop /etc/vr-teleop/tls
sudo install -m 0640 -o root -g vr-teleop deploy/config.example.yaml /etc/vr-teleop/config.yaml
sudo install -m 0644 deploy/vr-teleop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vr-teleop
```

예제 config는 dry-run으로 설치된다. 실제 robot을 활성화하려면 config를
검토해 `dry_run: false`로 바꾸고 systemd `ExecStart`에
`--confirm-hardware`를 명시적으로 추가한다. 인증서와 개인 키는
`/etc/vr-teleop/tls`에서 관리하고 저장소에 커밋하지 않는다.

자세한 상태 머신, IPC, 안전 불변조건과 완료 기준은 `AGENTS.md`를 따른다.
