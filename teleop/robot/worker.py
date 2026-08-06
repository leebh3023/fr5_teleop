from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass
from typing import Any

from teleop.config import TeleopConfig
from teleop.control_math import MotionPlanner
from teleop.ipc import LatestPoseMailbox
from teleop.robot.client import RobotClient, RobotClientError
from teleop.robot.fake_client import FakeRobotBehavior, FakeRobotClient
from teleop.robot.fairino_client import FairinoRobotClient
from teleop.robot.state import (
    ControlCommand,
    WorkerCounters,
    WorkerState,
    WorkerStatus,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSpec:
    generation: int
    config: TeleopConfig
    robot_kind: str
    fake_behavior: FakeRobotBehavior | None = None


def _make_client(spec: WorkerSpec) -> RobotClient:
    if spec.robot_kind == "fake":
        return FakeRobotClient(spec.fake_behavior)
    if spec.robot_kind == "fairino":
        return FairinoRobotClient(spec.config)
    raise ValueError(f"unknown robot kind: {spec.robot_kind}")


def run_robot_worker(
    spec: WorkerSpec,
    mailbox: LatestPoseMailbox,
    control_receive: Any,
    status_queue: Any,
    heartbeat_ns: Any,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(processName)s] [%(levelname)s] %(message)s",
    )
    config = spec.config
    client = _make_client(spec)
    planner = MotionPlanner(config)
    counters = WorkerCounters()
    state = WorkerState.STARTING
    fault: str | None = None
    reason: str | None = "worker_start"
    servo_started = False
    robot_tcp = None
    tick = 0
    last_jitter_ms = 0.0
    last_sdk_call_ms = 0.0
    last_status_ns = 0
    require_release = True
    last_grip = True
    force_release = False
    reset_requested = False
    shutdown_requested = False

    period_ns = int(config.servo_period_s * 1_000_000_000)
    pose_timeout_ns = int(config.pose_timeout_s * 1_000_000_000)
    status_period_ns = int(1_000_000_000 / config.status_hz)

    def publish_status(now_ns: int, transition_reason: str | None = None) -> None:
        nonlocal last_status_ns
        pose = mailbox.read()
        input_age_ms = None
        if pose.valid and pose.received_ns:
            input_age_ms = max(0.0, (now_ns - pose.received_ns) / 1_000_000)
        status = WorkerStatus(
            generation=spec.generation,
            state=state,
            tracking=state == WorkerState.ACTIVE,
            tick=tick,
            input_age_ms=input_age_ms,
            robot_tcp=robot_tcp,
            jitter_ms=last_jitter_ms,
            sdk_call_ms=last_sdk_call_ms,
            counters=WorkerCounters(
                servo_start_count=counters.servo_start_count,
                servo_cart_count=counters.servo_cart_count,
                servo_end_count=counters.servo_end_count,
                missed_ticks=counters.missed_ticks,
            ),
            fault=fault,
            reason=transition_reason or reason,
        )
        try:
            status_queue.put_nowait(status)
            last_status_ns = now_ns
        except queue.Full:
            pass

    def timed_call(operation: Any, *args: Any) -> Any:
        nonlocal last_sdk_call_ms
        started_ns = time.monotonic_ns()
        try:
            return operation(*args)
        finally:
            last_sdk_call_ms = (time.monotonic_ns() - started_ns) / 1_000_000

    def stop_servo(
        now_ns: int,
        stop_reason: str,
        *,
        fault_after: str | None = None,
        shutdown_after: bool = False,
    ) -> None:
        nonlocal state, fault, reason, servo_started, robot_tcp
        nonlocal require_release, last_grip
        state = WorkerState.STOPPING
        reason = stop_reason
        publish_status(now_ns, stop_reason)
        stop_error: str | None = None
        if servo_started:
            counters.servo_end_count += 1
            try:
                timed_call(client.servo_end)
            except Exception as exc:
                stop_error = str(exc)
            finally:
                servo_started = False
        planner.release()
        robot_tcp = None
        require_release = True
        last_grip = True
        if shutdown_after and stop_error is None:
            state = WorkerState.SHUTDOWN
        elif fault_after is not None or stop_error is not None:
            state = WorkerState.FAULT
            fault = fault_after or f"servo stop failed: {stop_error}"
        else:
            state = WorkerState.IDLE
        publish_status(time.monotonic_ns(), stop_reason)

    heartbeat_ns.value = time.monotonic_ns()
    publish_status(heartbeat_ns.value)
    try:
        timed_call(client.connect)
        timed_call(client.initialize)
        state = WorkerState.IDLE
        reason = "robot_ready"
        publish_status(time.monotonic_ns(), reason)
    except Exception as exc:
        state = WorkerState.FAULT
        fault = str(exc)
        reason = "startup_failed"
        publish_status(time.monotonic_ns(), reason)

    next_deadline_ns = time.monotonic_ns()
    try:
        while not shutdown_requested:
            now_ns = time.monotonic_ns()
            heartbeat_ns.value = now_ns
            tick += 1
            last_jitter_ms = (now_ns - next_deadline_ns) / 1_000_000

            try:
                while control_receive.poll():
                    command = control_receive.recv()
                    if command in {ControlCommand.RELEASE, ControlCommand.SESSION_LOST}:
                        force_release = True
                        mailbox.invalidate()
                    elif command == ControlCommand.FAULT_RESET:
                        reset_requested = True
                    elif command == ControlCommand.SHUTDOWN:
                        shutdown_requested = True
            except (EOFError, OSError):
                force_release = True
                shutdown_requested = True

            if shutdown_requested:
                stop_servo(
                    now_ns,
                    "shutdown",
                    shutdown_after=True,
                )
                break

            pose = mailbox.read()
            fresh = (
                pose.valid
                and pose.generation == spec.generation
                and now_ns >= pose.received_ns
                and now_ns - pose.received_ns <= pose_timeout_ns
            )

            if force_release:
                if state in {
                    WorkerState.ARMING,
                    WorkerState.ACTIVE,
                    WorkerState.STOPPING,
                } or servo_started:
                    stop_servo(now_ns, "session_release")
                require_release = True
                last_grip = True
                force_release = False

            if state == WorkerState.FAULT:
                if reset_requested and fresh and not pose.grip:
                    try:
                        timed_call(client.reset_fault)
                        fault = None
                        state = WorkerState.IDLE
                        reason = "fault_reset"
                        require_release = False
                        last_grip = False
                        publish_status(time.monotonic_ns(), reason)
                    except Exception as exc:
                        fault = str(exc)
                        reason = "fault_reset_failed"
                reset_requested = False

            elif state == WorkerState.IDLE:
                if fresh and not pose.grip:
                    require_release = False
                    last_grip = False
                elif fresh and pose.grip and not require_release and not last_grip:
                    state = WorkerState.ARMING
                    reason = "grip_rising_edge"
                    publish_status(now_ns, reason)
                    try:
                        robot_tcp = timed_call(client.get_current_tcp)
                        planner.engage(pose.position_m, robot_tcp)
                        timed_call(client.servo_start)
                        counters.servo_start_count += 1
                        servo_started = True
                        state = WorkerState.ACTIVE
                        reason = "servo_started"
                        publish_status(time.monotonic_ns(), reason)
                    except Exception as exc:
                        planner.release()
                        state = WorkerState.FAULT
                        fault = str(exc)
                        reason = "arming_failed"
                        publish_status(time.monotonic_ns(), reason)
                    last_grip = True

            elif state == WorkerState.ACTIVE:
                if not fresh:
                    stop_servo(now_ns, "pose_timeout")
                elif not pose.grip:
                    stop_servo(now_ns, "grip_released")
                else:
                    try:
                        robot_tcp = planner.target_for(pose.position_m)
                        timed_call(client.servo_cart, robot_tcp)
                        counters.servo_cart_count += 1
                    except Exception as exc:
                        stop_servo(
                            time.monotonic_ns(),
                            "servo_cart_failed",
                            fault_after=str(exc),
                        )

            if now_ns - last_status_ns >= status_period_ns:
                publish_status(now_ns)

            next_deadline_ns += period_ns
            after_work_ns = time.monotonic_ns()
            if after_work_ns > next_deadline_ns:
                behind_ns = after_work_ns - next_deadline_ns
                skipped = behind_ns // period_ns + 1
                counters.missed_ticks += int(skipped)
                next_deadline_ns += int(skipped) * period_ns
            delay_ns = next_deadline_ns - time.monotonic_ns()
            if delay_ns > 0:
                time.sleep(delay_ns / 1_000_000_000)
    except BaseException as exc:
        fault = f"worker crashed: {exc}"
        state = WorkerState.FAULT
        publish_status(time.monotonic_ns(), "worker_crash")
        raise
    finally:
        if servo_started:
            stop_servo(
                time.monotonic_ns(),
                "worker_finally",
                fault_after=fault,
                shutdown_after=fault is None,
            )
        try:
            timed_call(client.close)
        except Exception as exc:
            log.error("robot close failed: %s", exc)
        state = WorkerState.SHUTDOWN
        reason = "worker_stopped"
        heartbeat_ns.value = time.monotonic_ns()
        publish_status(heartbeat_ns.value, reason)
        try:
            control_receive.close()
        except OSError:
            pass
