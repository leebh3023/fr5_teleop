from pathlib import Path

import pytest

from teleop.config import GripperConfig, TeleopConfig
from teleop.robot.client import RobotClientError
from teleop.robot.fairino_client import FairinoRobotClient


class StubRobot:
    def __init__(self) -> None:
        self.servo_cart_call = None
        self.tcp_result = (0, [1, 2, 3, 4, 5, 6])

        self.gripper_call = None
        self.gripper_result = (0, [0, 1])

    def ServoCart(
        self,
        mode,
        desc_pos,
        exaxis,
        pos_gain,
        acc,
        vel,
        cmdT,
        filterT,
        gain,
    ):
        self.servo_cart_call = {
            "mode": mode,
            "desc_pos": desc_pos,
            "exaxis": exaxis,
            "pos_gain": pos_gain,
            "acc": acc,
            "vel": vel,
            "cmdT": cmdT,
            "filterT": filterT,
            "gain": gain,
        }
        return 0

    def GetActualTCPPose(self):
        return self.tcp_result

    def MoveGripper(
        self,
        index,
        pos,
        vel,
        force,
        maxtime,
        block,
        type,
        rotNum,
        rotVel,
        rotTorque,
    ):
        self.gripper_call = {
            "index": index,
            "pos": pos,
            "vel": vel,
            "force": force,
            "maxtime": maxtime,
            "block": block,
            "type": type,
            "rotNum": rotNum,
            "rotVel": rotVel,
            "rotTorque": rotTorque,
        }
        return 0

    def GetGripperMotionDone(self):
        return self.gripper_result


class LegacyStubRobot:
    def __init__(self) -> None:
        self.servo_cart_calls = []
        self.gripper_call = None

    def ServoCart(
        self,
        mode,
        desc_pos,
        pos_gain,
        acc,
        vel,
        cmdT,
        filterT,
        gain,
    ):
        self.servo_cart_calls.append(
            {
                "mode": mode,
                "desc_pos": desc_pos,
                "pos_gain": pos_gain,
                "acc": acc,
                "vel": vel,
                "cmdT": cmdT,
                "filterT": filterT,
                "gain": gain,
            }
        )
        return 0

    def MoveGripper(self, index, pos, vel, force, maxtime, block):
        self.gripper_call = {
            "index": index,
            "pos": pos,
            "vel": vel,
            "force": force,
            "maxtime": maxtime,
            "block": block,
        }
        return 0

    def GetGripperMotionDone(self):
        return 0, [0, 1]


def client_with_stub(
    *, gripper: GripperConfig | None = None
) -> tuple[FairinoRobotClient, StubRobot]:
    config = TeleopConfig(
        dry_run=False,
        robot_ip="192.0.2.1",
        sdk_path=Path("unused"),
        web_dir=Path(__file__).resolve().parents[2] / "web",
        tls_cert_path=None,
        tls_key_path=None,
        gripper=gripper or GripperConfig(),
    )
    client = FairinoRobotClient(config)
    stub = StubRobot()
    client._robot = stub
    client._inspect_sdk_signatures()
    return client, stub


def test_servo_cart_supplies_required_exaxis() -> None:
    client, stub = client_with_stub()
    target = (1.0, 2.0, 3.0, 180.0, 0.0, 0.0)
    client.servo_cart(target)
    assert stub.servo_cart_call["desc_pos"] == list(target)
    assert stub.servo_cart_call["exaxis"] == [0.0, 0.0, 0.0, 0.0]
    assert stub.servo_cart_call == {
        "mode": 0,
        "desc_pos": list(target),
        "exaxis": [0.0, 0.0, 0.0, 0.0],
        "pos_gain": [1.0] * 6,
        "acc": 0.0,
        "vel": 0.0,
        "cmdT": 0.008,
        "filterT": 0.0,
        "gain": 0.0,
    }


def test_integer_error_from_pose_query_is_normalized() -> None:
    client, stub = client_with_stub()
    stub.tcp_result = -4
    with pytest.raises(RobotClientError) as error:
        client.get_current_tcp()
    assert error.value.code == -4


def test_legacy_servo_cart_without_exaxis_is_detected_once() -> None:
    client, _ = client_with_stub()
    legacy = LegacyStubRobot()
    client._robot = legacy
    client._inspect_sdk_signatures()
    target = (1.0, 2.0, 3.0, 180.0, 0.0, 0.0)

    client.servo_cart(target)
    client.servo_cart(target)

    assert client._servo_cart_supports_exaxis is False
    assert len(legacy.servo_cart_calls) == 2
    assert legacy.servo_cart_calls[0] == {
        "mode": 0,
        "desc_pos": list(target),
        "pos_gain": [1.0] * 6,
        "acc": 0.0,
        "vel": 0.0,
        "cmdT": 0.008,
        "filterT": 0.0,
        "gain": 0.0,
    }


def test_gripper_uses_extended_signature_without_trial_call() -> None:
    client, stub = client_with_stub(gripper=GripperConfig(enabled=True))

    client.move_gripper(100)

    assert stub.gripper_call == {
        "index": 1,
        "pos": 100,
        "vel": 50,
        "force": 50,
        "maxtime": 3000,
        "block": 1,
        "type": 0,
        "rotNum": 0.0,
        "rotVel": 0,
        "rotTorque": 0,
    }


def test_gripper_motion_state_is_normalized() -> None:
    client, stub = client_with_stub(gripper=GripperConfig(enabled=True))
    stub.gripper_result = (0, [0, 1])

    state = client.get_gripper_motion_state()

    assert state.fault == 0
    assert state.done is True


def test_legacy_gripper_signature_omits_extended_arguments() -> None:
    client, _ = client_with_stub(gripper=GripperConfig(enabled=True))
    legacy = LegacyStubRobot()
    client._robot = legacy
    client._inspect_sdk_signatures()

    client.move_gripper(100)

    assert client._move_gripper_supports_extended_args is False
    assert legacy.gripper_call == {
        "index": 1,
        "pos": 100,
        "vel": 50,
        "force": 50,
        "maxtime": 3000,
        "block": 1,
    }


def test_ambiguous_servo_signature_fails_closed_before_motion() -> None:
    class AmbiguousRobot:
        calls = 0

        def ServoCart(self, **kwargs):
            self.calls += 1
            return 0

    client, _ = client_with_stub()
    ambiguous = AmbiguousRobot()
    client._robot = ambiguous

    with pytest.raises(RobotClientError, match="ambiguous SDK"):
        client._inspect_sdk_signatures()

    assert ambiguous.calls == 0


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"is_connect": True}, True),
        ({"is_conect": True}, True),
        ({"is_connect": False, "is_conect": True}, True),
        ({"is_connect": False}, False),
        ({}, None),
    ],
)
def test_connection_state_supports_legacy_spelling(
    attributes: dict[str, bool], expected: bool | None
) -> None:
    rpc_type = type("RpcType", (), attributes)
    assert FairinoRobotClient._connection_state(rpc_type) is expected
