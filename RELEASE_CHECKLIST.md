# Release checklist: 0.2.0rc2

Production deployment uses the source distribution with native venv and
systemd. The wheel contains the Python service package only; configuration,
WebXR static files and the FAIRINO SDK are external runtime resources.

## Automated release gate

- [x] Python version is `>=3.10,<3.11`
- [x] runtime, development and release dependencies are pinned
- [x] package version has a single source in `teleop.__version__`
- [x] default configuration is dry-run
- [x] hardware mode requires `--confirm-hardware`
- [x] TLS private keys and generated artifacts are Git-ignored
- [x] FAIRINO `ServoCart` arguments match the documented defaults
- [x] Ubuntu 22.04 WSL full test suite: 38 passed
- [x] dry-run server smoke test
- [x] source distribution and wheel build
- [x] `twine check` for built artifacts
- [x] release archive content and secret-exclusion inspection
- [x] Korean field installation and commissioning manual
- [x] offline Ubuntu 22.04 / Python 3.10 runtime wheelhouse
- [x] field bundle contents and recursive SHA-256 verification
- [x] rapid grip release/press survives latest-pose overwrite
- [x] stale timeout status exposes explicit rearm requirement
- [x] SDK V2.0.8 legacy connection flag and ServoCart signature adapter tests

## Stable 0.2.0 blockers

- [ ] Native Ubuntu 22.04 x86-64 10-minute fake timing/soak measurement
- [ ] Native systemd install, boot start and stop-timeout validation
- [ ] Quest 3 TLS trust, WebXR controller mapping and reconnect validation
- [ ] FR5 SDK/controller firmware compatibility confirmation
- [ ] Low-scale FR5 `ServoMoveStart -> ServoCart -> ServoMoveEnd` manual test
- [ ] Grip release, stale pose, WebXR end and network disconnect stop-time test
- [ ] Controller-side communication-disconnect stop configuration verification
- [ ] Physical E-stop and supervised commissioning sign-off

WSL and fake tests do not authorize actual robot operation. Stable release approval
requires recorded native timing and supervised hardware results.

## Unreleased next-RC validation

- [x] grip과 trigger transition의 lossless control-pipe 회귀 시험
- [x] `GRIPPER_ACTION`의 servo end, non-blocking command, completion,
  latched fault와 완료 후 re-clutch 시험
- [x] legacy/extended/ambiguous SDK signature adapter 시험
- [x] elapsed-time velocity limiter와 absolute step cap unit 시험
- [x] SDK operation별 p50/p95/p99/max 로그 집계 시험
- [x] Quest client telemetry validation/ack와 read-only operator monitor
- [x] parent-owned SIGINT의 graceful worker shutdown
- [x] Ubuntu 22.04 WSL 전체 suite: 58 passed
- [x] Ubuntu 22.04 WSL dry-run server smoke
- [ ] 실제 SDK V2.0.8에서 gripper signature와 completion 반환 기록
- [ ] 실제 FR5에서 trigger 20회 command/complete count 대조
- [ ] 그리퍼 완료 후 grip 유지 시 servo가 자동 재개되지 않음을 확인
- [ ] 실제 로봇 10분 timing/pose-gap/SDK-latency 로그 검토
- [ ] 새 release candidate 버전, bundle, checksum과 현장 매뉴얼 생성
