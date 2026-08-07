from __future__ import annotations

import logging
import multiprocessing
import queue
import time
from dataclasses import dataclass
from typing import Any

from teleop.config import TeleopConfig
from teleop.ipc import WorkerIpc, create_worker_ipc
from teleop.protocol import PoseMessage
from teleop.robot.fake_client import FakeRobotBehavior
from teleop.robot.state import ControlCommand, WorkerState, WorkerStatus
from teleop.robot.worker import WorkerSpec, run_robot_worker


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SupervisorHealth:
    alive: bool
    heartbeat_age_ms: float | None
    hung: bool
    generation: int


@dataclass(frozen=True)
class ShutdownReport:
    graceful: bool
    terminated: bool
    killed: bool
    exit_code: int | None


class RobotSupervisor:
    def __init__(
        self,
        config: TeleopConfig,
        *,
        robot_kind: str | None = None,
        fake_behavior: FakeRobotBehavior | None = None,
    ) -> None:
        self.config = config
        self.robot_kind = robot_kind or ("fake" if config.dry_run else "fairino")
        self.fake_behavior = fake_behavior
        self._context = multiprocessing.get_context("spawn")
        self._generation = 0
        self._process: Any = None
        self._ipc: WorkerIpc | None = None
        self._started_ns = 0
        self.latest_status: WorkerStatus | None = None
        self._last_grip: bool | None = None
        self._last_trigger: bool | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self) -> None:
        if self._process is not None and self._process.is_alive():
            raise RuntimeError("robot worker is already running")
        self._generation += 1
        self._last_grip = None
        self._last_trigger = None
        self._ipc = create_worker_ipc(self._context)
        spec = WorkerSpec(
            generation=self._generation,
            config=self.config,
            robot_kind=self.robot_kind,
            fake_behavior=self.fake_behavior,
        )
        self._process = self._context.Process(
            target=run_robot_worker,
            args=(
                spec,
                self._ipc.mailbox,
                self._ipc.control_receive,
                self._ipc.status_queue,
                self._ipc.heartbeat_ns,
            ),
            name=f"RobotWorker-{self._generation}",
            daemon=False,
        )
        self._started_ns = time.monotonic_ns()
        self._process.start()
        self._ipc.control_receive.close()
        log.info(
            "robot worker started generation=%d pid=%s kind=%s",
            self._generation,
            self.pid,
            self.robot_kind,
        )

    def publish_pose(self, pose: PoseMessage) -> None:
        if self._ipc is None:
            raise RuntimeError("robot worker is not running")
        self._ipc.mailbox.publish(pose, self._generation)
        if self._last_grip is None or pose.grip != self._last_grip:
            self.send_control(
                ControlCommand.GRIP_PRESSED
                if pose.grip
                else ControlCommand.GRIP_RELEASED
            )
            self._last_grip = pose.grip
        if self._last_trigger is None or pose.trigger != self._last_trigger:
            self.send_control(
                ControlCommand.TRIGGER_PRESSED
                if pose.trigger
                else ControlCommand.TRIGGER_RELEASED
            )
            self._last_trigger = pose.trigger

    def invalidate_pose(self) -> None:
        if self._ipc is not None:
            self._ipc.mailbox.invalidate()
        self._last_grip = None
        self._last_trigger = None
        log.info("latest pose invalidated generation=%d", self._generation)

    def send_control(self, command: ControlCommand) -> None:
        if self._ipc is None:
            return
        try:
            self._ipc.control_send.send(command)
            log.info(
                "worker control sent generation=%d command=%s",
                self._generation,
                command.value,
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            log.warning(
                "worker control send failed generation=%d command=%s error=%s",
                self._generation,
                command.value,
                exc,
            )

    def drain_status(self) -> list[WorkerStatus]:
        if self._ipc is None:
            return []
        statuses: list[WorkerStatus] = []
        while True:
            try:
                status = self._ipc.status_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(status, WorkerStatus):
                self.latest_status = status
                statuses.append(status)
        return statuses

    def health(self) -> SupervisorHealth:
        if self._process is None or self._ipc is None:
            return SupervisorHealth(False, None, False, self._generation)
        alive = self._process.is_alive()
        heartbeat = int(self._ipc.heartbeat_ns.value)
        baseline = heartbeat or self._started_ns
        age_ms = max(0.0, (time.monotonic_ns() - baseline) / 1_000_000)
        starting = (
            self.latest_status is None
            or self.latest_status.state == WorkerState.STARTING
        )
        timeout_s = (
            self.config.worker_startup_timeout_s
            if starting
            else self.config.worker_watchdog_s
        )
        return SupervisorHealth(
            alive=alive,
            heartbeat_age_ms=age_ms,
            hung=alive and age_ms > timeout_s * 1000,
            generation=self._generation,
        )

    def shutdown(self) -> ShutdownReport:
        if self._process is None:
            return ShutdownReport(True, False, False, None)
        process = self._process
        self.send_control(ControlCommand.SHUTDOWN)
        process.join(timeout=self.config.graceful_shutdown_s)
        graceful = not process.is_alive()
        terminated = False
        killed = False
        if process.is_alive():
            terminated = True
            process.terminate()
            process.join(timeout=1.0)
        if process.is_alive():
            killed = True
            process.kill()
            process.join(timeout=1.0)
        exit_code = process.exitcode
        self._close_ipc()
        self._process = None
        return ShutdownReport(graceful, terminated, killed, exit_code)

    def _close_ipc(self) -> None:
        if self._ipc is None:
            return
        for connection in (self._ipc.control_send, self._ipc.control_receive):
            try:
                connection.close()
            except OSError:
                pass
        try:
            self._ipc.status_queue.close()
            self._ipc.status_queue.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            pass
        self._ipc = None
