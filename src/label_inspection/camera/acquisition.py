"""Independent camera acquisition into a bounded frame buffer."""

from __future__ import annotations

import threading
import time
from typing import Optional

from .base import CameraSource
from .frame_buffer import FrameBuffer


class CameraAcquisition:
    """Own all potentially blocking camera reads on one daemon thread.

    Controllers wait on state/buffer changes only; they never call camera
    reads themselves.
    """

    def __init__(
        self,
        camera: CameraSource,
        buffer: FrameBuffer,
        *,
        poll_interval_s: float = 0.01,
    ) -> None:
        self.camera = camera
        self.buffer = buffer
        self.poll_interval_s = max(0.001, poll_interval_s)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._max_frames: Optional[int] = None
        self._captured_count = 0
        self._last_error: Optional[str] = None

    @property
    def captured_count(self) -> int:
        with self._lock:
            return self._captured_count

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, *, max_frames: Optional[int] = None) -> None:
        if max_frames is not None and max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        if self.alive:
            raise RuntimeError("camera acquisition is already running")
        self._max_frames = max_frames
        with self._lock:
            self._captured_count = 0
            self._last_error = None
        self._stop.clear()
        self._done.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="vision-camera-acquisition",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout_s: float, *, stop_event=None) -> int:
        """Wait for completion/deadline without ever entering camera I/O."""

        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        deadline = time.monotonic() + timeout_s
        while not self._done.is_set():
            if stop_event is not None and stop_event.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._done.wait(min(remaining, self.poll_interval_s))
        return self.captured_count

    def stop(self, *, join_timeout_s: float = 0.05) -> bool:
        """Request shutdown and return whether the reader thread exited.

        camera.close is best-effort cancellation for native reads. OpenCV
        backend behavior must still be verified on the deployment machine.
        The bounded join ensures the controller itself does not wait forever.
        """

        self._stop.set()
        close = getattr(self.camera, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(max(0.0, join_timeout_s))
        return not self.alive

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._max_frames is not None and self.captured_count >= self._max_frames:
                    break
                try:
                    packet = self.camera.read()
                except Exception:
                    with self._lock:
                        self._last_error = "CAMERA_READ_ERROR"
                    self._stop.wait(self.poll_interval_s)
                    continue
                if self._stop.is_set():
                    break
                if packet is None:
                    self._stop.wait(self.poll_interval_s)
                    continue
                self.buffer.append(packet)
                with self._lock:
                    self._captured_count += 1
                    self._last_error = None
        finally:
            self._done.set()


def capture_into_buffer(
    camera: CameraSource,
    buffer: FrameBuffer,
    *,
    max_frames: int,
    timeout_s: float,
    stop_event=None,
) -> int:
    """Capture on a worker thread while the caller observes a hard deadline."""

    acquisition = CameraAcquisition(camera, buffer)
    acquisition.start(max_frames=max_frames)
    try:
        return acquisition.wait(timeout_s, stop_event=stop_event)
    finally:
        acquisition.stop()
