import numpy as np

from label_inspection.camera.selector import FrameSelector
from label_inspection.schemas import FramePacket


def test_selector_returns_top_k_by_quality_and_preserves_frame_identity():
    packets = [
        FramePacket(frame_id=1, captured_at=99.0, frame="low"),
        FramePacket(frame_id=2, captured_at=99.1, frame="high"),
        FramePacket(frame_id=3, captured_at=99.2, frame="medium"),
    ]
    selector = FrameSelector(top_k=2, score_fn={"low": 0.1, "high": 0.9, "medium": 0.5}.get)

    selected = selector.select(packets, now=100.0)

    assert [packet.frame_id for packet in selected] == [2, 3]


def test_selector_empty_input_is_safe():
    assert FrameSelector(top_k=1).select([]) == []


def test_selector_rejects_stale_frames_before_global_ranking():
    selector = FrameSelector(top_k=2, score_fn=lambda frame: 1.0, max_frame_age_ms=500)
    packets = [
        FramePacket(1, 99.0, "stale"),
        FramePacket(2, 99.8, "fresh"),
    ]

    assert [packet.frame_id for packet in selector.select(packets, now=100.0)] == [2]


def test_downsampled_selector_keeps_deterministic_quality_order_and_originals():
    height, width = 720, 1280
    yy, xx = np.indices((height, width))
    checker = ((((xx // 16) + (yy // 16)) % 2) * 255).astype(np.uint8)
    sharp = np.repeat(checker[:, :, None], 3, axis=2)
    flat = np.full((height, width, 3), 128, dtype=np.uint8)
    dark = np.zeros((height, width, 3), dtype=np.uint8)
    originals = [frame.copy() for frame in (flat, sharp, dark)]
    packets = [
        FramePacket(1, 10.0, flat),
        FramePacket(2, 10.0, sharp),
        FramePacket(3, 10.0, dark),
    ]

    selected = FrameSelector(top_k=3, preview_long_edge=320).select(packets, now=10.0)

    assert [packet.frame_id for packet in selected] == [2, 1, 3]
    for packet, original in zip(packets, originals):
        assert np.array_equal(packet.frame, original)
