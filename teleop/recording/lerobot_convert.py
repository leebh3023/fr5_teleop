"""Convert raw teleop episodes into a LeRobotDataset v3.

Run in the ``lerobot`` environment (lerobot 0.4.x, Python 3.10) from the
project root::

    python -m teleop.recording.lerobot_convert \\
        --raw recordings --out datasets/fr5_pick --repo-id local/fr5_pick

Only numpy (and PyAV for camera episodes) is needed until the dataset is
written, so planning and vector building are testable without lerobot. The
raw episodes are self-describing: the sample layout is read from meta.json.

Alignment: a uniform ``fps`` grid spans the episode (trimmed to the teleop
motion by default). Each grid time takes
  * the latest robot sample at or before it (zero-order hold: what a policy
    would have observed), and
  * the camera frame whose capture time (host arrival minus
    ``camera_latency_ms``) is nearest to it.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


log = logging.getLogger(__name__)

EPISODE_DIR = re.compile(r"^episode_(\d{6})$")
ROBOT_TYPE = "fairino_fr5"
NUMPY_CODES = {"q": "<i8", "i": "<i4", "B": "|u1", "d": "<f8"}
ROTATIONS = ("rot6d", "quat", "euler")
ACTION_MODES = ("command", "next_state")
# A robot sample older than this at a grid time means the stream stalled.
ROBOT_STALE_NS = 50_000_000


@dataclass(frozen=True)
class ConvertOptions:
    fps: int = 30
    rotation: str = "rot6d"
    action_mode: str = "command"
    camera_latency_ms: float = 0.0
    trim_idle: bool = True
    trim_pad_s: float = 0.25
    include_failures: bool = False
    allow_incomplete: bool = False
    # Skip an episode if more grid frames than this lack a camera frame.
    max_camera_gap_ratio: float = 0.05
    vcodec: str = "libsvtav1"

    def validate(self) -> None:
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if self.rotation not in ROTATIONS:
            raise ValueError(f"rotation must be one of {', '.join(ROTATIONS)}")
        if self.action_mode not in ACTION_MODES:
            raise ValueError(f"action_mode must be one of {', '.join(ACTION_MODES)}")
        if self.trim_pad_s < 0 or not 0 <= self.max_camera_gap_ratio <= 1:
            raise ValueError("trim_pad_s and max_camera_gap_ratio are out of range")


@dataclass
class EpisodePlan:
    """Which raw robot sample and camera frame feed each dataset frame."""

    source: str
    task: str
    grid_ns: np.ndarray
    robot_index: np.ndarray
    frame_index: np.ndarray | None
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodeData:
    plan: EpisodePlan
    state: np.ndarray
    action: np.ndarray
    joint_pos: np.ndarray
    video_path: Path | None


class SkipEpisode(Exception):
    """The episode is valid raw data but not suitable for this dataset."""


# ── rotation math (Fairino rx,ry,rz: fixed-axis XYZ, R = Rz·Ry·Rx) ──────
# Must match teleop.control_math.euler_deg_to_quat; a unit test checks this.


def euler_deg_to_matrix(euler_deg: np.ndarray) -> np.ndarray:
    rx, ry, rz = np.radians(euler_deg).T
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    matrix = np.empty(euler_deg.shape[:-1] + (3, 3))
    matrix[..., 0, 0] = cz * cy
    matrix[..., 0, 1] = cz * sy * sx - sz * cx
    matrix[..., 0, 2] = cz * sy * cx + sz * sx
    matrix[..., 1, 0] = sz * cy
    matrix[..., 1, 1] = sz * sy * sx + cz * cx
    matrix[..., 1, 2] = sz * sy * cx - cz * sx
    matrix[..., 2, 0] = -sy
    matrix[..., 2, 1] = cy * sx
    matrix[..., 2, 2] = cy * cx
    return matrix


def matrix_to_rot6d(matrix: np.ndarray) -> np.ndarray:
    """First two columns (Zhou et al. 2019), continuous unlike Euler/quat."""
    return np.concatenate([matrix[..., :, 0], matrix[..., :, 1]], axis=-1)


def matrix_to_quat_xyzw(matrix: np.ndarray) -> np.ndarray:
    """Shepperd's method, then sign-continuous along the first axis."""
    m = matrix
    trace = m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2]
    quat = np.empty((len(m), 4))
    for i in range(len(m)):
        r = m[i]
        if trace[i] > 0:
            s = math.sqrt(trace[i] + 1.0) * 2
            quat[i] = [
                (r[2, 1] - r[1, 2]) / s,
                (r[0, 2] - r[2, 0]) / s,
                (r[1, 0] - r[0, 1]) / s,
                0.25 * s,
            ]
        elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
            s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2
            quat[i] = [
                0.25 * s,
                (r[0, 1] + r[1, 0]) / s,
                (r[0, 2] + r[2, 0]) / s,
                (r[2, 1] - r[1, 2]) / s,
            ]
        elif r[1, 1] > r[2, 2]:
            s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2
            quat[i] = [
                (r[0, 1] + r[1, 0]) / s,
                0.25 * s,
                (r[1, 2] + r[2, 1]) / s,
                (r[0, 2] - r[2, 0]) / s,
            ]
        else:
            s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2
            quat[i] = [
                (r[0, 2] + r[2, 0]) / s,
                (r[1, 2] + r[2, 1]) / s,
                0.25 * s,
                (r[1, 0] - r[0, 1]) / s,
            ]
    quat /= np.linalg.norm(quat, axis=1, keepdims=True)
    if len(quat):
        if quat[0, 3] < 0:
            quat[0] = -quat[0]
        for i in range(1, len(quat)):
            if np.dot(quat[i], quat[i - 1]) < 0:
                quat[i] = -quat[i]
    return quat


def rotation_names(rotation: str) -> list[str]:
    if rotation == "rot6d":
        return [f"rot6d_{i}" for i in range(6)]
    if rotation == "quat":
        return ["qx", "qy", "qz", "qw"]
    return ["rx", "ry", "rz"]


def pose_vector(tcp: np.ndarray, rotation: str) -> np.ndarray:
    """TCP [x,y,z mm, rx,ry,rz deg] → [x,y,z m, rotation...]."""
    position = tcp[:, :3] / 1000.0
    if rotation == "euler":
        rot = np.radians(tcp[:, 3:6])
    else:
        matrix = euler_deg_to_matrix(tcp[:, 3:6])
        rot = (
            matrix_to_rot6d(matrix)
            if rotation == "rot6d"
            else matrix_to_quat_xyzw(matrix)
        )
    return np.concatenate([position, rot], axis=1)


# ── raw episode loading ─────────────────────────────────────────────


def layout_dtype(layout: dict[str, Any]) -> np.dtype:
    descr: list[tuple[Any, ...]] = []
    for item in layout["fields"]:
        code = NUMPY_CODES[item["code"]]
        if item["count"] > 1:
            descr.append((item["name"], code, (item["count"],)))
        else:
            descr.append((item["name"], code))
    dtype = np.dtype(descr)
    expected = layout.get("record_size", layout.get("sample_size"))
    if expected is not None and dtype.itemsize != expected:
        raise ValueError(f"layout size {dtype.itemsize} != recorded {expected}")
    return dtype


def read_records(path: Path, dtype: np.dtype) -> np.ndarray:
    data = path.read_bytes()
    usable = len(data) - len(data) % dtype.itemsize
    return np.frombuffer(data[:usable], dtype=dtype)


def find_episodes(raw_dir: Path) -> list[Path]:
    """Finalized episodes only; .tmp/.incomplete directories are ignored."""
    return sorted(
        entry
        for entry in raw_dir.iterdir()
        if entry.is_dir()
        and EPISODE_DIR.match(entry.name)
        and (entry / "meta.json").is_file()
    )


def single_hand(meta: dict[str, Any]) -> str:
    hands = list(meta["hands"])
    if len(hands) != 1:
        raise SkipEpisode(
            f"expected one arm, found {hands or 'none'} (bimanual is not supported yet)"
        )
    return hands[0]


def plan_episode(
    episode_dir: Path,
    meta: dict[str, Any],
    options: ConvertOptions,
) -> tuple[EpisodePlan, np.ndarray]:
    if meta.get("format") != "fr5_teleop_raw":
        raise SkipEpisode(f"unknown raw format {meta.get('format')!r}")
    outcome = meta.get("outcome")
    if outcome != "success" and not (
        options.include_failures and outcome in {"failure", "interrupted"}
    ):
        raise SkipEpisode(f"outcome is {outcome}")
    if not meta.get("complete") and not options.allow_incomplete:
        raise SkipEpisode("raw episode is marked incomplete")
    hand = single_hand(meta)
    robot = read_records(
        episode_dir / meta["hands"][hand]["file"],
        layout_dtype(meta["sample_schema"]),
    )
    if len(robot) < 2:
        raise SkipEpisode("fewer than two robot samples")
    robot_t = robot["t_ns"]

    t_lo, t_hi = int(robot_t[0]), int(robot_t[-1])
    if options.trim_idle:
        active = np.flatnonzero(robot["servo_active"] == 1)
        if len(active) == 0:
            raise SkipEpisode("no teleoperated motion (servo never active)")
        pad_ns = int(options.trim_pad_s * 1e9)
        t_lo = max(t_lo, int(robot_t[active[0]]) - pad_ns)
        t_hi = min(t_hi, int(robot_t[active[-1]]) + pad_ns)

    cameras = meta.get("cameras") or {}
    if len(cameras) > 1:
        raise SkipEpisode("more than one camera is not supported yet")
    frames = None
    camera_meta = None
    if cameras:
        camera_meta = next(iter(cameras.values()))
        frames = read_records(
            episode_dir / camera_meta["frames_file"],
            layout_dtype(camera_meta["frame_layout"]),
        )
        if len(frames) == 0:
            raise SkipEpisode("camera recorded no frames")
        capture_t = frames["arrival_mono_ns"] - int(options.camera_latency_ms * 1e6)
        t_lo = max(t_lo, int(capture_t[0]))
        t_hi = min(t_hi, int(capture_t[-1]))

    period_ns = 1e9 / options.fps
    count = int(math.floor((t_hi - t_lo) / period_ns)) + 1
    if t_hi <= t_lo or count < 2:
        raise SkipEpisode("episode is shorter than two dataset frames")
    grid = t_lo + np.round(np.arange(count) * period_ns).astype(np.int64)

    robot_index = np.searchsorted(robot_t, grid, side="right") - 1
    robot_age = grid - robot_t[robot_index]
    report: dict[str, Any] = {
        "frames": count,
        "duration_s": round(float(grid[-1] - grid[0]) / 1e9, 3),
        "robot_stale_frames": int((robot_age > ROBOT_STALE_NS).sum()),
        "robot_age_ms_max": round(float(robot_age.max()) / 1e6, 2),
        "trimmed_s": round(
            float((meta["stop_ns"] - meta["start_ns"]) - (grid[-1] - grid[0])) / 1e9, 3
        ),
    }

    frame_index = None
    if frames is not None:
        capture_t = frames["arrival_mono_ns"] - int(options.camera_latency_ms * 1e6)
        right = np.clip(np.searchsorted(capture_t, grid), 1, len(capture_t) - 1)
        left = right - 1
        use_right = np.abs(capture_t[right] - grid) < np.abs(grid - capture_t[left])
        frame_index = np.where(use_right, right, left)
        error_ns = np.abs(capture_t[frame_index] - grid)
        gaps = int((error_ns > 0.75 * period_ns).sum())
        numbers = frames["frame_number"]
        report.update(
            {
                "camera_error_ms_p99": round(float(np.percentile(error_ns, 99)) / 1e6, 2),
                "camera_gap_frames": gaps,
                "camera_repeated_frames": int((np.diff(frame_index) == 0).sum()),
                "camera_internal_drops": int(
                    np.clip(np.diff(numbers) - 1, 0, None).sum()
                ),
            }
        )
        if gaps > options.max_camera_gap_ratio * count:
            raise SkipEpisode(
                f"{gaps}/{count} frames have no camera frame within 0.75 period"
            )
    plan = EpisodePlan(
        source=episode_dir.name,
        task=meta["task"],
        grid_ns=grid,
        robot_index=robot_index,
        frame_index=frame_index,
        report=report,
    )
    return plan, robot


def build_episode(
    episode_dir: Path,
    options: ConvertOptions,
) -> EpisodeData:
    meta = json.loads((episode_dir / "meta.json").read_text(encoding="utf-8"))
    plan, robot = plan_episode(episode_dir, meta, options)
    hand = single_hand(meta)
    arm = (meta.get("session") or {}).get("arms", {}).get(hand, {})
    open_position = float(arm.get("gripper_open_position", 0))
    closed_position = float(arm.get("gripper_closed_position", 100))

    def gripper(values: np.ndarray) -> np.ndarray:
        span = closed_position - open_position
        return np.clip((values - open_position) / span, 0.0, 1.0)[:, None]

    rows = robot[plan.robot_index]
    tcp = rows["tcp"]
    state_pose = pose_vector(tcp, options.rotation)
    if options.rotation == "euler":
        # The controller reports rx,ry,rz in (-180, 180]; near rx = 180 deg
        # they jump by 360 deg (seen in field data). Unwrap along time.
        state_pose[:, 3:6] = np.unwrap(state_pose[:, 3:6], axis=0)
    state = np.concatenate([state_pose, gripper(rows["gripper_position"])], axis=1)
    if options.action_mode == "command":
        # Hold the measured pose while the servo is not commanding motion
        # (idle, SLEEPING between clutches, gripper action).
        command = np.where(rows["cmd_valid"][:, None] == 1, rows["cmd_tcp"], tcp)
        command_pose = pose_vector(command, options.rotation)
        if options.rotation == "euler":
            # Put each commanded angle within pi of the same frame's state.
            delta = command_pose[:, 3:6] - state_pose[:, 3:6]
            command_pose[:, 3:6] = (
                state_pose[:, 3:6] + (delta + np.pi) % (2 * np.pi) - np.pi
            )
        action = np.concatenate(
            [command_pose, gripper(rows["gripper_target"])], axis=1
        )
    else:
        action = np.concatenate([state[1:], state[-1:]], axis=0)
    joint_pos = np.radians(rows["joint_pos_deg"])

    for name, values in (("state", state), ("action", action), ("joint_pos", joint_pos)):
        if not np.isfinite(values).all():
            raise SkipEpisode(f"non-finite {name} values (robot state unavailable)")
    plan.report["servo_active_ratio"] = round(float(rows["servo_active"].mean()), 3)

    video_path = None
    cameras = meta.get("cameras") or {}
    if cameras:
        video_path = episode_dir / next(iter(cameras.values()))["video"]
    return EpisodeData(
        plan=plan,
        state=state.astype(np.float32),
        action=action.astype(np.float32),
        joint_pos=joint_pos.astype(np.float32),
        video_path=video_path,
    )


def iter_video_frames(path: Path, indices: np.ndarray) -> Iterator[np.ndarray]:
    """Yield RGB frames for non-decreasing ``indices`` with one decode pass."""
    import av

    with av.open(str(path)) as container:
        decoded = container.decode(video=0)
        current_index = -1
        current: np.ndarray | None = None
        for wanted in indices:
            while current_index < wanted:
                try:
                    frame = next(decoded)
                except StopIteration as exc:
                    raise SkipEpisode(
                        f"video has only {current_index + 1} frames, need {wanted + 1}"
                    ) from exc
                current_index += 1
                if current_index == wanted:
                    current = frame.to_ndarray(format="rgb24")
            assert current is not None
            yield current


def dataset_features(
    options: ConvertOptions,
    camera: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    pose_names = ["x", "y", "z", *rotation_names(options.rotation), "gripper"]
    features: dict[str, dict[str, Any]] = {
        "observation.state": {
            "dtype": "float32",
            "shape": (len(pose_names),),
            "names": pose_names,
        },
        "observation.joint_pos": {
            "dtype": "float32",
            "shape": (6,),
            "names": [f"joint_{i}" for i in range(1, 7)],
        },
        "action": {
            "dtype": "float32",
            "shape": (len(pose_names),),
            "names": pose_names,
        },
    }
    if camera is not None:
        features[f"observation.images.{camera['name']}"] = {
            "dtype": "video",
            "shape": (camera["height"], camera["width"], 3),
            "names": ["height", "width", "channels"],
        }
    return features


def convert(
    raw_dir: Path,
    out_dir: Path,
    repo_id: str,
    options: ConvertOptions,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a LeRobotDataset; returns the conversion report."""
    options.validate()
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    episodes = find_episodes(raw_dir)
    if not episodes:
        raise ValueError(f"no finalized episodes in {raw_dir}")
    if out_dir.exists():
        if not overwrite:
            raise ValueError(f"{out_dir} exists; pass --overwrite to replace it")
        shutil.rmtree(out_dir)

    built: list[EpisodeData] = []
    skipped: dict[str, str] = {}
    for episode_dir in episodes:
        try:
            built.append(build_episode(episode_dir, options))
        except SkipEpisode as exc:
            skipped[episode_dir.name] = str(exc)
            log.warning("skip %s: %s", episode_dir.name, exc)
    if not built:
        raise ValueError(f"every episode was skipped: {skipped}")

    cameras = [
        (json.loads((raw_dir / d.plan.source / "meta.json").read_text())["cameras"] or {})
        for d in built
    ]
    camera_specs = {
        (c["name"], c["width"], c["height"])
        for cams in cameras
        for c in cams.values()
    }
    if len(camera_specs) > 1 or (camera_specs and any(not c for c in cameras)):
        raise ValueError(
            "episodes mix camera setups; convert them into separate datasets: "
            f"{sorted(camera_specs)}"
        )
    camera = next(iter(cameras[0].values())) if cameras[0] else None

    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=options.fps,
        features=dataset_features(options, camera),
        root=out_dir,
        robot_type=ROBOT_TYPE,
        use_videos=True,
        vcodec=options.vcodec,
        streaming_encoding=camera is not None,
    )
    image_key = f"observation.images.{camera['name']}" if camera else None
    converted: list[dict[str, Any]] = []
    try:
        for index, data in enumerate(built):
            images = (
                iter_video_frames(data.video_path, data.plan.frame_index)
                if data.video_path is not None
                else None
            )
            for k in range(len(data.plan.grid_ns)):
                frame: dict[str, Any] = {
                    "observation.state": data.state[k],
                    "observation.joint_pos": data.joint_pos[k],
                    "action": data.action[k],
                    "task": data.plan.task,
                }
                if images is not None:
                    frame[image_key] = next(images)
                dataset.add_frame(frame)
            dataset.save_episode()
            converted.append(
                {"episode_index": index, "source": data.plan.source,
                 "task": data.plan.task, **data.plan.report}
            )
            log.info("converted %s -> episode %d (%d frames)",
                     data.plan.source, index, len(data.plan.grid_ns))
    finally:
        dataset.finalize()

    report = {
        "raw_dir": str(raw_dir.resolve()),
        "repo_id": repo_id,
        "options": asdict(options),
        "camera": camera["name"] if camera else None,
        "episodes": converted,
        "skipped": skipped,
    }
    (out_dir / "fr5_conversion.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True, help="raw recordings dir")
    parser.add_argument("--out", type=Path, required=True, help="dataset root to create")
    parser.add_argument("--repo-id", required=True, help="e.g. local/fr5_pick")
    parser.add_argument("--fps", type=int, default=ConvertOptions.fps)
    parser.add_argument("--rotation", choices=ROTATIONS, default=ConvertOptions.rotation)
    parser.add_argument("--action-mode", choices=ACTION_MODES, default=ConvertOptions.action_mode)
    parser.add_argument("--camera-latency-ms", type=float, default=0.0,
                        help="subtract from camera arrival time (measure in R5)")
    parser.add_argument("--no-trim", action="store_true",
                        help="keep idle time before the first and after the last clutch")
    parser.add_argument("--trim-pad-s", type=float, default=ConvertOptions.trim_pad_s)
    parser.add_argument("--include-failures", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--max-camera-gap-ratio", type=float,
                        default=ConvertOptions.max_camera_gap_ratio)
    parser.add_argument("--vcodec", default=ConvertOptions.vcodec)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    options = ConvertOptions(
        fps=args.fps,
        rotation=args.rotation,
        action_mode=args.action_mode,
        camera_latency_ms=args.camera_latency_ms,
        trim_idle=not args.no_trim,
        trim_pad_s=args.trim_pad_s,
        include_failures=args.include_failures,
        allow_incomplete=args.allow_incomplete,
        max_camera_gap_ratio=args.max_camera_gap_ratio,
        vcodec=args.vcodec,
    )
    report = convert(args.raw, args.out, args.repo_id, options, overwrite=args.overwrite)
    print(
        f"converted {len(report['episodes'])} episodes "
        f"({sum(e['frames'] for e in report['episodes'])} frames) into {args.out}; "
        f"skipped {len(report['skipped'])}"
    )
    for name, reason in report["skipped"].items():
        print(f"  skipped {name}: {reason}")


if __name__ == "__main__":
    main()
