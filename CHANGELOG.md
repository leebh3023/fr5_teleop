# Changelog

이 프로젝트는 Semantic Versioning을 따르며, 실제 로봇 검증 전 빌드는
release candidate로 표시한다.

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
