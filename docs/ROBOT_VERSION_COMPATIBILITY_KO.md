# FAIRINO FR5 로봇 펌웨어 / SDK 호환성

현재 운용 중인 모든 FR5는 **펌웨어 v3.9.8**이고, 저장소에 동봉된 **SDK 2.2.8**
(`fairino-python-sdk-2.2.8_robot_v3.9.8/`)을 사용한다. 구버전 펌웨어(V3.7.8,
V3.9.2)와 해당 SDK(2.0.8, 2.2.2)는 더 이상 지원 대상이 아니다.

## 1. 기준 구성

| 항목 | 값 |
|---|---|
| 로봇 펌웨어 | v3.9.8 |
| SDK | 2.2.8 (`fairino-python-sdk-2.2.8_robot_v3.9.8/linux`) |
| `robot.sdk_path` | `fairino-python-sdk-2.2.8_robot_v3.9.8/linux` (config 파일 기준 상대 경로) |
| 사용하는 SDK 파일 | `linux/fairino/Robot.py` 하나 (순수 파이썬, 표준 라이브러리만 사용) |
| 통신 포트 | XML-RPC TCP 20003, CNDE 상태 TCP 20005, UDP 20007 |

양팔 구성(`config_bimanual.yaml`)은 두 팔 모두 top-level `robot.sdk_path`를
쓴다. 특정 팔만 다른 SDK가 필요할 때만 `robot.arms.<hand>.sdk_path`를 지정한다.

Docker 이미지에는 이 SDK의 `Robot.py`가 같은 상대 경로로 들어 있으므로 config를
고치지 않고 그대로 동작한다.

## 2. SDK adapter 동작 (`teleop/robot/fairino_client.py`)

- 연결 시 `ServoCart` signature를 검사한다. SDK 2.2.8은 `exaxis` 파라미터가
  있으므로 `exaxis=robot.exaxis_default`를 함께 보내고 `acc=0`, `vel=0`을 쓴다.
- 연결되면 `GetSDKVersion` 결과를 `Fairino connection ready: ip=... versions=...`
  로그로 남긴다. 단, Fairino SDK는 이 값을 한 단계 전 버전으로 고정해 반환한다
  (SDK 2.2.8 → `['SDK:V2.2.7', 'Robot:V3.9.7']`). 이 로그로 실제 SDK나 펌웨어
  버전을 판단하지 않는다. 펌웨어 버전은 컨트롤러 화면에서 확인한다.
- `Mode(0)`과 `SetSpeed`는 code 14(이미 해당 모드, 스위치 잠김)를 허용한다.
- `RobotEnable` 리셋(`reset_fault`)은 grip마다 하지 않고, 실제 fault가 있거나
  rearm이 필요할 때만 호출한다(`worker.py`의 `_arm_servo`). 매번 호출하면
  드라이브 재인가 지연 때문에 첫 `ServoCart`가 실패할 수 있다.

## 3. 트러블슈팅

### `ServoCart failed with code 14` (workspace 경계로 인한 위치 점프)
- **원인:** 로봇의 현재 TCP 좌표(예: `Z = -145mm`)가 `motion.workspace` 범위
  (예: `z: [50, 700]`) 밖에 있으면, `clamp_workspace`가 첫 tick(8ms)에 목표를
  경계로 끌어온다. 이때 수백 mm 점프 명령이 나가 제어기가 안전 정지(code 14)한다.
- **해결:** 로봇의 실제 시작 위치가 포함되도록 `workspace` 범위를 설정한다.
  양팔은 `robot.arms.<hand>.workspace`로 팔마다 따로 지정한다.

```yaml
motion:
  workspace:
    x: [-100.0, 500.0]
    y: [-360.0, -80.0]
    z: [-160.0, 420.0]  # 현재 로봇 TCP 좌표가 완전히 포함되도록
```

## 4. 로봇 실행 전 확인

1. `robot.ip`(양팔은 `robot.arms.<hand>.ip`)가 대상 로봇과 맞는지 확인한다.
2. `workspace`가 로봇의 현재 TCP 위치를 포함하는지 확인한다.
3. 실행: `python -m teleop --config config.yaml --confirm-hardware`
   (Docker: `docker compose run --rm teleop --confirm-hardware`)
4. 시작 로그에 `Fairino connection ready: ip=<대상 IP>`가 오류 없이 나오는지 확인한다.
