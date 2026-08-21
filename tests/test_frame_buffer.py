import threading
import time

import pytest

from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.schemas import FramePacket


def packet(frame_id: int, captured_at: float) -> FramePacket:
    return FramePacket(frame_id=frame_id, captured_at=captured_at, frame=f"frame-{frame_id}")


def test_buffer_is_bounded_and_keeps_newest_frames():
    buffer = FrameBuffer(max_size=2, window_ms=1000)
    for frame_id in range(3):
        buffer.append(packet(frame_id, 10.0 + frame_id * 0.1))

    assert len(buffer) == 2
    assert [item.frame_id for item in buffer.snapshot()] == [1, 2]


def test_snapshot_filters_stale_frames_without_mutating_buffer():
    buffer = FrameBuffer(max_size=4, window_ms=500)
    buffer.append(packet(1, 10.0))
    buffer.append(packet(2, 10.8))

    assert [item.frame_id for item in buffer.snapshot(now=11.0)] == [2]
    assert len(buffer) == 2


def test_snapshot_prefers_monotonic_age_when_available():
    buffer = FrameBuffer(max_size=4, window_ms=500)
    buffer.append(FramePacket(1, 9999.0, "old", captured_monotonic=10.0))
    buffer.append(FramePacket(2, 1.0, "fresh", captured_monotonic=10.8))

    assert [item.frame_id for item in buffer.snapshot(now=11.0, monotonic_now=11.0)] == [2]


def test_wait_for_frame_times_out_when_empty_and_wakes_on_append():
    buffer = FrameBuffer(max_size=2)
    assert buffer.wait_for_frame(timeout_s=0.001) is None

    thread = threading.Thread(target=lambda: (time.sleep(0.01), buffer.append(packet(9, time.time()))))
    thread.start()
    assert buffer.wait_for_frame(timeout_s=0.5).frame_id == 9
    thread.join()


def test_invalid_buffer_configuration_is_rejected():
    with pytest.raises(ValueError):
        FrameBuffer(max_size=0)
    with pytest.raises(ValueError):
        FrameBuffer(window_ms=-1)
