# ROBOTIS VR 텔레옵 구현 전략 분석

> 내부 agent 전용 문서. 현장 매뉴얼, source distribution, wheel 또는
> release bundle에 포함하지 않는다.

## 문서 목적

ROBOTIS AI Worker의 Meta Quest 3 VR 텔레옵 구현에서 재사용할 설계 원칙과
FR5 텔레옵에 그대로 적용하면 안 되는 부분을 구분한다. 이 문서는 ROBOTIS
코드를 이 저장소로 복사하거나 ROS 2로 이주하기 위한 지침이 아니다.

분석 기준은 2026-08-10에 확인한 다음 공식 소스다.

- `ROBOTIS-GIT/robotis_applications`, `jazzy`, commit
  `884f4f815fa8310ea430b13a5ffc0987047d89ab`
- `ROBOTIS-GIT/cyclo_control`, `main`, commit
  `ceffbd7562028f6b317e462911e2a0991b9ba735`
- ROBOTIS AI Worker VR Teleoperation 공식 운영 문서

비교 대상이 다른 ROBOTIS 시연 또는 이전 release라면 영상만 보고 같은
구현이라고 가정하지 말고 repository, branch, commit을 다시 확인한다.

## 핵심 결론

ROBOTIS와 현재 FR5 시스템은 Quest와 robot PC 사이에 HTTPS/WSS를 사용한다는
점만 같다. ROBOTIS의 부드러운 동작을 만드는 핵심은 WebSocket 라이브러리가
아니라 다음의 두 주기 제어 구조다.

```text
Quest 3 / WebXR
    │ Vuer HTTPS/WSS, controller + body event
    ▼
VR publisher (Python/ROS 2)
    │ latest reference, nominal 30 Hz
    │ BEST_EFFORT + KEEP_LAST(1)
    ▼
Cyclo VR controller (C++)
    │ fixed 100 Hz task-space velocity/QP loop
    │ joint trajectory
    ▼
ROS controller / robot hardware
```

WebSocket event가 도착할 때마다 모터 명령을 한 번 호출하는 구조가 아니다.
불규칙하게 도착하는 VR reference와 고정 주기의 motion generation을
분리한다. FR5에도 이 원칙은 적용하되, ROS 2, Vuer, 양팔 QP 구현 자체는
도입하지 않는다.

## 공식 소스에서 확인된 전략

### 1. VR 입력 주기와 로봇 제어 주기를 분리한다

`vr_publisher_sg2.py`의 기본값은 다음과 같다.

```text
stream_fps=30
pose_publish_hz=30.0
Vuer queue_len=3
```

Cyclo의 VR controller는 별도의 `100 Hz`, `dt=0.01 s` wall timer에서
실행된다. 즉 30 Hz의 새 pose가 없더라도 controller는 최신 목표에 대해
100 Hz로 연속적인 joint target을 만든다.

적용 원칙:

- network message arrival callback에서 Fairino SDK를 호출하지 않는다.
- 새 VR sample의 수와 ServoCart 호출 수를 동일하게 만들지 않는다.
- reference 수신 상태와 motion trajectory 상태를 서로 다른 object로 둔다.

### 2. 오래된 입력을 쌓지 않고 최신값만 유지한다

ROBOTIS VR publisher는 ROS QoS를 `BEST_EFFORT`, `KEEP_LAST`, `depth=1`로
구성한다. Vuer도 bounded queue를 사용한다. 이는 누락된 pose를 나중에
순서대로 재생하기보다 최신 reference를 우선한다는 의미다.

현재 저장소의 `LatestPoseMailbox`와 browser `bufferedAmount` drop 정책은
이미 같은 방향이다. 이 부분을 일반 FIFO pose queue로 되돌리지 않는다.

단, grip/trigger transition은 pose와 달리 유실되면 안 되므로 현재의
별도 control pipe를 유지한다.

### 3. publish gate와 controller activation을 분리한다

ROBOTIS는 두 squeeze가 threshold 이상일 때만 goal pose를 publish한다.
그것만으로 로봇을 즉시 활성화하지 않고 별도 reactivate 입력, 현재 robot
pose와 reference의 정렬 검사, 3초 activation delay를 거친다.

활성화 뒤에는 8초 동안 desired velocity를 선형 ramp한다. 따라서 재개
순간의 reference 오차가 한 tick의 큰 속도 명령으로 바뀌지 않는다.

FR5 적용 원칙:

- grip rising edge에서 새 VR/TCP origin을 잡는 현재 clutch 의미를 유지한다.
- `ServoMoveStart` 성공 직후 target을 급격히 따라가지 않도록 configurable
  activation ramp를 motion planner에 둔다.
- ramp는 stale timeout, grip release, workspace, step/velocity 제한을
  대체하지 않는다.

### 4. pose를 직접 joint position으로 복사하지 않는다

Cyclo controller는 Cartesian position/orientation error에 gain을 적용해
desired task velocity를 만들고, damping과 collision constraint가 포함된
QP를 풀어 joint velocity를 계산한다. 이후 고정 `dt`로 다음 joint target을
생성한다.

FR5는 controller 내부와 SDK가 다르므로 같은 QP를 복제하지 않는다. 다만
다음 성질은 필요하다.

- position target뿐 아니라 planner의 velocity와 acceleration 상태를 유지
- output 변화에 velocity, acceleration, jerk 상한 적용
- 입력 sample 간격이 달라도 같은 물리 trajectory가 유사한 output을 생성
- 긴 지연 뒤 보정 명령을 burst로 따라잡지 않음

### 5. 큰 reference jump는 정상 추종 대상이 아니다

ROBOTIS reference checker는 연속 pose 간 position 또는 orientation jump가
설정값을 넘으면 controller를 divergence 상태로 latch한다. 확인한 기본값은
`0.1 m`, `30 deg`다.

이 숫자는 AI Worker 양팔용 값이므로 FR5에 복사하지 않는다. FR5에서는
초기화된 VR origin 기준 변위, robot workspace, position scale을 반영한
별도의 보수적인 threshold를 승인해야 한다.

### 6. 네트워크 품질을 software만으로 해결한다고 가정하지 않는다

ROBOTIS 공식 문서도 Wi-Fi 성능이 낮으면 VR server를 robot PC에 두고
Quest 3를 USB-C Ethernet adapter로 유선 연결할 것을 권고한다.

공유기 위치에 따라 통신 품질이 달라지는 현장은 다음을 software bug와
분리한다.

- 작업자 몸과 Quest 본체에 의한 RF 차폐
- AP antenna 방향과 편파
- 반사와 다중경로
- 사용 channel의 간섭
- Quest tracking loss와 실제 WSS transport loss

Software는 손실된 RF packet을 복구할 수 없다. 할 수 있는 일은 backlog를
버리고, latest reference를 사용하고, 짧은 공백을 부드럽게 흡수하고,
stale 한계를 넘으면 안전하게 정지하는 것이다.

## ROBOTIS 구현에서 그대로 채택하지 않을 부분

### VR pose stale timeout이 명시적이지 않다

확인한 publisher/controller에는 controller WebSocket EOF 또는 VR pose age를
기준으로 servo를 종료하는 명시적인 timeout이 보이지 않는다. joint state
feedback timeout은 있지만 VR input timeout과는 다르다. 새 reference가
없으면 최신 목표를 계속 유지할 수 있다.

Vuer 내부 disconnect 동작은 application source만으로 단정하지 않는다.
어느 경우에도 현재 FR5의 다음 경로를 제거하지 않는다.

- WebSocket EOF → session loss → servo stop
- WebXR end → servo stop
- pose timeout → `ServoMoveEnd`
- worker fault/shutdown → ordered stop

### application-level sequence와 timing 진단이 약하다

ROBOTIS publisher source에는 현재 FR5 protocol의 `seq`, server receive
monotonic timestamp, client RTT, XR/pose gap telemetry와 같은 진단 경계가
없다. Vuer가 내부적으로 제공할 수 있는 기능과 application이 실제
기록하는 값을 혼동하지 않는다.

현재 진단 기능은 유지하고 ROBOTIS 코드에 맞추기 위해 제거하지 않는다.

### 선언된 low-pass 설정을 적용 코드로 오인하지 않는다

확인한 SG2 publisher에는 `low_pass_filter_alpha=0.5` 선언이 있지만 arm
pose 발행 경로에서 사용되지 않는다. ROBOTIS가 단순 EMA 하나로 jitter를
해결했다고 문서화하거나 이를 근거로 현재 EMA 값을 바꾸지 않는다.

### commanded state를 actual feedback으로 간주하지 않는다

Cyclo loop 일부는 이전 `q_desired`를 내부 feedback state로 사용해 다음
명령을 생성한다. 이는 해당 robot/controller stack의 설계 선택이다.
FR5에서 last target을 actual TCP로 간주하면 tracking error와 controller
lag을 숨길 수 있으므로 그대로 복제하지 않는다.

## 현재 FR5 구현과의 차이

```text
현재
WebXR frame → latest pose → 8 ms Python worker
            → EMA + step/velocity clamp
            → synchronous ServoCart(mode=0)

목표
WebXR frame → validated latest reference
            → seq-aware reference sampler
            → stateful velocity/acceleration/jerk-limited trajectory
            → best-effort ServoCart cadence
```

현재 남은 주요 차이는 다음과 같다.

1. `MotionPlanner.target_for()`는 새 pose인지와 관계없이 servo tick마다 EMA를
   적용한다. 동일 `seq`가 반복되어도 filter state가 변한다.
2. velocity와 per-step 제한은 있지만 acceleration/jerk state가 없다.
3. `ServoCart`는 동기 XML-RPC이며 SDK call latency가 servo cadence에 직접
   영향을 준다.
4. 현재 125 Hz는 목표일 뿐 hard real-time이 아니며 RPC가 지속적으로
   8 ms 안에 끝난다는 보장이 없다.

따라서 WSS library를 Vuer로 바꾸거나 서버를 ROS로 이주하는 작업보다
motion generation 경계를 먼저 개선한다.

## FR5에 적용할 목표 설계

### 데이터 흐름

```text
LatestPoseMailbox
    │ snapshot(seq, received_ns, position, grip)
    ▼
ReferenceSampler
    - generation/seq 증가 검사
    - 새 sample만 filter 입력으로 소비
    - reference age와 input gap 기록
    ▼
TrajectoryPlanner
    - target position
    - commanded position/velocity/acceleration
    - deadband, velocity, acceleration, jerk, workspace limit
    - activation ramp
    ▼
Fairino adapter
    - validated absolute TCP only
    - ServoCart call duration/error normalization
```

이름은 구현 중 바꿀 수 있지만 책임은 합치지 않는다.

### ReferenceSampler 규칙

- `generation`이 현재 controller generation과 다르면 sample을 거부한다.
- `seq <= last_consumed_seq`면 filter/reference state를 갱신하지 않는다.
- 새 sample의 server receive monotonic time으로 gap과 age를 계산한다.
- 새 sample이 없지만 아직 fresh하면 마지막 reference를 유지한다.
- fresh 구간 동안 planner는 마지막 목표를 향해 제한된 trajectory를 계속
  만들 수 있지만 새 입력이 온 것처럼 filter를 반복 적용하지 않는다.
- timeout이면 목표 유지가 아니라 기존 상태 머신의 stop 경로를 실행한다.
- 누락된 sequence를 보간하기 위해 과거 command를 burst replay하지 않는다.

### TrajectoryPlanner 규칙

- planner tick은 absolute monotonic deadline을 사용한다.
- 각 축 또는 3D vector norm 기준 deadband 선택은 unit test와 실제 Quest
  정지 노이즈 측정 뒤 결정한다.
- velocity, acceleration, jerk 제한은 config 한 곳에서 검증한다.
- 긴 SDK call 뒤 실제 elapsed time을 무제한 step 증가로 바꾸지 않는다.
- 새 clutch engage에서 position, velocity, acceleration, filter, ramp state를
  한 번에 초기화한다.
- grip release, stale, session loss, FAULT에서는 ramp-down만 믿지 않고
  `ServoMoveEnd`를 실행한다.
- orientation teleop은 `orientation.enabled`(기본 `false`) 뒤에 구현되어
  있다. 비활성 시에는 이전과 동일하게 robot origin orientation을
  유지한다. 활성 시에도 `orientation.max_deviation_deg`(기본 20°)로 origin
  대비 누적 회전을 제한하는데, 이는 §5의 ROBOTIS reference-divergence
  gate(위치 0.1 m / orientation jump 30° per-sample)와는 별개로 독립
  도출된 값이다 — ROBOTIS 값은 "연속 샘플 간 점프"를 잡는 latch
  threshold이고, 이 값은 "clutch engage 이후 누적 총 편차"를 잡는 별도의
  commissioning-safety bound다.

### SDK cadence 판정

실제 robot 로그에서 다음을 먼저 비교한다.

```text
servo_period_ms
ServoCart p50/p95/p99/max
missed_tick_count
actual command interval
pose receive gap
```

`ServoCart p95/p99`가 설정 period를 반복적으로 넘는다면 planner만 바꿔도
command cadence jitter는 남는다. 이 경우 공식 Fairino API가 지속 가능한
주기와 transport를 확인한 뒤 `servo_period_s`와 `cmdT`를 함께 승인한다.
확인 없이 UDP API가 있다고 가정하거나 vendor SDK를 patch하지 않는다.

## 원인 판별 기준

| 관측 결과 | 우선 원인 | 다음 조사 |
|---|---|---|
| AP 위치에 따라 XR/pose gap과 RTT가 함께 악화 | RF/Wi-Fi | AP 가시선, channel, Quest 유선 비교 |
| XR frame은 정상, valid controller pose gap만 증가 | Quest tracking | 방향/조명/controller occlusion |
| pose gap은 정상, SDK p99와 missed tick 증가 | Fairino SDK/XML-RPC | call별 latency와 실제 command interval |
| network/SDK가 정상, target이 미세 왕복 | input noise/planner | seq-aware sample, deadband, target derivative |
| target은 매끄럽지만 actual TCP만 진동 | robot/controller dynamics | 저주기 target/actual 비교, controller 승인 조사 |
| servo start/end count가 증가 | lifecycle/stale/grip edge | transition reason과 pose age 대조 |

하나의 육안 증상을 보고 WebSocket, XML-RPC 또는 robot 함수 중 하나로
단정하지 않는다. 같은 monotonic time 축으로 상관관계를 남긴다.

## 자동 검증 요구사항

구현 전에 다음 fake/unit scenario를 먼저 추가한다.

1. 동일 pose `seq`를 여러 servo tick 읽어도 reference filter update는 1회다.
2. 동일 물리 trajectory를 30/72/90 Hz 및 불규칙 gap으로 입력해도 출력
   속도·가속도 profile이 허용 오차 안에서 유사하다.
3. pose burst에서는 최신 reference만 소비하고 과거 pose를 재생하지 않는다.
4. velocity, acceleration, jerk, workspace 제한을 모든 축과 대각선에서
   검증한다.
5. activation ramp 중 grip release/stale/session loss가 즉시 stop 경로로
   연결된다.
6. 큰 reference jump는 motion command 전에 reject/latch된다.
7. SDK delay/fault injection에서도 `/health/live`와 parent watchdog이
   유지된다.
8. 성공한 각 ServoMoveStart에는 ServoMoveEnd 시도가 정확히 한 번 대응한다.

실제 FR5 테스트는 이 자동 검증 뒤 별도 수동 commissioning으로 수행한다.

## 구현 의사결정 요약

채택한다.

- latest-only bounded reference
- input rate와 actuator loop 분리
- seq-aware sample consumption
- stateful trajectory generation
- activation alignment/ramp와 reference jump gate
- network/SDK/planner metric 상관 분석
- 유선 Quest 비교를 포함한 RF 원인 분리

채택하지 않는다.

- Vuer 또는 ROS 2로의 전면 이주
- ROBOTIS의 30/100 Hz와 threshold 숫자 복사
- VR input timeout 없는 latest-goal 무기한 유지
- 두 controller squeeze UX의 기계적 복사
- `q_desired` 또는 last target을 실제 robot feedback으로 간주
- low-pass 상수 하나만 조정하는 jitter 해결

## 공식 참조

- [ROBOTIS VR Teleoperation 운영 문서](https://ai.robotis.com/ai_worker/operation_vr_teleoperation_ai_worker.html)
- [ROBOTIS Vuer SG2 publisher](https://github.com/ROBOTIS-GIT/robotis_applications/blob/884f4f815fa8310ea430b13a5ffc0987047d89ab/robotis_vuer/robotis_vuer/vr_publisher_sg2.py)
- [Cyclo AI Worker config](https://github.com/ROBOTIS-GIT/cyclo_control/blob/ceffbd7562028f6b317e462911e2a0991b9ba735/cyclo_motion_controller_ros/config/ai_worker_config.yaml)
- [Cyclo VR controller loop](https://github.com/ROBOTIS-GIT/cyclo_control/blob/ceffbd7562028f6b317e462911e2a0991b9ba735/cyclo_motion_controller_ros/src/nodes/ai_worker/vr_controller_node.cpp)
- [Cyclo reference jump checker](https://github.com/ROBOTIS-GIT/cyclo_control/blob/ceffbd7562028f6b317e462911e2a0991b9ba735/cyclo_motion_controller_ros/src/utils/reference_checker_node.cpp)
