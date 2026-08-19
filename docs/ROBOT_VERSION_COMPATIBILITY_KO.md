# FAIRINO FR5 로봇 펌웨어 버전별(V3.7.8 vs V3.9.2) 호환성 및 전환 가이드

이 문서는 FAIRINO FR5 로봇 펌웨어 버전 **Robot V3.7.8** 및 **Robot V3.9.2** 두 대의 장비를 교대 운용할 때의 통신 규격 차이, 원인별 트러블슈팅, 및 전환 시 체크해야 하는 코드/설정 항목을 설명합니다.

---

## 1. 펌웨어 및 SDK 버전별 주요 규격 차이

| 항목 | Robot V3.7.8 (구형) | Robot V3.9.2 (신형) |
|---|---|---|
| **권장 SDK 경로** | `/home/user/fairino-python-sdk-2.0.8_robot3.7.8/linux` | `/home/user/fairino-python-sdk-2.2.2_robot3.9.2/linux` |
| **기본 IP 예시** | `192.168.58.2` | `192.168.58.3` |
| **`ServoCart` XML-RPC 인자 수** | **9개** (`mode, desc_pos, pos_gain, exaxis, acc, vel, cmdT, filterT, gain`) | **8개** (`mode, desc_pos, pos_gain, acc, vel, cmdT, filterT, gain`) |
| **`exaxis` 파라미터 전송 여부** | 필수 전송 (`exaxis=[0.0, 0.0, 0.0, 0.0]`) | **전송 금지** (9개 전송 시 `<Fault -502>` 발생) |
| **`vel` (서보 속도) 파라미터** | `vel=0.0` 무시 후 정상 동작 | **`vel=0.0` 전송 시 에러 코드 14 반환** (`vel=100.0` 지정 필수) |
| **`Mode(0)` 호출 처리** | 0 리턴 (성공) | 이미 Auto 모드이거나 스위치 잠김 시 **코드 14 반환** (`_call_allow` 허용 필요) |
| **`RobotEnable` 리셋 타이밍** | 매 Grip마다 호출 시에도 동작 | 매 Grip마다 호출 시 드라이브 지연으로 **코드 14 반환** (초기화 시 1회만 호출) |

---

## 2. 원인별 트러블슈팅 상세 (Troubleshooting Detail)

### ① `<Fault -502: "Format string requests exactly 8 items from array, but array has 9 items.">`
* **원인:** Robot V3.9.2 제어기의 XML-RPC 서버는 `ServoCart` 호출 시 정확히 8개의 파라미터를 요구합니다. SDK `Robot.py`를 임의 수정하여 9번째 인자인 `exaxis`를 전송하면 이 에러가 발생합니다.
* **해결:** SDK V2.2.2의 `Robot.py` 내 `ServoCart` 메소드는 8개 인자를 유지해야 합니다. (`exaxis` 추가 금지)

### ② `ServoCart failed with code 14` (속도 설정 문제)
* **원인:** Robot V3.9.2 펌웨어는 `ServoCart` 전송 시 `vel: 0.0` (속도 0%)을 수락하지 않고 유효하지 않은 속도 값으로 판단하여 에러 코드 14를 리턴합니다. (Robot V3.7.8은 `vel: 0.0`을 무시했음)
* **해결:** `teleop/robot/fairino_client.py`의 `servo_cart` 딕셔너리에 `"vel": 100.0, "acc": 100.0`을 명시적으로 지정합니다.

### ③ `ServoCart failed with code 14` (Workspace 경계 벗어남으로 인한 위치 점프 문제)
* **원인:** 로봇의 현재 실제 TCP 좌표(예: `Z = -145.04mm`)가 `config.yaml`에 설정된 `workspace.z` 최소 범위(예: `[50.0, 700.0]`)를 벗어난 경우, `clamp_workspace` 로직에 의해 첫 틱(8ms)에서 `Z = -145mm -> +50mm`로 **195mm 위치 점프**가 발생합니다. 8ms 만에 195mm를 이동하라는 명령이 들어오면 제어기가 안전 정지(에러 코드 14)를 발생시킵니다.
* **해결:** 로봇의 실제 위치(X, Y, Z)가 포함되도록 `config.yaml`의 `workspace` 범위를 적절히 설정합니다. (예: `z: [-160.0, 420.0]`)

### ④ `Mode failed with code 14`
* **원인:** Robot V3.9.2는 티칭 펜던트/제어기 상자의 물리 스위치가 Auto 모드로 잠겨있거나 이미 해당 모드일 때 SDK를 통한 `Mode(0)` 명령에 대해 코드 14(현재 모드 유지/잠김)를 반환합니다.
* **해결:** `fairino_client.py`에서 `_call_allow("Mode", {14}, 0)` 형태로 코드 14를 허용하도록 처리합니다.

### ⑤ Grip 시 매번 `RobotEnable` 호출로 인한 드라이브 재인가 지연
* **원인:** Grip을 누를 때마다 `reset_fault()` (`RobotEnable(1)`)가 실행되면 모터 드라이브 전원이 매번 재인가되며 약 0.4초의 드라이브 지연이 생깁니다. 드라이브 준비 전 `ServoCart`가 전송되면 에러 코드 14가 리턴됩니다.
* **해결:** `worker.py`의 `_arm_servo`에서 `if self.require_release or self.fault is not None:` 조건을 두어 실제 에러 상황에서만 `reset_fault`를 호출합니다.

---

## 3. 로봇 버전 변경 시 체크리스트 및 코드 변경 방법

두 로봇 장비를 교체하여 실행할 때는 다음 파일들을 확인합니다.

### 1) `config.yaml` 설정 변경

대상이 되는 로봇의 IP, SDK 경로, 그리고 로봇 실제 위치에 맞는 `workspace` 범위를 설정합니다.

```yaml
# Robot V3.7.8 사용 시
robot:
  ip: 192.168.58.2
  sdk_path: /home/user/fairino-python-sdk-2.0.8_robot3.7.8/linux

# Robot V3.9.2 사용 시
robot:
  ip: 192.168.58.3
  sdk_path: /home/user/fairino-python-sdk-2.2.2_robot3.9.2/linux

motion:
  position_scale: 500.0
  ema_alpha: 0.2
  max_velocity_mm_s: 150.0
  max_step_mm: 2.0
  workspace:
    x: [-100.0, 500.0]
    y: [-360.0, -80.0]
    z: [-160.0, 420.0]  # 현재 로봇 TCP 좌표가 완전히 포함되도록 범위 설정
```

### 2) `teleop/robot/fairino_client.py` 코드 확인

`servo_cart` 메소드의 속도 파라미터 및 `initialize` 메소드가 아래와 같이 작성되어 있어야 두 펌웨어 모두에서 정상 동작합니다.

```python
    def initialize(self) -> None:
        # 코드 14(이미 해당 상태이거나 하드웨어 잠김)를 허용하도록 설정
        self._call_allow("ResetAllError", {14})
        self._call_allow("Mode", {14}, 0)
        self._call_allow("DragTeachSwitch", {14}, 0)
        self._call_allow("RobotEnable", {14}, 1)
        self._call_allow("SetSpeed", {14}, 100)

    def servo_cart(self, target: TcpPose) -> None:
        if self._robot is None:
            raise RobotClientError("ServoCart", None, "robot is not connected")
        arguments = {
            "mode": 0,
            "desc_pos": list(target),
            "pos_gain": [1.0] * 6,
            "acc": 100.0,  # V3.9.2 호환을 위해 100.0 설정 (0.0 설정 시 V3.9.2에서 에러 14 발생)
            "vel": 100.0,  # V3.9.2 호환을 위해 100.0 설정
            "cmdT": self.config.servo_period_s,
            "filterT": 0.0,
            "gain": 0.0,
        }
        if self._servo_cart_supports_exaxis is None:
            raise RobotClientError(
                "ServoCart",
                None,
                "SDK signature was not inspected during connect",
            )
        # SDK Signature에 exaxis가 정의된 경우에만 exaxis를 인자에 자동 추가함 (V3.7.8: 9개, V3.9.2: 8개)
        if self._servo_cart_supports_exaxis:
            arguments["exaxis"] = list(self.config.exaxis_default)
        result = self._robot.ServoCart(**arguments)
        self._expect_zero(result, "ServoCart")
```

### 3) `teleop/robot/worker.py` 코드 확인 (`_arm_servo`)

Grip을 누를 때마다 매번 `RobotEnable(1)`을 재호출하여 모터 드라이브 전원이 리셋되는 현상을 방지하기 위해 `_arm_servo` 메소드가 조건부로 `reset_fault`를 호출하도록 설정합니다.

```python
    def _arm_servo(self, now_ns: int, pose: SharedPose) -> None:
        self.state = WorkerState.ARMING
        self.reason = "grip_rising_edge"
        self._publish_status(now_ns, self.reason)
        try:
            # 실제 Fault가 발생했거나 rearm이 필요한 경우에만 reset_fault 호출
            if self.require_release or self.fault is not None:
                self._timed_call(self.client.reset_fault)
            self.robot_tcp = self._timed_call(self.client.get_current_tcp)
            self.planner.engage(pose.position_m, self.robot_tcp)
            self._timed_call(self.client.servo_start)
```

---

## 4. 로봇 교체 시 빠른 스위칭 절차

1. **로봇 3.7.8 테스트 시:**
   - `config.yaml`의 `robot.ip`를 `192.168.58.2`로 설정
   - `config.yaml`의 `robot.sdk_path`를 `/home/user/fairino-python-sdk-2.0.8_robot3.7.8/linux`로 설정
   - `config.yaml`의 `workspace` 범위를 로봇 3.7.8 현재 위치에 맞게 확인
   - 실행: `python -m teleop --config config.yaml --confirm-hardware`

2. **로봇 3.9.2 테스트 시:**
   - `config.yaml`의 `robot.ip`를 `192.168.58.3`으로 설정
   - `config.yaml`의 `robot.sdk_path`를 `/home/user/fairino-python-sdk-2.2.2_robot3.9.2/linux`로 설정
   - `config.yaml`의 `workspace` 범위를 로봇 3.9.2 현재 위치에 맞게 확인
   - 실행: `python -m teleop --config config.yaml --confirm-hardware`
