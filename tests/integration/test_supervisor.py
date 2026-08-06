from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from teleop.config import TeleopConfig
from teleop.protocol import PoseMessage
from teleop.robot.fake_client import FakeRobotBehavior
from teleop.robot.state import ControlCommand, WorkerState
from teleop.robot.supervisor import RobotSupervisor


def make_config() -> TeleopConfig:
    return TeleopConfig(
        web_dir=Path(__file__).resolve().parents[2] / "web",
        tls_cert_path=None,
        tls_key_path=None,
        servo_period_s=0.008,
        pose_timeout_s=0.100,
        worker_watchdog_s=0.500,
        graceful_shutdown_s=1.0,
        status_hz=20.0,
    )


def pose(seq: int, grip: bool, x: float = 0.0) -> PoseMessage:
    return PoseMessage(
        session_id="test",
        seq=seq,
        client_time_ms=float(seq),
        hand="right",
        position_m=(x, 1.0, 0.0),
        orientation_xyzw=(0.0, 0.0, 0.0, 1.0),
        grip=grip,
        trigger=False,
        received_ns=time.monotonic_ns(),
    )


def wait_for_state(
    supervisor: RobotSupervisor, state: WorkerState, timeout: float = 3.0
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for status in supervisor.drain_status():
            if status.state == state:
                return status
        time.sleep(0.01)
    raise AssertionError(
        f"worker did not reach {state}; latest={supervisor.latest_status}"
    )


def wait_for_status(supervisor: RobotSupervisor, predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for status in supervisor.drain_status():
            if predicate(status):
                return status
        time.sleep(0.01)
    raise AssertionError(
        f"worker did not reach expected status; latest={supervisor.latest_status}"
    )


def test_grip_release_sleeps_and_rising_edge_resumes_servo() -> None:
    supervisor = RobotSupervisor(make_config())
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True, 0.01))
        active = wait_for_state(supervisor, WorkerState.ACTIVE)
        assert active.counters.servo_start_count == 1

        supervisor.publish_pose(pose(3, False, 0.01))
        sleeping = wait_for_state(supervisor, WorkerState.SLEEPING)
        assert sleeping.counters.servo_end_count == 1

        supervisor.publish_pose(pose(4, True, 0.02))
        resumed = wait_for_state(supervisor, WorkerState.ACTIVE)
        assert resumed.counters.servo_start_count == 2
        assert resumed.counters.servo_end_count == 1
    finally:
        report = supervisor.shutdown()
        assert not report.killed


def test_grip_edges_survive_latest_pose_overwrite() -> None:
    config = replace(
        make_config(),
        servo_period_s=0.050,
        pose_timeout_s=0.500,
        worker_watchdog_s=1.000,
    )
    supervisor = RobotSupervisor(config)
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)

        # Both snapshots are published inside one worker period. The pose slot
        # ends as grip=True, but the release edge must survive on control IPC.
        supervisor.publish_pose(pose(1, False))
        supervisor.publish_pose(pose(2, True))
        active = wait_for_status(
            supervisor,
            lambda status: (
                status.state == WorkerState.ACTIVE
                and status.counters.servo_start_count == 1
            ),
        )
        assert not active.rearm_required

        supervisor.publish_pose(pose(3, False, 0.01))
        supervisor.publish_pose(pose(4, True, 0.02))
        resumed = wait_for_status(
            supervisor,
            lambda status: (
                status.state == WorkerState.ACTIVE
                and status.counters.servo_start_count == 2
                and status.counters.servo_end_count == 1
            ),
        )
        assert not resumed.rearm_required
    finally:
        report = supervisor.shutdown()
        assert not report.killed


def test_session_lost_stops_active_servo() -> None:
    supervisor = RobotSupervisor(make_config())
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True))
        wait_for_state(supervisor, WorkerState.ACTIVE)
        supervisor.send_control(ControlCommand.SESSION_LOST)
        stopped = wait_for_state(supervisor, WorkerState.IDLE)
        assert stopped.counters.servo_end_count == 1
    finally:
        supervisor.shutdown()


def test_session_lost_leaves_sleeping_state_for_idle() -> None:
    supervisor = RobotSupervisor(make_config())
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True))
        wait_for_state(supervisor, WorkerState.ACTIVE)
        supervisor.publish_pose(pose(3, False))
        wait_for_state(supervisor, WorkerState.SLEEPING)

        supervisor.send_control(ControlCommand.SESSION_LOST)
        stopped = wait_for_state(supervisor, WorkerState.IDLE)
        assert stopped.reason == "session_release"
        assert stopped.counters.servo_end_count == 1
    finally:
        supervisor.shutdown()


def test_stale_pose_stops_active_servo() -> None:
    supervisor = RobotSupervisor(make_config())
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True))
        wait_for_state(supervisor, WorkerState.ACTIVE)
        stopped = wait_for_state(supervisor, WorkerState.IDLE)
        assert stopped.reason == "pose_timeout"
        assert stopped.counters.servo_end_count == 1
        assert stopped.rearm_required
    finally:
        supervisor.shutdown()


def test_servo_start_failure_never_marks_servo_started() -> None:
    supervisor = RobotSupervisor(
        make_config(),
        fake_behavior=FakeRobotBehavior(fail_on="servo_start"),
    )
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True))
        fault = wait_for_state(supervisor, WorkerState.FAULT)
        assert fault.counters.servo_start_count == 0
        assert fault.counters.servo_end_count == 0
    finally:
        supervisor.shutdown()


def test_hung_sdk_call_is_contained_by_process_termination() -> None:
    config = replace(
        make_config(),
        pose_timeout_s=0.050,
        worker_watchdog_s=0.100,
        graceful_shutdown_s=0.100,
    )
    supervisor = RobotSupervisor(
        config,
        fake_behavior=FakeRobotBehavior(hang_on="servo_cart", hang_seconds=2.0),
    )
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(2, True))
        wait_for_state(supervisor, WorkerState.ACTIVE)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not supervisor.health().hung:
            time.sleep(0.01)
        assert supervisor.health().hung
    finally:
        report = supervisor.shutdown()
    assert not report.graceful
    assert report.terminated or report.killed
