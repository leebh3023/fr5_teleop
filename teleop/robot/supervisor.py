from __future__ import annotations

import logging
import multiprocessing
import queue
import time
from dataclasses import dataclass, replace
from typing import Any

from teleop.config import ArmConfig, TeleopConfig
from teleop.ipc import WorkerIpc, create_worker_ipc
from teleop.protocol import PoseMessage
from teleop.robot.fake_client import FakeRobotBehavior
from teleop.robot.state import ControlCommand, WorkerState, WorkerStatus
from teleop.robot.worker import WorkerSpec, run_robot_worker


log = logging.getLogger(__name__)

# Sentinel hand name used when running in single-robot (legacy) mode.
SINGLE_ARM_HAND = "right"


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


@dataclass
class _ArmSlot:
    """Mutable runtime state for one arm worker."""
    hand: str
    config: TeleopConfig
    robot_kind: str
    fake_behavior: FakeRobotBehavior | None
    context: Any
    generation: int = 0
    process: Any = None
    ipc: WorkerIpc | None = None
    started_ns: int = 0
    latest_status: WorkerStatus | None = None
    last_grip: bool | None = None
    last_trigger: bool | None = None


class RobotSupervisor:
    """Manages one or more independent robot worker processes.

    In legacy (single-robot) mode a single worker is created using
    ``TeleopConfig.robot_ip``.  When ``TeleopConfig.arms`` is populated
    a separate worker is spawned per arm, each with its own IPC channel.
    """

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

        # Build arm slots ──────────────────────────────────
        self._slots: dict[str, _ArmSlot] = {}
        if config.arms:
            for arm_cfg in config.arms:
                # Build a per-arm TeleopConfig that carries the arm-specific
                # robot_ip, gripper, workspace, and exaxis_default.
                arm_teleop = replace(
                    config,
                    robot_ip=arm_cfg.robot_ip,
                    gripper=arm_cfg.gripper,
                    workspace=arm_cfg.workspace,
                    exaxis_default=arm_cfg.exaxis_default,
                )
                self._slots[arm_cfg.hand] = _ArmSlot(
                    hand=arm_cfg.hand,
                    config=arm_teleop,
                    robot_kind=self.robot_kind,
                    fake_behavior=self.fake_behavior,
                    context=self._context,
                )
        else:
            # Legacy single-robot mode
            self._slots[SINGLE_ARM_HAND] = _ArmSlot(
                hand=SINGLE_ARM_HAND,
                config=config,
                robot_kind=self.robot_kind,
                fake_behavior=self.fake_behavior,
                context=self._context,
            )

    # ── public properties ────────────────────────────────

    @property
    def hands(self) -> tuple[str, ...]:
        return tuple(self._slots)

    @property
    def is_bimanual(self) -> bool:
        return len(self._slots) > 1

    @property
    def generation(self) -> int:
        """Max generation across all slots."""
        return max((s.generation for s in self._slots.values()), default=0)

    @property
    def pid(self) -> int | None:
        """PID of the first (or only) worker — kept for legacy callers."""
        for slot in self._slots.values():
            if slot.process is not None:
                return slot.process.pid
        return None

    @property
    def latest_status(self) -> WorkerStatus | None:
        """Latest status from the first (or only) slot — legacy compat."""
        for slot in self._slots.values():
            return slot.latest_status
        return None

    @latest_status.setter
    def latest_status(self, value: WorkerStatus | None) -> None:
        for slot in self._slots.values():
            slot.latest_status = value
            break

    # ── lifecycle ─────────────────────────────────────────

    def start(self) -> None:
        for slot in self._slots.values():
            self._start_slot(slot)

    def _start_slot(self, slot: _ArmSlot) -> None:
        if slot.process is not None and slot.process.is_alive():
            raise RuntimeError(f"robot worker ({slot.hand}) is already running")
        slot.generation += 1
        slot.last_grip = None
        slot.last_trigger = None
        slot.ipc = create_worker_ipc(slot.context)
        spec = WorkerSpec(
            generation=slot.generation,
            config=slot.config,
            robot_kind=slot.robot_kind,
            fake_behavior=slot.fake_behavior,
        )
        slot.process = slot.context.Process(
            target=run_robot_worker,
            args=(
                spec,
                slot.ipc.mailbox,
                slot.ipc.control_receive,
                slot.ipc.status_queue,
                slot.ipc.heartbeat_ns,
            ),
            name=f"RobotWorker-{slot.hand}-{slot.generation}",
            daemon=False,
        )
        slot.started_ns = time.monotonic_ns()
        slot.process.start()
        slot.ipc.control_receive.close()
        log.info(
            "robot worker started hand=%s generation=%d pid=%s kind=%s ip=%s",
            slot.hand,
            slot.generation,
            slot.process.pid,
            slot.robot_kind,
            slot.config.robot_ip,
        )

    # ── pose / control ────────────────────────────────────

    def publish_pose(self, pose: PoseMessage) -> None:
        hand = self._resolve_hand(pose.hand)
        slot = self._slots.get(hand)
        if slot is None or slot.ipc is None:
            return
        slot.ipc.mailbox.publish(pose, slot.generation)
        if slot.last_grip is None or pose.grip != slot.last_grip:
            self._send_control_slot(
                slot,
                ControlCommand.GRIP_PRESSED if pose.grip else ControlCommand.GRIP_RELEASED,
            )
            slot.last_grip = pose.grip
        if slot.last_trigger is None or pose.trigger != slot.last_trigger:
            self._send_control_slot(
                slot,
                ControlCommand.TRIGGER_PRESSED if pose.trigger else ControlCommand.TRIGGER_RELEASED,
            )
            slot.last_trigger = pose.trigger

    def invalidate_pose(self, hand: str | None = None) -> None:
        for slot in self._iter_slots(hand):
            if slot.ipc is not None:
                slot.ipc.mailbox.invalidate()
            slot.last_grip = None
            slot.last_trigger = None
            log.info("latest pose invalidated hand=%s generation=%d", slot.hand, slot.generation)

    def send_control(self, command: ControlCommand, hand: str | None = None) -> None:
        for slot in self._iter_slots(hand):
            self._send_control_slot(slot, command)

    def _send_control_slot(self, slot: _ArmSlot, command: ControlCommand) -> None:
        if slot.ipc is None:
            return
        try:
            slot.ipc.control_send.send(command)
            log.info(
                "worker control sent hand=%s generation=%d command=%s",
                slot.hand,
                slot.generation,
                command.value,
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            log.warning(
                "worker control send failed hand=%s generation=%d command=%s error=%s",
                slot.hand,
                slot.generation,
                command.value,
                exc,
            )

    # ── status / health ───────────────────────────────────

    def drain_status(self, hand: str | None = None) -> dict[str, list[WorkerStatus]]:
        result: dict[str, list[WorkerStatus]] = {}
        for slot in self._iter_slots(hand):
            statuses: list[WorkerStatus] = []
            if slot.ipc is not None:
                while True:
                    try:
                        status = slot.ipc.status_queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(status, WorkerStatus):
                        slot.latest_status = status
                        statuses.append(status)
            result[slot.hand] = statuses
        return result

    def health(self, hand: str | None = None) -> dict[str, SupervisorHealth]:
        result: dict[str, SupervisorHealth] = {}
        for slot in self._iter_slots(hand):
            result[slot.hand] = self._slot_health(slot)
        return result

    def _slot_health(self, slot: _ArmSlot) -> SupervisorHealth:
        if slot.process is None or slot.ipc is None:
            return SupervisorHealth(False, None, False, slot.generation)
        alive = slot.process.is_alive()
        heartbeat = int(slot.ipc.heartbeat_ns.value)
        baseline = heartbeat or slot.started_ns
        age_ms = max(0.0, (time.monotonic_ns() - baseline) / 1_000_000)
        starting = (
            slot.latest_status is None
            or slot.latest_status.state == WorkerState.STARTING
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
            generation=slot.generation,
        )

    # ── shutdown ──────────────────────────────────────────

    def shutdown(self) -> dict[str, ShutdownReport]:
        reports: dict[str, ShutdownReport] = {}
        for slot in self._slots.values():
            reports[slot.hand] = self._shutdown_slot(slot)
        return reports

    def _shutdown_slot(self, slot: _ArmSlot) -> ShutdownReport:
        if slot.process is None:
            return ShutdownReport(True, False, False, None)
        process = slot.process
        self._send_control_slot(slot, ControlCommand.SHUTDOWN)
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
        self._close_slot_ipc(slot)
        slot.process = None
        return ShutdownReport(graceful, terminated, killed, exit_code)

    def _close_slot_ipc(self, slot: _ArmSlot) -> None:
        if slot.ipc is None:
            return
        for connection in (slot.ipc.control_send, slot.ipc.control_receive):
            try:
                connection.close()
            except OSError:
                pass
        try:
            slot.ipc.status_queue.close()
            slot.ipc.status_queue.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            pass
        slot.ipc = None

    # ── helpers ───────────────────────────────────────────

    def _resolve_hand(self, hand: str) -> str:
        """Map pose hand to a slot name. For single-robot mode any hand maps
        to the single slot."""
        if hand in self._slots:
            return hand
        if not self.is_bimanual:
            return SINGLE_ARM_HAND
        return hand

    def _iter_slots(self, hand: str | None = None):
        if hand is not None:
            slot = self._slots.get(hand)
            if slot is not None:
                yield slot
        else:
            yield from self._slots.values()
