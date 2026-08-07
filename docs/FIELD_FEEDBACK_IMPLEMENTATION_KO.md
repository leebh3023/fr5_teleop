# 현장 피드백 정식 반영 내역

## 결론

rc2 및 `rc2-hf1` 현장 시험에서 관찰된 문제를 다음 release candidate의
정식 모듈 경계로 옮겼다. 임시 현장 코드의 `time.sleep()`과
`ServoMoveStart()` 자동 재호출은 채택하지 않았다. 변경은 fake/dry-run
자동 시험을 대상으로 하며, 실제 FR5 승인 시험을 대체하지 않는다.

## 문제별 원인과 수정

| 현장 증상 | 확인된 원인 | 정식 수정 | 검증 |
|---|---|---|---|
| grip을 다시 잡아도 가끔 움직이지 않음 | overwrite-only pose에서 짧은 grip transition이 사라질 수 있음 | grip press/release를 control pipe에 순서대로 보존 | 한 worker period 안의 release/press 통합 시험 |
| trigger가 씹히거나 그리퍼 동작이 불안정함 | trigger는 pose snapshot에만 있었고 현장 임시 코드는 worker를 고정 sleep으로 막음 | trigger transition도 control pipe로 보존, `GRIPPER_ACTION` 상태 추가 | trigger press/release overwrite 통합 시험 |
| 그리퍼 뒤 stale/FAULT 또는 servo lifecycle 꼬임 | 1.8초 sleep 동안 heartbeat/tick/pose 검사가 중지되고 완료 뒤 servo를 자동 시작 | `MoveGripper(block=1)` 접수 후 `GetGripperMotionDone()`을 주기적으로 polling | 완료, controller fault, rearm 통합 시험 |
| 최신 pose인데 stale로 취급될 가능성 | worker가 tick 시작 시각을 먼저 읽은 뒤 parent가 더 새로운 `received_ns`를 게시할 수 있음 | mailbox snapshot을 읽은 뒤 `monotonic_ns()`를 다시 읽어 freshness 계산 | process 회귀 시험 및 코드 순서 고정 |
| SDK별 `ServoCart`/`MoveGripper` 인자 오류 | V2.0.8 legacy와 신형 SDK signature 차이 | connect 시 `inspect.signature` 한 번 검사, 모호한 wrapper는 fail-closed | legacy/extended/ambiguous adapter unit test |
| 정지와 출발이 반복되는 지터링 | 100ms 부근 pose 공백이 `ServoMoveEnd`를 만들고 재무장마다 `ServoMoveStart` 발생 | pose timeout 200ms, max step 0.75mm, 반복 transition ERROR와 pose-gap 로그 | stale stop 및 transition counter 시험 |
| 전송 간격에 따라 이동 속도가 달라짐 | step limiter가 명령 횟수 기준이라 실제 elapsed time을 반영하지 않음 | `max_velocity_mm_s × 실제 ServoCart 간격`과 `max_step_mm` 중 작은 값 적용 | motion planner unit test |

## 그리퍼 상태 머신

```text
ACTIVE
  └─ trigger rising edge
       └─ STOPPING: ServoMoveEnd 정확히 1회
            └─ GRIPPER_ACTION: MoveGripper non-blocking 1회
                 ├─ done ──> SLEEPING
                 ├─ session lost + done ──> IDLE
                 └─ command/status/fault/timeout ──> FAULT
```

`SLEEPING` 완료 뒤 grip이 계속 눌려 있어도 servo를 시작하지 않는다.
그리퍼 완료 이후의 grip release와 그 다음 press가 모두 확인되어야
새 VR/TCP 원점을 잡고 `ARMING`할 수 있다.

그리퍼 동작 중 session이 사라져도 controller에 접수된 동작을 취소할
수 있다고 가정하지 않는다. worker는 완료 또는 fault를 관찰한 뒤
`IDLE`/`FAULT`로 간다. 물리 정지는 controller 설정과 E-stop으로
별도 보장해야 한다.

## 지터링의 남은 원인과 판별법

이번 수정으로 반복적인 pose-timeout start/end와 명령 횟수 기반 속도
변동을 줄였지만, 실제 로봇에서 보이는 모든 진동이 소프트웨어
start/end 때문이라고 단정할 수는 없다.

1. `reason=pose_timeout`과 start/end count가 함께 증가하면 Quest,
   브라우저, WSS 또는 server 수신 공백이다.
2. `slow SDK call`과 `missed tick`이 함께 증가하면 XML-RPC/SDK 지연이
   8ms loop를 방해한 것이다.
3. 둘 다 정상인데 TCP가 미세하게 왕복하면 WebXR position noise,
   EMA, controller-side interpolation 또는 robot dynamics를 조사한다.
4. `servo_start_count`가 그대로인데 육안 진동만 남으면 start/end
   반복이 원인이 아니다. ServoCart target/actual TCP의 별도 저주기
   계측이 필요하다.

현재 로그는 pose sequence 누락, Quest client-time 간격, server 수신
간격, SDK 작업별 10초 p50/p95/p99/max 지연, missed tick, state
reason과 lifecycle count를 서로 대조할 수 있게 남긴다. 매 frame pose
전체는 기록하지 않는다.

## 새 설정

```yaml
timing:
  pose_timeout_s: 0.200

motion:
  max_velocity_mm_s: 50.0
  max_step_mm: 0.75

gripper:
  enabled: false
  index: 1
  activate_on_start: false
  initially_closed: false
  open_position: 0
  closed_position: 100
  velocity: 50
  force: 50
  command_max_time_ms: 3000
  action_timeout_s: 5.0
  poll_period_s: 0.050
```

실제 배포 예제는 최초 commissioning을 위해
`max_velocity_mm_s: 31.25`, `max_step_mm: 0.25`, gripper
velocity/force 30을 사용한다. 그리퍼는 controller-side 설정과
수동 확인 전까지 활성화하지 않는다.

## 실제 장비에서 남은 확인

- SDK V2.0.8 / Robot V3.7.8에서 inspected signature와
  `GetGripperMotionDone()` 반환 형태 기록
- trigger 20회에서 command/complete count 일치
- 그리퍼 완료 뒤 grip 유지 시 servo start count 불변
- 완료 후 release/press 시 새 원점으로 한 번만 servo start
- trigger 중 WebXR 종료과 network disconnect 동작
- 10분간 pose gap, SDK call, missed tick, start/end count 수집
- 실제 TCP 진동이 남으면 target/actual TCP의 저주기 안전 계측

이 항목이 끝나기 전에는 `Unreleased` 소스를 stable 또는 실제 로봇
승인 빌드로 표시하지 않는다.
