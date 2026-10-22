# 현장 검증 작업 지시서 (v0.3.3)

이 문서는 현장 PC에서 작업하는 Claude에게 주는 지시서다. 작업자(사람)와 함께
아래 순서대로 검증하고, 결과를 보고서로 남긴다. 작업자와는 한국어로 대화한다.

## 0. 배경

- 프로젝트: Meta Quest(WebXR)로 Fairino FR5 로봇을 원격 조작하고 시연을 녹화한다.
  저장소: https://github.com/leebh3023/fr5_teleop
- 배포 방식: 프로그램은 Docker 이미지 `ghcr.io/leebh3023/fr5_teleop:0.3.3`
  (public, 로그인 불필요)로 실행한다. 현장 PC에서는 이미지를 빌드하지 않는다.
  컨테이너는 `compose.yaml`로 실행하며 `config.yaml`, `certs/`, `recordings/`를
  호스트에서 마운트한다. config는 재시작만 하면 반영된다.
- 로봇: FR5, IP `192.168.57.3`(같은 LAN의 `.57.2`는 FR3), SDK 2.2.8(이미지에 포함).
- 직전 현장 검증(v0.3.2, 2026-10-21)은 전 항목 통과했다. 보고서:
  `docs/field_reports/2026-10-21_v0.3.2.md`.

### v0.3.3에서 바뀐 것 (이번 검증 대상)

| 구분 | 내용 |
|---|---|
| 위치 | `motion.position_scale` 900 → **1500** (손 10cm → 로봇 15cm), `max_velocity_mm_s` 220 → **250** |
| 회전 | `orientation.scale` 1.0 → **0.5** (손 20° → 로봇 10°), `orientation.ema_alpha` 0.2 → **0.08** (응답 시간 상수 약 40ms → 약 100ms). 손목이 조금 돌아갈 때 로봇이 불필요하게 회전하던 문제 대응 |
| 지표 | `missed_ticks`를 servo가 도는 중의 누락만 세도록 바꾸고, servo가 꺼진 상태의 대기(그리퍼 `move_gripper`, `servo_start`)는 `idle_overrun_ticks`로 분리. `/monitor`의 `Tick / missed` 칸이 `<tick> / <missed> (idle <N>)`로 표시됨 |
| config | `robot.ip` `192.168.57.3`, `motion.workspace.x` `[-100, 800]`이 저장소 기본값이 됨 |

### 이미 알려진 사항 (문제로 보고하지 않는다)

- 시작 로그 `Fairino connection ready ... versions=['SDK:V2.2.7', 'Robot:V3.9.7']`:
  Fairino SDK가 버전 문자열을 한 단계 전 값으로 고정해 반환한다. 실제 SDK는
  2.2.8이다. 버전 확인 항목은 없다.
- 그리퍼 동작 중 servo가 멈추는 것은 설계다. FR5 컨트롤 박스는 servo(`ServoCart`)가
  도는 중 `MoveGripper`를 거부하므로, trigger는 `IDLE`/`SLEEPING`에서만 받고
  servo를 먼저 멈춘다. 그리퍼 호출이 1~2초 걸려도 `idle_overrun_ticks`만 늘어나면 정상이다.
- 카메라는 D435(`8086:0b07`, IMU 없음)일 수 있다. D435와 D435i 모두 지원한다.
- 호스트에 librealsense udev 규칙(`/usr/lib/udev/rules.d/60-librealsense2-udev-rules.rules`)이
  이미 있고 `/dev/video*`가 `crw-rw-rw-`면 `install_realsense_udev.sh`는 생략한다.

## 1. 반드시 지킬 규칙

1. **로봇이 움직일 수 있는 명령(`--confirm-hardware`)은 매번 작업자에게 먼저
   알리고 동의를 받은 뒤 실행한다.** 작업자가 비상정지 버튼 옆에 있는지 확인한다.
   이번 버전은 scale과 속도가 커졌으므로, 첫 하드웨어 실행 전에 작업자에게
   "손을 조금만 움직여도 로봇이 이전보다 약 1.7배 크게 움직인다"고 알린다.
2. Claude는 로봇과 Quest를 직접 볼 수 없다. 움직임 방향, 체감, 화면 표시는
   작업자에게 구체적으로 물어보고 답을 그대로 기록한다. 추측으로 "통과"라고 적지 않는다.
3. **`teleop/` 아래 코드는 수정하지 않는다.** 문제를 찾으면 증상, 로그, 재현
   방법을 보고서에 적는다. `config.yaml`은 작업자가 요청할 때만 고치고, 바꾼
   값과 이유를 보고서에 적는다.
4. 실행 중 이상 동작(급가속, 반대 방향, 멈추지 않음)이 보이면 즉시 작업자에게
   알리고 `docker stop fr5`로 정지한다. 비상정지는 작업자가 판단한다.
5. 커밋과 push는 작업자가 요청할 때만 한다. 인증서(`certs/*.pem`), `.env`,
   `recordings/`, `datasets/`는 git에 넣지 않는다. 녹화 데이터는 git patch가 아니라
   파일 복사(USB, scp)로 전달한다.

## 2. 실행 방식 (Claude용)

Claude의 셸은 대화형 TTY가 아니므로 컨테이너를 백그라운드로 띄우고 로그로 확인한다.

```bash
cd ~/fr5_teleop
docker compose run -d --name fr5 teleop                      # dry-run
docker compose run -d --name fr5 teleop --confirm-hardware   # 실제 로봇 (규칙 1)
docker logs fr5 2>&1 | tail -50                               # 로그
curl -sk https://127.0.0.1:8443/health/ready | python3 -m json.tool
docker stop fr5 && docker rm fr5                              # 정상 종료 (SIGTERM)
```

- 한 번에 하나의 컨테이너만 띄운다. `docker ps -a`로 남은 `fr5`가 없는지 확인한다.
- `docker stop`은 정상 종료 경로다(worker 종료 → 녹화 마무리).
- config를 바꾼 뒤에는 `docker stop fr5 && docker rm fr5` 후 다시 실행해야 반영된다.

## 3. 검증 절차

각 항목의 결과(통과/실패/보류)와 근거(로그 줄, 작업자 답변)를 기록한다.

### 3-1. v0.3.3으로 갱신

1. 사전 확인: `id -u`가 `1000`, `docker ps`가 권한 오류 없이 동작.
2. 현재 로컬 수정을 보존한다. 지난 검증에서 현장 `config.yaml`에 그리퍼 사용
   (`gripper.enabled: true`), 카메라 사용(`recording.camera.enabled: true`) 등이
   들어 있었다.

   ```bash
   cd ~/fr5_teleop
   git status
   git diff config.yaml > ~/config_field_before_v0.3.3.diff   # 현장 값 기록
   git stash                                                  # 로컬 수정이 있을 때만
   git fetch --tags && git checkout v0.3.3
   ```

3. v0.3.3의 `config.yaml`에 현장 전용 값을 **작업자에게 확인한 뒤** 다시 넣는다.
   `~/config_field_before_v0.3.3.diff`를 보고 아래만 옮긴다.
   - `gripper.enabled: true` (현장에서 그리퍼를 쓰는 경우)
   - `recording.camera.enabled: true` (카메라를 쓰는 경우)
   - 그 밖의 값(IP, workspace, scale 등)은 v0.3.3 값이 우선이다. 현장 값이 달랐다면
     차이를 보고서에 적고 작업자에게 어느 쪽을 쓸지 묻는다.
   - 확인 후 `git stash drop`(stash가 있을 때).

4. 이미지와 인증서:

   ```bash
   mkdir -p recordings
   echo "TELEOP_TAG=0.3.3" > .env
   docker compose pull
   openssl x509 -in certs/cert.pem -noout -ext subjectAltName   # Quest 접속 주소 포함 확인
   ```

   인증서에 Quest 접속 주소가 없으면 `certs/*.pem`을 백업한 뒤 지우고
   `TELEOP_HOST_IP=<주소> bash scripts/make_cert.sh`로 다시 만든다.
   백업은 저장소 밖(예: `~/cert_backup/`)에 둔다.

### 3-2. 이미지와 설정 확인 (로봇 동작 없음)

```bash
docker compose run --rm -T --entrypoint python teleop -c \
  "import teleop; from teleop.control_math import remap_axis_vr_to_robot as f; print(teleop.__version__, f((1,2,3)))"
docker compose run --rm -T --entrypoint python teleop -c \
  "from pathlib import Path; from teleop.config import TeleopConfig; c=TeleopConfig.from_yaml(Path('/app/config.yaml')); o=c.orientation; print(c.robot_ip, c.position_scale, c.max_velocity_mm_s, o.enabled, o.scale, o.ema_alpha, c.gripper.enabled, c.recording.camera.enabled)"
```

기대: 첫 줄 `0.3.3 (3, 1, 2)`. 둘째 줄 `192.168.57.3 1500.0 250.0 True 0.5 0.08`
다음에 현장 값(그리퍼, 카메라 사용 여부).

### 3-3. dry-run (로봇 동작 없음)

1. `docker compose run -d --name fr5 teleop`
2. 로그에 `starting teleop mode=DRY-RUN url=https://0.0.0.0:8443`, 약 20초 뒤 `(healthy)`.
3. `/health/ready`의 `counters`에 `idle_overrun_ticks` 키가 있는지.
4. 작업자 확인: Quest 접속 정상, `/monitor`의 `Tick / missed` 칸이
   `<숫자> / <숫자> (idle <숫자>)` 형식인지.
5. `docker stop fr5 && docker rm fr5`. 로그에 `graceful=True`.

### 3-4. 실제 로봇: 이번 변경 (규칙 1 적용)

`docker compose run -d --name fr5 teleop --confirm-hardware` 후 아래를 확인한다.
체감 항목은 작업자의 말을 그대로 적는다.

| # | 항목 | 확인 방법 | 기대 / 기록할 것 |
|---|---|---|---|
| N1 | 연결 | `docker logs fr5` | `Fairino connection ready: ip=192.168.57.3`, 오류 없음 |
| N2 | 위치 scale | 작업자가 grip을 잡고 평소처럼 목표 지점까지 이동 | 이전보다 손을 적게 움직여도 되는지, 너무 민감하지 않은지. 축 방향(앞/뒤, 좌/우, 위/아래)이 그대로인지 |
| N3 | 속도 | 빠르게 손을 움직여 봄 | 튐이나 끊김이 없는지, 로봇이 손을 너무 늦게 따라오는지. `/health/ready`의 `missed_ticks` 기록 |
| N4 | 회전 둔감화 | (a) 손목을 의식하지 않고 위치만 이동 (b) 일부러 roll, pitch, yaw를 한 축씩 크게 회전 | (a) 로봇이 불필요하게 회전하지 않는지 (b) 의도한 회전은 같은 방향으로 따라오는지, 반응이 너무 느리거나 작지 않은지 |
| N5 | 그리퍼와 새 지표 | 그리퍼를 쓰는 경우: grip을 놓고 trigger로 여닫기 3회 | `/health/ready`에서 그리퍼 동작 전후 `idle_overrun_ticks`는 늘고 `missed_ticks`는 거의 그대로인지. 두 값의 전후 숫자 기록 |
| N6 | dead-man | grip을 놓음 | 즉시 정지, `SLEEPING` |
| N7 | 종료 | grip을 놓은 상태에서 `docker stop fr5` | `graceful=True`, `robot close failed` 없음, 종료 코드 0 (`docker inspect -f '{{.State.ExitCode}}' fr5`) |

N2~N4에서 작업자가 값을 바꿔 보고 싶어 하면 아래 범위에서 `config.yaml`을 고치고
재시작한다. 시도한 값과 체감을 모두 기록한다.

| 키 | 지금 | 조정 방향 |
|---|---|---|
| `motion.position_scale` | 1500 | 민감하면 1200~1300, 아직 많이 움직여야 하면 1800 |
| `motion.max_velocity_mm_s` | 250 | 250 초과는 저장소 안전 테스트 상한을 넘으므로, 필요하면 원하는 값만 보고서에 적는다 |
| `orientation.scale` | 0.5 | 아직 민감하면 0.3~0.4, 회전이 너무 작으면 0.6~0.7 |
| `orientation.ema_alpha` | 0.08 | 회전이 늦어 답답하면 0.1~0.12, 아직 떨리면 0.05 |

`orientation.scale`이 0.5면 회전 상한(`max_deviation_deg: 20`)에 닿으려면 손을 40°
돌려야 한다. 손목 미세 회전을 완전히 없애려면 데드밴드 기능(코드 추가)이 필요하므로,
N4 (a)가 여전히 불만족이면 그 사실을 보고서에 적는다.

### 3-5. 녹화 회귀 확인 (카메라 포함)

카메라를 쓰는 config로 하드웨어 모드에서:

1. 작업자가 `/monitor`에서 task를 입력하고 녹화 → 조작 → 성공 저장 1회.
2. 녹화 중 `docker stop fr5` 1회 → `interrupted`로 저장되는지.
3. 각 에피소드 확인:

   ```bash
   ls -ln recordings/
   python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m['task'], m['outcome'], m['complete'], m['duration_s'], m['session']['teleop_version'], {k:(v['frames'], v['dropped_frames'], v['error']) for k,v in m['cameras'].items()})" recordings/episode_XXXXXX/meta.json
   ```

   기대: `.tmp`/`.incomplete` 아님, 소유자 UID 1000, `teleop_version` `0.3.3`,
   카메라 `frames` > 0, `error` `None`.

task 문자열은 학습 때 지시문으로 쓰이므로 `pick`보다 `pick up the blue cube`처럼
구체적으로 입력해 달라고 작업자에게 안내한다.

## 4. 문제 해결

| 증상 | 확인 순서 |
|---|---|
| `docker` 권한 오류 | `getent group docker`, 재로그인, 새 터미널 |
| 로봇 연결 실패 | `ping 192.168.57.3`, `config.yaml`의 `robot.ip`, 로봇 컨트롤러 모드. 로그 오류 원문 기록 |
| Quest 접속 불가/인증서 경고 반복 | 인증서 SAN의 IP가 Quest 접속 주소와 같은지 |
| 컨테이너 시작 직후 종료 | `docker logs fr5`의 마지막 오류(config 키/타입 오류는 시작 단계에서 거부됨) |
| 카메라 인식 안 됨 | `lsusb \| grep -i realsense`, `lsusb -t`에서 `5000M`인지. USB 2(`480M`)거나 커널 로그에 `Cannot enable. Maybe the USB cable is bad?`가 반복되면 짧은 USB 3 케이블로 교체(지난 검증에서 케이블이 원인) |
| 카메라 `Permission denied` | `/dev/video*` 권한, 컨테이너 안 `id`에 `video(44)` |

## 5. 보고서와 전달

1. `docs/field_reports/<YYYY-MM-DD>_v0.3.3.md`에 작성한다.
   - 환경: OS, 커널, Docker 버전, 이미지 digest
     (`docker image inspect ghcr.io/leebh3023/fr5_teleop:0.3.3 -f '{{index .RepoDigests 0}}'`)
   - 3-1 ~ 3-5 항목별 표: 항목, 결과, 근거
   - N2~N4 체감 기록과 시도한 config 값, 작업자가 최종으로 고른 값
   - 실패/보류 항목: 증상, 재현 방법, 로그 원문
   - 현장 `config.yaml`과 v0.3.3의 차이(`git diff config.yaml`)
   - 개발 PC 후속 작업 제안
2. 작업자에게 요약을 보여 주고, 원하면 보고서만 커밋해 patch로 만든다.

   ```bash
   git checkout -b field-report-v0.3.3
   git add docs/field_reports/<파일>.md
   git commit -m "docs: field verification report for v0.3.3 (<날짜>)"
   git format-patch -1 -o ~/
   ```

3. 녹화 데이터(`recordings/episode_*`)는 커밋하지 않는다. 필요하면
   `tar czf ~/field_episodes_<날짜>.tgz -C recordings episode_XXXXXX ...`로 묶어 전달한다.
