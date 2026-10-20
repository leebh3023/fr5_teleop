from __future__ import annotations

import json
import queue
import time
from pathlib import Path

import pytest

from teleop.config import CameraConfig, RecordingConfig, TeleopConfig
from teleop.recording.camera import (
    CameraRecorder,
    CameraUnavailable,
    FakeCameraSource,
)
from teleop.recording.recorder import RecorderRuntime, RecorderSpec, RecorderState
from teleop.recording.schema import CAMERA_FRAME_LAYOUT

from tests.unit.test_recording import sample as robot_sample


av = pytest.importorskip("av")
numpy = pytest.importorskip("numpy")


def fake_camera(**overrides) -> CameraConfig:
    values = {
        "enabled": True,
        "source": "fake",
        "width": 64,
        "height": 48,
        "fps": 30,
    }
    values.update(overrides)
    return CameraConfig(**values)


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def decode_frames(path: Path, pixel_format: str) -> list:
    with av.open(str(path)) as container:
        return [
            frame.to_ndarray(format=pixel_format)
            for frame in container.decode(video=0)
        ]


@pytest.mark.parametrize("depth", [False, True])
def test_camera_episode_video_matches_frame_timestamps(
    tmp_path: Path, depth: bool
) -> None:
    config = fake_camera(depth=depth)
    camera = CameraRecorder(config, FakeCameraSource(config))
    camera.start()
    try:
        wait_until(lambda: camera.streaming)
        start_ns = time.monotonic_ns()
        camera.begin_episode(tmp_path, start_ns)
        time.sleep(0.4)
        stop_ns = time.monotonic_ns()
        camera.end_episode(stop_ns)
        wait_until(lambda: camera.caught_up(stop_ns))
        meta = camera.finish_episode()
    finally:
        camera.stop()

    assert meta["error"] is None
    assert meta["dropped_frames"] == 0
    # 0.4 s at 30 fps, allowing for scheduling slack.
    assert 8 <= meta["frames"] <= 14
    assert meta["timestamp_domain"] == "fake_monotonic"

    records = list(CAMERA_FRAME_LAYOUT.iter_records(tmp_path / meta["frames_file"]))
    assert [r["frame_index"] for r in records] == list(range(meta["frames"]))
    arrivals = [r["arrival_mono_ns"] for r in records]
    assert arrivals == sorted(arrivals)
    assert start_ns <= arrivals[0] and arrivals[-1] <= stop_ns
    numbers = [r["frame_number"] for r in records]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))

    color = decode_frames(tmp_path / meta["video"], "rgb24")
    assert len(color) == meta["frames"]
    assert color[0].shape == (48, 64, 3)

    if depth:
        depth_frames = decode_frames(tmp_path / meta["depth_video"], "gray16le")
        assert len(depth_frames) == meta["frames"]
        # FFV1 is lossless: the fake writes 500 + frame_number everywhere.
        for frame, number in zip(depth_frames, numbers):
            assert frame.dtype == numpy.uint16
            assert (frame == 500 + number).all()
    else:
        assert meta["depth_video"] is None


class BrokenSource(FakeCameraSource):
    def start(self):
        raise RuntimeError("No device connected")


def make_recorder(root: Path, camera: CameraConfig, source) -> RecorderRuntime:
    runtime = RecorderRuntime(
        RecorderSpec(
            root_dir=root,
            hands=("right",),
            session_metadata={},
            camera=camera,
        ),
        queue.Queue(),
        queue.Queue(),
    )
    runtime._prepare_root()
    runtime.camera = CameraRecorder(camera, source)
    runtime.camera.start()
    return runtime


def test_recording_refuses_to_start_without_camera_stream(tmp_path: Path) -> None:
    config = fake_camera()
    recorder = make_recorder(tmp_path, config, BrokenSource(config))
    try:
        wait_until(lambda: recorder.camera.last_error is not None)
        recorder._handle(("start", time.monotonic_ns(), "task"))
        assert recorder.state == RecorderState.IDLE
        assert "No device connected" in recorder.last_error
        assert not list(tmp_path.iterdir())
        status = recorder.camera.status()
        assert status["streaming"] is False
    finally:
        recorder.camera.stop()


def test_recorder_episode_includes_camera_metadata(tmp_path: Path) -> None:
    config = fake_camera()
    recorder = make_recorder(tmp_path, config, FakeCameraSource(config))
    try:
        wait_until(lambda: recorder.camera.streaming)
        start_ns = time.monotonic_ns()
        recorder._handle(("start", start_ns, "wipe the table"))
        time.sleep(0.2)
        stop_ns = time.monotonic_ns()
        recorder._handle(("samples", "right", 1, 0, 0, [robot_sample(stop_ns)]))
        recorder._handle(("stop", stop_ns, "success"))
        wait_until(lambda: recorder.camera.caught_up(stop_ns))
        recorder._handle(("tick",))
        assert recorder.state == RecorderState.IDLE
    finally:
        recorder.camera.stop()
    meta = json.loads((tmp_path / "episode_000000" / "meta.json").read_text())
    camera_meta = meta["cameras"]["wrist"]
    assert camera_meta["frames"] > 0
    assert camera_meta["video"] == "camera_wrist.mp4"
    assert camera_meta["frame_layout"]["record_size"] == CAMERA_FRAME_LAYOUT.size
    assert meta["complete"] is True
    assert recorder.last_saved["camera_frames"] == {"wrist": camera_meta["frames"]}


def test_camera_begin_episode_requires_streaming(tmp_path: Path) -> None:
    config = fake_camera()
    camera = CameraRecorder(config, FakeCameraSource(config))
    with pytest.raises(CameraUnavailable, match="not streaming"):
        camera.begin_episode(tmp_path, 0)


def test_camera_config_parsing_and_validation(tmp_path: Path) -> None:
    web = Path(__file__).resolve().parents[2] / "web"
    path = tmp_path / "config.yaml"
    path.write_text(
        "tls: {enabled: false}\n"
        f"server: {{web_dir: {web}}}\n"
        "recording:\n"
        "  enabled: true\n"
        "  camera: {enabled: true, source: fake, serial: 123456789012,\n"
        "           width: 1280, height: 720, fps: 15, depth: true}\n",
        encoding="utf-8",
    )
    camera = TeleopConfig.from_yaml(path).recording.camera
    assert camera.enabled and camera.depth
    assert camera.serial == "123456789012"
    assert (camera.width, camera.height, camera.fps) == (1280, 720, 15)

    with pytest.raises(ValueError, match="not a D435i color resolution"):
        CameraConfig(width=800, height=600).validate()
    with pytest.raises(ValueError, match="fps"):
        CameraConfig(fps=25).validate()
    with pytest.raises(ValueError, match="source"):
        CameraConfig(source="webcam").validate()
    with pytest.raises(ValueError, match="name"):
        CameraConfig(name="wrist cam").validate()
    # A disabled recording section never requires camera packages.
    RecordingConfig(enabled=False, camera=CameraConfig(enabled=True)).validate()


def test_camera_process_records_episode(tmp_path: Path) -> None:
    from teleop.recording.camera import CameraProcessHandle

    handle = CameraProcessHandle(fake_camera(depth=True))
    handle.start()
    try:
        wait_until(lambda: handle.streaming, timeout=10.0)
        start_ns = time.monotonic_ns()
        handle.begin_episode(tmp_path, start_ns)
        time.sleep(0.3)
        stop_ns = time.monotonic_ns()
        handle.end_episode(stop_ns)
        wait_until(lambda: handle.caught_up(stop_ns))
        meta = handle.finish_episode()
        assert handle.status()["streaming"] is True
    finally:
        handle.stop()
    assert meta["error"] is None
    assert meta["frames"] >= 5
    assert len(decode_frames(tmp_path / meta["video"], "rgb24")) == meta["frames"]
    assert len(decode_frames(tmp_path / meta["depth_video"], "gray16le")) == meta["frames"]
    assert handle.process is None


def test_realsense_without_device_reports_error_without_hanging(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyrealsense2")
    from teleop.recording.camera import CameraProcessHandle

    import pyrealsense2 as rs

    if len(rs.context().query_devices()) > 0:
        pytest.skip("a RealSense device is connected")
    handle = CameraProcessHandle(CameraConfig(enabled=True, source="realsense"))
    handle.start()
    try:
        wait_until(lambda: handle.last_error is not None, timeout=10.0)
        assert "no RealSense device connected" in handle.last_error
        with pytest.raises(CameraUnavailable, match="no RealSense device"):
            handle.begin_episode(tmp_path, time.monotonic_ns())
        started = time.monotonic()
    finally:
        handle.stop()
    assert time.monotonic() - started < 3.0
