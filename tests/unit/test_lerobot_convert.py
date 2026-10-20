from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from teleop.control_math import euler_deg_to_quat
from teleop.recording.lerobot_convert import (
    ConvertOptions,
    SkipEpisode,
    build_episode,
    dataset_features,
    euler_deg_to_matrix,
    find_episodes,
    iter_video_frames,
    matrix_to_quat_xyzw,
    matrix_to_rot6d,
)
from teleop.recording.schema import (
    CAMERA_FRAME_LAYOUT,
    build_sample,
    pack_sample,
    schema_metadata,
)
from teleop.robot.state import WorkerState


ROOT = Path(__file__).resolve().parents[2]
PERIOD_NS = 8_000_000
T0 = 10_000_000_000


def robot_sample(
    i: int,
    *,
    active: bool,
    t_ns: int | None = None,
    state_valid: bool = True,
) -> tuple:
    x = 300.0 + i  # 1 mm per 8 ms tick
    return build_sample(
        t_ns=T0 + i * PERIOD_NS if t_ns is None else t_ns,
        tick=i,
        generation=1,
        state=WorkerState.ACTIVE if active else WorkerState.SLEEPING,
        fresh=True,
        servo_active=active,
        clutch_id=1,
        cmd_tcp=(x + 0.5, 0.0, 400.0, 180.0, 0.0, 90.0) if active else None,
        gripper_target=100.0 if i >= 50 else 0.0,
        gripper_busy=False,
        vr_valid=True,
        vr_seq=i,
        vr_received_ns=T0 + i * PERIOD_NS,
        vr_position_m=(0.0, 1.0, 0.0),
        vr_orientation_xyzw=(0.0, 0.0, 0.0, 1.0),
        grip=active,
        trigger=False,
        joint_pos_deg=(0.0, -90.0, 90.0, -90.0, 90.0, float(i)) if state_valid else None,
        joint_vel_deg_s=(0.0,) * 6 if state_valid else None,
        tcp=(x, 0.0, 400.0, 180.0, 0.0, 90.0) if state_valid else None,
        gripper_position=50.0 if state_valid else None,
        robot_frame_cnt=i % 256,
        robot_time_ms=i * 8,
    )


def write_episode(
    root: Path,
    index: int,
    *,
    samples: int = 125,
    active: range | None = None,
    camera_times_ns: list[int] | None = None,
    frame_numbers: list[int] | None = None,
    outcome: str = "success",
    complete: bool = True,
    invalid_states: range = range(0),
) -> Path:
    episode = root / f"episode_{index:06d}"
    episode.mkdir(parents=True)
    active = active if active is not None else range(samples)
    data = b"".join(
        pack_sample(
            robot_sample(i, active=i in active, state_valid=i not in invalid_states)
        )
        for i in range(samples)
    )
    (episode / "robot_right.bin").write_bytes(data)
    cameras = {}
    if camera_times_ns is not None:
        import av

        numbers = frame_numbers or list(range(len(camera_times_ns)))
        records = b"".join(
            CAMERA_FRAME_LAYOUT.pack((k, t, t, t / 1e6, numbers[k]))
            for k, t in enumerate(camera_times_ns)
        )
        (episode / "camera_wrist_frames.bin").write_bytes(records)
        with av.open(str(episode / "camera_wrist.mp4"), mode="w") as container:
            stream = container.add_stream("libx264", rate=30)
            stream.width, stream.height, stream.pix_fmt = 32, 24, "yuv420p"
            stream.options = {"crf": "0"}
            for k in range(len(camera_times_ns)):
                # Frame k is a flat gray of level 4 * k for identification.
                image = np.full((24, 32, 3), (4 * k) % 256, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                frame.pts = k
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        cameras["wrist"] = {
            "name": "wrist",
            "width": 32,
            "height": 24,
            "video": "camera_wrist.mp4",
            "frames_file": "camera_wrist_frames.bin",
            "frames": len(camera_times_ns),
            "frame_layout": CAMERA_FRAME_LAYOUT.metadata(),
        }
    meta = {
        "format": "fr5_teleop_raw",
        "format_version": 1,
        "episode_index": index,
        "task": f"task {index}",
        "outcome": outcome,
        "complete": complete,
        "start_ns": T0,
        "stop_ns": T0 + samples * PERIOD_NS,
        "hands": {"right": {"file": "robot_right.bin", "samples": samples}},
        "cameras": cameras,
        "sample_schema": schema_metadata(),
        "session": {
            "arms": {
                "right": {"gripper_open_position": 0, "gripper_closed_position": 100}
            }
        },
    }
    (episode / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return episode


def test_rotation_matches_teleop_euler_convention() -> None:
    rng = np.random.default_rng(0)
    for euler in rng.uniform(-179, 179, size=(50, 3)):
        matrix = euler_deg_to_matrix(euler[None, :])
        quat = matrix_to_quat_xyzw(matrix)[0]
        expected = np.array(euler_deg_to_quat(*euler))
        assert min(np.abs(quat - expected).max(), np.abs(quat + expected).max()) < 1e-9
        # rot6d is the first two columns of an orthonormal matrix.
        rot6d = matrix_to_rot6d(matrix)[0]
        assert abs(np.dot(rot6d[:3], rot6d[3:])) < 1e-9
        assert np.allclose(np.linalg.norm(rot6d[:3]), 1.0)


def test_quaternions_are_sign_continuous() -> None:
    # Rotating through 180 deg flips a canonical quaternion's sign.
    euler = np.array([[0.0, 0.0, angle] for angle in np.linspace(170, 190, 21)])
    quat = matrix_to_quat_xyzw(euler_deg_to_matrix(euler))
    assert (np.sum(quat[1:] * quat[:-1], axis=1) > 0).all()


def test_robot_only_episode_uses_zero_order_hold(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, 0, samples=125)
    data = build_episode(episode, ConvertOptions(trim_idle=False))
    plan = data.plan
    # 124 ticks * 8 ms = 0.992 s → 30 Hz grid of 30 frames.
    assert len(plan.grid_ns) == 30
    assert plan.frame_index is None and data.video_path is None
    # Latest sample at or before each grid time.
    robot_t = T0 + np.arange(125) * PERIOD_NS
    for k, t in enumerate(plan.grid_ns):
        i = plan.robot_index[k]
        assert robot_t[i] <= t < robot_t[i] + PERIOD_NS
    k = 10
    i = plan.robot_index[k]
    # state = [x m, y, z, rot6d, gripper]; rx=180, rz=90.
    assert data.state[k][0] == pytest.approx((300.0 + i) / 1000.0)
    assert data.state[k][2] == pytest.approx(0.4)
    assert np.allclose(data.state[k][3:9], [0, 1, 0, 1, 0, 0], atol=1e-6)
    assert data.state[k][9] == pytest.approx(0.5)
    # Command action is the sent ServoCart target, 0.5 mm ahead.
    assert data.action[k][0] == pytest.approx((300.5 + i) / 1000.0)
    assert data.joint_pos[k][5] == pytest.approx(math.radians(i))
    assert plan.report["robot_stale_frames"] == 0


def test_idle_edges_are_trimmed_and_idle_action_holds(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, 0, samples=250, active=range(60, 200))
    data = build_episode(episode, ConvertOptions(trim_pad_s=0.0))
    robot_index = data.plan.robot_index
    assert 59 <= robot_index[0] <= 60
    assert 196 <= robot_index[-1] <= 199
    untrimmed = build_episode(episode, ConvertOptions(trim_idle=False))
    idle = untrimmed.plan.robot_index < 60
    # Without a command the action holds the measured pose.
    assert np.allclose(untrimmed.action[idle, :9], untrimmed.state[idle, :9])
    # The gripper action is the commanded target, normalized to [0, 1].
    assert set(np.unique(untrimmed.action[:, 9])) == {0.0, 1.0}


def test_next_state_action_mode(tmp_path: Path) -> None:
    episode = write_episode(tmp_path, 0)
    data = build_episode(
        episode, ConvertOptions(trim_idle=False, action_mode="next_state")
    )
    assert np.array_equal(data.action[:-1], data.state[1:])
    assert np.array_equal(data.action[-1], data.state[-1])


@pytest.mark.parametrize(
    ("rotation", "dims"), [("rot6d", 10), ("quat", 8), ("euler", 7)]
)
def test_rotation_options_set_vector_size(
    tmp_path: Path, rotation: str, dims: int
) -> None:
    episode = write_episode(tmp_path, 0)
    data = build_episode(episode, ConvertOptions(trim_idle=False, rotation=rotation))
    assert data.state.shape[1] == dims == data.action.shape[1]
    features = dataset_features(ConvertOptions(rotation=rotation), None)
    assert features["observation.state"]["shape"] == (dims,)
    assert len(features["action"]["names"]) == dims


def test_camera_frames_are_matched_to_nearest_capture_time(tmp_path: Path) -> None:
    # 30 fps camera with jitter, starting 20 ms after the robot.
    rng = np.random.default_rng(1)
    times = [
        int(T0 + 20e6 + k * 1e9 / 30 + rng.uniform(-3e6, 3e6)) for k in range(28)
    ]
    episode = write_episode(tmp_path, 0, camera_times_ns=times)
    data = build_episode(episode, ConvertOptions(trim_idle=False))
    plan = data.plan
    assert plan.grid_ns[0] == times[0]
    capture = np.array(times)
    for k, t in enumerate(plan.grid_ns):
        assert np.abs(capture - t).argmin() == plan.frame_index[k]
    assert plan.report["camera_gap_frames"] == 0
    # Decoding yields exactly the planned frames (gray level 4 * index).
    levels = [
        int(round(image.mean() / 4))
        for image in iter_video_frames(data.video_path, plan.frame_index)
    ]
    assert levels == plan.frame_index.tolist()


def test_camera_latency_shifts_matching(tmp_path: Path) -> None:
    times = [int(T0 + 100e6 + k * 1e9 / 30) for k in range(20)]
    episode = write_episode(tmp_path, 0, camera_times_ns=times)
    plain = build_episode(episode, ConvertOptions(trim_idle=False))
    shifted = build_episode(
        episode, ConvertOptions(trim_idle=False, camera_latency_ms=40.0)
    )
    # Frames now count as captured 40 ms earlier: robot samples start 40 ms
    # earlier for the same frames.
    assert shifted.plan.grid_ns[0] == plain.plan.grid_ns[0] - 40_000_000
    assert shifted.plan.robot_index[0] == plain.plan.robot_index[0] - 5


def test_camera_gaps_skip_episode_and_count_internal_drops(tmp_path: Path) -> None:
    times = [int(T0 + k * 1e9 / 30) for k in range(10)]
    times += [int(T0 + k * 1e9 / 30) for k in range(20, 30)]  # 0.33 s hole
    numbers = list(range(10)) + list(range(20, 30))
    episode = write_episode(tmp_path, 0, camera_times_ns=times, frame_numbers=numbers)
    with pytest.raises(SkipEpisode, match="no camera frame"):
        build_episode(episode, ConvertOptions(trim_idle=False))
    data = build_episode(
        episode, ConvertOptions(trim_idle=False, max_camera_gap_ratio=1.0)
    )
    assert data.plan.report["camera_gap_frames"] > 0
    assert data.plan.report["camera_internal_drops"] == 10


def test_episode_filters(tmp_path: Path) -> None:
    failure = write_episode(tmp_path, 0, outcome="failure")
    with pytest.raises(SkipEpisode, match="outcome is failure"):
        build_episode(failure, ConvertOptions())
    assert build_episode(failure, ConvertOptions(include_failures=True))

    incomplete = write_episode(tmp_path, 1, complete=False)
    with pytest.raises(SkipEpisode, match="incomplete"):
        build_episode(incomplete, ConvertOptions())
    assert build_episode(incomplete, ConvertOptions(allow_incomplete=True))

    idle = write_episode(tmp_path, 2, active=range(0))
    with pytest.raises(SkipEpisode, match="servo never active"):
        build_episode(idle, ConvertOptions())

    # Longer than one 33 ms grid step so a dataset frame must use it.
    broken = write_episode(tmp_path, 3, invalid_states=range(40, 46))
    with pytest.raises(SkipEpisode, match="non-finite"):
        build_episode(broken, ConvertOptions(trim_idle=False))

    (tmp_path / "episode_000004.incomplete").mkdir()
    (tmp_path / "episode_000005.tmp").mkdir()
    assert [p.name for p in find_episodes(tmp_path)] == [
        f"episode_{i:06d}" for i in range(4)
    ]


@pytest.mark.skipif(
    not os.environ.get("LEROBOT_PYTHON"),
    reason="set LEROBOT_PYTHON to a Python with lerobot 0.4.x installed",
)
def test_convert_and_load_with_lerobot(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    times = [int(T0 + k * 1e9 / 30) for k in range(28)]
    write_episode(raw, 0, camera_times_ns=times)
    write_episode(raw, 1, camera_times_ns=times, outcome="failure")
    write_episode(raw, 2, camera_times_ns=times)
    out = tmp_path / "dataset"
    script = f"""
import json, sys
from pathlib import Path
from teleop.recording.lerobot_convert import main
main(["--raw", {str(raw)!r}, "--out", {str(out)!r}, "--repo-id", "local/test",
      "--no-trim", "--vcodec", "h264"])
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("local/test", root={str(out)!r}, video_backend="pyav")
sample = ds[3]
print("RESULT" + json.dumps({{
    "episodes": ds.num_episodes,
    "frames": ds.num_frames,
    "tasks": sorted(set(ds.meta.tasks.index)),
    "image": list(sample["observation.images.wrist"].shape),
    "state": len(sample["observation.state"]),
}}))
"""
    result = subprocess.run(
        [os.environ["LEROBOT_PYTHON"], "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT")]
    assert lines, result.stdout[-2000:] + result.stderr[-2000:]
    summary = json.loads(lines[0][len("RESULT"):])
    assert summary == {
        "episodes": 2,
        "frames": 56,
        "tasks": ["task 0", "task 2"],
        "image": [3, 24, 32],
        "state": 10,
    }
    report = json.loads((out / "fr5_conversion.json").read_text())
    assert report["skipped"] == {"episode_000001": "outcome is failure"}
