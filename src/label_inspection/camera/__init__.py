"""Camera sources and bounded frame selection primitives."""

from .base import CameraSource
from .frame_buffer import FrameBuffer
from .selector import FrameSelector

__all__ = ["CameraSource", "FrameBuffer", "FrameSelector"]
