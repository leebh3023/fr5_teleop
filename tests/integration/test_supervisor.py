from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from teleop.app import TeleopRuntime
from teleop.config import ArmConfig, GripperConfig, TeleopConfig
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


def pose(
    seq: int,
    grip: bool,
    x: float = 0.0,
    *,
    trigger: bool = False,
    hand: str = "right",
) -> PoseMessage:
    return PoseMessage(
        session_id="test",
        seq=seq,
        client_time_ms=float(seq),
        hand=hand,
        position_m=(x, 1.0, 0.0),
        orientation_xyzw=(0.0, 0.0, 0.0, 1.0),
        grip=grip,
        trigger=trigger,
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


def wait_for_hand_state(
    supervisor: RobotSupervisor,
    hand: str,
    state: WorkerState,
    timeout: float = 3.0,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervisor.drain_status_by_hand()
        status = supervisor.latest_status_by_hand().get(hand)
        if status is not None and status.state == state:
            return status
        time.sleep(0.01)
    raise AssertionError(
        f"worker {hand} did not reach {state}; "
        f"latest={supervisor.latest_status_by_hand()}"
    )


def test_bimanual_workers_route_pose_and_shutdown_independently() -> None:
    config = replace(
        make_config(),
        pose_timeout_s=0.500,
        worker_watchdog_s=1.000,
        arms=(ArmConfig(hand="left"), ArmConfig(hand="right")),
    )
    supervisor = RobotSupervisor(config)
    supervisor.start()
    reports = None
    try:
        wait_for_hand_state(supervisor, "left", WorkerState.IDLE)
        wait_for_hand_state(supervisor, "right", WorkerState.IDLE)
        assert set(supervisor.health_by_hand()) == {"left", "right"}
        assert all(health.alive for health in supervisor.health_by_hand().values())

        supervisor.publish_pose(pose(0, False, hand="left"))
        supervisor.publish_pose(pose(1, True, 0.01, hand="left"))
        left = wait_for_hand_state(supervisor, "left", WorkerState.ACTIVE)
        assert left.counters.servo_start_count == 1
        assert supervisor.latest_status_by_hand()["right"].state == WorkerState.IDLE

        supervisor.publish_pose(pose(0, False, hand="right"))
        supervisor.publish_pose(pose(1, True, 0.01, hand="right"))
        right = wait_for_hand_state(supervisor, "right", WorkerState.ACTIVE)
        assert right.counters.servo_start_count == 1
    finally:
        reports = supervisor.shutdown_by_hand()

    assert set(reports) == {"left", "right"}
    assert all(not report.killed for report in reports.values())


async def test_bimanual_worker_fault_stops_active_peer() -> None:
    config = replace(
        make_config(),
        pose_timeout_s=0.500,
        worker_watchdog_s=1.000,
        arms=(ArmConfig(hand="left"), ArmConfig(hand="right")),
    )
    runtime = TeleopRuntime(config)
    runtime.supervisor = RobotSupervisor(
        config,
        fake_behavior={
            "left": FakeRobotBehavior(fail_on="servo_start"),
            "right": FakeRobotBehavior(),
        },
    )
    await runtime.start()

    async def wait_hand(hand: str, state: WorkerState):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = runtime.supervisor.latest_status_by_hand().get(hand)
            if status is not None and status.state == state:
                return status
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"worker {hand} did not reach {state}; "
            f"latest={runtime.supervisor.latest_status_by_hand()}"
        )

    try:
        await wait_hand("left", WorkerState.IDLE)
        await wait_hand("right", WorkerState.IDLE)

        runtime.supervisor.publish_pose(pose(0, False, hand="right"))
        runtime.supervisor.publish_pose(pose(1, True, hand="right"))
        await wait_hand("right", WorkerState.ACTIVE)

        runtime.supervisor.publish_pose(pose(0, False, hand="left"))
        runtime.supervisor.publish_pose(pose(1, True, hand="left"))
        left_fault = await wait_hand("left", WorkerState.FAULT)
        right_stopped = await wait_hand("right", WorkerState.IDLE)

        assert left_fault.reason == "arming_failed"
        assert right_stopped.reason == "session_release"
        assert right_stopped.counters.servo_end_count == 1
    finally:
        await runtime.stop()


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


def test_trigger_action_stops_servo_and_requires_new_grip_edge() -> None:
    config = replace(
        make_config(),
        pose_timeout_s=0.500,
        gripper=GripperConfig(
            enabled=True,
            command_max_time_ms=100,
            action_timeout_s=0.500,
            poll_period_s=0.010,
        ),
    )
    supervisor = RobotSupervisor(
        config,
        fake_behavior=FakeRobotBehavior(gripper_motion_s=0.080),
    )
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)

        supervisor.publish_pose(pose(2, False, trigger=True))
        action = wait_for_state(supervisor, WorkerState.GRIPPER_ACTION)
        assert action.gripper_busy

        supervisor.publish_pose(pose(3, False, trigger=False))
        sleeping = wait_for_status(
            supervisor,
            lambda status: (
                status.counters.gripper_complete_count == 1
            ),
        )
    finally:
        supervisor.shutdown()


def test_trigger_edges_survive_pose_overwrite() -> None:
    config = replace(
        make_config(),
        servo_period_s=0.050,
        pose_timeout_s=0.500,
        worker_watchdog_s=1.000,
        gripper=GripperConfig(
            enabled=True,
            command_max_time_ms=100,
            action_timeout_s=0.500,
            poll_period_s=0.010,
        ),
    )
    supervisor = RobotSupervisor(config)
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)

        supervisor.publish_pose(pose(3, False, trigger=True))
        supervisor.publish_pose(pose(4, False, trigger=False))
        completed = wait_for_status(
            supervisor,
            lambda status: (
                status.counters.gripper_complete_count == 1
            ),
        )
        assert completed.counters.gripper_command_count == 1
    finally:
        supervisor.shutdown()


def test_gripper_fault_is_latched_without_servo_restart() -> None:
    config = replace(
        make_config(),
        pose_timeout_s=0.500,
        gripper=GripperConfig(
            enabled=True,
            command_max_time_ms=100,
            action_timeout_s=0.500,
            poll_period_s=0.010,
        ),
    )
    supervisor = RobotSupervisor(
        config,
        fake_behavior=FakeRobotBehavior(gripper_fault=1),
    )
    supervisor.start()
    try:
        wait_for_state(supervisor, WorkerState.IDLE)
        supervisor.publish_pose(pose(1, False))
        time.sleep(0.03)
        supervisor.publish_pose(pose(3, False, trigger=True))

        fault = wait_for_state(supervisor, WorkerState.FAULT)
        assert fault.reason == "gripper_fault"
        assert fault.counters.gripper_complete_count == 0
    finally:
        supervisor.shutdown()
