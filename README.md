# VR Teleop Bridge for Fairino FR5

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
  sdk_path: fairino-python-sdk-2.2.8_robot_v3.9.8/linux
motion:
  position_scale: 500.0
  max_velocity_mm_s: 50.0
  max_step_mm: 0.75
gripper:
  enabled: false
orientation:
  enabled: false
```

회전(roll/pitch/yaw) teleop은 `orientation.enabled`(기본 `false`)로 켠다.
그리퍼와 마찬가지로 기본은 실험적/비활성이다: Fairino의 rx,ry,rz Euler
컨벤션이 vendor SDK 어디에도 문서화되어 있지 않아, 표준 산업용 관례로
가정하고 구현했지만 실제 로봇에서 축/부호를 검증하기 전까지는 켜지
않는다(실제 로봇에서 한 축씩 수동 검증한 뒤 켠다). `orientation.max_step_deg`,
`orientation.max_angular_velocity_deg_s`, `orientation.max_deviation_deg`는
위치의 `max_step_mm`/`max_velocity_mm_s`/workspace에 대응하는 보수적
안전값이다.

`timing.servo_transition_window_s` 안에
`timing.servo_transition_limit`개 이상의 `ServoMoveStart/End` 전환이
발생하면 worker가 누적 start/end count, 최근 전환 원인, pose sequence와
입력 age를 `ERROR`로 기록한다. 기본값은 1초 안에 4회이며 로그 폭주를
막기 위해 같은 window 동안 한 번만 기록한다.

다른 설정 파일은 `--config`로 선택한다. `--host`, `--port`, `--scale`,
`--sdk-path`, `--pose-timeout-ms`, `--no-tls`는 YAML 값을 일시적으로
override한다.

```bash
python -m teleop --config config_bimanual.yaml
```

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
python -m teleop --config config.yaml --confirm-hardware
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

## Demonstration recording

`recording.enabled: true`이면 별도 Recorder process가 시연 데이터를
`recording.root_dir`에 raw episode로 저장한다. 녹화는 관측 전용이며
로봇 명령을 보내지 않고, 녹화 실패는 로봇 상태 머신에 영향을 주지 않는다.

1. `/monitor`의 "Demonstration recording"에 언어 지시문(task)을 입력한다.
2. **녹화 시작** 후 Quest로 시연한다. grip을 놓은 SLEEPING 구간도 계속
   기록되며 `servo_active=0`으로 표시된다.
3. **성공 저장** / **실패 저장** / **폐기** 중 하나로 끝낸다.

헤드셋을 쓴 채로도 조작할 수 있다(오른손): **A** 시작 / 성공 저장, **B** 실패
저장, **B 1초** 폐기. A는 `/monitor`에서 입력한 task(서버의 `next_task`)로
시작하며, 진동과 시야 위쪽 상태등(녹화 중 빨강)으로 결과를 알려준다. 자세한
내용은 `docs/record.md` 참고.

```text
recordings/episode_000000/
  meta.json          task, outcome, complete, timing, session config, sample schema
  robot_right.bin    servo tick(8 ms)마다 1 sample, teleop/recording/schema.py 참고
  events.jsonl       start/stop/worker 재시작 이벤트
```

sample에는 같은 tick의 관측(관절각, 관절속도, TCP, 그리퍼 위치 — SDK의
CNDE 상태 캐시에서 RPC 없이 읽음), 실제로 보낸 `ServoCart` target과
그리퍼 목표, Quest 원본 pose/grip/trigger, worker 상태가 들어간다. 모든
timestamp는 worker와 recorder가 공유하는 `CLOCK_MONOTONIC` ns다.
`meta.json`의 `complete: false`는 sample batch 누락, worker 재시작 또는
누락된 팔이 있었다는 뜻이다. numpy로 읽을 때는
`numpy.fromfile(path, dtype=numpy.dtype(schema.numpy_dtype_descr()))`를
사용한다. 서버 비정상 종료로 남은 `.tmp` episode는 다음 시작 때
`.incomplete`로 바뀐다.

### 손목 카메라 (RealSense D435i)

`recording.camera.enabled: true`이면 별도 Camera process가 D435i color를
계속 스트리밍하고, 에피소드 구간의 프레임만 인코딩한다. librealsense
호출은 GIL을 쥔 채 수 초간 블록될 수 있어 recorder와 프로세스를 분리했다.

```bash
# setup.sh가 함께 설치한다. 수동 설치 시:
.venv-linux/bin/python -m pip install -r requirements-camera.txt
# 비root 접근에는 librealsense udev 규칙이 필요하다 (librealsense 저장소의
# config/99-realsense-libusb.rules를 /etc/udev/rules.d/에 복사 후 재연결).
```

```yaml
recording:
  camera:
    enabled: true      # false면 로봇 데이터만 기록
    source: realsense  # fake: 장치 없이 합성 프레임 (dry-run 테스트용)
    serial: null       # RealSense가 여러 대일 때 지정
    width: 640
    height: 480
    fps: 30
    depth: false       # true: color에 정렬한 16-bit depth를 FFV1 무손실로 저장
```

에피소드에는 다음 파일이 추가된다.

```text
camera_wrist.mp4          RGB H.264 (CRF 18), pts = frame index
camera_wrist_depth.mkv    depth: true일 때 uint16 depth, FFV1
camera_wrist_frames.bin   프레임별 arrival_mono_ns(로봇 t_ns와 같은 시계),
                          wall clock, device timestamp, frame_number
```

카메라가 켜져 있는데 스트리밍 중이 아니면 녹화 시작이 거부되고 `/monitor`에
원인이 표시된다. 프레임 drop, frame_number 누락 또는 인코딩 오류가 있으면
`meta.json`의 `complete`가 `false`가 된다. `arrival_mono_ns`는 노출 시점이
아니라 host 수신 시점이므로 카메라-로봇 지연은 실제 장비에서 별도로
측정해 변환 시 보정한다.

### LeRobotDataset 변환

raw episode는 `lerobot` 환경에서 LeRobotDataset v3(30 Hz)로 변환한다.

```bash
~/miniforge3/envs/lerobot/bin/python -m teleop.recording.lerobot_convert \
    --raw recordings --out datasets/fr5_pick_cube --repo-id local/fr5_pick_cube
```

state/action은 TCP 위치(m) + 회전 6D + 그리퍼(0~1)이고, 손목 카메라 영상과
task가 함께 들어간다. 옵션과 정렬 원리는 `docs/record.md` 참고.

## Docker

배포용 이미지는 `ghcr.io/leebh3023/fr5_teleop`이다. Python 3.10, 고정된
의존성(`requirements-lock.txt`, `requirements-camera.txt`), SDK 2.2.8의
`Robot.py`가 들어 있어 호스트 Python 버전과 무관하게 동작한다. `v*` 태그를
push하면 GitHub Actions가 컨테이너 안에서 테스트를 통과시킨 뒤 이미지를 올린다.

설계상 고정된 실행 조건(`compose.yaml`):

- `network_mode: host`: 로봇 TCP 20003·20005, UDP 20007과 Quest 8443을 NAT 없이 쓴다.
- `init: true`, `stop_grace_period: 15s`: spawn된 worker/recorder 프로세스를
  정리하고, 녹화 종료(약 12초)가 SIGKILL로 잘리지 않게 한다.
- `restart: "no"`: 하드웨어 모드는 자동으로 다시 시작하지 않는다.
- 컨테이너는 UID/GID 1000으로 실행된다. 호스트 계정도 `id -u`가 1000이어야
  `recordings/` 파일 소유권과 `certs/key.pem`(600) 읽기가 맞는다.

### 현장 PC 설치

1. Docker Engine 설치(docker.com의 Ubuntu apt 저장소) 후
   `sudo usermod -aG docker $USER`로 docker 그룹에 추가하고 다시 로그인한다.
2. 이미지가 private이면 `read:packages` 권한 PAT로 로그인한다.

   ```bash
   docker login ghcr.io -u leebh3023
   ```
3. 실행 디렉터리를 만든다. 저장소의 `compose.yaml`, `config.yaml`,
   `config_bimanual.yaml`, `scripts/make_cert.sh`만 있으면 된다.

   ```bash
   mkdir -p ~/fr5_teleop/scripts ~/fr5_teleop/recordings
   cd ~/fr5_teleop
   # compose.yaml, config.yaml, config_bimanual.yaml, scripts/make_cert.sh 복사
   TELEOP_HOST_IP=192.168.1.50 bash scripts/make_cert.sh
   docker compose pull
   ```

   `recordings/`는 미리 만들어야 한다. 없으면 Docker가 root 소유로 만들어
   컨테이너가 쓸 수 없다.

4. 인증서(`certs/cert.pem`, `certs/key.pem`)는 3번의 `make_cert.sh`가 만든다.
   - `TELEOP_HOST_IP`는 **Quest가 접속할 PC 주소**다(`ip -4 -br addr`로 확인).
     로봇 LAN(`192.168.58.x`) 주소가 아니다. 생략하면 `hostname -I`의 첫
     주소를 쓰므로, 네트워크가 여러 개인 현장 PC에서는 항상 지정한다.
   - 이미 인증서가 있으면 새로 만들지 않고, 주소가 다를 때 경고만 한다.
     PC 주소가 바뀌었거나 1년 유효기간이 지나면 `certs/*.pem`을 지우고
     다시 실행한다. 컨테이너는 다음 실행부터 새 인증서를 쓴다.
   - 자체 서명 인증서이므로 Quest 브라우저에서 처음 접속할 때 나오는 경고를
     승인해야 WebXR이 동작한다. 인증서를 다시 만들면 다시 승인한다.

### 실행

세션마다 직접 실행하고 Ctrl+C로 종료한다. 첫 Ctrl+C(또는 `docker stop`의
SIGTERM)에 worker 종료와 녹화 마무리를 순서대로 진행하며, 그 뒤의 Ctrl+C는
무시한다. 종료가 멈춘 경우에만 `docker kill`을 쓴다.

```bash
docker compose run --rm teleop                                   # dry-run
docker compose run --rm teleop --confirm-hardware                # 단일 로봇
docker compose run --rm teleop --config /app/config_bimanual.yaml --confirm-hardware
```

config 파일은 `/app`에 읽기 전용으로 마운트되므로 workspace, scale 등은 호스트의
`config.yaml`을 고친 뒤 다시 실행하면 된다. config 안의 상대 경로(`web`,
`certs/...`, `recordings`, `sdk_path`)는 모두 `/app` 기준으로 해석된다.

### 이미지 갱신과 고정

```bash
TELEOP_TAG=0.3.0 docker compose pull          # 특정 버전 받기
TELEOP_TAG=0.3.0 docker compose run --rm teleop --confirm-hardware
```

`TELEOP_TAG`를 생략하면 `latest`를 쓴다. 현장에서는 검증한 버전 태그를
`.env`에 `TELEOP_TAG=0.3.0`으로 고정하는 것을 권장한다.

### 개발 PC에서 빌드와 검증

```bash
docker build --target test .        # 컨테이너 안에서 pytest + 서버 smoke
docker compose build                # 런타임 이미지 빌드
docker compose run --rm teleop      # dry-run 실행
```

릴리스: `teleop/__init__.py`의 `__version__`을 올리고 같은 이름의 태그
(`v0.3.0`)를 push한다.

손목 카메라(RealSense)를 컨테이너에서 쓰는 설정은 아직 포함하지 않았다.
카메라 의존성은 이미지에 들어 있지만 USB 장치 전달과 호스트 udev 규칙은 다음
단계에서 추가한다.
