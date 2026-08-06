from pathlib import Path

import pytest

from teleop.config import TeleopConfig
from teleop.robot.client import RobotClientError
from teleop.robot.fairino_client import FairinoRobotClient


class StubRobot:
    def __init__(self) -> None:
        self.servo_cart_call = None
        self.tcp_result = (0, [1, 2, 3, 4, 5, 6])

    def ServoCart(self, **kwargs):
        self.servo_cart_call = kwargs
        return 0

    def GetActualTCPPose(self):
        return self.tcp_result


class LegacyStubRobot:
    def __init__(self) -> None:
        self.servo_cart_calls = []

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


def client_with_stub() -> tuple[FairinoRobotClient, StubRobot]:
    config = TeleopConfig(
        dry_run=False,
        robot_ip="192.0.2.1",
        sdk_path=Path("unused"),
        web_dir=Path(__file__).resolve().parents[2] / "web",
        tls_cert_path=None,
        tls_key_path=None,
    )
    client = FairinoRobotClient(config)
    stub = StubRobot()
    client._robot = stub
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
