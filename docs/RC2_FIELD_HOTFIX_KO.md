# rc2 현장 핫픽스와 후속 수정 계획

> 상태: rc2 현장 사본 복구용으로만 유지한다. 저장소의 정식
> `Unreleased` 구현은 signature 사전 검사, lossless trigger IPC,
> `GRIPPER_ACTION`, non-blocking 완료 polling과 time-based velocity
> limiter로 이 임시 패치 경로를 대체한다.

## 적용 대상과 목적

이 문서는 `vr-teleop-0.2.0rc2`, Fairino FR5 펌웨어 V3.7.8,
Python SDK V2.0.8 현장 사본을 위한 임시 대응 절차다.

핫픽스 `rc2-hf1`은 다음 두 문제만 보수적으로 완화한다.

1. `ServoCart`에 `exaxis`를 시험 전송한 뒤 `TypeError`로 호환성을
   판별하던 동작을 제거한다.
2. 100ms를 조금 넘는 pose 공백으로 반복 정지되는 빈도를 낮춘다.
3. pose timeout 순간의 sequence, input age, 마지막 SDK 호출 시간과
   missed tick 누계를 WARNING 로그로 남긴다.

이는 stable release가 아니다. 실제 pose 공백의 발생 위치, ServoCart 호출
주기 분포와 controller 통신 단절 정지는 별도로 검증해야 한다.

## 현장에서 먼저 지켜야 할 사항

- 로봇 주변을 비우고 물리 E-stop을 담당할 감시자를 둔다.
- grip을 놓고 로봇이 정지한 것을 확인한다.
- teleop 서비스를 정상 종료한 다음 패치를 실행한다.
- 스크립트는 실행 중인 teleop process를 발견하면 기본적으로 중단한다.
- 스크립트는 로봇 명령, 서비스 정지 또는 서비스 재시작을 자동 수행하지 않는다.
- `--allow-running`은 파일만 먼저 고쳐야 하는 특별한 상황에만 사용한다.
  실행 중인 process에는 변경 내용이 반영되지 않는다.

## 사용 방법

스크립트를 rc2 프로젝트 폴더로 복사한 뒤 해당 폴더에서 실행한다.

```bash
cd /home/user/fr5_teleop/vr-teleop-0.2.0rc2/vr_teleop-0.2.0rc2

# 먼저 대상과 예상 변경 라인 확인
bash rc2_field_hotfix.sh --check

# 패치 적용
bash rc2_field_hotfix.sh
```

스크립트가 `hotfix/` 폴더 안에 있는 경우 다음처럼 실행할 수 있다.

```bash
bash hotfix/rc2_field_hotfix.sh --check .
bash hotfix/rc2_field_hotfix.sh .
```

실행 결과에는 파일별 논리 변경 라인, 추가/삭제 라인과 합계가 표시된다.
수정 전 파일은 다음 형태로 같은 폴더에 백업된다.

```text
fairino_client.py.pre-rc2-hf1-YYYYMMDD-HHMMSS.bak
worker.py.pre-rc2-hf1-YYYYMMDD-HHMMSS.bak
config.yaml.pre-rc2-hf1-YYYYMMDD-HHMMSS.bak
test_fairino_adapter.py.pre-rc2-hf1-YYYYMMDD-HHMMSS.bak  # tests가 있는 경우
```

스크립트를 다시 실행하면 이미 반영된 항목은 건너뛰며 수정 라인 수를
0으로 표시한다. 알려진 rc2 구조와 다르면 임의 수정하지 않고 중단한다.

## 핫픽스 설정의 의미

```yaml
timing:
  pose_timeout_s: 0.200
  worker_watchdog_s: 0.500

motion:
  max_step_mm: 0.75
```

`pose_timeout_s`를 100ms에서 200ms로 늘리는 대신 `max_step_mm`을
1.5mm에서 0.75mm로 낮춘다. 명령 주기가 정확히 8ms라고 가정하면
stale pose 동안 step limiter가 추가로 진행할 수 있는 계산상 상한은
두 설정 모두 약 18.75mm다.

이 수치는 실제 정지거리 보장이 아니다. SDK 호출 지연, controller
보간, `ServoMoveEnd` 처리 시간과 robot dynamics는 별도 측정해야 한다.
로봇 반응이 느려지는 것은 의도된 현장 안전 절충이다.

500ms timeout은 적용하지 않는다. 현재 기본 watchdog과 같아 설정 검증에
실패하며, watchdog까지 늘리면 SDK hang containment가 느려진다.

## 지터링 원인 분류

### 현재 로그로 확정된 원인

`reason=pose_timeout`과 서로 같은 `servo_start_count`,
`servo_end_count`는 pose 공백마다 `ServoMoveEnd`가 실행되고 작업자의
재무장 뒤 `ServoMoveStart`가 다시 실행된 사실을 보여준다. 이 과정은
육안으로 정지와 출발이 반복되는 지터링처럼 보일 수 있다.

### 가능성이 높지만 측정이 필요한 원인

1. Quest WebXR frame에서 오른손 pose를 얻지 못한 구간
2. 브라우저 main thread 정체 또는 `bufferedAmount` pose drop
3. Wi-Fi/WSS 수신 간격 증가
4. 불규칙한 `ServoCart` 완료 시간
5. 명령 한 회당 `max_step_mm`을 적용해 명령 빈도 변화가 TCP 속도
   변화로 바뀌는 현상
6. VR 위치 노이즈가 EMA를 통과해 작은 반대 방향 명령을 반복하는 현상

상태의 `sdk_call_ms`는 평균이 아니라 마지막 SDK 호출 한 건이다.
`IDLE / pose_timeout` 상태에서 보이는 값은 마지막 `ServoMoveEnd` 시간일
수 있으므로 이를 ServoCart 평균 시간으로 해석하면 안 된다.

FAIRINO 문서는 ServoCart의 `cmdT`를 명령 전달 주기로 정의하고 예제에서
8ms마다 명령을 전달한다. `cmdT=0.008`을 유지한 채 실제 전달 간격이
크게 흔들리는지는 작업명별 분포를 수집해 판단해야 한다.

참고: [FAIRINO SDK Manual - Cartesian space servo mode motion](https://fairino-doc-en.readthedocs.io/3.6.7/SDKManual/CPPRobotMovement.html#cartesian-space-servo-mode-motion)

## 정식 소스 반영 상태와 남은 수정

### P0: SDK 호출 호환성 — Unreleased 반영

- `inspect.signature` 검사를 connect 단계에서 한 번만 수행한다.
- signature에 명시적인 `exaxis`가 있을 때만 전달한다.
- signature 확인 실패 또는 `**kwargs`뿐인 모호한 wrapper는 hardware
  시작을 거부하거나 명시적인 검증된 SDK profile을 요구한다.
- 호환성 판별을 위해 motion RPC를 시험 호출하지 않는다.
- SDK/firmware, 선택된 signature profile을 시작 로그에 남긴다.
- 제어기가 에러 14를 반환하는 wrapper를 포함한 회귀 테스트를 추가한다.

### P0: 입력 공백 관측성 — 핵심 로그 반영, 장기 metric 남음

- Quest에서 XR frame 수, 유효 controller pose 수, 전송 수,
  `bufferedAmount` drop 수와 각 최대 간격을 집계한다.
- 서버에서 pose sequence gap과 수신 간격 p50/p95/p99/max를 집계한다.
- worker에서 timeout 당시 pose sequence와 정확한 input age를 기록한다.
- `sdk_call_ms`를 작업명별 p50/p95/p99/max로 분리한다.
- 누적 `missed_ticks`뿐 아니라 측정 구간의 tick 수와 비율을 기록한다.

### P1: 명령 주기와 motion limiter — velocity limiter 반영

- 실제 경과 시간 기반 `max_velocity_mm_s`와 단일 명령
  `max_step_mm` cap을 함께 적용했다.
- 필요하면 acceleration 및 jerk limiter를 추가한다.
- 긴 overrun 뒤 명령을 몰아서 보내지 않는 현재 deadline skip은 유지한다.
- 실제 ServoCart 전달 간격과 `cmdT`의 불일치를 측정한다.
- 8ms를 지속할 수 없으면 숫자만 변경하지 말고 controller가 허용하는
  주기, SDK transport와 보간 동작을 먼저 검증한다.

### P1: 포즈 품질

- controller pose가 사라진 경우와 WebSocket 지연을 별도 reason으로
  구분한다.
- VR 원점 주변 deadband와 position noise 통계를 검토한다.
- EMA 계수는 실제 noise/latency 측정 후 조정한다.
- grip threshold와 hysteresis 상태를 UI와 진단 로그에 표시한다.

### P1: 상태 머신과 현장 UX

- `pose_timeout` 후 `rearm_required`가 된 사실과 “grip을 완전히 놓고
  다시 누르기”를 Quest와 모니터 화면에 즉시 표시한다.
- 최근 stop reason, input age와 pose drop을 작업자가 볼 수 있게 한다.
- 반복 start/end 경보에 해당 시간 구간의 pose gap과 SDK latency를 함께
  기록한다.

## 핫픽스 적용 후 최소 확인

1. hardware 옵션 없이 config와 Python 문법을 먼저 확인한다.
2. dry-run으로 Quest를 연결해 5분 동안 grip 유지 및 tracking-loss를
   재현한다.
3. 실제 로봇에서는 가장 낮은 허용 속도와 좁은 workspace로 시작한다.
4. 정지/출발 횟수, timeout 당시 input age, ServoCart 호출 간격을 기록한다.
5. grip 해제, WebXR 종료, Wi-Fi 단절 때 실제 정지 시간을 각각 측정한다.

통신 단절 정지 시간과 controller 측 stop-on-disconnect가 검증되기 전에는
핫픽스를 stable release 또는 안전 승인으로 간주하지 않는다.

## 다음 릴리즈의 진단 로그 강화

정식 소스에는 다음 로그가 추가된다. pose 좌표 전체는 저장하지 않는다.

- 모든 worker state transition:
  generation, state, reason, servo lifecycle count, rearm 상태, pose sequence,
  input age, 마지막 SDK 작업과 시간
- 느린 SDK 호출 1초 집계:
  작업명, 마지막/최대 호출 시간, 발생 횟수, servo period
- servo deadline miss 1초 집계:
  구간/누적 missed tick, 최대 overrun, jitter, 마지막 SDK 작업
- pose timeout:
  timeout 기준, pose sequence, input age, SDK 작업, missed tick
- WebSocket pose stream:
  10초간 sample 수, sequence 누락, 서버 수신 최대 간격,
  Quest client-time 최대 간격
- 브라우저 backpressure로 버린 frame도 sequence를 소비해 다음 정상
  pose에서 누락 개수를 서버 로그로 확인
- controller claim/release, WebXR end, fault reset, protocol 오류
- parent/worker PID와 worker generation

서버 수신 간격만 커지고 Quest client-time 간격은 정상이라면 네트워크나
서버 수신 지연 가능성이 높다. 두 간격이 함께 커지면 Quest WebXR frame,
controller tracking 또는 브라우저 main thread 정체 가능성이 높다.

systemd 환경의 최근 로그는 다음처럼 수집한다.

```bash
sudo journalctl -u vr-teleop --since "15 minutes ago" \
  --no-pager -o short-iso > vr-teleop-field.log
```

직접 실행할 때는 stderr까지 포함해 저장한다.

```bash
.venv/bin/python -m teleop --config config.yaml --confirm-hardware \
  2>&1 | tee "vr-teleop-$(date +%Y%m%d-%H%M%S).log"
```

로그 파일에는 TLS key, credential 또는 매 frame pose 원문을 넣지 않는다.
