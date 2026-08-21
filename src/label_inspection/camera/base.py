"""Camera source contracts.

The inspection pipeline consumes ``FramePacket`` objects and therefore does
not depend on a particular camera SDK.  RTSP is the first implementation;
USB and GigE adapters can implement the same protocol later.
"""

from __future__ import annotations

from typing import Iterator, Optional, Protocol

from ..schemas import FramePacket


class CameraSource(Protocol):
    """Minimal source contract required by the frame acquisition loop."""

    def open(self) -> bool:
        """Open the source, returning whether it is ready for reads."""

    def read(self) -> Optional[FramePacket]:
        """Read one frame or return ``None`` when a reconnect is needed."""

    def frames(self, stop_event=None) -> Iterator[FramePacket]:
        """Yield frames until the optional stop event is set."""

    def close(self) -> None:
        """Release resources and stop future reads."""
