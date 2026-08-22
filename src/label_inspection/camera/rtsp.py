"""OpenCV-backed RTSP reader with reconnect and clean shutdown."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from ..schemas import FramePacket


@dataclass(frozen=True)
class RTSPHealth:
    connected: bool
    stale: bool
    frames_received: int
    last_frame_at: Optional[float]
    last_frame_monotonic: Optional[float]
    reconnect_count: int
    last_error: Optional[str]

    @property
    def last_frame_timestamp(self) -> Optional[float]:
        """Backward-compatible alias; health payloads use last_frame_at."""

        return self.last_frame_at


class RTSPCamera:
    """Read timestamped frames from an RTSP URL.

    OpenCV is imported only when ``open`` is called, so importing the V2
    package remains possible in a lightweight unit-test environment.
    """

    def __init__(
        self,
        url: str,
        *,
        backend: str = "ffmpeg",
        reconnect_delay_s: float = 0.5,
        max_reconnect_delay_s: float = 5.0,
        open_timeout_ms: int = 5000,
        read_timeout_ms: int = 2000,
        max_frame_age_ms: int = 1000,
    ) -> None:
        self.url = url
        self.backend = backend.lower()
        self.reconnect_delay_s = max(0.0, reconnect_delay_s)
        self.max_reconnect_delay_s = max(self.reconnect_delay_s, max_reconnect_delay_s)
        self.open_timeout_ms = max(1, open_timeout_ms)
        self.read_timeout_ms = max(1, read_timeout_ms)
        self.max_frame_age_ms = max(1, max_frame_age_ms)
        self._capture = None
        self._cv2 = None
        self._frame_id = 0
        self._closed = False
        self._next_retry_at = 0.0
        self._current_delay = self.reconnect_delay_s
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._deferred_disconnect_started = False
        self._close_complete = threading.Event()
        self._close_complete.set()
        self._open_attempts = 0
        self._frames_received = 0
        self._last_frame_timestamp: Optional[float] = None
        self._last_frame_monotonic: Optional[float] = None
        self._reconnect_count = 0
        self._last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        with self._lock:
            try:
                return bool(self._capture is not None and self._capture.isOpened())
            except Exception:
                return False

    @property
    def health(self) -> RTSPHealth:
        return self.health_snapshot()

    def health_snapshot(self, *, now_monotonic: Optional[float] = None) -> RTSPHealth:
        current = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            stale = (
                self._last_frame_monotonic is None
                or (current - self._last_frame_monotonic) * 1000.0 > self.max_frame_age_ms
            )
            return RTSPHealth(
                connected=self._capture is not None and self._safe_is_opened(self._capture),
                stale=stale,
                frames_received=self._frames_received,
                last_frame_at=self._last_frame_timestamp,
                last_frame_monotonic=self._last_frame_monotonic,
                reconnect_count=self._reconnect_count,
                last_error=self._last_error,
            )

    @staticmethod
    def _safe_is_opened(capture) -> bool:
        try:
            return bool(capture.isOpened())
        except Exception:
            return False

    def has_fresh_frame(self, max_age_ms: int, *, now_monotonic: Optional[float] = None) -> bool:
        health = self.health_snapshot(now_monotonic=now_monotonic)
        if health.last_frame_monotonic is None:
            return False
        current = time.monotonic() if now_monotonic is None else now_monotonic
        return (current - health.last_frame_monotonic) * 1000.0 <= max_age_ms

    def _load_cv2(self):
        if self._cv2 is None:
            try:
                import cv2
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "RTSPCamera requires opencv-python-headless; install the base V2 dependencies"
                ) from exc
            self._cv2 = cv2
        return self._cv2

    def open(self) -> bool:
        if self._closed:
            return False
        now = time.monotonic()
        if now < self._next_retry_at:
            return False

        cv2 = self._load_cv2()
        capture = None
        self._open_attempts += 1
        if self._open_attempts > 1:
            self._reconnect_count += 1
        try:
            backend = (
                cv2.CAP_FFMPEG
                if self.backend == "ffmpeg" and hasattr(cv2, "CAP_FFMPEG")
                else None
            )
            params: list[int] = []
            if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                params.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms])
            if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                params.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms])
            if self.backend == "ffmpeg" and hasattr(cv2, "CAP_FFMPEG"):
                try:
                    capture = (
                        cv2.VideoCapture(self.url, backend, params)
                        if params
                        else cv2.VideoCapture(self.url, backend)
                    )
                except TypeError:
                    capture = cv2.VideoCapture(self.url, backend)
            else:
                capture = cv2.VideoCapture(self.url)
            self._set_capture_property(capture, cv2, "CAP_PROP_BUFFERSIZE", 1)
            self._set_capture_property(
                capture, cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", self.open_timeout_ms
            )
            self._set_capture_property(
                capture, cv2, "CAP_PROP_READ_TIMEOUT_MSEC", self.read_timeout_ms
            )
            opened = self._safe_is_opened(capture)
        except Exception:
            opened = False

        release_capture = None
        with self._lock:
            if opened and not self._closed:
                self._capture = capture
                self._current_delay = self.reconnect_delay_s
                self._next_retry_at = 0.0
                self._last_error = None
                return True
            if capture is not None:
                release_capture = capture
            self._capture = None
            if self._closed:
                self._last_error = "CLOSED"
            else:
                self._last_error = "OPEN_FAILED"
                self._next_retry_at = now + self._current_delay
                self._current_delay = min(
                    max(self.reconnect_delay_s, self._current_delay * 2 or 0.5),
                    self.max_reconnect_delay_s,
                )
        if release_capture is not None:
            try:
                release_capture.release()
            except Exception:
                pass
        return False

    @staticmethod
    def _set_capture_property(capture, cv2, name: str, value: int) -> None:
        if capture is None or not hasattr(cv2, name):
            return
        try:
            capture.set(getattr(cv2, name), value)
        except Exception:
            pass

    @staticmethod
    def _release_capture(capture) -> None:
        try:
            capture.release()
        except Exception:
            pass

    def _detach_capture(self):
        with self._lock:
            capture, self._capture = self._capture, None
        return capture

    def _disconnect_now(self) -> None:
        capture = self._detach_capture()
        if capture is not None:
            self._release_capture(capture)

    def _deferred_disconnect(self) -> None:
        try:
            with self._read_lock:
                self._disconnect_now()
        finally:
            with self._lock:
                self._deferred_disconnect_started = False
            self._close_complete.set()

    def _disconnect(self) -> None:
        if self._read_lock.acquire(blocking=False):
            try:
                self._disconnect_now()
            finally:
                self._read_lock.release()
            with self._lock:
                deferred_disconnect_started = self._deferred_disconnect_started
            if not deferred_disconnect_started:
                self._close_complete.set()
            return
        with self._lock:
            if self._deferred_disconnect_started:
                return
            self._deferred_disconnect_started = True
            self._close_complete.clear()
        threading.Thread(
            target=self._deferred_disconnect,
            name="vision-camera-deferred-close",
            daemon=True,
        ).start()

    def read(self) -> Optional[FramePacket]:
        if self._closed:
            return None
        if not self.connected and not self.open():
            return None

        with self._read_lock:
            if self._closed:
                return None
            with self._lock:
                capture = self._capture
            if capture is None:
                return None

            try:
                ok, frame = capture.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                with self._lock:
                    self._last_error = "READ_FAILED"
                detached = self._detach_capture()
                if detached is not None:
                    self._release_capture(detached)
                return None

            captured_at = time.time()
            captured_monotonic = time.monotonic()
            packet = FramePacket(
                frame_id=self._frame_id,
                captured_at=captured_at,
                frame=frame,
                source="rtsp",
                captured_monotonic=captured_monotonic,
            )
            with self._lock:
                self._frame_id += 1
                self._frames_received += 1
                self._last_frame_timestamp = captured_at
                self._last_frame_monotonic = captured_monotonic
                self._last_error = None
            return packet

    def frames(self, stop_event=None) -> Iterator[FramePacket]:
        while not self._closed and not (stop_event and stop_event.is_set()):
            packet = self.read()
            if packet is not None:
                yield packet
                continue
            if stop_event is not None:
                stop_event.wait(min(max(self._current_delay, 0.01), 1.0))
            else:
                time.sleep(min(max(self._current_delay, 0.01), 1.0))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._disconnect()

    def wait_closed(self, *, timeout_s: float = 0.0) -> bool:
        """Wait until any deferred native capture release has completed.

        ``close`` stays bounded so it can be called while an OpenCV read is
        blocked.  Callers that are about to terminate a process should use
        this method after stopping the acquisition worker, otherwise a
        daemon cleanup thread may still be inside FFmpeg teardown.
        """

        if timeout_s < 0:
            raise ValueError("timeout_s must be >= 0")
        with self._lock:
            if not self._closed:
                return False
        return self._close_complete.wait(timeout_s)
