# AGENTS.md

## 문서 목적

이 문서는 Meta Quest 3 WebXR 컨트롤러로 Fairino FR5를 조작하는 텔레옵 브리지의 구현·검증·배포 기준이다. 저장소에서 작업하는 모든 agent는 기능 추가보다 안전한 정지, 입력 만료, 오류 격리, 재현 가능한 배포를 먼저 만족시킨다.

## 최종 목표

Ubuntu 22.04 x86-64 장비 한 대에서 다음 구조를 안정적으로 실행하는 것이 최종 목표다.

```text
Meta Quest 3
    WebXR UI (JavaScript)
        │ HTTPS / versioned WebSocket
        ▼
Teleop Web Process (Python 3.10 + aiohttp)
    - 연결, TLS, protocol 검증
    - 단일 controller lease
    - 최신 pose 게시
    - 상태/오류를 UI에 전송
        │ bounded IPC
        ▼
RobotWorker Process (Python 3.10)
    - Fairino SDK의 유일한 application-level 소유자
    - 8 ms best-effort servo loop
    - clutch/servo/fault 상태 머신
    - stale input 및 lifecycle 정지
        │ Fairino Python SDK
        ▼
Fairino FR5 Controller
```

완성된 시스템은 다음을 만족해야 한다.

1. WebSocket 이벤트 루프는 Fairino SDK 지연이나 hang의 영향을 받지 않는다.
2. 로봇 SDK는 별도 프로세스에 격리되고, 부모 프로세스가 worker health를 감시한다.
3. servo tick은 WebXR frame 및 WebSocket 수신 주기와 분리된다.
4. grip 해제, WebXR 종료, 연결 해제, pose timeout, worker fault, 서버 종료가 모두 명시적인 stop 경로로 연결된다.
5. 실제 로봇 없이 protocol, 계산, 상태 머신, IPC, process lifecycle을 자동 검증할 수 있다.
6. Ubuntu 22.04에서 한 번의 문서화된 설치 절차와 `systemd` unit으로 재현 가능하게 배포한다.
7. 실제 로봇 활성화에는 명시적인 hardware 옵션과 작업자 확인이 필요하며 기본 실행은 항상 dry-run이다.

125 Hz는 일반 Ubuntu 커널, Python, XML-RPC 환경에서 hard real-time 보장이 아니다. 8 ms는 목표 주기이며 실제 jitter와 missed tick을 측정해 공개한다. 안전은 주기 정확도 하나가 아니라 dead-man, stale timeout, controller 측 통신 단절 정지, 물리적 비상 정지를 겹쳐서 확보한다.

## 확정된 기술 선택

- 타깃 OS: Ubuntu 22.04 LTS, x86-64
- 서버 런타임: Python 3.10
- 비동기 웹 계층: `asyncio` + `aiohttp`
- 로봇 격리: `multiprocessing`의 별도 `RobotWorker` process
- 브라우저 계층: 기존 JavaScript/WebXR 유지
- 서비스 관리자: `systemd`
- 테스트: `pytest`

Node.js 서버로 전체 이주하지 않는다. WebXR UI는 JavaScript가 적합하지만 첨부된 Fairino SDK는 Python 전용이며 동기 XML-RPC와 내부 socket/thread를 사용한다. Node 서버를 추가하면 결국 Python child service가 필요해져 개발·배포 런타임이 두 개가 된다.

Python worker는 일반 thread가 아니라 process로 둔다. 첨부 SDK의 여러 메서드는 socket 오류 시 무한 재시도할 수 있어 thread를 취소할 수 없기 때문이다. process 강제 종료는 정상 정지의 대체물이 아니라 최후의 containment 수단이다.

## 현재 저장소와 확인된 환경

- `server.py`: 웹, 계산, 상태, SDK 호출이 한 파일에 결합된 현재 진입점
- `web/index.html`: Quest WebXR UI와 WebSocket client
- `setup.sh`: 인증서 생성과 패키지 설치를 의도한 초기 스크립트
- `fairino-python-sdk-main/`: Windows/Linux SDK, 예제, build 산출물이 섞인 vendor tree
- `certs/`: 현재 자체 서명 인증서와 개인 키가 존재
- `main.py`: PyCharm 샘플이며 애플리케이션 진입점이 아님
- 의존성 선언 파일과 프로젝트 테스트는 아직 없음
- 현재 디렉터리는 Git 저장소로 초기화되어 있지 않음

Ubuntu 22.04 WSL smoke test에서 다음을 확인했다.

- Ubuntu 22.04.5 LTS의 기본 Python은 3.10.12다.
- `fairino-python-sdk-main/linux/fairino/Robot.py` import가 성공한다.
- 포함된 native extension은 CPython 3.10, x86-64 ELF이며 `libc`에 링크된다.
- `server.py`, `main.py`, `web/index.html`의 현재 Python/JavaScript 구문은 유효하다.
- WSL Python에는 `numpy`, `pytest`가 있고 `aiohttp`는 없다.
- Linux Node runtime은 설치되어 있지 않다.

vendor SDK 소스가 보고하는 버전은 `SDK V2.2.7 / Robot V3.9.7`이지만 동봉 README는 V2.0.9까지만 기록한다. 예제와 구현의 `cmdType` 값도 서로 맞지 않는 부분이 있으므로 예제 코드를 API 계약으로 간주하지 않는다.

현재 즉시 수정해야 하는 호환성 오류는 다음과 같다.

1. `server.py`는 프로젝트 상위의 Linux SDK를 찾지만 실제 SDK는 프로젝트 내부에 있다.
2. 첨부 SDK의 `ServoCart()`는 필수 `exaxis` 인자를 요구하지만 현재 서버는 전달하지 않는다.
3. `Robot.RPC()`는 연결 실패 시에도 객체를 반환할 수 있는데 현재 서버는 무조건 연결 성공으로 기록한다.
4. SDK 오류 반환은 상황에 따라 정수 또는 tuple일 수 있는데 현재 코드는 tuple만 가정한다.
5. 종료 시 `ServoMoveEnd()`만 호출하고 `CloseRPC()`와 SDK 내부 thread/socket lifecycle을 명시적으로 정리하지 않는다.
6. `webxr_ended`는 로그로만 처리되어 마지막 pose가 `grip=true`였을 때 servo가 남을 수 있다.
7. 현재 인증서 개인 키는 저장소 안에 있다. Git을 초기화하기 전에 외부 secret 경로로 옮기고 ignore 규칙을 추가해야 한다.

SDK의 `example/` 파일은 테스트가 아니다. 실제 로봇을 즉시 움직이는 top-level 코드가 많으므로 agent는 이를 자동 실행하지 않는다.

## 제안 디렉터리 구조

리팩터링 후 목표 구조는 다음과 같다. 이름은 구현 중 소폭 조정할 수 있지만 경계와 책임은 유지한다.

```text
pyproject.toml
requirements.txt
README.md
server.py                       # 임시 호환 wrapper, 최종적으로 얇은 entry point

teleop/
    __init__.py
    __main__.py                 # python -m teleop
    config.py                   # validated immutable configuration
    protocol.py                 # WebSocket schema와 dataclass
    control_math.py             # 좌표 변환, EMA, clamp
    controller_lease.py         # 단일 controller 소유권
    ipc.py                      # latest pose, control, status channel
    app.py                      # aiohttp application/lifecycle
    metrics.py                  # timing과 fault counters

    robot/
        __init__.py
        client.py               # RobotClient Protocol
        fairino_client.py       # vendor SDK adapter
        fake_client.py          # deterministic fake/hang/fault injection
        state.py                # worker state와 transition
        worker.py               # child process entry point와 servo loop
        supervisor.py           # spawn/watchdog/graceful stop/kill

web/
    index.html

tests/
    unit/
    integration/
    smoke/

deploy/
    vr-teleop.service
    config.example.toml
```

vendor SDK는 애플리케이션 package 안으로 복사하지 않는다. 기본 SDK 경로는 프로젝트 내부 Linux SDK로 둘 수 있지만 운영 배포에서는 config 또는 CLI로 명시한다.

## 구성 설계

안전과 timing 관련 값은 frozen dataclass인 `TeleopConfig` 한 곳에서 관리하고 시작 전에 전부 검증한다.

최소 설정 필드는 다음과 같다.

```text
runtime:
    dry_run
    log_level
    status_hz

server:
    host
    port
    tls_cert_path
    tls_key_path
    allowed_origins
    control_token
    max_ws_message_bytes

robot:
    ip
    sdk_path
    servo_period_s
    pose_timeout_s
    worker_watchdog_s
    graceful_shutdown_s
    position_scale
    max_step_mm
    workspace_bounds
    exaxis_default
```

초기 dry-run 기준값은 기존 코드의 `servo_period_s=0.008`, `position_scale=500`, `max_step_mm=1.5`를 유지한다. `pose_timeout_s=0.100`, `status_hz=10`, `worker_watchdog_s=0.500`은 검증을 시작하기 위한 보수적 후보값이지 하드웨어 승인값이 아니다. 실제 로봇 테스트 결과 없이 timeout, step, workspace, scale 제한을 완화하지 않는다.

CLI보다 config 파일을 기준으로 하고 CLI는 배포별 경로나 dry-run 선택만 override한다. 실제 로봇 모드는 `--robot <ip>`와 별도의 `--confirm-hardware`가 함께 있을 때만 허용한다.

## WebSocket protocol 설계

protocol은 반드시 정수 `version`을 갖는다. 서버가 지원하지 않는 version은 연결 단계에서 거부한다.

pose 메시지의 목표 형태는 다음과 같다.

```json
{
  "version": 1,
  "type": "pose",
  "session_id": "server-issued-id",
  "seq": 1234,
  "client_time_ms": 456789.25,
  "hand": "right",
  "position_m": [0.0, 1.2, -0.3],
  "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
  "grip": true,
  "trigger": false
}
```

서버는 다음을 검증한다.

- message byte 크기
- `version`, `type`, `session_id`, 증가하는 `seq`
- 배열 길이와 숫자 type
- 모든 수의 유한성
- position의 비정상적인 절대 범위
- quaternion norm의 최소값과 정규화 가능성
- 허용된 `hand`
- controller lease 소유자 여부

`client_time_ms`는 지연 분석에만 사용한다. stale timeout은 서로 다른 장치의 clock을 비교하지 않고 서버가 수신한 `time.monotonic_ns()`를 기준으로 계산한다.

control event는 최소한 다음을 구분한다.

```text
webxr_started
webxr_ended
claim_control
release_control
fault_reset
```

`webxr_ended`, `release_control`, controller WebSocket EOF는 worker의 stop control event로 전달한다. 브라우저도 WebXR 종료 전에 마지막 `grip=false` pose를 best-effort로 보내지만 서버는 그 메시지에 의존하지 않는다.

status 메시지는 pose마다 회신하지 않고 기본 10 Hz로 제한한다.

```json
{
  "version": 1,
  "type": "status",
  "state": "ACTIVE",
  "tracking": true,
  "controller_id": "id",
  "ack_seq": 1234,
  "input_age_ms": 11.2,
  "robot_tcp": [300, 0, 400, 180, 0, 0],
  "servo": {
    "tick": 9912,
    "missed_ticks": 2,
    "last_jitter_ms": 0.4
  },
  "fault": null
}
```

브라우저는 `WebSocket.bufferedAmount` 상한을 확인한다. 상한을 넘으면 새 pose를 보내지 않고 다음 frame의 최신 pose로 대체한다. 과거 pose를 나중에 몰아서 재생하지 않는다.

## controller lease 설계

- 동시에 여러 WebSocket이 접속할 수 있지만 controller는 한 session만 허용한다.
- 첫 controller 또는 명시적인 `claim_control` 성공 session에 lease를 부여한다.
- 다른 client는 observer status만 받거나 명시적인 busy 오류를 받는다.
- lease는 WebSocket, WebXR session, server-issued `session_id`에 묶는다.
- controller EOF, WebXR 종료, protocol violation, lease timeout은 즉시 lease를 해제하고 worker에 stop을 보낸다.
- worker 재시작 뒤에는 이전 lease와 grip 상태를 복원하지 않는다. 사용자가 grip을 완전히 놓았다가 다시 눌러야 ARMING이 가능하다.

## process와 IPC 설계

부모는 `multiprocessing.get_context("spawn")`으로 worker를 생성한다. Ubuntu의 기본 `fork`를 암묵적으로 사용하지 않는다. 실행 중인 asyncio loop, lock, socket 또는 SDK 내부 thread 상태를 child에 상속하지 않기 위해서다.

Fairino SDK import와 `Robot.RPC()` 생성은 child process 안에서만 수행한다. 부모 process와 테스트 discovery 단계에서는 vendor SDK에 연결하지 않는다.

IPC는 목적별로 분리한다.

1. `LatestPoseMailbox`
   - 고정 크기 shared memory와 짧게 유지되는 lock으로 구현한다.
   - pose를 누적하지 않고 항상 한 개의 최신 snapshot만 보관한다.
   - snapshot에는 session generation, sequence, server receive monotonic time, pose, buttons, valid flag가 들어간다.
   - lock을 잡은 상태에서 SDK 호출, logging, JSON 처리 또는 sleep을 하지 않는다.
2. control channel
   - parent→worker 단방향 pipe다.
   - `RELEASE`, `SESSION_LOST`, `FAULT_RESET`, `SHUTDOWN`처럼 유실되면 안 되는 저빈도 event를 전달한다.
   - pipe EOF도 `SESSION_LOST`로 취급한다.
3. status channel
   - worker→parent 단방향 bounded channel이다.
   - telemetry는 오래된 값을 버릴 수 있지만 FAULT 원인과 state transition은 다음 status에 계속 포함한다.
   - status 전송 때문에 servo loop가 block되면 안 된다.
4. heartbeat
   - worker가 SDK call 밖에서 갱신하는 monotonic timestamp다.
   - heartbeat가 `worker_watchdog_s`를 넘으면 부모는 worker hang으로 판단한다.

worker generation마다 새 IPC object를 만든다. 강제 종료된 worker가 사용하던 lock, pipe, queue를 다음 worker에서 재사용하지 않는다.

부모의 종료 순서는 다음과 같다.

1. 새 controller 입력 차단
2. worker에 `SHUTDOWN` 전송
3. `graceful_shutdown_s` 동안 `STOPPING -> SHUTDOWN` 대기
4. timeout이면 SIGTERM
5. 추가 timeout 뒤에도 남아 있으면 SIGKILL
6. 강제 종료 사실과 물리적 정지 보장 실패 가능성을 명확히 기록

강제 종료 뒤 worker를 자동으로 ACTIVE 상태로 복구하지 않는다. 새 process는 항상 IDLE/FAULT에서 시작하며 작업자의 명시적 reset과 새 grip rising edge가 필요하다.

## RobotWorker 상태 머신

상태는 worker 한 곳에서만 변경한다.

```text
STARTING
    -> IDLE          SDK 연결/초기화 성공
    -> FAULT         연결 또는 초기화 실패

IDLE
    -> ARMING        유효하고 fresh한 controller pose에서 grip rising edge
    -> SHUTDOWN      종료 요청

ARMING
    -> ACTIVE        TCP 원점 획득 + ServoMoveStart 성공
    -> STOPPING      grip/lease/session 상실
    -> FAULT         SDK 오류

ACTIVE
    -> STOPPING      grip 해제, stale pose, session 상실, 종료 요청
    -> FAULT         ServoCart 또는 safety 오류

STOPPING
    -> IDLE          정상 stop 완료
    -> FAULT         stop 실패
    -> SHUTDOWN      종료 경로의 stop 완료

FAULT
    -> IDLE          grip=false, 연결 정상, 명시적 fault_reset 성공
    -> SHUTDOWN      종료 요청
```

상태와 별도로 `servo_started` boolean을 둔다. `ServoMoveStart()`가 정확히 0을 반환했을 때만 `True`가 된다. `True`가 된 각 cycle에는 `ServoMoveEnd()` 시도가 정확히 한 번 대응해야 한다.

새 ARMING에서는 다음 값을 한 transition에서 초기화한다.

- VR origin position/quaternion
- 실제 robot TCP origin
- EMA/filter state
- `last_target_tcp`
- `last_send/tick` 기준 시간
- clutch session sequence

FAULT는 latch된다. grip을 계속 누른 상태에서 자동 재시작하지 않는다.

## servo loop 설계

- clock은 `time.monotonic_ns()`를 사용한다.
- 목표 tick은 이전 tick 완료 시각이 아니라 절대 `next_deadline += period`로 계산한다.
- 심한 overrun 뒤 밀린 명령을 burst로 따라잡지 않는다. missed tick을 기록하고 다음 유효 deadline으로 건너뛴다.
- 매 tick에서 control event를 먼저 처리하고, 최신 pose age를 확인한 뒤에만 target을 계산한다.
- grip=true라도 pose age가 timeout을 넘으면 STOPPING으로 전이한다.
- target 계산은 순수 함수로 유지하고 SDK adapter에 이미 검증된 6D target만 전달한다.
- 회전 텔레옵은 현재 범위가 아니다. orientation은 protocol에서 수집·검증하되 초기 구현은 robot origin orientation을 유지한다.
- `MAX_DELTA_PER_STEP` 같은 중복 제한을 만들지 않는다. 실제 적용되는 step 제한은 config 한 곳에만 둔다.
- status에는 tick period, jitter, SDK call duration, overrun, missed tick을 포함한다.

## Fairino adapter 설계

`RobotClient` Protocol은 애플리케이션이 필요한 최소 기능만 노출한다.

```text
connect
initialize
get_current_tcp
servo_start
servo_cart
servo_end
reset_fault
close
```

`FairinoRobotClient`의 책임은 다음과 같다.

- Ubuntu와 Python version, SDK 경로, architecture 검증
- child process 안에서만 `sys.path`와 vendor import 처리
- `Robot.RPC.is_connect` 및 실제 harmless query를 이용한 연결 검증
- SDK의 정수/tuple 혼합 반환을 일관된 `RobotResult`로 정규화
- 모든 SDK 오류 코드를 typed exception 또는 result로 변환
- `ServoCart(mode=0, ..., exaxis=[0,0,0,0])`처럼 첨부 SDK 시그니처를 정확히 적용
- `ServoMoveEnd()` 뒤 `CloseRPC()` 호출
- SDK/robot software version을 시작 로그에 기록

vendor `Robot.py`를 직접 수정하지 않는다. 꼭 patch해야 하면 원본 hash, patch 이유, 대상 SDK/firmware version과 회귀 테스트를 별도 문서화한다.

첨부 SDK는 자체 UDP/CNDE thread를 생성하고 XML-RPC 메서드에서 무한 재시도할 수 있다. adapter가 timeout을 완전히 보장한다고 가정하지 않는다. parent watchdog과 process 격리를 유지한다.

`SetRobotStopOnComDisc()`는 반드시 실제 controller firmware와 사용 포트에서 별도 검증한다. 동봉 문서의 port ID는 현재 SDK가 사용하는 XML-RPC/CNDE/UDP port와 일치하지 않는 부분이 있으므로 호출 성공만으로 통신 단절 정지가 구성됐다고 판단하지 않는다.

## WebXR client 개발 영역

`web/index.html`은 다음을 수정한다.

- server-issued session ID와 protocol version 사용
- pose에 증가하는 `seq` 포함
- WebSocket 연결과 controller lease가 없으면 VR control 활성화를 제한
- `bufferedAmount` 기반 backpressure와 drop counter 표시
- WebXR `end`에서 `release_control`과 best-effort grip=false 전송
- WebSocket 재연결 뒤 기존 grip 상태로 자동 재개하지 않음
- server의 `IDLE/ARMING/ACTIVE/STOPPING/FAULT`를 UI에 구분 표시
- stale input, worker fault, controller busy, TLS 오류를 사용자에게 보이게 표시
- status를 pose acknowledgement가 아닌 독립 stream으로 처리

VR 내부 화면이 비어 있어도 fault와 tracking 상태를 작업자가 확인할 별도 모니터 UI 또는 명확한 시청각 피드백을 추후 고려한다.

## 안전 불변조건

다음 조건을 위반하는 변경은 merge 또는 실제 로봇 테스트 대상이 될 수 없다.

- 부모 asyncio process에서는 Fairino SDK 메서드를 호출하지 않는다.
- 하나의 worker process만 로봇 SDK lifecycle을 소유한다.
- grip은 dead-man switch이며 stale pose는 grip=false와 동등하게 취급한다.
- `ServoMoveStart` 성공 전에는 ACTIVE가 될 수 없다.
- `ServoMoveStart` 실패 후 `servo_started=True`로 설정하지 않는다.
- controller disconnect, WebXR end, lease loss, protocol fault가 stop event로 연결된다.
- worker restart 후 이전 tracking을 자동 복원하지 않는다.
- SDK 오류를 로그만 남기고 다음 target을 계속 보내지 않는다.
- workspace clamp는 입력 검증, step 제한, timeout, dead-man을 대체하지 않는다.
- 실제 로봇 모드는 명시적인 IP와 hardware 확인 flag 없이는 시작되지 않는다.
- 실제 로봇 자동 테스트와 vendor example 자동 실행을 금지한다.
- 인증서 개인 키, controller credential, control token을 로그 또는 저장소에 남기지 않는다.
- 물리적 비상 정지와 주변 안전 확보 없이 수동 로봇 테스트를 시작하지 않는다.

## 관측성과 health

최소 endpoint는 다음과 같다.

- `/health/live`: aiohttp event loop가 응답 가능한지
- `/health/ready`: config, worker, SDK 연결 상태가 요청한 mode에서 준비됐는지
- `/ws`: versioned WebSocket

로그에는 다음을 포함한다.

- process ID와 worker generation
- controller/session ID의 비밀이 아닌 축약 식별자
- 모든 state transition과 reason
- SDK version, robot software version, SDK error code
- stop reason과 stop/close 결과
- worker hang, SIGTERM/SIGKILL 여부

pose 전체를 매 frame 기록하지 않는다. timing metric은 일정 구간으로 집계한다.

```text
servo_tick_count
servo_missed_tick_count
servo_jitter_ms p50/p95/p99/max
sdk_call_ms p50/p95/p99/max
pose_age_ms
pose_drop_count
ws_client_count
worker_restart_count
fault_count by reason
```

## 구현 단계와 exit criteria

### 0. 재현 가능한 기반

- `pyproject.toml`, pinned dependency file, README 추가
- Ubuntu 22.04/Python 3.10 platform check
- Linux SDK 경로 설정화
- secret과 build/log 산출물 ignore 규칙 설계
- `setup.sh`를 `python3 -m venv`와 `python -m pip` 기반으로 수정

완료 조건: 새 Ubuntu 22.04 환경에서 dry-run dependency 설치와 import smoke test가 문서대로 성공한다.

### 1. 순수 core 분리

- protocol dataclass와 validation
- coordinate mapping, EMA, workspace/step clamp
- immutable config
- worker state transition reducer

완료 조건: SDK와 aiohttp 없이 unit test가 실행되고 모든 edge case가 통과한다.

### 2. Robot adapter

- `RobotClient` Protocol
- `FakeRobotClient`
- `FairinoRobotClient`
- 반환 형태 정규화와 `exaxis` 수정
- connect/start/cart/end/close 호출 순서

완료 조건: fake에서 성공, SDK error, exception, hang을 재현할 수 있고 Linux SDK import smoke test가 통과한다.

### 3. worker process와 IPC

- spawn context
- latest pose mailbox
- control/status/heartbeat channel
- fixed-period loop
- supervisor와 graceful/forced shutdown

완료 조건: worker가 SDK call에서 hang되어도 `/health/live`와 WebSocket이 응답하고 watchdog이 fault를 보고한다.

### 4. aiohttp와 WebXR 통합

- controller lease
- versioned protocol
- status publisher
- disconnect/WebXR end stop
- browser backpressure

완료 조건: 두 client 경쟁, reconnect, burst, malformed input, stale input 통합 테스트가 통과한다.

### 5. Ubuntu 배포

- config/secret 외부화
- `systemd` unit
- signal과 reboot lifecycle
- 운영 로그와 health check

완료 조건: native Ubuntu 22.04에서 boot start, 정상 stop, worker crash recovery가 재현된다.

### 6. 제한된 실제 로봇 commissioning

- firmware/SDK compatibility 확인
- controller 통신 단절 정지 설정 검증
- 낮은 scale, 좁은 workspace, 저속 조건에서 순차 테스트
- 물리 E-stop과 observer 확보

완료 조건: 승인된 수동 체크리스트와 측정 결과가 남고, 실패 시 실제 정지 시간이 기록된다.

## 테스트 전략

### Unit

- VR 축 매핑과 m→mm 변환
- quaternion/position validation
- EMA 초기화와 새 clutch reset
- workspace와 step 제한
- state transition 전 경우
- SDK 반환 int/tuple/error normalization
- config 범위 검증

### Process integration

- burst pose에서 최신 값만 소비
- pose timeout과 grip release stop
- controller/WebXR disconnect stop
- `ServoMoveStart` 실패 시 ACTIVE 진입 금지
- `ServoCart` 오류 후 FAULT
- `ServoMoveEnd` 정확히 한 번 대응
- SDK call hang 중 aiohttp responsiveness
- watchdog SIGTERM/SIGKILL과 새 IPC generation
- worker restart 후 grip 재무장 요구
- shutdown 중 `ServoMoveEnd -> CloseRPC`

### Web integration

- protocol version mismatch
- malformed/oversized/non-finite JSON
- 두 번째 controller lease 거부
- observer status
- WebSocket reconnect
- client/server backpressure
- TLS와 static UI

### WSL smoke

WSL은 기능 smoke test에 사용할 수 있지만 `/mnt/c` filesystem timing을 native Ubuntu 성능으로 해석하지 않는다. timing acceptance는 WSL ext4 또는 실제 Ubuntu target에서 다시 측정한다. WSL에서 실제 로봇에 연결하거나 자동 motion test를 수행하지 않는다.

의존성 파일이 추가된 뒤 표준 smoke 흐름은 다음 형태다.

```bash
python3 -m venv /tmp/vr-teleop-venv
source /tmp/vr-teleop-venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
python -m teleop --dry-run --no-tls
```

`--no-tls`는 localhost/자동 테스트 전용이다. Quest WebXR 운영 경로는 secure context를 위해 TLS를 사용한다.

### 실제 로봇 수동 테스트

자동화하지 말고 순서대로 사람이 승인한다.

1. 연결만 수행하고 motion command 없음
2. SDK/firmware version과 controller state 확인
3. ServoMoveStart 후 즉시 ServoMoveEnd
4. grip release
5. WebXR end
6. WebSocket cable/network disconnect
7. stale pose injection
8. worker process termination
9. 작은 단일 축 이동
10. 3축 제한 이동

각 단계에서 예상 SDK 호출, 실제 정지 시간, controller error를 기록한다.

## 정량 완료 기준

최종 완료는 “서버가 켜진다”가 아니라 다음 증거를 모두 갖춘 상태다.

- 전체 자동 테스트 통과
- 10분 dry-run/fake soak에서 process memory와 IPC backlog가 계속 증가하지 않음
- stale pose의 stop 결정이 `pose_timeout + servo_period` 이내에 발생
- fake SDK 1초 hang 중 `/health/live` p99 응답이 100 ms 이내
- native Ubuntu fake adapter 기준 8 ms loop의 jitter p99 목표가 ±2 ms 이내
- missed tick과 SDK call duration을 측정·보고
- 성공한 각 ServoMoveStart에 ServoMoveEnd 시도가 정확히 한 번 존재
- disconnect/fault/shutdown별 호출 순서 테스트 통과
- worker 강제 종료 뒤 자동 motion 재개 없음
- reboot 후 systemd 기동 및 stop timeout 검증
- 실제 로봇에서는 controller가 승인한 정지 시간과 통신 단절 동작을 별도 기록

±2 ms jitter는 성능 목표이지 안전 보증이 아니다. 달성하지 못하면 측정 근거를 남기고 SDK 통신 방식, OS scheduling, controller buffering을 조사한 뒤 목표를 재승인한다.

## Ubuntu 배포 원칙

운영 배치는 다음처럼 분리한다.

```text
/opt/vr-teleop/                 application + venv
/etc/vr-teleop/config.toml      runtime config
/etc/vr-teleop/tls/cert.pem     certificate
/etc/vr-teleop/tls/key.pem      private key
systemd: vr-teleop.service
```

- 전용 비특권 service user를 사용한다.
- 서비스 instance는 한 대의 robot당 하나만 허용한다.
- `Restart=on-failure`, `KillMode=control-group`, 명시적 `TimeoutStopSec`를 설정한다.
- service stop이 먼저 애플리케이션의 graceful shutdown을 기다리게 한다.
- 인증서와 key 권한을 최소화한다.
- 처음에는 Docker보다 native venv + systemd를 우선한다. robot NIC routing, signal, process watchdog, timing을 단순하게 유지하기 위해서다.
- 여러 aiohttp/Gunicorn worker를 띄우지 않는다. robot/controller lease의 단일 소유권을 깨뜨린다.

## 코드 작성 규칙

- 새 Python 코드는 type hint와 dataclass를 사용한다.
- network, protocol, control math, SDK adapter, process lifecycle을 모듈 경계로 분리한다.
- 실제 시간에 의존하는 코드는 injectable clock으로 테스트 가능하게 한다.
- broad `except Exception`으로 제어 오류를 숨기지 않는다. process/SDK 경계에서 문맥을 추가하고 FAULT로 변환한다.
- JSON 오류를 조용히 무시하지 않고 protocol error와 rate-limited log로 남긴다.
- 전역 mutable `bridge`를 제거하고 aiohttp app state와 명시적인 supervisor object를 사용한다.
- `time.time()`을 deadline과 age 계산에 사용하지 않는다.
- `time.sleep()`을 asyncio process에서 사용하지 않는다.
- `asyncio.to_thread()`로 servo SDK 호출을 보내지 않는다. IPC 같은 짧은 비-SDK blocking 경계에서만 제한적으로 사용할 수 있다.
- vendor SDK 내부 구현과 예제를 일반 application style로 복사하지 않는다.
- `main.py` 샘플을 확장하지 않는다.
- 실제 robot과 fake가 같은 `RobotClient` interface를 사용한다.
- 안전 상수 변경에는 근거, test, dry-run 측정값을 함께 제출한다.

## agent 작업 절차

1. 변경 전에 이 문서와 관련 모듈을 읽고 실제 robot 연결 여부를 확인한다.
2. 기본 명령은 dry-run/fake여야 한다.
3. vendor example이나 실제 robot command를 자동 실행하지 않는다.
4. SDK 관련 조사에서는 전체 vendor tree를 무작정 검색하지 말고 `linux/fairino/Robot.py`와 필요한 example만 제한적으로 본다.
5. 변경 후 unit → process integration → WSL smoke 순서로 검증한다.
6. timing 결과는 실행 환경이 WSL인지 native Ubuntu인지 명시한다.
7. 실제 robot 검증이 남아 있으면 완료라고 표현하지 않고 수동 체크리스트로 분리한다.
8. 변경 보고에는 테스트 결과, 안전 영향, 새 설정, 알려진 미검증 사항을 포함한다.
