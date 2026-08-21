import threading
import time

from label_inspection.camera.acquisition import capture_into_buffer
from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.security import mask_url_credentials
from label_inspection.schemas import FramePacket


class FakeCamera:
    def __init__(self):
        self.next_id = 0

    def read(self):
        frame_id = self.next_id
        self.next_id += 1
        return FramePacket(
            frame_id,
            time.time(),
            f"frame-{frame_id}",
            captured_monotonic=time.monotonic(),
        )


class BlockingCamera:
    def __init__(self, delay_s=0.3):
        self.delay_s = delay_s
        self.read_started = threading.Event()
        self.read_thread = None
        self.closed = False

    def read(self):
        self.read_thread = threading.current_thread().name
        self.read_started.set()
        time.sleep(self.delay_s)
        return None

    def close(self):
        self.closed = True


def test_capture_count_is_independent_from_bounded_buffer_length():
    buffer = FrameBuffer(max_size=8)

    captured = capture_into_buffer(FakeCamera(), buffer, max_frames=30, timeout_s=1.0)

    assert captured == 30
    assert len(buffer) == 8
    assert [packet.frame_id for packet in buffer.snapshot()] == list(range(22, 30))


def test_blocking_camera_read_does_not_block_controller_deadline():
    camera = BlockingCamera(delay_s=0.3)
    started = time.monotonic()

    captured = capture_into_buffer(
        camera,
        FrameBuffer(max_size=2),
        max_frames=1,
        timeout_s=0.02,
    )
    elapsed = time.monotonic() - started

    assert camera.read_started.is_set()
    assert camera.read_thread == "vision-camera-acquisition"
    assert captured == 0
    assert camera.closed is True
    assert elapsed < 0.15


def test_rtsp_credentials_are_masked_without_mutating_connection_url():
    source = "rtsp://user:secret@host:8554/path?transport=tcp"
    masked = mask_url_credentials(source)

    assert masked == "rtsp://user:***@host:8554/path?transport=tcp"
    assert "secret" not in masked
    assert source.endswith("transport=tcp")
