# VR Teleop Bridge for Fairino FR5

Meta Quest 3의 WebXR controller pose를 받아 Fairino FR5의 Cartesian servo target으로 변환하는 Ubuntu 22.04용 teleoperation bridge다.

현재 패키지 버전은 `0.2.0rc2`이며, 저장소에는 현장 피드백을 반영한
다음 release candidate용 변경이 `Unreleased`로 누적되어 있다.
자동 검증 범위와 stable release 전에
필요한 실제 장비 검증은 `RELEASE_CHECKLIST.md`에 구분되어 있다.
운영 배포 단위는 source distribution을 풀어 설치하는 native
venv+systemd 구성이다. wheel은 Python service 코드만 제공하며 별도
config, WebXR static files와 Fairino SDK 경로가 필요하다.
source distribution도 특정 firmware용 vendor SDK를 포함하지 않는다.
controller firmware와 일치하는 공식 SDK를 별도로 배치한다.
현장 release bundle의 `requirements-lock.txt`와 `wheelhouse/`는
Ubuntu 22.04 / Python 3.10 x86-64 오프라인 설치에 사용한다.

## Architecture

```text
Quest WebXR (JavaScript)
    -> HTTPS WebSocket
Python aiohttp process
    -> hand별 latest-pose mailbox + control IPC
Python RobotWorker process (robot당 1개)
    -> hand별 Fairino Python SDK
FR5 controller 1대 또는 좌/우 2대
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

PC에 network interface가 여러 개이면 Quest가 접근할 주소를 명시한다.

```bash
TELEOP_HOST_IP=192.168.1.50 ./setup.sh
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
  max_velocity_mm_s: 50.0
  max_step_mm: 0.75
gripper:
  enabled: false
```

`timing.servo_transition_window_s` 안에
`timing.servo_transition_limit`개 이상의 `ServoMoveStart/End` 전환이
발생하면 worker가 누적 start/end count, 최근 전환 원인, pose sequence와
입력 age를 `ERROR`로 기록한다. 기본값은 1초 안에 4회이며 로그 폭주를
막기 위해 같은 window 동안 한 번만 기록한다.

다른 설정 파일은 `--config`로 선택한다. `--host`, `--port`, `--scale`,
`--sdk-path`, `--pose-timeout-ms`, `--no-tls`는 YAML 값을 일시적으로
override한다.

```bash
python -m teleop --config /etc/vr-teleop/config.yaml
```

### Bimanual configuration

`config_bimanual.yaml`은 좌·우 FR5를 각각 독립 `RobotWorker` process와
IPC generation으로 격리하는 안전한 dry-run 예제다. `robot.ip`와
`robot.arms`는 함께 사용할 수 없으며 양팔 모드는 `left`, `right`를
각각 정확히 한 번 설정해야 한다.

```yaml
runtime:
  dry_run: true
robot:
  sdk_path: fairino-python-sdk-main/linux
  arms:
    left:
      ip: 192.168.58.2
    right:
      ip: 192.168.58.3
```

```bash
python -m teleop --config config_bimanual.yaml --no-tls
```

WebXR pose sequence, mailbox, grip/trigger control event, heartbeat와 status는
hand별로 분리된다. 한쪽 worker가 hang 또는 비정상 종료하면 양팔을 하나의
안전 domain으로 보고 두 pose를 모두 무효화하고 두 worker에 stop을 보낸 뒤
controller lease를 해제한다. 종료도 두 worker에 먼저 `SHUTDOWN`을 broadcast한
후 하나의 공통 deadline으로 join/terminate/kill한다. 이 구조는 두 controller의
servo clock을 hard real-time으로 동기화하지는 않는다.

서버는 WebSocket `hello.control_hands`로 허용 hand를 알린다. 단일 로봇
모드에서는 WebXR가 오른손(없으면 하나의 fallback controller)만 선택해
`right` stream으로 정규화한다. 좌·우 pose를 하나의 worker mailbox에
번갈아 넣지 않으며, 허용되지 않은 hand 메시지는 protocol error로 거부한다.

실제 양팔 모드는 예제의 `runtime.dry_run`을 `false`로 바꾸고 두 IP, SDK,
workspace를 장비별로 승인한 뒤 `--confirm-hardware`를 사용한다. 단일 로봇용
`--robot` CLI override는 양팔 설정과 함께 사용할 수 없다.

Health endpoints:

```text
GET /health/live
GET /health/ready
GET /ws
GET /monitor
```

`/monitor`는 control lease를 claim하지 않는 작업자용 observer 화면이다.
Quest/WebXR의 frame·pose gap, WebSocket RTT/drop과 robot worker 상태를
Ubuntu PC나 별도 작업자 단말에서 확인한다. 알려진 문제와 원인 분리
절차는 `docs/KNOWN_ISSUES_KO.md`를 따른다.

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

`ServoCart`는 FAIRINO 공식 예제에 맞춰 절대 좌표 mode, 8 ms `cmdT`,
`acc=0`, `vel=0`, `filterT=0`, `gain=0`을 사용한다. SDK가 미개방으로
표시한 파라미터를 motion tuning 용도로 변경하지 않는다.
SDK V2.0.8의 legacy signature에는 `exaxis`가 없으므로 adapter가
signature 차이를 감지해 해당 인자만 제외한다. 연결 flag도 신형
`is_connect`와 legacy `is_conect`를 모두 지원한다.

## Grip sleep behavior

grip 해제는 WebXR session이나 controller lease를 종료하지 않는다.
worker는 `ServoMoveEnd()`를 한 번 호출하고 `SLEEPING` 상태가 된다.
같은 WebXR session에서 grip을 다시 누르면 현재 VR/TCP 원점을 새로
잡고 servo를 재개한다. WebXR 종료, WebSocket 끊김, 명시적 control
release는 session을 실제로 해제하고 `IDLE`로 돌아간다.

pose는 최신 snapshot만 유지하지만 grip press/release 전환은 별도의
비손실 control pipe로 전달한다. 두 전환이 한 worker tick 안에 연속
도착해도 release edge가 최신 pose에 덮어써지지 않는다. stale timeout
뒤에는 UI의 재무장 안내에 따라 grip을 완전히 놓았다 다시 누른다.

## Trigger/gripper behavior

그리퍼는 기본 비활성이다. controller-side gripper 설정과 수동 저위험
검증을 마친 뒤 `gripper.enabled: true`로 켠다. ACTIVE 상태에서
trigger rising edge가 들어오면 worker는 먼저 `ServoMoveEnd()`로
Cartesian servo를 종료하고 non-blocking `MoveGripper`를 보낸다.
고정 sleep 대신 `GetGripperMotionDone()`을 polling하며, 완료 후에는
자동으로 servo를 재시작하지 않는다. 작업자는 완료 후 grip을 완전히
놓았다 다시 눌러야 한다.

grip과 trigger transition은 모두 latest-pose mailbox와 별도의 비손실
control pipe로 전달된다. WebXR UI에는 `GRIPPER_ACTION`, 그리퍼 목표
위치와 재무장 요구가 표시된다.

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
실제 장비 설치와 commissioning 절차는 `docs/FIELD_MANUAL_KO.md`를 따른다.
