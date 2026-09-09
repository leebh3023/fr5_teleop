# Changelog

이 프로젝트는 Semantic Versioning을 따르며, 실제 로봇 검증 전 빌드는
release candidate로 표시한다.

## [Unreleased]

### Added

- optional rotation (roll/pitch/yaw) teleop behind `orientation.enabled`
  (default `false`): the VR controller quaternion is now mapped to the
  robot's rx,ry,rz target, with per-tick angular-step and
  origin-deviation clamps mirroring the existing position limiter.
  Fairino's rx,ry,rz Euler convention is not documented anywhere in the
  vendor SDK; the implementation assumes the common industrial fixed-axis
  XYZ convention and isolates that assumption to a handful of pure
  functions in `control_math.py`. Not yet verified on real hardware — see
  AGENTS.md's manual real-robot test protocol before enabling outside
  dry-run.
- Quest XR frame/controller pose gap, tracking loss, WebSocket RTT,
  buffered bytes와 pose send/drop을 보고하는 1초 client telemetry
- control lease를 claim하지 않는 `/monitor` 작업자 화면
- AP 좌우 위치·Quest 방향별 RF/WebSocket dry-run 시험표와 알려진
  문제 우선순위 문서
- optional trigger-controlled gripper configuration and explicit
  `GRIPPER_ACTION` worker state
- lossless trigger press/release IPC, gripper completion polling and
  command/complete counters
- time-based `max_velocity_mm_s` limiter combined with the absolute
  per-command `max_step_mm` cap
- worker state transition에 PID/generation, stop reason, rearm 상태,
  servo lifecycle count와 마지막 SDK 작업을 포함한 진단 로그
- 느린 SDK 호출과 servo deadline miss의 1초 rate-limited 요약
- SDK operation별 10초 `p50/p95/p99/max` 호출 시간 요약
- startup/servo/gripper/reset/close fault의 exception type과 traceback
- Quest client-time과 서버 수신 시간을 분리한 pose stream gap 및
  10초 summary 로그
- WebSocket controller lifecycle과 rate-limited protocol 오류 로그
- rc2 현장 사본에 안전하게 적용할 수 있는 `rc2-hf1` Bash 핫픽스와
  한글 적용·rollback·진단 문서

### Changed

- worker lifecycle와 timing 진단을 `RobotWorkerRuntime`과
  `WorkerDiagnostics`로 분리해 상태 전이와 로그 집계 책임을 명확화
- `ServoCart`/`MoveGripper` signature를 connect 시 한 번 검사하며
  motion RPC trial call을 사용하지 않음
- 그리퍼 명령은 non-blocking으로 접수하고 고정 sleep 대신
  `GetGripperMotionDone`을 polling
- pose mailbox snapshot을 읽은 뒤 monotonic clock을 다시 읽어, parent가
  동시에 게시한 최신 pose timestamp가 worker의 오래된 tick timestamp보다
  새로워지는 경쟁 조건 제거
- 브라우저 backpressure로 폐기한 pose도 sequence를 소비해 다음 정상
  메시지에서 server가 누락 frame 수를 진단
- 로그 형식에 process PID를 추가하고 pose 전체 좌표는 기록하지 않음

### Fixed

- Ctrl+C가 parent와 RobotWorker를 동시에 interrupt해 child가
  `KeyboardInterrupt` FAULT로 종료되던 signal ownership 문제
- application shutdown에서 동일한 `SHUTDOWN` command를 두 번 보내던
  lifecycle 중복
- trigger 그리퍼 동작 중 worker를 고정 sleep으로 막아 watchdog/stale
  판정과 servo lifecycle이 꼬이던 현장 임시 구현 제거
- 그리퍼 완료 직후 grip을 계속 누른 상태에서 ServoMoveStart가 자동
  재호출될 수 있던 재무장 경로 차단
- trigger transition이 latest-pose overwrite에 유실될 수 있던 문제
- SDK V2.0.8/신형 SDK의 서로 다른 `MoveGripper` signature 호환

## [0.2.0rc2] - 2026-08-06

### Fixed

- 한 worker tick 안에 `grip=false -> true` pose가 연속 도착할 때
  overwrite-only mailbox가 release edge를 지워 재무장되지 않던 문제
- Quest grip analog 값에 hysteresis를 적용해 button threshold 부근의
  press/release 채터링 완화
- SDK V2.0.8의 `is_conect` 연결 flag와 `exaxis` 없는 legacy
  `ServoCart` signature 호환

### Changed

- pose는 최신 snapshot을 유지하고 grip transition만 비손실 control
  pipe로 분리
- status에 `rearm_required`를 추가하고 WebXR UI에
  “grip을 완전히 놓았다 다시 누르세요” 안내 표시
- firmware V3.7.8/SDK V2.0.8의 20004 state socket과 신형 SDK의
  20005 CNDE 연결을 현장 매뉴얼에서 구분

### Safety

- vendor `Robot.py`에는 자동 fallback patch를 적용하지 않으며
  controller firmware와 일치하는 공식 SDK를 `robot.sdk_path`로 선택
- stale timeout 및 session loss 뒤 자동 ACTIVE 복귀를 허용하지 않음

## [0.2.0rc1] - 2026-08-06

### Added

- WebSocket process와 전용 `RobotWorker` process 분리
- overwrite-only latest-pose mailbox, controller lease와 worker watchdog
- grip 해제 시 WebXR/lease를 유지하는 `SLEEPING` 상태
- `config.yaml` 기반 server, TLS, SDK, timing, motion 설정
- 빠른 `ServoMoveStart/End` 전환에 대한 rate-limited `ERROR` 감시
- unit, process, WebSocket integration과 server smoke test
- 필드 엔지니어용 한글 설치·Quest 3 연결·commissioning 매뉴얼과
  오프라인 dependency wheelhouse를 포함하는 전달용 release bundle

### Changed

- `ServoCart`의 `acc`, `vel`, `filterT`, `gain`을 FAIRINO 공식 문서의
  미개방 파라미터 기본값인 `0`으로 통일
- servo tick을 WebXR frame 도착 주기에서 분리하고 monotonic 8 ms
  deadline을 사용
- 실제 로봇 모드에 `--confirm-hardware` 확인을 추가

### Safety

- pose timeout, grip release, WebXR/session loss, SDK error와 shutdown을
  명시적인 servo stop 경로로 연결
- 실제 로봇 자동 테스트는 포함하지 않으며 stable `0.2.0` 전에
  `RELEASE_CHECKLIST.md`의 수동 commissioning 항목을 완료해야 함

### Reference

- [FAIRINO Cartesian servo movement documentation](https://fairino-doc-en.readthedocs.io/3.6.7/SDKManual/CPPRobotMovement.html)
