# VR Teleop Bridge 0.2.0rc2 현장 설치·운용 매뉴얼

대상 장비는 Meta Quest 3, FAIRINO FR5, Ubuntu 22.04 x86-64 PC다.
이 문서는 릴리즈 후보 `0.2.0rc2`의 설치, dry-run 확인, 실제 로봇
commissioning과 종료 절차를 설명한다.

> 저장소의 `Unreleased` 변경에는 rc2 현장 피드백에 따른 trigger/gripper
> 상태 머신과 추가 진단이 포함되어 있다. 새 release bundle이 만들어지기
> 전에는 이 문서의 rc2 파일명과 새 소스 동작을 혼합해 배포하지 않는다.

> 경고: 이 프로그램의 grip과 pose timeout은 운용 보조 기능이며
> 안전 인증 정지 장치가 아니다. 물리적 비상 정지 장치, FAIRINO
> 제어기의 안전 설정과 현장 위험성 평가를 대체하지 않는다. 최초 실제
> 로봇 시험은 안전 책임자와 감시자가 입회한 상태에서 저속으로 수행한다.

## 1. 릴리즈 상태와 알려진 제한

- 이 빌드는 stable이 아닌 release candidate다.
- Ubuntu 22.04 WSL의 fake robot 자동 테스트는 통과했지만 실제 FR5,
  Quest 3, native systemd 조합은 현장에서 확인해야 한다.
- 컨트롤러 위치만 로봇 TCP 위치에 반영하며 손목 회전은 사용하지 않는다.
  Unreleased 소스의 trigger/gripper 기능은 기본 비활성이며 별도
  commissioning 뒤에만 활성화한다.
- 한 번에 WebXR client 한 대만 제어권을 갖는다.
- vendor source의 보고 버전과 README 기록은 서로 다를 수 있다. 현장
  controller firmware와 정확히 짝이 맞는 공식 SDK를 별도 준비해
  `robot.sdk_path`로 지정한다. 확인된 조합은 Robot V3.7.8과 Python
  SDK V2.0.8이며 이 조합은 legacy state socket 20004를 사용한다.
- 프로그램의 실제 로봇 초기화는 `ResetAllError()`, 자동 모드 전환,
  drag mode 해제, robot enable을 순서대로 수행한다. 실제 로봇 모드로
  서버를 시작하는 순간 이 초기화가 실행된다.

## 2. 전달 파일

릴리즈 디렉터리에는 다음 파일이 있어야 한다.

| 파일 | 용도 |
|---|---|
| `README_FIRST_KO.md` | 이 현장 매뉴얼 |
| `vr_teleop-0.2.0rc2.tar.gz` | Web UI, 설정, adapter와 테스트를 포함한 소스 배포본 |
| `vr_teleop-0.2.0rc2-py3-none-any.whl` | Python service 패키지 |
| `wheelhouse/` | Ubuntu 22.04 / Python 3.10 x86-64용 runtime dependency |
| `requirements-lock.txt` | wheelhouse와 일치하는 전체 runtime dependency 버전 |
| `SHA256SUMS` | 전달 파일 무결성 확인 |
| `CHANGELOG.md` | 변경 이력 |
| `RELEASE_CHECKLIST.md` | stable 전 검증 항목 |

## 3. 작업 전 안전 체크

다음 항목이 하나라도 충족되지 않으면 실제 로봇 모드를 실행하지 않는다.

- [ ] FR5 설치, tool/TCP 설정과 payload 설정을 확인했다.
- [ ] 로봇 작업 영역에 사람과 장애물이 없다.
- [ ] 감시자가 로봇 전체를 볼 수 있고 E-stop에 즉시 접근할 수 있다.
- [ ] E-stop을 실제로 눌러 정지 상태가 되는지 확인했다.
- [ ] 제어기에서 통신 단절 시 정지 설정을 확인했다.
- [ ] controller firmware와 선택한 Python SDK의 정확한 조합을 확인했다.
- [ ] 최초 workspace를 현재 TCP 주변의 작은 영역으로 계산했다.
- [ ] Quest와 PC의 배터리·전원·네트워크가 안정적이다.
- [ ] dry-run 절차를 먼저 완료했다.

## 4. 권장 네트워크 구성

PC에 로봇용 유선 NIC와 Quest용 LAN/Wi-Fi NIC를 분리하는 구성을
권장한다.

```text
FR5 controller 192.168.58.2
        |
        | 전용 Ethernet / 192.168.58.0/24
        |
Ubuntu PC robot NIC 192.168.58.10
Ubuntu PC Quest NIC 192.168.1.50
        |
        | 동일한 신뢰 LAN 또는 전용 AP
        |
Quest 3
```

`192.168.58.2`는 FAIRINO의 공장 기본 controller IP다. 현장 주소가
다르면 teaching pendant에서 확인한 실제 주소를 사용한다. Quest가
브라우저로 여는 주소는 로봇 IP가 아니라 Quest에서 접근 가능한 Ubuntu
PC의 주소다.

### 4.1 Ubuntu 주소 확인

```bash
ip -br address
ip route
nmcli connection show
```

로봇 전용 NIC에 주소를 지정하는 예시는 다음과 같다. 연결 이름은 현장
값으로 바꾼다.

```bash
sudo nmcli connection modify "ROBOT-NIC" \
  ipv4.method manual ipv4.addresses 192.168.58.10/24 \
  ipv4.gateway "" ipv4.dns ""
sudo nmcli connection up "ROBOT-NIC"
```

기본 gateway는 Quest/사내망 쪽 NIC 한 곳에만 둔다. 같은
`192.168.58.0/24` 주소를 두 NIC에 동시에 지정하지 않는다.

### 4.2 FR5 통신 확인

```bash
ping -c 3 192.168.58.2
nc -vz -w 2 192.168.58.2 20003
nc -vz -w 2 192.168.58.2 20004
nc -vz -w 2 192.168.58.2 20005
```

XML-RPC `20003/TCP`는 공통이다. Robot V3.7.8/SDK V2.0.8은 realtime
state `20004/TCP`를 사용하고, 신형 SDK는 CNDE `20005/TCP`를 사용할
수 있다. 현장 firmware와 SDK 조합에 해당하는 state port만 열려 있어야
하며, 20005가 거부된다는 이유로 다른 버전 SDK의 vendor 소스를
자동 수정하지 않는다.

### 4.3 Quest 경로 확인

Quest와 같은 네트워크의 다른 장치에서 다음 주소가 Ubuntu PC를
가리키는지 확인한다.

```text
https://<QUEST에서 접근 가능한 Ubuntu PC 주소>:8443/
```

Ubuntu 방화벽을 사용하는 경우 Quest 네트워크에서 오는 `8443/TCP`만
허용한다. 인터넷에 직접 노출하지 않는다.

## 5. 파일 검증과 Ubuntu 설치

### 5.1 무결성 확인

릴리즈 디렉터리에서 실행한다.

```bash
sha256sum -c SHA256SUMS
```

모든 항목이 `OK`여야 한다. 실패한 파일은 사용하지 않고 릴리즈
담당자에게 다시 받는다.

### 5.2 OS 준비

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssl curl netcat-openbsd
python3 --version
uname -m
```

Python은 `3.10.x`, architecture는 `x86_64`여야 한다.

### 5.3 전용 계정과 프로그램 설치

아래 명령은 릴리즈 디렉터리에서 실행한다.

```bash
tar -xzf vr_teleop-0.2.0rc2.tar.gz

sudo useradd --system --home /opt/vr-teleop \
  --shell /usr/sbin/nologin vr-teleop 2>/dev/null || true
sudo install -d -o vr-teleop -g vr-teleop /opt/vr-teleop
sudo cp -a vr_teleop-0.2.0rc2/. /opt/vr-teleop/
sudo cp -a wheelhouse /opt/vr-teleop/
sudo cp vr_teleop-0.2.0rc2-py3-none-any.whl /opt/vr-teleop/
sudo chown -R vr-teleop:vr-teleop /opt/vr-teleop

sudo -u vr-teleop python3 -m venv /opt/vr-teleop/.venv-linux
sudo -u vr-teleop /opt/vr-teleop/.venv-linux/bin/python -m pip install \
  --no-index --find-links /opt/vr-teleop/wheelhouse \
  -r /opt/vr-teleop/requirements-lock.txt
sudo -u vr-teleop /opt/vr-teleop/.venv-linux/bin/python -m pip install \
  --no-index --no-deps \
  /opt/vr-teleop/vr_teleop-0.2.0rc2-py3-none-any.whl
```

wheelhouse가 없는 개발용 배포본에서는 인터넷 연결 후
`python -m pip install -r requirements.txt`를 사용한다.

## 6. firmware와 일치하는 FAIRINO SDK 설치

릴리즈에는 특정 firmware용 SDK를 넣지 않는다. FAIRINO에서 받은 공식
SDK 중 controller firmware와 정확히 일치하는 버전을 별도 디렉터리에
배치한다. Robot V3.7.8 현장 확인 조합의 예:

```bash
sudo install -d -o root -g vr-teleop /opt/fairino-sdk-2.0.8_robot3.7.8
sudo cp -a /media/field/SDK-2.0.8/linux \
  /opt/fairino-sdk-2.0.8_robot3.7.8/
sudo find /opt/fairino-sdk-2.0.8_robot3.7.8 -type d -exec chmod 0755 {} \;
sudo find /opt/fairino-sdk-2.0.8_robot3.7.8 -type f -exec chmod 0644 {} \;
test -f \
  /opt/fairino-sdk-2.0.8_robot3.7.8/linux/fairino/Robot.py
```

SDK example 파일은 실행하지 않는다. `Robot.py`를 다른 SDK의 통신
방식으로 patch하지 않고 adapter의 `sdk_path`만 바꾼다.

## 7. TLS 인증서

WebXR은 secure context가 필요하므로 Quest용 접속은 HTTPS/WSS를
사용한다. 인증서의 SAN에는 Quest가 접속할 Ubuntu PC의 IP 또는 DNS
이름이 반드시 포함되어야 하며 Quest Browser가 발급자를 신뢰해야 한다.

운영 환경에서는 조직의 내부 CA 또는 신뢰 가능한 DNS 인증서를
권장한다. 단순히 인증서 경고 화면을 통과한 상태를 운영 준비 완료로
판정하지 않는다. Quest에서 CA를 신뢰시키는 절차는 현장 MDM/보안
정책을 따른다.

인증서와 키를 설치한다.

```bash
sudo install -d -m 0750 -o root -g vr-teleop /etc/vr-teleop/tls
sudo install -m 0644 server-cert.pem /etc/vr-teleop/tls/cert.pem
sudo install -m 0640 -o root -g vr-teleop server-key.pem \
  /etc/vr-teleop/tls/key.pem

openssl x509 -in /etc/vr-teleop/tls/cert.pem -noout \
  -subject -issuer -dates -ext subjectAltName
```

dry-run용 자체 서명 인증서는 다음처럼 만들 수 있다. `QUEST_HOST_IP`에는
Quest가 접근할 PC 주소를 넣는다.

```bash
QUEST_HOST_IP=192.168.1.50
sudo openssl req -x509 -newkey rsa:2048 \
  -keyout /etc/vr-teleop/tls/key.pem \
  -out /etc/vr-teleop/tls/cert.pem \
  -days 30 -nodes \
  -subj "/CN=${QUEST_HOST_IP}" \
  -addext "subjectAltName=IP:${QUEST_HOST_IP},IP:127.0.0.1"
sudo chown root:vr-teleop /etc/vr-teleop/tls/key.pem
sudo chmod 0640 /etc/vr-teleop/tls/key.pem
```

이 자체 서명 인증서는 현장 CA 신뢰 설정 없이 운영용으로 사용하지
않는다.

## 8. 설정 파일

```bash
sudo install -d -m 0750 -o root -g vr-teleop /etc/vr-teleop
sudo install -m 0640 -o root -g vr-teleop \
  /opt/vr-teleop/deploy/config.example.yaml \
  /etc/vr-teleop/config.yaml
sudoedit /etc/vr-teleop/config.yaml
```

처음에는 반드시 `runtime.dry_run: true`를 유지한다.

### 7.1 주요 설정

| YAML 항목 | 의미 | 최초 현장 권장 |
|---|---|---|
| `runtime.dry_run` | fake/실제 로봇 선택 | `true` |
| `server.host` | 수신 interface | `0.0.0.0` |
| `server.port` | Quest HTTPS 포트 | `8443` |
| `server.allowed_origins` | 허용 Web origin | 운영 URL 하나로 제한 |
| `tls.cert_path`, `key_path` | 인증서와 개인 키 | `/etc/vr-teleop/tls/...` |
| `robot.ip` | FR5 controller 주소 | 현장 확인값 |
| `robot.sdk_path` | firmware 일치 Linux SDK | 현장 SDK 절대 경로 |
| `timing.pose_timeout_s` | pose 무수신 정지 시간 | `0.200`에서 검증 시작 |
| `motion.position_scale` | VR 1 m당 로봇 이동 mm | 최초 `100.0` |
| `motion.max_velocity_mm_s` | 실제 경과 시간 기준 TCP 명령 속도 상한 | 최초 `31.25` |
| `motion.max_step_mm` | 8 ms tick당 최대 이동 | 최초 `0.25` |
| `motion.workspace` | base 좌표계 TCP 경계 mm | 현재 TCP 주변으로 축소 |
| `gripper.enabled` | trigger 그리퍼 제어 | 최초 `false` |

최초 실제 로봇용 예시:

```yaml
runtime:
  dry_run: false
  log_level: INFO
  status_hz: 10

server:
  host: 0.0.0.0
  port: 8443
  web_dir: /opt/vr-teleop/web
  max_ws_message_bytes: 4096
  allowed_origins:
    - https://192.168.1.50:8443

tls:
  enabled: true
  cert_path: /etc/vr-teleop/tls/cert.pem
  key_path: /etc/vr-teleop/tls/key.pem

robot:
  ip: 192.168.58.2
  sdk_path: /opt/fairino-sdk-2.0.8_robot3.7.8/linux
  exaxis_default: [0.0, 0.0, 0.0, 0.0]

timing:
  servo_period_s: 0.008
  servo_transition_window_s: 1.0
  servo_transition_limit: 4
  pose_timeout_s: 0.200
  worker_watchdog_s: 0.500
  worker_startup_timeout_s: 10.0
  graceful_shutdown_s: 2.0

motion:
  position_scale: 100.0
  ema_alpha: 0.25
  max_velocity_mm_s: 31.25
  max_step_mm: 0.25
  workspace:
    x: [300.0, 400.0]
    y: [-50.0, 50.0]
    z: [300.0, 400.0]

gripper:
  enabled: false
  index: 1
  activate_on_start: false
  initially_closed: false
  open_position: 0
  closed_position: 100
  velocity: 30
  force: 30
  command_max_time_ms: 3000
  action_timeout_s: 5.0
  poll_period_s: 0.050
```

위 workspace 숫자는 형식 예시일 뿐이다. 현장 TCP를 측정하지 않고
복사해서 사용하지 않는다. `max_velocity_mm_s: 31.25`가 시간 기준
속도를 제한하고 `max_step_mm: 0.25`는 단일 명령의 절대 상한으로
추가 적용된다. 지연 뒤 밀린 이동량을 한 번에 보정하지 않는다.

좌표 매핑은 다음과 같다.

```text
Robot X = -VR Z
Robot Y = -VR X
Robot Z = +VR Y
```

`position_scale: 100`이면 손을 10 cm 움직였을 때 목표 TCP 변화는
10 mm다.

## 9. dry-run 시운전

### 9.1 foreground 실행

```bash
sudo -u vr-teleop /opt/vr-teleop/.venv-linux/bin/python -m teleop \
  --config /etc/vr-teleop/config.yaml --dry-run
```

다른 터미널에서 확인한다.

```bash
curl -k https://127.0.0.1:8443/health/live
curl -k https://127.0.0.1:8443/health/ready
```

응답의 `live`, `ready`가 `true`여야 한다. `Ctrl+C`로 종료하고 worker
shutdown 로그를 확인한다.

### 9.2 Quest 3 연결

1. Quest 3을 Quest용 LAN/AP에 연결한다.
2. Quest Browser에서 `https://<Ubuntu Quest NIC 주소>:8443/`를 연다.
3. 인증서가 신뢰되며 주소 표시줄에 인증서 오류가 없는지 확인한다.
4. 화면의 `VR 세션 시작` 버튼을 누르고 immersive VR 진입을 허용한다.
5. WebSocket `연결됨`, WebXR `활성`을 확인한다.
6. 오른손 controller를 정지한 상태에서 grip을 짧게 누른다.
7. UI가 `추적 중`과 `슬립` 사이에서 정상 전환되는지 확인한다.
8. grip을 놓아도 WebXR은 종료되지 않고 `슬립`이 되어야 한다.
9. grip을 다시 누르면 그 순간의 controller와 TCP 위치를 새 원점으로
   잡는다.

dry-run에서 이 절차가 실패하면 실제 로봇 모드로 전환하지 않는다.

## 10. 실제 FR5 최초 시험

### 10.1 시작 직전

1. 로봇을 충돌 위험이 없는 자세에 둔다.
2. 감시자가 E-stop을 잡는다.
3. Quest controller의 grip이 눌리지 않았는지 확인한다.
4. `/etc/vr-teleop/config.yaml`에서 robot IP, Quest origin, 인증서
   경로, 저속 scale, step과 실제 workspace를 다시 읽어 확인한다.
5. `runtime.dry_run: false`를 설정한다.

### 10.2 foreground hardware 실행

최초 시험은 systemd가 아니라 터미널 foreground에서 한다.

```bash
sudo -u vr-teleop /opt/vr-teleop/.venv-linux/bin/python -m teleop \
  --config /etc/vr-teleop/config.yaml --confirm-hardware
```

이 명령은 연결 직후 오류 초기화, 자동 모드, drag 해제와 robot enable을
수행한다. 로그에 다음을 확인한다.

- `mode=HARDWARE <robot-ip>`
- `Fairino connection ready`
- worker `IDLE` 또는 health ready
- SDK/controller version

### 10.3 최소 이동 시험

1. Quest에서 VR 세션을 시작하되 grip을 누르지 않는다.
2. UI 상태와 로봇 정지 상태를 확인한다.
3. controller를 안정적으로 고정한 후 grip을 누른다.
4. 로봇이 튀지 않고 `ACTIVE`가 되는지 확인한다.
5. 손을 한 축으로 1 cm 이하 움직여 좌표 방향을 확인한다.
6. grip을 놓고 `ServoMoveEnd`, `SLEEPING`과 실제 정지를 확인한다.
7. 손을 다른 위치로 옮긴 뒤 grip을 다시 누른다. 재개 순간 위치 점프가
   없어야 한다.
8. Quest 화면을 종료하거나 Wi-Fi를 끊어 지정된 timeout 안에 정지하는지
   확인한다.
9. `Ctrl+C` 종료 시 `ServoMoveEnd` 뒤 worker shutdown이 기록되는지
   확인한다.

방향이 다르거나 예상보다 크게 움직이면 즉시 grip을 놓고 E-stop을
준비한 상태에서 서버를 종료한다. scale을 올려 문제를 덮지 않는다.

## 11. systemd 운영

dry-run service부터 설치한다.

```bash
sudo install -m 0644 /opt/vr-teleop/deploy/vr-teleop.service \
  /etc/systemd/system/vr-teleop.service
sudo systemctl daemon-reload
sudo systemctl enable --now vr-teleop
sudo systemctl status vr-teleop --no-pager
curl -k https://127.0.0.1:8443/health/ready
```

실제 로봇 운용은 foreground commissioning을 모두 통과한 뒤에만
활성화한다. hardware 확인 옵션은 systemd drop-in에 명시한다.

```bash
sudo systemctl edit vr-teleop
```

편집기에 다음을 입력한다.

```ini
[Service]
ExecStart=
ExecStart=/opt/vr-teleop/.venv-linux/bin/python -m teleop --config /etc/vr-teleop/config.yaml --confirm-hardware
```

적용한다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart vr-teleop
sudo journalctl -u vr-teleop -n 100 --no-pager
```

## 12. 정상 상태와 정지 의미

| 상태 | 의미 | 현장 조치 |
|---|---|---|
| `STARTING` | worker/SDK 시작 중 | 10초 이상이면 로그 확인 |
| `IDLE` | servo 비활성, 입력 대기 | 정상 |
| `ARMING` | TCP 원점 조회와 servo 시작 중 | 짧게 지나가야 함 |
| `ACTIVE` | grip 유지, ServoCart 송신 중 | 감시 유지 |
| `SLEEPING` | grip 해제 후 ServoMoveEnd 완료 | 정상, WebXR 유지 |
| `STOPPING` | 정지 명령 처리 중 | 로봇 정지 확인 |
| `FAULT` | SDK/worker 오류 | 아래 장애 절차 수행 |
| `SHUTDOWN` | worker 종료 | 정상 종료 상태 |

`pose_timeout_s`보다 새 pose가 오래 도착하지 않으면 worker는 마지막
목표를 계속 재사용하지 않고 servo를 종료한다. 한 update 사이에 여러
WebSocket pose가 오면 가장 최신 pose만 사용하며 과거 pose를 재생하지
않는다.

## 13. 장애 및 비상 대응

### 로봇이 예상하지 않은 방향/속도로 움직임

1. grip을 즉시 놓는다.
2. 정지가 확인되지 않으면 E-stop을 누른다.
3. `sudo systemctl stop vr-teleop` 또는 foreground에서 `Ctrl+C`.
4. 좌표 방향, scale, workspace와 실제 TCP/tool 설정을 확인한다.

### `FAULT` 표시

1. grip을 놓고 로봇이 정지했는지 확인한다.
2. 로그에서 최초 오류와 SDK error code를 기록한다.
3. 원인을 해결하기 전 반복 재시작하거나 robot error를 계속 reset하지
   않는다.
4. 원인 해결 후 Quest 세션을 종료하고 서비스를 재시작한다.

### `rapid servo start/end transitions detected`

입력 timeout, grip 접점/버튼 상태, Quest frame drop 또는 SDK 호출
지연으로 servo start/end가 기본값 기준 1초에 4회 이상 반복된 상태다.
실제 이동을 중단하고 다음 값을 함께 보존한다.

- `start_count`, `end_count`
- `reason`
- `pose_seq`, `input_age_ms`
- 같은 시점의 `sdk_call_ms`, `jitter_ms`, `missed_ticks`

### WebXR 버튼 비활성 또는 연결 실패

- Quest 주소가 `https://`인지 확인한다.
- 인증서 SAN이 Quest가 접속한 IP/DNS와 일치하는지 확인한다.
- Quest Browser가 인증서를 신뢰하는지 확인한다.
- `curl -k https://<PC>:8443/health/ready`를 다른 LAN 장치에서 확인한다.
- 방화벽, AP client isolation과 `allowed_origins`를 확인한다.

### FR5 연결 실패

- `ping`, `20003/TCP`와 firmware에 맞는 `20004/TCP` 또는
  `20005/TCP`를 다시 확인한다.
- PC route가 올바른 robot NIC로 향하는지 `ip route get <robot-ip>`로
  확인한다.
- 다른 프로그램이 동시에 controller를 제어하지 않는지 확인한다.
- SDK/controller version 조합을 확인한다.

## 14. 로그 수집

systemd 운용:

```bash
sudo journalctl -u vr-teleop --since "10 minutes ago" \
  --no-pager -o short-iso > vr-teleop-field.log
```

직접 실행한 경우에는 stderr를 포함한다.

```bash
.venv/bin/python -m teleop --config config.yaml --confirm-hardware \
  2>&1 | tee "vr-teleop-$(date +%Y%m%d-%H%M%S).log"
```

다음 정보와 함께 릴리즈 담당자에게 전달한다.

- 릴리즈 버전과 `SHA256SUMS` 결과
- Ubuntu `uname -a`, `python3 --version`
- FR5 model, controller firmware, SDK version 로그
- PC robot NIC/Quest NIC 주소와 route
- Quest OS/Browser version
- 재현 시각, worker state, grip 상태
- E-stop 사용 여부와 실제 정지 결과

진단 로그에서 다음 항목을 함께 확인한다.

- `pose stream gap/summary`: Quest client-time 간격과 서버 수신 간격
  (`missing_sequences`는 브라우저 backpressure drop 포함)
- `pose timeout stopping servo`: timeout 당시 sequence와 input age
- `slow SDK call summary`: 작업명별 느린 호출
- `servo deadline miss summary`: missed tick과 최대 overrun
- `worker state`: start/cart/end 누계와 state transition reason

pose 원본을 매 frame 수집하거나 외부에 공개하지 않는다.

## 15. 종료와 rollback

정상 종료:

```bash
sudo systemctl stop vr-teleop
sudo journalctl -u vr-teleop -n 50 --no-pager
```

종료 로그에서 servo stop과 worker shutdown을 확인한다. 이전 버전으로
돌릴 때도 먼저 서비스를 중지하고 로봇의 실제 정지를 확인한 뒤
`/opt/vr-teleop`을 교체한다. 개인 키와 현장별 config는 백업하되 릴리즈
아카이브나 Git에 넣지 않는다.

## 16. 공식 참고 자료

- FAIRINO Python SDK 기본/RPC:
  <https://fairino-doc-en.readthedocs.io/3.6.7/SDKManual/PythonRobotBase.html>
- FAIRINO Cartesian servo movement:
  <https://fairino-doc-en.readthedocs.io/3.6.7/SDKManual/CPPRobotMovement.html>
- FAIRINO teaching pendant:
  <https://fairino-doc-en.readthedocs.io/latest/CobotsManual/teaching_pendant_software.html>

현장 안전 설정, controller firmware 및 teaching pendant 조작은 설치된
장비 버전의 FAIRINO 공식 매뉴얼과 조직의 안전 절차를 우선한다.
