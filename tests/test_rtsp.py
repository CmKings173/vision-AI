import threading

from label_inspection.camera.rtsp import RTSPCamera


class FakeCapture:
    def __init__(self):
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        return True, [[1, 2, 3]]

    def release(self):
        self.released = True

    def set(self, property_id, value):
        return True


class FakeCV2:
    CAP_FFMPEG = 1900

    def __init__(self):
        self.capture = None

    def VideoCapture(self, url, backend):
        assert url == "rtsp://test"
        assert backend == self.CAP_FFMPEG
        self.capture = FakeCapture()
        return self.capture


def test_rtsp_reader_stamps_frames_and_closes_capture():
    camera = RTSPCamera("rtsp://test", reconnect_delay_s=0, max_frame_age_ms=100)
    fake_cv2 = FakeCV2()
    camera._cv2 = fake_cv2

    packet = camera.read()

    assert packet is not None
    assert packet.frame_id == 0
    assert packet.source == "rtsp"
    assert packet.captured_monotonic is not None
    assert camera.connected
    assert camera.health.frames_received == 1
    assert camera.health.last_frame_at == packet.captured_at
    assert camera.health.last_frame_timestamp == packet.captured_at
    assert camera.health.stale is False
    assert camera.has_fresh_frame(1000)
    assert camera.health_snapshot(
        now_monotonic=packet.captured_monotonic + 0.2
    ).stale is True

    camera.close()
    assert not camera.connected
    assert fake_cv2.capture.released


class TimeoutCV2(FakeCV2):
    CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    CAP_PROP_READ_TIMEOUT_MSEC = 54
    CAP_PROP_BUFFERSIZE = 38

    def __init__(self):
        super().__init__()
        self.params = None

    def VideoCapture(self, url, backend, params):
        self.params = params
        self.capture = FakeCapture()
        return self.capture


def test_rtsp_passes_timeout_parameters_when_backend_supports_them():
    camera = RTSPCamera("rtsp://test", open_timeout_ms=1234, read_timeout_ms=567)
    fake_cv2 = TimeoutCV2()
    camera._cv2 = fake_cv2

    assert camera.open()
    assert fake_cv2.params == [
        fake_cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        1234,
        fake_cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        567,
    ]
    camera.close()


class FailedReadCapture(FakeCapture):
    def read(self):
        return False, None


class ReconnectCV2(FakeCV2):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def VideoCapture(self, url, backend):
        self.calls += 1
        self.capture = FailedReadCapture() if self.calls == 1 else FakeCapture()
        return self.capture


def test_rtsp_exposes_reconnect_and_error_health():
    camera = RTSPCamera("rtsp://test", reconnect_delay_s=0)
    camera._cv2 = ReconnectCV2()

    assert camera.read() is None
    assert camera.health.last_error == "READ_FAILED"
    assert camera.read() is not None
    assert camera.health.reconnect_count == 1
    assert camera.health.frames_received == 1
    camera.close()


class BlockingOpenCV2(FakeCV2):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.resume = threading.Event()

    def VideoCapture(self, url, backend):
        self.started.set()
        self.resume.wait(1.0)
        self.capture = FakeCapture()
        return self.capture


class BlockingReadCapture(FakeCapture):
    def __init__(self):
        super().__init__()
        self.read_started = threading.Event()
        self.resume = threading.Event()
        self.release_completed = threading.Event()
        self.released_during_read = False

    def read(self):
        self.read_started.set()
        self.resume.wait(1.0)
        return True, [[1, 2, 3]]

    def release(self):
        if not self.resume.is_set():
            self.released_during_read = True
        super().release()
        self.release_completed.set()


class BlockingReadOpenCV2(FakeCV2):
    def VideoCapture(self, url, backend):
        self.capture = BlockingReadCapture()
        return self.capture


def test_close_during_blocking_open_cannot_reanimate_or_leak_capture():
    camera = RTSPCamera("rtsp://test", reconnect_delay_s=0)
    fake_cv2 = BlockingOpenCV2()
    camera._cv2 = fake_cv2
    opened = []
    thread = threading.Thread(target=lambda: opened.append(camera.open()))
    thread.start()
    assert fake_cv2.started.wait(0.2)

    camera.close()
    fake_cv2.resume.set()
    thread.join(0.5)

    assert opened == [False]
    assert fake_cv2.capture.released is True
    assert camera.connected is False


def test_close_during_read_defers_release_until_native_read_returns():
    camera = RTSPCamera("rtsp://test", reconnect_delay_s=0)
    fake_cv2 = BlockingReadOpenCV2()
    camera._cv2 = fake_cv2
    packets = []
    thread = threading.Thread(target=lambda: packets.append(camera.read()))
    thread.start()

    assert fake_cv2.capture.read_started.wait(0.2)
    camera.close()

    assert fake_cv2.capture.released is False
    fake_cv2.capture.resume.set()
    thread.join(0.5)

    assert packets[0] is not None
    assert fake_cv2.capture.release_completed.wait(0.5)
    assert fake_cv2.capture.released_during_read is False
    assert fake_cv2.capture.released is True
