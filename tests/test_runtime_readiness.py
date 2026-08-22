import importlib.util
import os
import types

import pytest

from label_inspection.runtime import collect_runtime_checks, package_check, python_supported


def test_runtime_check_reports_supported_range_without_hiding_current_state():
    checks = {check.name: check for check in collect_runtime_checks()}
    assert checks["Python"].status == ("PASS" if python_supported() else "FAIL")
    assert checks["OpenCV"].status == "PASS"
    assert checks["NumPy"].status == "PASS"


def test_python_version_gate_matches_project_metadata():
    assert not python_supported((3, 9, 6))
    assert python_supported((3, 10, 0))
    assert python_supported((3, 12, 9))
    assert not python_supported((3, 13, 0))


def test_actual_import_link_failure_is_reported_even_if_metadata_could_exist():
    def broken_import(module_name):
        raise OSError("shared library cannot be loaded")

    check = package_check("Paddle import", "paddle", importer=broken_import)

    assert check.status == "FAIL"
    assert "OSError" in check.value
    assert "shared library" in check.value


def test_paddle_gpu_readiness_is_independent_from_torch_cuda():
    paddle = types.SimpleNamespace(
        __version__="3.fake",
        device=types.SimpleNamespace(
            is_compiled_with_cuda=lambda: False,
            cuda=types.SimpleNamespace(device_count=lambda: 0),
            get_device=lambda: "cpu",
        ),
    )
    torch = types.SimpleNamespace(
        __version__="2.fake",
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            get_device_name=lambda index: "Fake CUDA GPU",
        ),
    )
    modules = {
        "cv2": types.SimpleNamespace(__version__="4.fake"),
        "numpy": types.SimpleNamespace(__version__="2.fake"),
        "paddle": paddle,
        "paddleocr": types.SimpleNamespace(__version__="3.fake"),
        "zxingcpp": types.SimpleNamespace(__version__="2.fake"),
        "torch": torch,
    }

    checks = {
        check.name: check
        for check in collect_runtime_checks(
            importer=modules.__getitem__,
            environ={
                "VISION_DETECTOR": "yolo",
                "VISION_DETECTOR_DEVICE": "cuda:0",
                "VISION_OCR_DEVICE": "gpu:0",
            },
        )
    }

    assert checks["Torch import"].status == "PASS"
    assert checks["Torch CUDA available"].status == "PASS"
    assert checks["Paddle import"].status == "PASS"
    assert checks["Paddle CUDA build"].status == "FAIL"
    assert checks["Paddle GPU available"].status == "FAIL"
    assert checks["Paddle device"].status == "FAIL"


def test_paddle_import_failure_marks_runtime_not_ready():
    modules = {
        "cv2": types.SimpleNamespace(__version__="4.fake"),
        "numpy": types.SimpleNamespace(__version__="2.fake"),
        "paddleocr": types.SimpleNamespace(__version__="3.fake"),
        "zxingcpp": types.SimpleNamespace(__version__="2.fake"),
    }

    def importer(module_name):
        if module_name == "paddle":
            raise ImportError("paddle binary unavailable")
        return modules[module_name]

    checks = {check.name: check for check in collect_runtime_checks(importer=importer)}

    assert checks["Paddle import"].status == "FAIL"
    assert checks["Paddle CUDA build"].status == "FAIL"
    assert "ImportError" in checks["Paddle import"].value


def test_tensorrt_runtime_checks_do_not_require_paddle(tmp_path):
    class FakeLogger:
        WARNING = 1

        def __init__(self, severity):
            self.severity = severity

    class FakeTensorRT:
        Logger = FakeLogger
        Builder = lambda logger: object()

    modules = {
        "cv2": types.SimpleNamespace(__version__="4.fake"),
        "numpy": types.SimpleNamespace(__version__="2.fake"),
        "zxingcpp": types.SimpleNamespace(__version__="2.fake"),
        "tensorrt": FakeTensorRT,
        "cuda": types.SimpleNamespace(),
    }
    det = tmp_path / "det.engine"
    rec = tmp_path / "rec.engine"
    keys = tmp_path / "keys.txt"
    det.write_bytes(b"engine")
    rec.write_bytes(b"engine")
    keys.write_text("A\nB\n", encoding="utf-8")

    checks = {
        check.name: check
        for check in collect_runtime_checks(
            importer=modules.__getitem__,
            environ={
                "VISION_OCR_ENGINE": "tensorrt",
                "VISION_OCR_DET_ENGINE": str(det),
                "VISION_OCR_REC_ENGINE": str(rec),
                "VISION_OCR_CHAR_DICT": str(keys),
            },
        )
    }

    assert checks["TensorRT import"].status == "PASS"
    assert checks["TensorRT builder"].status == "PASS"
    assert checks["Paddle import"].status == "INFO"
    assert checks["TensorRT det engine"].status == "PASS"


@pytest.mark.runtime
@pytest.mark.requires_paddle
@pytest.mark.skipif(
    importlib.util.find_spec("paddleocr") is None
    or importlib.util.find_spec("paddle") is None
    or os.getenv("VISION_RUN_RUNTIME_TESTS") != "1",
    reason="Paddle runtime missing or VISION_RUN_RUNTIME_TESTS is not enabled",
)
def test_real_ppocr_runtime_when_installed():
    from label_inspection.ocr.ppocr import PPOCRAdapter
    from tests.fixtures.quality import sharp_label

    result = PPOCRAdapter().recognize(sharp_label())
    assert result.success, result.error_code
    assert result.lines


@pytest.mark.runtime
@pytest.mark.requires_zxing
@pytest.mark.skipif(
    importlib.util.find_spec("zxingcpp") is None,
    reason="ZXing-C++ runtime not installed on current host",
)
def test_real_zxing_runtime_when_installed():
    import cv2

    if not hasattr(cv2, "QRCodeEncoder_create"):
        pytest.skip("OpenCV QR encoder is unavailable on this host")
    from label_inspection.barcode.zxing import ZXingBarcodeDecoder

    image = cv2.QRCodeEncoder_create().encode("ABC123")
    results = ZXingBarcodeDecoder(use_variants=False).decode(image)
    assert any(result.value == "ABC123" for result in results)


@pytest.mark.runtime
@pytest.mark.requires_rtsp
@pytest.mark.skipif(
    not os.getenv("VISION_RTSP_URL") or os.getenv("VISION_RUN_RUNTIME_TESTS") != "1",
    reason="VISION_RTSP_URL or VISION_RUN_RUNTIME_TESTS is not configured",
)
def test_real_rtsp_runtime_when_configured():
    from label_inspection.camera.acquisition import capture_into_buffer
    from label_inspection.camera.frame_buffer import FrameBuffer
    from label_inspection.camera.rtsp import RTSPCamera

    camera = RTSPCamera(os.environ["VISION_RTSP_URL"])
    buffer = FrameBuffer(max_size=1)
    captured = capture_into_buffer(camera, buffer, max_frames=1, timeout_s=5.0)
    assert captured == 1
    assert buffer.latest() is not None
