# 시연 데이터 녹화 (Demonstration Recording)

FR5 텔레옵으로 시연하는 동안, 모방학습(LeRobot)에 쓸 데이터를 원본(raw) 형태로
저장하는 기능을 설명한다. 이 문서는 세 가지를 나누어 다룬다.

- **원리**: 왜 이런 구조인지
- **사용법**: 어떻게 녹화하는지
- **파일별 역할**: 어떤 코드가 무엇을 하는지

| 단계 | 내용 | 상태 |
|---|---|---|
| R1 | 로봇 데이터 기록 (125 Hz) | 완료 |
| R2 | 손목 카메라 RealSense D435i 녹화 (30 fps) | 완료 (실제 카메라 연결 검증 전) |
| R3 | Quest A/B 버튼으로 녹화 조작, 헤드셋 진동/상태등 피드백 | 완료 (Quest 실기기 검증 전) |
| R4 | raw episode → LeRobotDataset v3 변환기 (30 Hz) | 완료 (실제 녹화 데이터로 학습 검증 전) |
| R5 | 실제 장비에서 카메라-로봇 지연 측정 및 보정 | 예정 |

---

## 1. 원리

### 1.1 설계 원칙

1. **녹화는 관측만 한다.** 녹화 코드는 로봇 명령을 보내지 않는다. 녹화가
   실패해도 로봇 상태 머신은 영향을 받지 않고, 해당 에피소드에만
   "불완전(`complete: false`)" 표시가 붙는다.
2. **servo loop를 막지 않는다.**
   - worker는 파일을 쓰지 않는다.
   - 로봇 상태는 RPC 대신 SDK가 받아 둔 캐시에서 읽는다.
   - 큐가 가득 차면 기다리지 않고 해당 데이터를 버린 뒤 그 개수를 센다.
3. **블록될 수 있는 장치 코드는 별도 프로세스에 둔다.**
   - Fairino SDK가 RobotWorker 프로세스에 있는 것과 같은 이유로, 카메라는
     Camera 프로세스에 둔다.
   - librealsense의 `pipeline.start()`는 장치가 없으면 **GIL을 쥔 채 약
     15초 동안 블록**된다(실측). 스레드로 두면 Recorder 전체가 멈춘다.
4. **원본을 손실 없이 남기고, 학습 포맷 변환은 나중에 한다** (DROID 방식).
   로봇 데이터는 8 ms tick마다 전부, 카메라 데이터는 프레임마다 실제 시각과
   함께 저장한다. 학습 fps(30 Hz)와 action 표현은 변환 단계(R4)에서 정한다.

### 1.2 프로세스 구조

```text
Quest 3 (WebXR)                 /monitor (작업자 브라우저)
      │ pose, grip,                   │ record start/stop/discard,
      │ record start/stop/discard     │ set_task (언어 지시문)
      │ (오른손 A/B)                  │
      ▼                      ▼
┌────────────────────── 메인 프로세스 (aiohttp) ──────────────────────┐
│ WebSocket, controller lease, status 송출                           │
│ _recording_loop (20 ms 주기): worker 샘플 묶음 → Recorder로 전달    │
└──────┬──────────────────────────────────────────────┬──────────────┘
       │ sample_queue (팔마다, worker generation마다)   │ command_queue
       │                                              ▼
┌──────┴──────────────┐                ┌──────────────────────────────┐
│ RobotWorker 프로세스 │                │ Recorder 프로세스              │
│ 8 ms servo tick     │                │ 에피소드 상태, 파일 쓰기       │
│ 샘플 생성, 10개씩 묶음│                │ meta.json / robot_*.bin      │
└──────┬──────────────┘                └──────────────┬───────────────┘
       │ Fairino SDK (CNDE 상태 캐시)                  │ 명령/응답/상태만 오감
       ▼                                              ▼
   FR5 controller                      ┌──────────────────────────────┐
                                       │ Camera 프로세스               │
                                       │ 캡처 스레드 + 인코딩 스레드    │
                                       │ camera_*.mp4 / *_frames.bin  │
                                       └──────────────┬───────────────┘
                                                      ▼
                                              RealSense D435i (USB 3)
```

- **Recorder 큐에 쓰는 쪽은 메인 프로세스뿐이다.** worker가 Recorder 큐에
  직접 쓰면, worker가 강제 종료될 때 큐 내부 lock이 깨질 수 있다. 그래서
  worker는 세대(generation)마다 새로 만들어지는 자기 `sample_queue`에만 쓰고,
  메인 프로세스가 이를 받아 Recorder로 넘긴다.
- **카메라 프레임은 Camera 프로세스 밖으로 나가지 않는다.** 캡처한 프로세스
  안에서 바로 인코딩한다. 프로세스 사이에는 작은 명령, 응답, 상태만 오간다.

### 1.3 로봇 데이터: tick마다 샘플 1개

worker의 servo tick(8 ms) 안에서 일어나는 순서는 다음과 같다.

```text
tick 시작
 ├─ control 이벤트 처리 (grip/trigger/session)
 ├─ 최신 Quest pose 읽기 (mailbox)
 ├─ 로봇 상태 읽기  ← 관측(observation), get_state_snapshot()
 ├─ 상태 머신 진행, ServoCart(target) 전송  ← 행동(action)
 └─ 샘플 1개 생성 → 10개가 모이면 sample_queue에 put_nowait
```

- **관측을 명령보다 먼저 읽는다.** 학습된 정책이 실행될 때 "관측 → 행동"
  순서로 동작하는 것과 같게 맞췄다. fake 로봇 테스트에서 "다음 tick의 관측이
  직전 tick의 명령과 같음"을 확인했다.
- **action은 VR 입력이 아니라 실제 전송한 명령값이다.** clamp, 필터, step
  제한을 모두 거친 뒤 `ServoCart`로 보낸 target TCP를 저장한다. 정책이 출력해야
  할 값이 바로 이것이다.
- **로봇 상태는 RPC 없이 읽는다.** SDK 2.2.x는 20005 포트(CNDE)로 8 ms마다
  상태를 받아 `robot.robot_state_pkg`에 저장한다. 이 구조체를 복사만 하므로
  tick당 비용은 약 1.5 µs다.
  - SDK 2.0.8, 2.2.2, 2.2.8 모두 같은 필드가 있는 것을 확인했다.
  - 패킷을 아직 받지 못한 경우는 `None`으로 처리하고, 파일에는 NaN이 저장된다.
- **grip을 놓은 구간(SLEEPING)도 계속 기록한다.** 해당 구간은
  `servo_active=0`, `cmd_valid=0`으로 표시되며, 쓸지 말지는 변환 단계에서
  정한다(DROID의 `skip_action`과 같은 방식).

### 1.4 카메라 데이터

- 카메라는 서버가 켜져 있는 동안 **계속 스트리밍**한다. 에피소드를 시작하는
  시점에 이미 자동 노출이 안정되어 있게 하기 위해서다.
- 프레임의 **수신 시각이 에피소드 구간 안에 들 때만** 인코딩 스레드로 보낸다.
  인코딩 큐(60프레임, 약 2초)가 가득 차면 그 프레임을 버리고 개수를 센다.
- 영상의 pts는 프레임 번호(0, 1, 2, …)다. **실제 촬영 시각은
  `camera_<name>_frames.bin`의 같은 번호 레코드**에 들어 있다.
- 인코더 스레드는 2개로 제한했다(`ENCODER_THREADS`). x264가 CPU 코어를 모두
  쓰면 servo worker의 타이밍을 해칠 수 있기 때문이다.

### 1.5 시간 동기화

- 모든 timestamp는 같은 PC의 **`CLOCK_MONOTONIC`(ns)**이다. worker, 메인,
  Recorder, Camera 프로세스가 같은 시계를 쓰므로 별도 보정 없이 바로 비교할
  수 있다.
  - 로봇 샘플의 `t_ns`: 로봇 상태를 읽은 시각
  - 카메라의 `arrival_mono_ns`: PC가 프레임을 받은 시각
- **주의:** `arrival_mono_ns`는 노출 시점이 아니라 수신 시점이므로 노출부터
  USB 전송까지의 지연이 포함되어 있다. 지연 분석을 위해
  `arrival_wall_ns`(wall clock)와 `device_ts_ms`(librealsense timestamp,
  domain은 `meta.json`에 기록)를 함께 저장한다. 실제 지연은 R5에서 측정해
  변환할 때 보정한다.
- 로봇 쪽 기준 시각으로 controller 시계(`robot_time_ms`, 자정부터의 ms)와
  CNDE `robot_frame_cnt`도 저장한다.

### 1.6 에피소드 수명주기

```text
IDLE ──start(task)──▶ RECORDING ──stop(success|failure)──▶ FINALIZING ──▶ IDLE
                         │                                   (tail 대기)
                         └──discard──▶ 삭제 후 IDLE
서버 종료 중 RECORDING ──▶ outcome=interrupted로 저장
```

- **start**
  - 메인 프로세스가 명령을 받은 시각이 `start_ns`가 된다.
  - `episode_NNNNNN.tmp/` 폴더를 만든다.
  - 카메라가 켜져 있는데 스트리밍 중이 아니면 시작을 거부한다(`last_error`).
- **stop**
  - 받은 시각이 `stop_ns`가 된다. 그 시각보다 뒤의 샘플과 프레임은 버린다.
  - worker 샘플은 최대 80 ms 늦게 도착할 수 있다. 그래서 모든 팔과 카메라에서
    `stop_ns` 이후 데이터가 한 번이라도 보이거나 1초(`TAIL_WAIT_S`)가 지나면
    finalize한다.
- **finalize**
  - 파일을 `fsync`한 뒤 `meta.json`을 원자적으로 쓴다.
  - `.tmp`를 떼도록 폴더 이름을 바꾸고, 상위 폴더도 `fsync`한다.
- **discard:** 폴더를 삭제하고, 번호는 다음 에피소드가 다시 쓴다.
- **비정상 종료로 남은 `.tmp`:** 다음 시작 때 `.incomplete`로 이름이 바뀐다.
  변환기는 이 폴더를 기본으로 제외할 예정이다.
- **task:** 비어 있으면 시작할 수 없다. task 없이 시작하면 서버의 "다음 task"
  (`next_task`)를 쓴다. 다음 task는 `set_task`나 직전 start로 정해지며, 서버를
  재시작하면 가장 최근에 저장된 에피소드의 task로 복원된다. Quest는 글자를
  입력할 수 없으므로 A 버튼은 항상 이 다음 task로 시작한다.

### 1.7 Quest 버튼과 헤드셋 피드백 (R3)

Quest 몰입 모드에서는 HTML 화면이 보이지 않는다. 화면에는 패스스루 위에 투명한
WebGL 레이어만 그린다. 그래서 녹화 조작과 피드백을 모두 헤드셋 안에서 처리한다.

- **입력:** 오른손 컨트롤러의 xr-standard gamepad `buttons[4]`(A)와
  `buttons[5]`(B)의 눌림 순간(edge)만 쓴다. grip, trigger, controller lease와는
  독립적이다(녹화는 로봇을 움직이지 않음).
- **판단:** `web/recording_controls.js`의 `RecordButtons`가 서버가 보고한
  recorder 상태에 따라 명령을 고른다(1.6의 상태 머신과 같은 규칙).
  - 명령을 보낸 뒤 600 ms 동안은 다음 명령을 무시한다. 두 번 빠르게 눌러
    "시작 직후 저장"이 되는 것을 막기 위해서다.
  - B는 길게 누르기(폐기)와 구분해야 하므로, 짧게 누른 경우 **손을 뗄 때**
    실패 저장을 보낸다.
- **진동:** 명령을 보내는 즉시 패턴으로 알린다. 시작은 짧게 1번, 성공은 짧게
  2번, 실패는 길게 1번, 폐기는 짧게 3번, 거부·오류는 강하게 0.5초다.
- **상태등:** 양쪽 눈 시야 중앙 위쪽에 작은 사각형을 그린다. 머리에 고정되어
  따라다닌다. WebGL `scissor` + `clear`만 쓰므로 셰이더가 필요 없다.
  - 녹화 중에는 빨강이 깜빡이고, 마무리(FINALIZING) 중에는 노랑이다.
  - 저장되면 1.5초 동안 초록(성공), 주황(실패) 또는 흰색(interrupted)으로
    바뀐다. 폐기는 회색이다.
  - 거부나 오류는 보라색으로 빠르게 깜빡인다.
- **서버가 거부한 경우:** task가 없거나 카메라가 스트리밍 중이 아니면 서버가
  시작을 거부한다. recorder는 오류가 날 때마다 `error_seq`를 1씩 올리고,
  Quest는 명령을 보낸 뒤 2초 안에 이 값이 증가하면 오류 피드백을 준다. 오류
  문자열이 아니라 번호로 판단하므로, 같은 오류가 반복되거나 명령 전부터 남아
  있던 오류도 올바르게 구분한다.

### 1.8 LeRobot 변환 (R4)

raw episode는 손실 없는 원본이다. 학습에 쓸 LeRobotDataset v3는 변환기
(`teleop/recording/lerobot_convert.py`)로 따로 만든다. 변환기는 `meta.json`에
기록된 레이아웃으로 raw 파일을 읽으므로 teleop 서버 코드에 의존하지 않는다.

**시간 정렬.** 에피소드 구간 위에 균일한 30 Hz 격자를 만들고, 격자의 각
시각마다 다음 두 가지를 고른다.

- **로봇 샘플:** 그 시각 이전의 가장 최근 샘플(zero-order hold). 정책이 그
  시각에 실제로 볼 수 있었던 값이다. 8 ms 간격이므로 최대 8 ms 이전 값이다.
- **카메라 프레임:** 촬영 시각(수신 시각 − `camera_latency_ms`)이 가장 가까운
  프레임. 카메라 jitter와 drop이 있어도 dataset은 정확히 30 Hz가 되고, 빈
  곳은 이웃 프레임을 반복해서 채운다(반복 수는 보고서에 기록).

**구간 자르기 (기본값).** 처음 servo를 시작하기 전과 마지막 servo가 끝난 뒤의
대기 시간은 0.25초 여유만 남기고 잘라낸다. 중간에 grip을 놓은 구간(clutch)은
자르지 않는다. 시간이 끊기면 영상이 튀기 때문이며, 대신 이 구간의 action은
"현재 자세 유지"가 된다.

**벡터 구성 (한 팔, 기본 `rot6d`).**

| feature | shape | 내용 |
|---|---|---|
| `observation.state` | (10,) | 측정 TCP 위치 x,y,z (m) + 회전 6D + 그리퍼 (0=열림, 1=닫힘) |
| `action` | (10,) | 같은 구성의 명령값. 기본(`command`): 그 tick에 보낸 `ServoCart` target과 그리퍼 목표. servo가 꺼져 있으면 측정 자세를 그대로 유지 |
| `observation.joint_pos` | (6,) | 관절각 (rad) |
| `observation.images.wrist` | (480, 640, 3) | 손목 카메라 (LeRobot 기본 AV1로 재인코딩) |
| `task` | — | `meta.json`의 언어 지시문 |

- **회전:** Fairino rx,ry,rz(고정축 XYZ, `R = Rz·Ry·Rx`, `control_math`와 같은
  정의)를 다음 중 하나로 바꾼다. 같은 정의인지는 테스트로 확인한다.
  - `rot6d`: 회전 행렬의 앞 두 열, 연속적이어서 학습에 유리
  - `quat`: xyzw, 에피소드 안에서 부호 연속
  - `euler`: rad. controller는 rx,ry,rz를 (−180°, 180°]로 보고하므로 rx가 180°
    근처일 때 값이 360°씩 튄다(현장 데이터에서 에피소드당 10회 이상). 그래서
    state는 시간축으로 unwrap하고, action은 같은 frame의 state에서 ±π 안에
    오도록 맞춘다. rot6d와 quat은 이 처리가 필요 없다.
- **그리퍼:** 세션 메타데이터의 open/closed 값으로 0~1로 정규화한다.
- **action 모드:** `--action-mode next_state`를 주면 action을 "다음 frame의
  state"로 정의한다.

**제외 규칙 (기본값).** 다음 에피소드는 변환에서 뺀다. 모두 옵션으로 포함할
수 있다.
- outcome이 `success`가 아닌 것
- `complete: false`인 것
- servo가 한 번도 켜지지 않은 것
- 카메라 공백이 5%를 넘는 것
- 로봇 상태에 NaN이 있는 것

제외된 에피소드와 이유는 `fr5_conversion.json`에 기록된다.

### 1.9 `complete` 판정

`meta.json`의 `complete`는 아래 조건을 **모두** 만족할 때만 `true`다.

- 설정된 모든 팔에서 샘플이 들어옴 (`missing_hands`가 비어 있음)
- worker → 메인, 메인 → Recorder 양쪽 모두 batch drop이 0
- 에피소드 동안 worker가 재시작되지 않음 (`generations`가 1개)
- (카메라 사용 시) 녹화 프레임 수 > 0, drop 0, 인코딩 오류 없음

카메라 내부의 frame drop은 `complete`에 반영되지 않는다. 변환할 때
`frames.bin`의 `frame_number` 연속성으로 따로 확인한다.

### 1.10 종료 순서

`SIGINT`/`SIGTERM`을 받으면 다음 순서로 종료한다.

1. pose 무효화, status 송출 중지
2. **로봇 worker 종료** (`ServoMoveEnd → CloseRPC`). 이 동안에도 녹화 펌프는
   계속 돌아 마지막 샘플까지 받는다.
3. 남은 샘플을 Recorder로 전달
4. Recorder 종료: 열린 에피소드를 `interrupted`로 finalize하고 Camera
   프로세스를 종료한다.

worker의 `sample_queue`에는 `cancel_join_thread()`를 걸어 두었다. 녹화 데이터를
비우지 못하더라도 worker 종료(로봇 정지 순서)가 지연되지 않게 하기 위해서다.
systemd 같은 서비스 관리자로 운영한다면 카메라 영상을 마무리할 시간을
고려해 stop timeout(`TimeoutStopSec`)을 12초 이상으로 둔다.

---

## 2. 사용법

### 2.1 설치

`setup.sh`가 카메라 의존성까지 함께 설치한다. 수동으로 설치하려면 다음을
실행한다.

```bash
.venv-linux/bin/python -m pip install -r requirements-camera.txt
# av==15.1.0, numpy==2.2.6, pyrealsense2==2.59.1.11053
```

- 카메라를 쓰지 않으면(`recording.camera.enabled: false`) 위 패키지가 없어도
  된다. 해당 모듈은 카메라를 켰을 때만 import한다.
- RealSense를 root가 아닌 사용자(서비스 계정 포함)로 쓰려면 librealsense udev
  규칙이 필요하다. librealsense 저장소의 `config/99-realsense-libusb.rules`를
  `/etc/udev/rules.d/`에 복사한 뒤 카메라를 다시 연결한다.
- D435i는 반드시 **USB 3 포트**에 연결한다.

### 2.2 설정 (`config.yaml`)

```yaml
recording:
  enabled: true            # 녹화 기능 전체 (false면 recorder를 띄우지 않음)
  root_dir: recordings     # 상대 경로는 이 YAML 파일 기준
  camera:
    enabled: false         # true: D435i 녹화, false: 로봇 데이터만
    source: realsense      # fake: 장치 없이 합성 프레임 (dry-run 테스트용)
    name: wrist            # 파일 이름에 쓰임: camera_wrist.mp4
    serial: null           # RealSense가 여러 대일 때 serial 지정
    width: 640             # D435i color 해상도만 허용
    height: 480
    fps: 30                # 6, 15, 30, 60
    depth: false           # true: color에 정렬한 16-bit depth를 FFV1로 저장
```

- 알 수 없는 키, 잘못된 타입, D435i가 지원하지 않는 해상도는 서버 시작 때
  거부된다.
- `camera.enabled: true`인데 필요한 패키지가 없으면 시작할 때
  "install requirements-camera.txt" 오류를 낸다.
- 서비스로 운영할 때는 `root_dir`를 실행 계정이 쓸 수 있는 경로
  (예: `/var/lib/vr-teleop/recordings`)로 지정한다.

### 2.3 녹화 절차

1. 서버를 실행하고(`python -m teleop`), 작업자 PC에서 `https://<서버>:8443/monitor`를
   연다.
2. **Demonstration recording** 카드에서 상태를 확인한다.
   - `Recorder`가 `IDLE`이어야 한다.
   - 카메라를 쓰는 경우 `Camera`가 `wrist 30.0 fps`처럼 초록색이어야 한다.
     `NOT STREAMING`이면 시작 버튼이 비활성화되고, `Error`에 원인이 표시된다.
3. **Language instruction**에 지시문을 입력한다.
   - 예: `pick up the red cube and place it in the box`
   - 입력값은 브라우저에 저장되어 다음 에피소드에도 남는다.
4. **● 녹화 시작**을 누른 뒤 Quest로 시연한다.
   - grip은 기존처럼 dead-man 역할을 하며 녹화와는 무관하다.
5. 끝나면 다음 중 하나를 누른다.
   - **✔ 성공 저장**: `outcome: success`
   - **✘ 실패 저장**: `outcome: failure`. 학습에서 제외하거나 분석용으로 쓴다.
   - **폐기**: 확인창을 거친 뒤 폴더를 삭제한다.
6. `Last saved`에서 저장 결과(번호, 결과, 길이, 프레임 수)를 확인한다.
   `(incomplete)`가 붙어 있으면 `meta.json`을 확인한다.

녹화 조작은 controller lease와 무관하다. 그래서 Quest가 조작권을 가진 상태에서도
모니터가 녹화를 제어할 수 있다.

### 2.4 Quest 버튼으로 녹화

혼자 작업할 때는 헤드셋을 쓴 채로 녹화를 조작할 수 있다.

1. **준비(헤드셋 쓰기 전):** `/monitor`에서 task를 입력한다. 입력은 0.5초 뒤
   서버로 전송되고(`set_task`), `Next task (Quest A)`가 초록이면 반영된 것이다.
   Quest 브라우저의 시작 페이지 "시연 녹화" 카드에서도 다음 task를 확인할 수 있다.
2. **녹화:** VR 세션에 들어가 다음 버튼을 쓴다(모두 오른손 컨트롤러).

| 버튼 | recorder 상태 | 동작 | 진동 | 상태등 |
|---|---|---|---|---|
| A | IDLE | 녹화 시작 (다음 task) | 짧게 1번 | 빨강 깜빡임 |
| A | RECORDING | 성공으로 저장 | 짧게 2번 | 초록 1.5초 |
| B (짧게, 뗄 때) | RECORDING | 실패로 저장 | 길게 1번 | 주황 1.5초 |
| B 1초 이상 | RECORDING | 폐기 | 짧게 3번 | 회색 1.5초 |
| 그 외 | 녹화 꺼짐, FINALIZING, 연결 끊김 등 | 거부 | 강하게 0.5초 | 보라 깜빡임 |

3. 같은 task로 계속 녹화하면 A(시작) → 시연 → A(저장)를 반복하면 된다. task를
   바꿀 때만 모니터에서 입력한다.

- grip(dead-man)과 trigger(그리퍼)는 녹화와 무관하게 기존대로 동작한다.
- 녹화 중 실수로 A를 누르면 그 시점까지가 성공으로 저장된다. 이 경우 모니터에서
  해당 에피소드를 확인하고, 필요하면 변환할 때 제외한다.

### 2.5 WebSocket 메시지

모니터 외의 클라이언트(R3의 Quest 버튼 등)도 같은 메시지로 녹화를 제어한다.

```json
{"version": 1, "type": "record", "session_id": "<hello에서 받은 id>",
 "action": "start", "task": "pick up the red cube"}
{"version": 1, "type": "record", "session_id": "...", "action": "stop", "outcome": "success"}
{"version": 1, "type": "record", "session_id": "...", "action": "discard"}
{"version": 1, "type": "record", "session_id": "...", "action": "set_task", "task": "stack the cups"}
```

- `task`는 `start`와 `set_task`에만 쓸 수 있고 최대 1000자다. `start`에서
  생략하면 `next_task`를 쓴다. `set_task`는 녹화 중에도 보낼 수 있으며, 이 경우
  다음 에피소드부터 적용된다.
- `outcome`은 `stop`에만 쓰며 `success` 또는 `failure`다.
- 녹화가 꺼져 있거나 Recorder가 응답하지 않으면
  `{"type": "error", "code": "recording_unavailable"}`가 돌아온다.
- 상태는 기존 `status` 메시지의 `recording` 필드로 전달된다.
  - 주요 필드: `state`, `episode_index`, `task`(녹화 중이면 현재 task),
    `next_task`, `elapsed_s`, `samples`, `dropped_batches`, `last_saved`,
    `last_error`, `error_seq`, `camera{streaming, fps, episode_frames,
    episode_dropped, last_error, serial}`, `alive`
  - 녹화가 꺼져 있으면 `recording`은 `null`이다.

### 2.6 LeRobotDataset으로 변환

`lerobot` 환경(lerobot 0.4.x, Python 3.10)에서 **프로젝트 루트**에서 실행한다.

```bash
cd fr5_teleop
~/miniforge3/envs/lerobot/bin/python -m teleop.recording.lerobot_convert \
    --raw recordings --out datasets/fr5_pick_cube --repo-id local/fr5_pick_cube
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--fps` | 30 | dataset fps |
| `--rotation` | `rot6d` | `rot6d` / `quat` / `euler` |
| `--action-mode` | `command` | `command`: 보낸 명령, `next_state`: 다음 frame의 state |
| `--camera-latency-ms` | 0 | 카메라 지연 보정 (R5에서 측정) |
| `--no-trim` / `--trim-pad-s` | 자르기 / 0.25 | 앞뒤 대기 구간 처리 |
| `--include-failures` | 꺼짐 | failure·interrupted 에피소드도 포함 |
| `--allow-incomplete` | 꺼짐 | `complete: false` 에피소드도 포함 |
| `--max-camera-gap-ratio` | 0.05 | 카메라 공백 frame 비율 상한 |
| `--vcodec` | `libsvtav1` | LeRobot 코덱 (`h264` 등) |
| `--overwrite` | 꺼짐 | `--out`이 있으면 지우고 다시 만듦 |

- 한 번에 dataset 하나를 새로 만든다. raw 폴더를 task별로 나누거나, 같은 폴더를
  통째로 변환한다. task가 여러 개면 task index가 자동으로 붙는다.
- 카메라 구성(이름, 해상도)이 다른 에피소드가 섞여 있으면 변환하지 않고 오류를
  낸다.
- 결과 폴더의 `fr5_conversion.json`에 다음 내용이 남는다.
  - 에피소드별 원본 폴더, frame 수, 잘라낸 시간, 로봇 샘플 최대 나이,
    카메라 정렬 오차 p99, 반복 frame 수, 카메라 내부 drop 수
  - 제외된 에피소드와 이유

**확인과 학습.** 사용자 `lerobot` 환경에서는 `torchcodec`이 로드되지 않는다
(`libnppicc.so.12` 없음, 2026-10-19 확인). 이 문제가 해결될 때까지는 영상
backend로 `pyav`를 지정한다.

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("local/fr5_pick_cube", root="datasets/fr5_pick_cube",
                    video_backend="pyav")
```

학습할 때도 같은 이유로 `--dataset.video_backend=pyav`를 준다. torchcodec을 고쳐서
쓰려면 CUDA NPP 라이브러리(`nvidia-npp-cu12`)를 설치해야 한다.

### 2.7 녹화 데이터 읽기

```python
import json
import numpy as np
import av
from pathlib import Path
from teleop.recording.schema import ROBOT_LAYOUT, CAMERA_FRAME_LAYOUT, STATE_NAMES

ep = Path("recordings/episode_000000")
meta = json.loads((ep / "meta.json").read_text())

robot = np.fromfile(ep / "robot_right.bin",
                    dtype=np.dtype(ROBOT_LAYOUT.numpy_dtype_descr()))
robot["t_ns"], robot["tcp"], robot["cmd_tcp"], robot["cmd_valid"]

cam = meta["cameras"]["wrist"]
frames = np.fromfile(ep / cam["frames_file"],
                     dtype=np.dtype(CAMERA_FRAME_LAYOUT.numpy_dtype_descr()))
with av.open(str(ep / cam["video"])) as container:
    images = [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]
# images[i]의 수신 시각 = frames["arrival_mono_ns"][i]

# 각 프레임에 가장 가까운 로봇 샘플
idx = np.searchsorted(robot["t_ns"], frames["arrival_mono_ns"])
```

numpy 없이 읽으려면 `ROBOT_LAYOUT.iter_records(path)`를 쓴다. 파일 끝에 잘린
레코드가 있으면 무시한다.

---

## 3. 데이터 형식

### 3.1 에피소드 폴더

```text
recordings/
  episode_000000/
    meta.json                 에피소드 메타데이터 (3.2)
    robot_right.bin           로봇 샘플, 329 byte × tick 수 (3.3)
    events.jsonl              start / stop / worker 재시작 이벤트 (3.5)
    camera_wrist.mp4          RGB, H.264 libx264 CRF 18, yuv420p, keyframe 1초
    camera_wrist_depth.mkv    (depth: true) uint16 depth, FFV1 gray16le 무손실
    camera_wrist_frames.bin   프레임 timestamp, 40 byte × 프레임 수 (3.4)
  episode_000001.incomplete/  서버 비정상 종료로 finalize되지 못한 에피소드
```

### 3.2 `meta.json` 주요 필드

| 필드 | 설명 |
|---|---|
| `format`, `format_version` | `fr5_teleop_raw`, `1` |
| `episode_index`, `task`, `outcome` | 번호, 언어 지시문, `success` / `failure` / `interrupted` |
| `complete`, `missing_hands` | 1.9의 판정 결과 |
| `started_at` | 시작 시각 (wall clock, ISO 8601, timezone 포함) |
| `start_ns`, `stop_ns`, `duration_s` | 녹화 구간 (CLOCK_MONOTONIC) |
| `hands.<hand>` | `file`, `samples`, `first_t_ns`, `last_t_ns`, `worker_dropped_batches`, `generations` |
| `forward_dropped_batches` | 메인 → Recorder 전달 중 버려진 batch 수 |
| `cameras.<name>` | `frames`, `dropped_frames`, `error`, 해상도/fps/codec, `timestamp_domain`, `device`(model, serial, firmware, color intrinsics, depth scale) |
| `sample_schema` | 로봇 샘플의 struct format과 필드 목록, `state_names` |
| `session` | teleop 버전, git commit, robot kind, dry-run 여부, 팔별 IP/SDK/그리퍼 열림·닫힘 값, servo 주기, scale, 속도/step 제한, 단위 |

예시 (dry-run, fake 카메라, 일부 생략):

```json
{
  "format": "fr5_teleop_raw", "format_version": 1,
  "episode_index": 0, "task": "smoke camera", "outcome": "success",
  "complete": true, "missing_hands": [],
  "started_at": "2026-10-19T16:50:46.565+09:00",
  "start_ns": 1936182955262, "stop_ns": 1946692687802, "duration_s": 10.51,
  "hands": {"right": {"file": "robot_right.bin", "samples": 1314,
            "worker_dropped_batches": 0, "generations": [1]}},
  "forward_dropped_batches": 0,
  "cameras": {"wrist": {"video": "camera_wrist.mp4", "frames": 315,
              "dropped_frames": 0, "error": null, "fps": 30,
              "codec": "h264 (libx264 crf 18, yuv420p)", "timestamp_domain": "..."}},
  "session": {"robot_kind": "fake", "servo_period_s": 0.008,
              "tcp_units": "mm, deg (Fairino rx,ry,rz)", "git_commit": "0dd1867..."}
}
```

### 3.3 로봇 샘플 (`robot_<hand>.bin`)

little-endian, padding 없음, 레코드당 329 byte. 정의는
`teleop/recording/schema.py`의 `ROBOT_LAYOUT`에 있다.

| 필드 | 타입 × 개수 | 설명 |
|---|---|---|
| `t_ns` | int64 | 로봇 상태를 읽은 시각 (CLOCK_MONOTONIC) |
| `tick`, `generation` | int64 | servo tick 번호, worker 세대 |
| `state` | uint8 | worker 상태 코드 → `meta.sample_schema.state_names[state]` |
| `fresh` | uint8 | pose가 timeout 이내인지 |
| `servo_active` | uint8 | ServoMoveStart 이후인지 (`servo_started`) |
| `clutch_id` | int32 | grip 재무장마다 1씩 증가 |
| `cmd_valid` | uint8 | 이번 tick에 ServoCart를 보냈는지 |
| `cmd_tcp` | float64 × 6 | 보낸 target `[x,y,z,rx,ry,rz]` (mm, deg). 안 보냈으면 NaN |
| `gripper_target` | float64 | 그리퍼 명령 목표 위치 (0~100) |
| `gripper_busy` | uint8 | GRIPPER_ACTION 상태인지 |
| `vr_valid` | uint8 | pose mailbox가 이번 worker 세대에서 유효한지 |
| `vr_seq`, `vr_received_ns` | int64 | Quest pose seq, 서버 수신 시각 |
| `vr_position_m` | float64 × 3 | Quest 컨트롤러 위치 (WebXR 좌표, m) |
| `vr_orientation_xyzw` | float64 × 4 | Quest 컨트롤러 자세 quaternion |
| `grip`, `trigger` | uint8 | 버튼 상태 |
| `state_valid` | uint8 | 아래 관측값이 유효한지 |
| `joint_pos_deg` | float64 × 6 | 관절각 (deg) |
| `joint_vel_deg_s` | float64 × 6 | 관절속도 (deg/s) |
| `tcp` | float64 × 6 | 실제 TCP `[x,y,z,rx,ry,rz]` (mm, deg) |
| `gripper_position` | float64 | 그리퍼 실제 위치 (0~100) |
| `robot_frame_cnt` | int32 | CNDE 패킷 카운터 (없으면 -1). **현장 FR5(SDK 2.2.8)에서는 항상 0**으로, CNDE가 이 필드를 채우지 않는다. 상태 갱신 여부는 `robot_time_ms`로 판단한다 |
| `robot_time_ms` | int64 | controller 시계, 자정부터의 ms (없으면 -1). 현장 데이터에서 host 시계와 같은 속도로 증가함을 확인 |

### 3.4 카메라 프레임 timestamp (`camera_<name>_frames.bin`)

레코드당 40 byte이며, 영상의 프레임 순서와 같은 순서로 저장된다. 정의는
`CAMERA_FRAME_LAYOUT`에 있다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `frame_index` | int64 | 영상 안의 프레임 번호 (= pts) |
| `arrival_mono_ns` | int64 | PC 수신 시각, 로봇 `t_ns`와 같은 시계 |
| `arrival_wall_ns` | int64 | 같은 순간의 wall clock |
| `device_ts_ms` | float64 | librealsense frame timestamp |
| `frame_number` | int64 | 카메라 frame counter. 값이 건너뛰면 카메라나 USB 단에서 누락된 것 |

### 3.5 `events.jsonl`

한 줄에 이벤트 하나씩 기록된다. 현재 기록하는 이벤트는 `start`(task 포함),
`stop`(outcome 포함), `worker_generation_changed`(녹화 중 worker 재시작)다.

---

## 4. 파일별 역할

### 4.1 새로 추가한 파일

| 파일 | 역할 |
|---|---|
| `teleop/recording/__init__.py` | 녹화 패키지 |
| `teleop/recording/schema.py` | 바이너리 레코드 형식 정의. `BinaryLayout`(struct pack, numpy dtype, numpy 없이 읽기), `ROBOT_LAYOUT`, `CAMERA_FRAME_LAYOUT`, worker가 쓰는 `build_sample()`, worker 상태 코드표 |
| `teleop/recording/recorder.py` | **Recorder 프로세스.** `RecorderRuntime`은 에피소드 상태 머신(start/stop/discard/set_task/finalize/abort), 파일 쓰기, `meta.json` 생성, `.tmp → .incomplete` 처리, 시작 시 최근 task 복원, `error_seq`를 맡는다. `RecorderHandle`은 메인 프로세스 쪽 대리자로 프로세스 생성/종료, 샘플 전달, drop 집계를 맡는다. `build_session_metadata()`는 서버 실행 단위 설정을 기록한다 |
| `teleop/recording/camera.py` | **Camera 프로세스.** 프레임 소스는 `RealSenseSource`(D435i color + 선택적 depth 정렬)와 `FakeCameraSource`(합성 프레임) 두 가지다. `EpisodeVideoWriter`는 에피소드별 인코딩 스레드(H.264 / FFV1)와 timestamp 파일을, `CameraRecorder`는 캡처 스레드, 자동 재시작, 상태를 맡는다. `run_camera_process()`는 프로세스 진입점이고, `CameraProcessHandle`은 Recorder 쪽 대리자로 `CameraRecorder`와 같은 인터페이스를 가진다 |
| `requirements-camera.txt` | 카메라용 고정 의존성 (`av`, `numpy`, `pyrealsense2`) |
| `tests/unit/test_recording.py` | 샘플 왕복 변환, numpy dtype 일치, record 메시지 파싱, config, 에피소드 구간 자르기, task 필수/재사용, drop·worker 재시작 시 incomplete, tail timeout, `.tmp` 복구 |
| `tests/unit/test_camera_recording.py` | 영상 프레임 수와 timestamp 일치, depth 무손실, 카메라 없을 때 시작 거부, 카메라 메타데이터, config 검증, Camera 프로세스 경로, 실제 `realsense` 소스에서 장치 없을 때 멈추지 않음 |
| `web/recording_controls.js` | **Quest 버튼 로직** (DOM/WebXR 미사용 순수 JS). `RecordButtons`(A/B → start/stop/discard/rejected, 길게 누르기, cooldown), `recordLightColor()`(상태등 색), `RecordStatusTracker`(저장·`error_seq` 감지 → 피드백), 진동 패턴과 색 표 |
| `tests/integration/test_recording_flow.py` | 실제 WebSocket + worker + Recorder (+ fake 카메라) end-to-end. 모니터가 lease 없이 녹화를 제어하고, 관측이 명령보다 먼저 읽히는지 확인. 녹화가 꺼져 있을 때 오류 응답. 모니터 `set_task` → Quest가 task 없이 시작/저장 |
| `teleop/recording/lerobot_convert.py` | **R4 변환기** (`python -m teleop.recording.lerobot_convert`). raw 레이아웃 읽기, 30 Hz 격자 계획(`plan_episode`: ZOH 로봇 샘플, 가장 가까운 카메라 프레임, 구간 자르기, 제외 규칙), 벡터 구성(`build_episode`: 위치 m, 회전 rot6d/quat/euler, 그리퍼 정규화, command/next_state action), 한 번에 디코딩(`iter_video_frames`), LeRobot 쓰기(`convert`)와 보고서. lerobot은 쓰기 단계에서만 import |
| `tests/unit/test_lerobot_convert.py` | 회전 정의가 `control_math`와 일치, quaternion 부호 연속, ZOH, 구간 자르기와 유지 action, next_state, 회전 옵션별 차원, 카메라 최근접 매칭과 실제 디코딩 프레임 일치, 지연 보정, 공백·drop, 제외 규칙. `LEROBOT_PYTHON`을 지정하면 실제 lerobot으로 변환하고 다시 로드 |
| `tests/unit/test_recording_controls_js.py` | headless Chrome으로 `recording_controls.js`의 22개 시나리오 검사(짧게/길게 누르기, cooldown, 거부, 상태등, 반복 오류 감지). Chrome이 없으면 skip |

### 4.2 수정한 파일

| 파일 | 변경 내용 |
|---|---|
| `teleop/config.py` | `RecordingConfig`(enabled, root_dir, camera), `CameraConfig`(D435i 해상도/fps/소스 검증, 필요 패키지 확인), YAML `recording` 섹션 파싱 |
| `teleop/robot/client.py` | `RobotStateSnapshot` dataclass, `RobotClient.get_state_snapshot()` |
| `teleop/robot/fairino_client.py` | `get_state_snapshot()`: SDK `robot_state_pkg`(CNDE 캐시)를 RPC 없이 복사. 첫 패킷 전에는 `None`, 구조가 다르면 `RobotClientError` |
| `teleop/robot/fake_client.py` | fake `get_state_snapshot()` (현재 TCP, frame counter) |
| `teleop/ipc.py` | `WorkerIpc.sample_queue` (녹화가 켜졌을 때만 생성, 64 batch) |
| `teleop/robot/worker.py` | tick마다 상태 snapshot을 명령 전에 읽음, 이번 tick의 ServoCart target, `clutch_id`, 10 tick 단위 `put_nowait`, drop 집계, 종료 시 flush |
| `teleop/robot/supervisor.py` | `sample_queue`를 worker에 전달, `drain_samples_by_hand()`, 종료 시 큐 정리 |
| `teleop/protocol.py` | `RecordCommand`와 `type: "record"` 메시지 검증 (start/stop/discard/set_task) |
| `teleop/app.py` | Recorder 생성/종료, 20 ms 녹화 펌프, record 명령 처리, status의 `recording` 필드, 종료 순서(worker 먼저) |
| `web/monitor.html` | 녹화 카드: task 입력(브라우저 저장, 서버로 `set_task` 동기화), 서버 `Next task` 일치 표시, 시작/성공/실패/폐기 버튼, Recorder·카메라 상태, 카메라가 스트리밍 중이 아니면 시작 비활성화 |
| `web/index.html` | (R3) 오른손 A/B → `RecordButtons`, 녹화 명령 전송, 진동, 시야 상태등(`drawRecordLight`), 시작 페이지 "시연 녹화" 카드 |
| `config.yaml` | `recording.enabled: true`, `camera.enabled: false` |
| `config_bimanual.yaml` | `recording` 섹션 추가 (기본 비활성) |
| `setup.sh`, `pyproject.toml` | 카메라 의존성 설치, `camera` extra |
| `.gitignore` | `recordings/` |
| `README.md` | 녹화 / 손목 카메라 섹션 |

---

## 5. 검증 결과

모두 dry-run(fake robot) 기준이며 native Ubuntu(이 개발 PC)에서 측정했다.
실제 FR5와 D435i로는 아직 검증하지 않았다.

| 항목 | 결과 |
|---|---|
| R4 변환 (lerobot 0.4.4 env) | 서버로 녹화한 clutch 에피소드(대기 1초 → 시연 2초 → grip 놓음 1초 → 시연 2초 → 대기 1초, fake 카메라 640×480): 앞뒤 1.58초를 잘라 168 frame. 움직임·유지 구간 경계가 기록 패턴과 일치하고, 유지 구간의 action은 state와 정확히 같음. 로봇 샘플 stale 0, 카메라 정렬 오차 p99 2.4 ms. `LeRobotDataset`으로 다시 로드해 state·action·task·30 Hz timestamp·action chunk(`delta_timestamps`)를 확인했고, 영상은 원본과 평균 0.3~0.7/255 차이(AV1 재인코딩) |
| **현장 FR5 데이터** (2026-10-20, Docker v0.3.0, SDK 2.2.8, 카메라·그리퍼 없음, 3 에피소드) | raw 샘플: tick 누락 0, 간격 p99 9.1~10.4 ms(max 17.6 ms), `state_valid` 100%, `robot_time_ms`가 host 시계와 같은 속도(29.48 s vs 29.49 s). 측정 TCP는 명령을 72~88 ms(9~11 tick) 늦게 따라가며 이후 오차 0.3~0.7 mm. rx가 ±180°를 넘나든 횟수 13·10회 |
| 현장 데이터 R4 변환 | success 2개 → 1416 frame(27.9 s + 19.2 s, 앞뒤 대기 1.6 s·11.9 s 제거), failure 1개 기본 제외. scipy로 따로 재구성한 값과 state·action·관절·timestamp 최대 오차 3e-8(float32 정밀도). rx 순환에도 rot6d frame 간 최대 변화 0.011. action과 state 위치 차이 평균 4.6~6.0 mm(약 80 ms 추종 지연). 50-step action chunk 로드 확인. raw 파일은 변환 전후 md5 동일 |
| 자동 테스트 | 138개 통과 (`LEROBOT_PYTHON` 지정 시 lerobot 실변환 포함, 미지정 시 1개 skip). 그 외 이전 단계 결과: 124개 통과 (3회 반복, 기존 98개 포함). JS 테스트는 의도적으로 넣은 회귀(long-press 900 ms)를 잡아내는 것을 확인함 |
| 실제 페이지 로드 (headless Chrome, dry-run 서버) | `/`와 `/monitor` 모두 JS 오류 없음, 녹화 상태와 다음 task 표시 |
| 로봇 데이터 10.5초 | 1314 샘플, tick 누락 0, 간격 p50 8.00 / p99 8.24 ms |
| fake 카메라 640×480 + depth 10.5초 | 315 프레임, frame_number 누락 0, 간격 p99 33.67 ms |
| 프레임 ↔ 가장 가까운 로봇 샘플 | p50 1.9 / p99 3.8 ms (8 ms 주기에서 이론상 최선) |
| 인코딩 중 servo 타이밍 | missed tick 0 |
| 디코딩 | mp4와 mkv 모두 정확히 315 프레임 |
| 녹화 중 Ctrl+C | worker 정상 종료 → 에피소드가 `interrupted`로 저장 |
| 실제 `realsense` 소스, 장치 없음 | 즉시 "no RealSense device connected" 표시, 녹화 거부, 텔레옵 정상 |
| 샘플링 비용 | tick당 약 1.5 µs (fake client) |

---

## 6. 실제 장비에서 확인할 것 / 알려진 제한

1. **CNDE 상태 갱신:** 현장 데이터로 확인했다(5장). `robot_frame_cnt`는 항상
   0이므로 쓰지 않고 `robot_time_ms`로 판단한다. SDK는 필드를 하나씩 갱신하므로
   관절과 TCP가 서로 인접한 패킷(8 ms 차이)에서 왔을 수 있다.
2. **D435i 실측:**
   - udev 규칙을 설치했는지, USB 3로 연결됐는지 확인한다.
   - 실제 30 fps가 나오는지, `frame_number` 누락이 없는지 본다.
   - 파일 크기를 확인한다. fake 영상은 단순해서 10초에 0.1 MB였으므로 실제
     크기는 따로 측정해야 한다.
   - CPU 부하와 servo jitter를 측정한다.
3. **카메라-로봇 지연(R5):** `arrival_mono_ns`에는 전송 지연이 포함되어 있다.
   로봇을 빠르게 움직였다 멈추고 영상과 `tcp`가 바뀌는 시점을 비교해 지연을
   잰 뒤, 변환기에서 보정값으로 쓴다.
4. **카메라 내부 drop:** 카메라나 USB 단에서 빠진 프레임은 `complete`에
   반영되지 않는다. 변환할 때 `frame_number` 연속성으로 확인한다.
5. **양팔 설정:** 코드는 팔별 파일(`robot_left.bin`, `robot_right.bin`)을
   지원하지만 카메라는 1대만 지원한다. 현재 운용은 한 팔이다.
6. **Quest 실기기 (R3):** 헤드셋에서 확인할 것들이다.
   - A/B 버튼 매핑(`buttons[4]`/`[5]`)이 맞는지
   - `hapticActuators[0].pulse()` 진동이 오는지
   - 상태등이 시야 위쪽에서 보이고 두 눈에서 하나로 겹쳐 보이는지

   위치(뷰포트 높이 68%, 폭 5%)는 착용해 보고 조정한다. 몰입 모드에서는 녹화
   중인 task 문구를 볼 수 없으므로 task는 시작 전에 확인한다.

---

## 7. 다음 단계

- **데이터셋 구성 주의:** 현장 녹화는 robot IP 192.168.57.3(에피소드 0)과
  192.168.57.2(에피소드 1·2)로 서로 다른 로봇에서 나왔다. 변환기는 로봇을
  구분하지 않으므로, 로봇이나 작업대 배치가 다르면 raw 폴더를 나눠서 변환한다.
  그리퍼가 꺼진 상태로 녹화하면 `gripper` 차원은 항상 0이다.
- **학습 확인:** 실제 FR5 + D435i로 녹화한 데이터를 변환해 ACT나 Diffusion
  Policy를 짧게 학습시켜 본다. 이를 통해 다음을 정한다.
  - action 표현(`command` 또는 `next_state`)
  - 회전 표현
  - `observation.joint_pos`를 state에 넣을지 여부
- **양팔/다중 카메라 변환:** 지금 변환기는 한 팔과 카메라 1대만 지원한다.
- **R5 — 지연 보정:** 실제 장비에서 지연을 측정해 변환기 offset에 반영한다.
