# 현장 검증 작업 지시서 (v0.3.2)

이 문서는 현장 PC에서 작업하는 Claude에게 주는 지시서다. 작업자(사람)와 함께
아래 순서대로 검증하고, 결과를 보고서로 남긴다. 작업자와는 한국어로 대화한다.

## 0. 배경

- 프로젝트: Meta Quest(WebXR)로 Fairino FR5 로봇을 원격 조작하고 시연을 녹화한다.
  저장소: https://github.com/leebh3023/fr5_teleop
- 배포 방식: 프로그램은 Docker 이미지 `ghcr.io/leebh3023/fr5_teleop:0.3.2`
  (public, 로그인 불필요)로 실행한다. 현장 PC에서는 이미지를 빌드하지 않는다.
  컨테이너는 `compose.yaml`로 실행하며 `config.yaml`, `certs/`, `recordings/`를
  호스트에서 마운트한다.
- 로봇: FR5, 펌웨어 v3.9.8, IP `192.168.57.2`, SDK 2.2.8(이미지에 포함).
- v0.3.1에 반영된 현장 수정(이번에 재확인 대상):
  - VR→로봇 축 매핑 `remap_axis_vr_to_robot`: `(x, y, z) → (z, x, y)`
    (로봇 x = VR z, 로봇 y = VR x, 로봇 z = VR y)
  - `config.yaml`: IP `192.168.57.2`, `max_velocity_mm_s: 220`, `max_step_mm: 5`,
    `gripper.enabled: false`, `orientation.enabled: true`
- v0.3.2에서 새로 추가된 것(처음 검증):
  - 손목 카메라 RealSense D435i를 컨테이너에서 사용(`compose.yaml`에 `/dev`,
    `/run/udev` 마운트와 장치 권한 추가, 호스트 udev 규칙 설치 스크립트)
- 사용법 전체는 저장소의 `README.md` "Docker" 섹션에 있다. 이 문서와 다르면
  이 문서를 따르고 차이를 보고서에 적는다.

## 1. 반드시 지킬 규칙

1. **로봇이 움직일 수 있는 명령(`--confirm-hardware`)은 매번 작업자에게 먼저
   알리고 동의를 받은 뒤 실행한다.** 작업자가 비상정지 버튼 옆에 있는지 확인한다.
2. Claude는 로봇과 Quest를 직접 볼 수 없다. 움직임 방향, 화면 표시 같은 물리적
   확인은 작업자에게 구체적으로 요청하고, 작업자의 답을 그대로 기록한다.
   추측으로 "통과"라고 적지 않는다.
3. **`teleop/` 아래 코드는 수정하지 않는다.** 문제를 찾으면 증상, 로그, 재현
   방법을 보고서에 적는다. `config.yaml`은 작업자가 요청할 때만 고치고, 바꾼
   값을 보고서에 적는다.
4. 실행 중 이상 동작(급가속, 반대 방향, 멈추지 않음)이 보이면 즉시 작업자에게
   알리고 `docker stop fr5`로 정지한다. 비상정지는 작업자가 판단한다.
5. 커밋과 push는 작업자가 요청할 때만 한다. 인증서(`certs/*.pem`), `.env`,
   `recordings/`는 커밋하지 않는다(이미 `.gitignore`에 있음).

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

- 한 번에 하나의 컨테이너만 띄운다(포트 8443, 로봇 연결 공유 불가).
  `docker ps -a`로 남은 `fr5`가 없는지 확인하고 시작한다.
- `docker stop`은 정상 종료 경로다(worker 종료 → 녹화 마무리). 15초 안에 끝나지
  않을 때만 Docker가 강제 종료한다.

## 3. 검증 절차

각 항목의 결과(통과/실패/보류)와 근거(로그 줄, 작업자 답변)를 기록한다.

### 3-1. 설치 또는 갱신

1. 사전 확인: `id -u`가 `1000`, `docker ps`가 권한 오류 없이 동작.
   `permission denied`면 작업자에게 docker 그룹 추가 후 재로그인을 요청한다.
2. 저장소 상태 확인.
   - `~/fr5_teleop`이 없으면 README "방법 A"대로 clone한다.
   - 있으면 `git status`와 `git diff`를 확인한다. 현장 수정(IP, 속도, 축 매핑)은
     이미 v0.3.2에 들어 있다. 로컬 수정이 그 외에도 있으면 내용을 보고서에
     옮겨 적은 뒤 진행한다.

   ```bash
   cd ~/fr5_teleop
   git stash                 # 로컬 수정이 있을 때만
   git fetch --tags && git checkout v0.3.2
   git stash show -p         # 남은 수정 확인 후 기록 (v0.3.2와 중복이면 git stash drop)
   ```

3. 녹화 폴더, 인증서, 이미지.

   ```bash
   mkdir -p recordings
   ip -4 -br addr                                    # Quest가 접속할 주소를 작업자와 확인
   TELEOP_HOST_IP=<그 주소> bash scripts/make_cert.sh   # 이미 있으면 IP 경고만 확인
   echo "TELEOP_TAG=0.3.2" > .env
   docker compose pull
   ```

   - 로봇 LAN(`192.168.57.x`) 주소로 인증서를 만들지 않는다.
   - `config.yaml`의 `robot.ip`가 `192.168.57.2`인지 확인한다. `ping -c 3 192.168.57.2`.

### 3-2. 이미지 확인 (로봇 동작 없음)

```bash
docker compose run --rm -T --entrypoint python teleop -c \
  "import teleop; from teleop.control_math import remap_axis_vr_to_robot as f; print(teleop.__version__, f((1,2,3)))"
```

기대값: `0.3.2 (3, 1, 2)`

### 3-3. dry-run (로봇 동작 없음)

1. `docker compose run -d --name fr5 teleop`
2. 로그에 `starting teleop mode=DRY-RUN url=https://0.0.0.0:8443`.
3. 약 20초 뒤 `docker ps`에 `(healthy)`.
4. 작업자에게 확인 요청: Quest 브라우저 `https://<PC주소>:8443` 접속(인증서 경고
   승인), PC 브라우저 `https://<PC주소>:8443/monitor` 접속.
5. `docker stop fr5 && docker rm fr5`. 로그에
   `SIGTERM received: shutting down (further signals ignored)`와
   `robot worker shutdown: {... graceful=True ...}`.

### 3-4. 실제 로봇 (규칙 1 적용)

`docker compose run -d --name fr5 teleop --confirm-hardware` 후:

| # | 항목 | 확인 방법 | 기대 |
|---|---|---|---|
| H1 | 연결/SDK | `docker logs fr5` | `Fairino connection ready: ip=192.168.57.2 versions=...`에 SDK 2.2.8, 오류 없음 |
| H2 | 상태 | `/health/ready` | `ready: true`, `state: IDLE`, `fault: null` |
| H3 | 위치 축 | 작업자가 grip을 잡고 손을 한 축씩 천천히 이동 (앞/뒤, 좌/우, 위/아래) | 로봇이 같은 방향으로 이동. 축마다 결과 기록 |
| H4 | 회전 축 | roll, pitch, yaw를 한 축씩 천천히 | 같은 방향으로 회전. 축마다 결과 기록 |
| H5 | dead-man | grip을 놓음 | 즉시 정지, 상태 `SLEEPING` 또는 `IDLE` |
| H6 | 속도 | 보통 속도로 움직임 | 튀거나 끊기지 않음. `/health/ready`의 `missed_ticks`(servo 중 누락), `idle_overrun_ticks`(servo가 꺼진 상태의 그리퍼·servo 시작 대기, 정상), `jitter_ms`, `servo_transition` 관련 ERROR 로그 유무 기록 |
| H7 | 종료 | grip을 놓은 상태에서 `docker stop fr5` | `state=STOPPING reason=shutdown` → `state=SHUTDOWN reason=worker_stopped`, `robot worker shutdown ... graceful=True`, `robot close failed` 없음. 종료 코드(`docker inspect -f '{{.State.ExitCode}}' fr5`) 0 |

H7 후 `docker rm fr5`. 그리퍼는 config에서 꺼져 있으므로 검증하지 않는다.

작업자에게 대화형 Ctrl+C 종료도 한 번 요청한다: 작업자 터미널에서
`docker compose run --rm teleop --confirm-hardware` 실행 → Ctrl+C(여러 번 눌러도
됨) → 정상 종료 로그 확인.

### 3-5. 녹화, 카메라 없이 (`camera.enabled: false` 그대로)

1. 3-4처럼 하드웨어 모드로 실행한다.
2. 작업자가 `/monitor`에서 task를 입력하고 녹화 시작 → 몇 초 조작 → 성공 저장.
3. 확인:

   ```bash
   ls -ln recordings/
   python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m['task'], m['outcome'], m['complete'], m['duration_s'])" recordings/episode_XXXXXX/meta.json
   ```

   기대: 새 `episode_XXXXXX/`(`.tmp`, `.incomplete` 아님), 소유자 UID 1000,
   `meta.json`, `robot_right.bin`, `events.jsonl` 존재, outcome `success`.

### 3-6. 손목 카메라 RealSense D435i (처음 검증)

1. 호스트 udev 규칙 설치(sudo 필요, 작업자 동의 후):

   ```bash
   sudo bash scripts/install_realsense_udev.sh
   ```

   작업자에게 카메라를 뺐다가 **USB 3.x 포트**에 다시 꽂아 달라고 요청한다.

2. 호스트 확인:

   ```bash
   lsusb -d 8086:0b3a          # D435i 존재
   lsusb -t | grep -i -B2 uvc  # 5000M(USB3)인지 기록
   ls -l /dev/video*           # RealSense 노드가 crw-rw-rw-
   ```

3. 컨테이너에서 장치 확인:

   ```bash
   docker compose run --rm -T --entrypoint python teleop -c \
     "import pyrealsense2 as rs; print([d.get_info(rs.camera_info.name) + ' ' + d.get_info(rs.camera_info.serial_number) for d in rs.context().query_devices()])"
   ```

   기대: `['Intel RealSense D435I <serial>']`. 빈 목록이면 4절 문제 해결.

4. 작업자 동의 후 `config.yaml`에서 `recording.camera.enabled: true`로 바꾼다
   (다른 카메라 값은 그대로: 640x480, 30 fps, depth false).
5. dry-run으로 먼저 실행(`docker compose run -d --name fr5 teleop`):
   - 로그에 `camera process started`, `camera streaming name=wrist info=...`.
   - 작업자 확인: `/monitor`의 Camera가 `wrist <fps> fps (<serial>)` 형태의 초록 표시이고 fps가 약 30.
   - 작업자가 녹화 10초 → 성공 저장. 에피소드 폴더에 `camera_wrist.mp4`,
     `camera_wrist_frames.bin`. 프레임 수 확인:

     ```bash
     docker compose run --rm -T --entrypoint python teleop -c \
       "import av; c=av.open('/app/recordings/episode_XXXXXX/camera_wrist.mp4'); s=c.streams.video[0]; print(s.frames, s.average_rate, s.width, s.height)"
     ```

     기대: 약 300프레임(10초 × 30 fps), `640 480`.
   - `meta.json`의 `cameras` 항목 전체를 기록한다(`frames`, `dropped_frames`, 오류 여부 포함).
     `dropped_frames`가 0이 아니면 실패가 아니라 관찰 사항으로 적는다.
6. 재연결: 실행 중 작업자가 카메라를 뽑았다가 다시 꽂는다.
   - 로그에 재시도(`retrying in 2 s`, `restarting stream`) 후 다시
     `camera streaming`. `/monitor`가 NOT STREAMING → 초록으로 복귀.
   - 복귀 후 녹화 1회가 정상 저장되는지.
7. 하드웨어 모드에서 녹화 1회(카메라 + 로봇): 3-4 H5 상태에서 시작, 짧게 조작,
   저장. 녹화 중 `missed_ticks`가 늘지 않는지 기록(카메라 인코딩이 servo
   루프에 영향을 주는지).
8. 녹화 중 종료: 녹화가 진행 중일 때 `docker stop fr5` → 에피소드가
   `interrupted`로 저장되고 `camera_wrist.mp4`가 열리는지(5번 명령).

## 4. 문제 해결

| 증상 | 확인 순서 |
|---|---|
| `docker` 권한 오류 | `getent group docker`, 재로그인, 새 터미널 |
| 로봇 연결 실패 | `ping 192.168.57.2`, `config.yaml`의 `robot.ip`, 로봇 컨트롤러 모드. 로그의 오류 원문 기록 |
| Quest 접속 불가/인증서 경고 반복 | `openssl x509 -in certs/cert.pem -noout -ext subjectAltName`의 IP가 Quest 접속 주소와 같은지 |
| 컨테이너 시작 직후 종료 | `docker logs fr5`의 마지막 오류(config 키/타입 오류는 시작 단계에서 거부됨) |
| 카메라 빈 목록 | 호스트 `lsusb -d 8086:0b3a` → 없으면 케이블/포트. 있으면 `ls -l /dev/video*` 권한, `/etc/udev/rules.d/99-realsense-libusb.rules` 존재, `lsmod \| grep uvcvideo`, 재연결 |
| 카메라 `Permission denied` | udev 규칙 설치 후 재연결했는지. 컨테이너 안 `id`에 `video(44)`가 있는지 |
| 위 확인 후에도 카메라 실패 | **진단용으로만** 권한 문제를 배제한다: 아래 override로 1회 실행해 결과를 기록하고, 이 설정을 계속 쓰지는 않는다 |

진단용 override(`/tmp/diag.yaml`):

```yaml
services:
  teleop:
    privileged: true
```

```bash
docker compose -f compose.yaml -f /tmp/diag.yaml run --rm -T --entrypoint python teleop -c \
  "import pyrealsense2 as rs; print(len(rs.context().query_devices()))"
```

privileged에서만 보이면 device_cgroup_rules에 빠진 장치가 있다는 뜻이다. 그때
`ls -l /dev/video* /dev/media* /dev/hidraw* 2>/dev/null`와
`grep -E 'video|media|hidraw|usb' /proc/devices` 출력을 보고서에 붙인다.

## 5. 보고서

`docs/field_reports/<YYYY-MM-DD>_v0.3.2.md`에 작성한다.

- 환경: 현장 PC OS(`lsb_release -ds`), 커널(`uname -r`), Docker 버전, 이미지 digest
  (`docker image inspect ghcr.io/leebh3023/fr5_teleop:0.3.2 -f '{{index .RepoDigests 0}}'`),
  카메라 serial/firmware, USB 속도
- 3-1 ~ 3-6 항목별 표: 항목, 결과(통과/실패/보류), 근거(로그 줄 또는 작업자 답변)
- 실패/보류 항목: 증상, 재현 방법, 관련 로그 원문(앞뒤 20줄)
- 현장에서 바꾼 `config.yaml` 값과 이유(`git diff config.yaml`)
- 개발 PC에서 해야 할 후속 작업 제안(코드 수정이 필요해 보이는 부분)

보고서 작성 후 작업자에게 요약을 보여 주고, 커밋/push 여부를 묻는다.
