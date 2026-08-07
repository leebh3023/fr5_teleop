from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Any

from teleop.config import TeleopConfig
from teleop.control_math import MotionPlanner, TcpPose
from teleop.ipc import LatestPoseMailbox, SharedPose
from teleop.robot.client import RobotClient
from teleop.robot.diagnostics import WorkerDiagnostics
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


@dataclass
class ControlBatch:
    grip_events: list[ControlCommand] = field(default_factory=list)
    trigger_events: list[ControlCommand] = field(default_factory=list)
    session_release: bool = False
    fault_reset: bool = False
    shutdown: bool = False


def _make_client(spec: WorkerSpec) -> RobotClient:
    if spec.robot_kind == "fake":
        return FakeRobotClient(spec.fake_behavior)
    if spec.robot_kind == "fairino":
        return FairinoRobotClient(spec.config)
    raise ValueError(f"unknown robot kind: {spec.robot_kind}")


class RobotWorkerRuntime:
    """Owns the robot SDK and all state transitions inside the child process."""

    SERVO_STATES = {
        WorkerState.ARMING,
        WorkerState.ACTIVE,
        WorkerState.STOPPING,
    }

    def __init__(
        self,
        spec: WorkerSpec,
        mailbox: LatestPoseMailbox,
        control_receive: Any,
        status_queue: Any,
        heartbeat_ns: Any,
    ) -> None:
        self.spec = spec
        self.config = spec.config
        self.mailbox = mailbox
        self.control_receive = control_receive
        self.status_queue = status_queue
        self.heartbeat_ns = heartbeat_ns

        self.client = _make_client(spec)
        self.planner = MotionPlanner(self.config)
        self.counters = WorkerCounters()
        self.diagnostics = WorkerDiagnostics(
            config=self.config,
            generation=self.spec.generation,
            mailbox=self.mailbox,
        )

        self.state = WorkerState.STARTING
        self.reason: str | None = "worker_start"
        self.fault: str | None = None
        self.servo_started = False
        self.robot_tcp: TcpPose | None = None
        self.require_release = True
        self.last_grip = True

        self.tick = 0
        self.last_servo_command_ns = 0
        self.last_status_ns = 0

        self.gripper_closed = self.config.gripper.initially_closed
        self.gripper_position = (
            self.config.gripper.closed_position
            if self.gripper_closed
            else self.config.gripper.open_position
        )
        self.pending_gripper_position: int | None = None
        self.gripper_deadline_ns = 0
        self.next_gripper_poll_ns = 0
        self.session_lost_during_gripper = False

        self.period_ns = int(self.config.servo_period_s * 1_000_000_000)
        self.pose_timeout_ns = int(
            self.config.pose_timeout_s * 1_000_000_000
        )
        self.status_period_ns = int(1_000_000_000 / self.config.status_hz)
        self.shutdown_requested = False

    def run(self) -> None:
        self._publish_initial_status()
        self._initialize_client()

        next_deadline_ns = time.monotonic_ns()
        try:
            while not self.shutdown_requested:
                tick_started_ns = time.monotonic_ns()
                self.heartbeat_ns.value = tick_started_ns
                self.tick += 1
                self.diagnostics.last_jitter_ms = (
                    tick_started_ns - next_deadline_ns
                ) / 1_000_000

                commands = self._drain_control()
                if commands.shutdown:
                    if self.state == WorkerState.GRIPPER_ACTION:
                        log.warning(
                            "shutdown requested during gripper action; "
                            "controller-side gripper motion may continue "
                            "generation=%d target=%s",
                            self.spec.generation,
                            self.pending_gripper_position,
                        )
                    self.shutdown_requested = True
                    self._stop_servo(
                        time.monotonic_ns(),
                        "shutdown",
                        shutdown_after=True,
                    )
                    break

                pose = self.mailbox.read()
                # Read the clock after the shared mailbox. A parent publish can
                # otherwise make received_ns newer than a stale tick timestamp.
                now_ns = time.monotonic_ns()
                self.heartbeat_ns.value = now_ns
                fresh = self._pose_is_fresh(pose, now_ns)

                self._handle_session_release(commands, now_ns)
                self._handle_trigger_events(
                    commands.trigger_events,
                    now_ns,
                    pose,
                    fresh,
                )
                grip_press_requested = self._handle_grip_events(
                    commands.grip_events,
                    now_ns,
                )
                self._advance_state(
                    time.monotonic_ns(),
                    pose,
                    fresh,
                    grip_press_requested,
                    commands.fault_reset,
                )

                status_now_ns = time.monotonic_ns()
                if status_now_ns - self.last_status_ns >= self.status_period_ns:
                    self._publish_status(status_now_ns)
                next_deadline_ns = self._finish_tick(next_deadline_ns)
        except BaseException as exc:
            self.fault = f"worker crashed: {exc}"
            self.state = WorkerState.FAULT
            self._publish_status(time.monotonic_ns(), "worker_crash")
            raise
        finally:
            self._close()

    def _publish_initial_status(self) -> None:
        now_ns = time.monotonic_ns()
        self.heartbeat_ns.value = now_ns
        self._publish_status(now_ns)

    def _initialize_client(self) -> None:
        try:
            self._timed_call(self.client.connect)
            self._timed_call(self.client.initialize)
            self.state = WorkerState.IDLE
            self.reason = "robot_ready"
            self._publish_status(time.monotonic_ns(), self.reason)
        except Exception as exc:
            self._enter_fault("startup_failed", exc)

    def _drain_control(self) -> ControlBatch:
        batch = ControlBatch()
        try:
            while self.control_receive.poll():
                command = self.control_receive.recv()
                if command in {
                    ControlCommand.RELEASE,
                    ControlCommand.SESSION_LOST,
                }:
                    batch.session_release = True
                    self.mailbox.invalidate()
                elif command in {
                    ControlCommand.GRIP_PRESSED,
                    ControlCommand.GRIP_RELEASED,
                }:
                    batch.grip_events.append(command)
                elif command in {
                    ControlCommand.TRIGGER_PRESSED,
                    ControlCommand.TRIGGER_RELEASED,
                }:
                    batch.trigger_events.append(command)
                elif command == ControlCommand.FAULT_RESET:
                    batch.fault_reset = True
                elif command == ControlCommand.SHUTDOWN:
                    batch.shutdown = True
        except (EOFError, OSError):
            batch.session_release = True
            batch.shutdown = True
        return batch

    def _pose_is_fresh(self, pose: SharedPose, now_ns: int) -> bool:
        return (
            pose.valid
            and pose.generation == self.spec.generation
            and now_ns >= pose.received_ns
            and now_ns - pose.received_ns <= self.pose_timeout_ns
        )

    def _handle_session_release(
        self,
        commands: ControlBatch,
        now_ns: int,
    ) -> None:
        if not commands.session_release:
            return
        if self.state == WorkerState.GRIPPER_ACTION:
            self.session_lost_during_gripper = True
            self.require_release = True
            self.last_grip = True
            self.reason = "session_release_during_gripper"
            self._publish_status(now_ns, self.reason)
            return
        if self.state in self.SERVO_STATES or self.servo_started:
            self._stop_servo(now_ns, "session_release")
        elif self.state == WorkerState.SLEEPING:
            self.state = WorkerState.IDLE
            self.reason = "session_release"
            self._publish_status(now_ns, self.reason)
        self.require_release = True
        self.last_grip = True

    def _handle_trigger_events(
        self,
        events: list[ControlCommand],
        now_ns: int,
        pose: SharedPose,
        fresh: bool,
    ) -> None:
        for event in events:
            if event != ControlCommand.TRIGGER_PRESSED:
                continue
            if not self.config.gripper.enabled:
                log.info(
                    "trigger ignored because gripper is disabled generation=%d",
                    self.spec.generation,
                )
                continue
            if self.state != WorkerState.ACTIVE or not fresh or not pose.grip:
                log.info(
                    "trigger ignored generation=%d state=%s fresh=%s grip=%s",
                    self.spec.generation,
                    self.state.value,
                    fresh,
                    pose.grip,
                )
                continue
            self._begin_gripper_action(now_ns)

    def _handle_grip_events(
        self,
        events: list[ControlCommand],
        now_ns: int,
    ) -> bool:
        press_requested = False
        for event in events:
            if event == ControlCommand.GRIP_RELEASED:
                if self.state in self.SERVO_STATES or self.servo_started:
                    self._stop_servo(
                        now_ns,
                        "grip_released",
                        next_state=WorkerState.SLEEPING,
                    )
                else:
                    self.require_release = False
                    self.last_grip = False
            else:
                press_requested = True
        return press_requested

    def _advance_state(
        self,
        now_ns: int,
        pose: SharedPose,
        fresh: bool,
        grip_press_requested: bool,
        reset_requested: bool,
    ) -> None:
        if self.state == WorkerState.FAULT:
            self._handle_fault_reset(
                now_ns,
                pose,
                fresh,
                reset_requested,
            )
        elif self.state == WorkerState.GRIPPER_ACTION:
            self._poll_gripper(now_ns)
        elif self.state in {WorkerState.IDLE, WorkerState.SLEEPING}:
            self._handle_idle_or_sleeping(
                now_ns,
                pose,
                fresh,
                grip_press_requested,
            )
        elif self.state == WorkerState.ACTIVE:
            self._handle_active(now_ns, pose, fresh)

    def _handle_fault_reset(
        self,
        now_ns: int,
        pose: SharedPose,
        fresh: bool,
        reset_requested: bool,
    ) -> None:
        if not reset_requested or not fresh or pose.grip:
            return
        try:
            self._timed_call(self.client.reset_fault)
            self.fault = None
            self.state = WorkerState.IDLE
            self.reason = "fault_reset"
            self.require_release = False
            self.last_grip = False
            self._publish_status(time.monotonic_ns(), self.reason)
        except Exception as exc:
            self.fault = str(exc)
            self.reason = "fault_reset_failed"
            log.error(
                "robot fault reset failed generation=%d",
                self.spec.generation,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._publish_status(time.monotonic_ns(), self.reason)

    def _handle_idle_or_sleeping(
        self,
        now_ns: int,
        pose: SharedPose,
        fresh: bool,
        grip_press_requested: bool,
    ) -> None:
        if fresh and not pose.grip:
            self.require_release = False
            self.last_grip = False
            return
        if (
            fresh
            and pose.grip
            and not self.require_release
            and (grip_press_requested or not self.last_grip)
        ):
            self._arm_servo(now_ns, pose)

    def _arm_servo(self, now_ns: int, pose: SharedPose) -> None:
        self.state = WorkerState.ARMING
        self.reason = "grip_rising_edge"
        self._publish_status(now_ns, self.reason)
        try:
            self.robot_tcp = self._timed_call(self.client.get_current_tcp)
            self.planner.engage(pose.position_m, self.robot_tcp)
            self._timed_call(self.client.servo_start)
            self.servo_started = True
            self.counters.servo_start_count += 1
            self.diagnostics.record_servo_transition(
                time.monotonic_ns(),
                "start",
                self.reason,
                self.counters,
            )
            self.state = WorkerState.ACTIVE
            self.reason = "servo_started"
            self.last_grip = True
            self.last_servo_command_ns = time.monotonic_ns()
            self._publish_status(time.monotonic_ns(), self.reason)
        except Exception as exc:
            if self.servo_started:
                self._stop_servo(
                    time.monotonic_ns(),
                    "arming_failed",
                    fault_after=exc,
                )
            else:
                self.planner.release()
                self._enter_fault("arming_failed", exc)

    def _handle_active(
        self,
        now_ns: int,
        pose: SharedPose,
        fresh: bool,
    ) -> None:
        if not fresh:
            self.diagnostics.log_pose_timeout(
                now_ns,
                pose,
                self.counters,
            )
            self._stop_servo(now_ns, "pose_timeout")
            return
        if not pose.grip:
            self._stop_servo(
                now_ns,
                "grip_released",
                next_state=WorkerState.SLEEPING,
            )
            return
        try:
            elapsed_s = max(
                0.0,
                (now_ns - self.last_servo_command_ns) / 1_000_000_000,
            )
            step_limit_mm = min(
                self.config.max_step_mm,
                self.config.max_velocity_mm_s * elapsed_s,
            )
            self.robot_tcp = self.planner.target_for(
                pose.position_m,
                step_limit_mm=step_limit_mm,
            )
            self._timed_call(self.client.servo_cart, self.robot_tcp)
            self.last_servo_command_ns = now_ns
            self.counters.servo_cart_count += 1
        except Exception as exc:
            self._stop_servo(
                time.monotonic_ns(),
                "servo_cart_failed",
                fault_after=exc,
            )

    def _begin_gripper_action(self, now_ns: int) -> None:
        target = (
            self.config.gripper.open_position
            if self.gripper_closed
            else self.config.gripper.closed_position
        )
        self._stop_servo(
            now_ns,
            "gripper_requested",
            next_state=WorkerState.GRIPPER_ACTION,
        )
        if self.state != WorkerState.GRIPPER_ACTION:
            return
        try:
            self._timed_call(self.client.move_gripper, target)
        except Exception as exc:
            self._enter_fault("gripper_command_failed", exc)
            return

        command_completed_ns = time.monotonic_ns()
        self.pending_gripper_position = target
        self.gripper_deadline_ns = command_completed_ns + int(
            self.config.gripper.action_timeout_s * 1_000_000_000
        )
        self.next_gripper_poll_ns = command_completed_ns
        self.session_lost_during_gripper = False
        self.counters.gripper_command_count += 1
        self.reason = "gripper_moving"
        self._publish_status(command_completed_ns, self.reason)

    def _poll_gripper(self, now_ns: int) -> None:
        if now_ns >= self.gripper_deadline_ns:
            self._enter_fault(
                "gripper_timeout",
                RuntimeError("gripper did not complete before action timeout"),
            )
            return
        if now_ns < self.next_gripper_poll_ns:
            return
        self.next_gripper_poll_ns = now_ns + int(
            self.config.gripper.poll_period_s * 1_000_000_000
        )
        try:
            motion = self._timed_call(
                self.client.get_gripper_motion_state
            )
        except Exception as exc:
            self._enter_fault("gripper_status_failed", exc)
            return
        if motion.fault != 0:
            self._enter_fault(
                "gripper_fault",
                RuntimeError(f"gripper fault={motion.fault}"),
            )
            return
        if not motion.done:
            return

        if self.pending_gripper_position is None:
            self._enter_fault(
                "gripper_state_invalid",
                RuntimeError("gripper completed without a pending target"),
            )
            return
        self.gripper_position = self.pending_gripper_position
        self.gripper_closed = (
            self.gripper_position == self.config.gripper.closed_position
        )
        self.pending_gripper_position = None
        self.counters.gripper_complete_count += 1
        self.state = (
            WorkerState.IDLE
            if self.session_lost_during_gripper
            else WorkerState.SLEEPING
        )
        # A release/press performed while the gripper was still moving does
        # not rearm Cartesian motion. Require a release observed after the
        # controller reports the gripper action complete.
        self.require_release = True
        self.last_grip = True
        self.reason = "gripper_complete"
        self._publish_status(time.monotonic_ns(), self.reason)

    def _stop_servo(
        self,
        now_ns: int,
        stop_reason: str,
        *,
        fault_after: Exception | str | None = None,
        shutdown_after: bool = False,
        next_state: WorkerState = WorkerState.IDLE,
    ) -> None:
        self.state = WorkerState.STOPPING
        self.reason = stop_reason
        self._publish_status(now_ns, stop_reason)
        stop_error: str | None = None
        if self.servo_started:
            self.counters.servo_end_count += 1
            try:
                self._timed_call(self.client.servo_end)
            except Exception as exc:
                stop_error = str(exc)
                log.error(
                    "servo end failed generation=%d reason=%s",
                    self.spec.generation,
                    stop_reason,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            finally:
                self.servo_started = False
                self.diagnostics.record_servo_transition(
                    time.monotonic_ns(),
                    "end",
                    stop_reason,
                    self.counters,
                )
        self.planner.release()
        self.robot_tcp = None
        self.last_servo_command_ns = 0
        if next_state == WorkerState.SLEEPING:
            self.require_release = False
            self.last_grip = False
        elif next_state == WorkerState.GRIPPER_ACTION:
            # A trigger action is a clutch boundary. Even when the operator
            # keeps holding grip, motion must not resume until a release and a
            # new press have both been observed.
            self.require_release = True
            self.last_grip = True
        elif next_state != WorkerState.GRIPPER_ACTION:
            self.require_release = True
            self.last_grip = True
        if shutdown_after and stop_error is None:
            self.state = WorkerState.SHUTDOWN
        elif fault_after is not None or stop_error is not None:
            self.state = WorkerState.FAULT
            self.fault = (
                str(fault_after)
                if fault_after is not None
                else f"servo stop failed: {stop_error}"
            )
            if isinstance(fault_after, Exception):
                log.error(
                    "servo lifecycle fault generation=%d reason=%s",
                    self.spec.generation,
                    stop_reason,
                    exc_info=(
                        type(fault_after),
                        fault_after,
                        fault_after.__traceback__,
                    ),
                )
        else:
            self.state = next_state
        self._publish_status(time.monotonic_ns(), stop_reason)

    def _enter_fault(self, reason: str, error: Exception) -> None:
        log.error(
            "worker fault generation=%d reason=%s error_type=%s",
            self.spec.generation,
            reason,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        self.planner.release()
        self.robot_tcp = None
        self.last_servo_command_ns = 0
        self.state = WorkerState.FAULT
        self.reason = reason
        self.fault = str(error)
        self.pending_gripper_position = None
        self.require_release = True
        self.last_grip = True
        self._publish_status(time.monotonic_ns(), reason)

    def _publish_status(
        self,
        now_ns: int,
        transition_reason: str | None = None,
    ) -> None:
        pose = self.mailbox.read()
        input_age_ms = (
            max(0.0, (now_ns - pose.received_ns) / 1_000_000)
            if pose.valid and pose.received_ns
            else None
        )
        effective_reason = transition_reason or self.reason
        status = WorkerStatus(
            generation=self.spec.generation,
            state=self.state,
            tracking=self.state == WorkerState.ACTIVE,
            rearm_required=self.require_release,
            tick=self.tick,
            input_age_ms=input_age_ms,
            robot_tcp=self.robot_tcp,
            jitter_ms=self.diagnostics.last_jitter_ms,
            sdk_call_ms=self.diagnostics.last_sdk_call_ms,
            counters=WorkerCounters(
                servo_start_count=self.counters.servo_start_count,
                servo_cart_count=self.counters.servo_cart_count,
                servo_end_count=self.counters.servo_end_count,
                missed_ticks=self.counters.missed_ticks,
                gripper_command_count=self.counters.gripper_command_count,
                gripper_complete_count=self.counters.gripper_complete_count,
            ),
            fault=self.fault,
            reason=effective_reason,
            gripper_enabled=self.config.gripper.enabled,
            gripper_busy=self.state == WorkerState.GRIPPER_ACTION,
            gripper_position=(
                self.pending_gripper_position
                if self.pending_gripper_position is not None
                else self.gripper_position
            ),
        )
        self.diagnostics.log_state_transition(
            status,
            servo_started=self.servo_started,
            pose_seq=pose.seq if pose.valid else None,
        )
        try:
            self.status_queue.put_nowait(status)
            self.last_status_ns = now_ns
        except queue.Full:
            pass

    def _timed_call(self, operation: Any, *args: Any) -> Any:
        return self.diagnostics.timed_call(
            operation,
            *args,
            state=self.state,
            tick=self.tick,
        )

    def _finish_tick(self, next_deadline_ns: int) -> int:
        next_deadline_ns += self.period_ns
        after_work_ns = time.monotonic_ns()
        if after_work_ns > next_deadline_ns:
            behind_ns = after_work_ns - next_deadline_ns
            skipped = behind_ns // self.period_ns + 1
            self.counters.missed_ticks += int(skipped)
            self.diagnostics.record_deadline_miss(
                after_work_ns,
                skipped=int(skipped),
                overrun_ms=behind_ns / 1_000_000,
                counters=self.counters,
                state=self.state,
                tick=self.tick,
            )
            next_deadline_ns += int(skipped) * self.period_ns
        delay_ns = next_deadline_ns - time.monotonic_ns()
        if delay_ns > 0:
            time.sleep(delay_ns / 1_000_000_000)
        return next_deadline_ns

    def _close(self) -> None:
        if self.servo_started:
            self._stop_servo(
                time.monotonic_ns(),
                "worker_finally",
                fault_after=self.fault,
                shutdown_after=self.fault is None,
            )
        try:
            self._timed_call(self.client.close)
        except Exception as exc:
            log.error(
                "robot close failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        self.state = WorkerState.SHUTDOWN
        self.reason = "worker_stopped"
        now_ns = time.monotonic_ns()
        self.heartbeat_ns.value = now_ns
        self._publish_status(now_ns, self.reason)
        try:
            self.control_receive.close()
        except OSError:
            pass


def run_robot_worker(
    spec: WorkerSpec,
    mailbox: LatestPoseMailbox,
    control_receive: Any,
    status_queue: Any,
    heartbeat_ns: Any,
) -> None:
    logging.basicConfig(
        level=getattr(logging, spec.config.log_level),
        format=(
            "%(asctime)s [pid=%(process)d] [%(processName)s] "
            "[%(levelname)s] %(message)s"
        ),
    )
    RobotWorkerRuntime(
        spec,
        mailbox,
        control_receive,
        status_queue,
        heartbeat_ns,
    ).run()
