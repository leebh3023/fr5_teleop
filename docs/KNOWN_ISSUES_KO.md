# VR Teleop 알려진 문제와 수정 순서

## 목적

현장 증상을 추측으로 한꺼번에 수정하지 않고, 관측 가능한 원인으로
분리해 한 단계씩 해결한다. 각 항목은 실제 로봇 동작 여부와 무관하게
먼저 dry-run으로 재현하고, 로봇 시험은 별도 안전 승인 뒤 수행한다.

## P0-1. 방향에 따른 Quest Wi-Fi/WebSocket 단절

### 현장 관찰

- 특정 방향과 각도에서 WebSocket 지연·끊김이 심해진다.
- 공유기를 작업자 오른쪽에 두면 통신이 떨어진다.
- 공유기를 왼쪽에 두면 끊김 없이 동작한다.

### 현재 판단

좌우 위치만 바꿔 재현성이 크게 달라진다면 애플리케이션 WebSocket
로직보다 Quest와 AP 사이 RF 경로가 1순위다. 작업자 머리·몸·로봇
구조물에 의한 차폐, Quest 안테나의 방향성, AP 안테나 편파와 현장
반사에 의한 multipath를 의심한다. TCP/WebSocket은 무선 손실을
재전송하므로 RF packet loss가 직접 끊김 대신 큰 RTT, 수신 공백,
한꺼번에 도착하는 pose로 보일 수 있다.

Meta 지원이 제시하는 기본 조건도 PC-AP 유선 연결, Quest 전용
5 GHz AC/AX, 같은 방 또는 line-of-sight, AP 높이 1m 이상,
non-mesh 구성이다.

- [Meta 지원의 Quest 무선 구성 점검 항목](https://communityforums.atmeta.com/discussions/OtherTroubleshooting/3s-air-link-not-working/1345035)
- [Quest 3 연결 문제에서 router/headset firmware 점검 안내](https://communityforums.atmeta.com/discussions/PairingConnection/quest-3-keeps-losing-wifi-connection/1346267)

### 즉시 운용 원칙

- 실제 로봇 운용에서는 재현상 안정적인 왼쪽 AP 위치를 임시 표준으로
  고정한다.
- AP는 같은 방, 작업 영역과 가시선, 바닥에서 1m 이상에 둔다.
- Ubuntu PC는 AP에 Ethernet으로 연결한다.
- mesh/repeater와 자동 band steering을 사용하지 않고 Quest 전용
  SSID/band를 검토한다.
- AP 또는 Quest firmware 변경은 한 번에 하나씩 하고 시험 기록을
  남긴다.

### 소프트웨어 진단

클라이언트는 1초마다 다음 누적값과 구간 최대값을 서버에 보낸다.

- XR frame 수와 최대 frame gap
- 유효 controller pose 수와 최대 pose gap
- pose send/drop 및 tracking loss 누계
- WebSocket `bufferedAmount`
- telemetry 왕복 RTT last/max

작업자는 Quest 내부 페이지가 아니라 Ubuntu PC의
`https://<server>:8443/monitor`에서 확인한다. 서버 로그에도 10초
summary와 품질 저하 WARNING이 남는다.

## P0-2. Quest 내부 상태 UI를 작업자가 보지 못함

immersive WebXR 중에는 시작 페이지의 상태 정보가 작업자에게 사실상
보이지 않는다. 안전 판단이나 장애 분석을 Quest UI에 의존하지 않는다.

### 수정

- 별도 observer WebSocket을 사용하는 `/monitor` 화면 추가
- robot state, rearm, input age와 fault 표시
- Quest RTT/frame/pose/drop/tracking 표시
- worker jitter, SDK call, missed tick과 servo lifecycle count 표시

monitor는 control lease를 claim하지 않으며 로봇 명령을 보내지 않는다.

## P0-3. 입력 단절 때 반복 stop/start

### 현상

pose timeout마다 `ServoMoveEnd`, 작업자 재무장마다 `ServoMoveStart`가
반복되어 큰 지터처럼 보였다.

### 현재 상태

- pose timeout 200ms
- timeout/session loss 뒤 자동 ACTIVE 복귀 금지
- grip을 놓았다 다시 누르는 명시적 rearm
- start/end 급변 ERROR 및 timeout 당시 pose age 로그

이 항목은 완화됐지만 방향성 Wi-Fi 단절의 근본 원인을 해결하지 않는다.

## P1-1. ACTIVE를 유지하면서 남는 미세 지터

### 유력 원인

1. WebXR 위치 노이즈에 robot-space deadband가 없다.
2. EMA가 새 pose 기준이 아니라 8ms servo tick마다 같은 pose에도
   반복 적용된다.
3. velocity cap은 있지만 acceleration/jerk와 방향 반전 제한이 없다.
4. 새 pose가 불규칙하게 들어오면 필터의 실효 응답도 달라진다.

### 다음 수정

- pose sequence가 바뀔 때만 input filter 갱신
- elapsed-time 기반 low-pass filter
- robot-space deadband와 hysteresis
- acceleration/jerk limiter
- full pose 대신 target delta/noise p50/p95/p99 로그

## P1-2. `ServoCart` XML-RPC 전달 주기 변동

동봉 SDK의 `ServoCart`는 실제 명령 전 `GetSafetyCode()`를 호출하고
동기 RPC를 수행한다. 정상 호출도 두 번의 controller 왕복을 포함할 수
있으며 socket error에서는 내부 재시도가 발생한다. `cmdT=0.008`과 실제
전달 간격이 다르면 controller가 일정하지 않은 target stream을 받는다.

### 판별

- pose/client telemetry는 정상
- SDK call p95/p99가 8ms를 넘음
- worker missed tick과 deadline overrun이 함께 증가

### 다음 조사

- 현장 SDK V2.0.8 `ServoCart` wrapper의 안전 조회와 retry 확인
- 실제 command interval p50/p95/p99 기록
- controller가 지원하는 공식 streaming transport 확인
- XML-RPC safety check를 임의 제거하지 않음

## P1-3. 그리퍼와 Cartesian servo lifecycle

trigger 시 먼저 servo를 종료하고 non-blocking gripper 명령을 접수한다.
완료 polling 중 worker를 sleep으로 막지 않으며 완료 후 자동 servo
재개를 금지한다. 실제 FR5에서 20회 command/complete와 re-clutch를
추가 확인해야 한다.

## P2. 로봇 자세·동역학·기구 조건

software target과 전달 주기가 안정적인데 실제 TCP만 떨리면 다음을
조사한다.

- 특정 자세의 wrist/elbow singularity
- tool/TCP, payload와 center-of-mass 설정
- 설치 방향, base 설정과 느슨한 기구물
- controller firmware와 drive 상태

고정 TCP target 시험을 여러 자세에서 수행해 특정 자세에서만
재현되는지 확인한다.

## 원인 분리용 dry-run 시험표

실제 로봇을 움직이지 않고 AP 위치와 작업자 방향만 바꿔 각 조건을
60초 측정한다.

| AP 위치 | 작업자/Quest 방향 | RTT max | XR gap max | pose gap max | drops | tracking loss | server receive gap |
|---|---|---:|---:|---:|---:|---:|---:|
| 왼쪽 | 정면 | | | | | | |
| 왼쪽 | 오른쪽 90° | | | | | | |
| 왼쪽 | 뒤 180° | | | | | | |
| 오른쪽 | 정면 | | | | | | |
| 오른쪽 | 오른쪽 90° | | | | | | |
| 오른쪽 | 뒤 180° | | | | | | |

판별 기준:

- RTT/server gap만 증가: RF/Wi-Fi/TCP 가능성이 높다.
- XR gap도 증가: Quest/browser frame stall 가능성이 높다.
- XR frame은 정상이나 pose gap/tracking loss 증가: controller tracking
  또는 가림 문제다.
- client 지표는 정상이나 SDK/missed tick 증가: robot SDK/XML-RPC다.
- start/end는 그대로이고 target delta만 왕복: filter/deadband 문제다.

## 수정 순서

1. client telemetry와 외부 monitor
2. 좌우 AP/방향 dry-run matrix 수집
3. pose sequence 기반 time-aware filter와 deadband
4. acceleration/jerk limiter
5. ServoCart transport timing 조사
6. 제한된 실제 로봇 고정-target 및 자세별 시험
