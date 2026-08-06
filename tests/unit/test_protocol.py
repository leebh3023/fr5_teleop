import math

import pytest

from teleop.protocol import ControlEvent, PoseMessage, ProtocolError, parse_client_message


def valid_pose() -> dict:
    return {
        "version": 1,
        "type": "pose",
        "session_id": "session",
        "seq": 1,
        "client_time_ms": 12.5,
        "hand": "right",
        "position_m": [0.1, 1.2, -0.3],
        "orientation_xyzw": [0.0, 0.0, 0.0, 2.0],
        "grip": True,
        "trigger": False,
    }


def test_pose_is_validated_and_quaternion_is_normalized() -> None:
    message = parse_client_message(valid_pose(), 123)
    assert isinstance(message, PoseMessage)
    assert message.received_ns == 123
    assert message.orientation_xyzw == (0.0, 0.0, 0.0, 1.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_m", [0, 0]),
        ("position_m", [math.nan, 0, 0]),
        ("orientation_xyzw", [0, 0, 0, 0]),
        ("grip", 1),
        ("seq", -1),
        ("version", True),
    ],
)
def test_invalid_pose_is_rejected(field: str, value: object) -> None:
    payload = valid_pose()
    payload[field] = value
    with pytest.raises(ProtocolError):
        parse_client_message(payload, 123)


def test_control_event_is_parsed() -> None:
    message = parse_client_message(
        {
            "version": 1,
            "type": "event",
            "session_id": "session",
            "event": "release_control",
        },
        123,
    )
    assert message == ControlEvent("session", "release_control")
