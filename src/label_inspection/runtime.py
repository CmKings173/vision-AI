"""Actual-import runtime readiness checks for local and GX10 diagnostics."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


MIN_PYTHON = (3, 10)
MAX_PYTHON_EXCLUSIVE = (3, 13)
Importer = Callable[[str], Any]


@dataclass(frozen=True)
class RuntimeCheck:
    name: str
    value: str
    status: str


def python_supported(version_info=None) -> bool:
    version = sys.version_info if version_info is None else version_info
    return MIN_PYTHON <= tuple(version[:2]) < MAX_PYTHON_EXCLUSIVE


def unsupported_python_message() -> Optional[str]:
    if python_supported():
        return None
    version = platform.python_version()
    return f"Unsupported Python {version}. Required >=3.10,<3.13. Recommended: Python 3.11."


def package_check(
    name: str,
    module: str,
    distribution: Optional[str] = None,
    *,
    importer: Optional[Importer] = None,
) -> RuntimeCheck:
    """Import the module first; package metadata alone never means ready."""

    check, _ = _import_module(
        name,
        module,
        distribution=distribution,
        importer=importer or importlib.import_module,
    )
    return check


def collect_runtime_checks(
    *,
    importer: Optional[Importer] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[RuntimeCheck]:
    loader = importer or importlib.import_module
    env = os.environ if environ is None else environ
    python_status = "PASS" if python_supported() else "FAIL"
    checks = [
        RuntimeCheck("Python", platform.python_version(), python_status),
        RuntimeCheck("Architecture", platform.machine(), "INFO"),
    ]

    opencv_check, _ = _import_module(
        "OpenCV", "cv2", distribution="opencv-python-headless", importer=loader
    )
    numpy_check, _ = _import_module("NumPy", "numpy", importer=loader)
    zxing_check, _ = _import_module(
        "ZXing-C++ import", "zxingcpp", distribution="zxing-cpp", importer=loader
    )
    checks.extend([opencv_check, numpy_check, zxing_check])

    ocr_engine = env.get("VISION_OCR_ENGINE", "ppocr").strip().lower().replace("_", "-")
    detector = env.get("VISION_DETECTOR", "FixedROI").strip().lower().replace("_", "-")
    if ocr_engine == "ppocr":
        paddle_check, paddle = _import_module(
            "Paddle import", "paddle", distribution="paddlepaddle", importer=loader
        )
        paddleocr_check, _ = _import_module(
            "PaddleOCR import", "paddleocr", importer=loader
        )
        checks.extend([paddle_check, paddleocr_check])
        checks.extend(_paddle_device_checks(paddle, env.get("VISION_OCR_DEVICE", "cpu")))
    elif ocr_engine == "ppocr-v6":
        paddleocr_check, _ = _import_module(
            "PaddleOCR import", "paddleocr", importer=loader
        )
        transformers_check, _ = _import_module(
            "Transformers import", "transformers", distribution="transformers", importer=loader
        )
        checks.extend(
            [
                RuntimeCheck("Paddle import", "not selected by Transformers backend", "INFO"),
                paddleocr_check,
                transformers_check,
            ]
        )
    elif ocr_engine in {"tensorrt", "tensor-rt"}:
        checks.extend(_tensorrt_checks(loader, env))
        checks.extend(
            [
                RuntimeCheck("Paddle import", "not selected by OCR engine", "INFO"),
                RuntimeCheck("PaddleOCR import", "not selected by OCR engine", "INFO"),
                RuntimeCheck("Paddle device", "not selected", "INFO"),
            ]
        )
    else:
        checks.extend(
            [
                RuntimeCheck("Paddle import", "unsupported OCR engine", "FAIL"),
                RuntimeCheck("PaddleOCR import", "unsupported OCR engine", "FAIL"),
                RuntimeCheck("Paddle device", "unsupported OCR engine", "FAIL"),
            ]
        )

    torch_selected = detector in {"ultralytics", "yolo"} or ocr_engine == "ppocr-v6"
    if torch_selected:
        torch_check, torch = _import_module(
            "Torch import", "torch", distribution="torch", importer=loader
        )
        checks.append(torch_check)
        requested_device = (
            env.get("VISION_OCR_DEVICE", "gpu:0")
            if ocr_engine == "ppocr-v6"
            else env.get("VISION_DETECTOR_DEVICE", "cpu")
        )
        checks.extend(_torch_device_checks(torch, requested_device))
    else:
        checks.extend(
            [
                RuntimeCheck("Torch import", "not selected by detector", "INFO"),
                RuntimeCheck("Torch CUDA available", "not checked", "INFO"),
                RuntimeCheck("Torch device", "not selected", "INFO"),
            ]
        )

    checks.append(
        RuntimeCheck(
            "RTSP source",
            "configured" if env.get("VISION_RTSP_URL") else "not configured",
            "INFO",
        )
    )
    return checks


def _tensorrt_checks(
    importer: Importer,
    environ: Mapping[str, str],
) -> list[RuntimeCheck]:
    checks: list[RuntimeCheck] = []
    tensorrt_check, tensorrt = _import_module(
        "TensorRT import", "tensorrt", importer=importer
    )
    cuda_check, _ = _import_module(
        "CUDA Python import", "cuda.bindings.runtime", importer=importer
    )
    if cuda_check.status != "PASS":
        legacy_cuda_check, _ = _import_module(
            "CUDA Python import", "cuda.cudart", importer=importer
        )
        if legacy_cuda_check.status == "PASS":
            cuda_check = legacy_cuda_check
    checks.extend([tensorrt_check, cuda_check])
    if tensorrt is None:
        checks.append(RuntimeCheck("TensorRT builder", "unavailable: import failed", "FAIL"))
    else:
        try:
            logger = tensorrt.Logger(tensorrt.Logger.WARNING)
            builder = tensorrt.Builder(logger)
            checks.append(
                RuntimeCheck(
                    "TensorRT builder",
                    "ready" if builder is not None else "unavailable",
                    "PASS" if builder is not None else "FAIL",
                )
            )
        except Exception as exc:
            checks.append(
                RuntimeCheck("TensorRT builder", _import_error(exc), "FAIL")
            )
    for name, variable in (
        ("TensorRT det engine", "VISION_OCR_DET_ENGINE"),
        ("TensorRT rec engine", "VISION_OCR_REC_ENGINE"),
        ("TensorRT char dict", "VISION_OCR_CHAR_DICT"),
    ):
        path = environ.get(variable, "").strip()
        if not path:
            checks.append(RuntimeCheck(name, f"not configured ({variable})", "FAIL"))
        else:
            checks.append(
                RuntimeCheck(name, path, "PASS" if Path(path).is_file() else "FAIL")
            )
    return checks


def _import_module(
    name: str,
    module_name: str,
    *,
    distribution: Optional[str] = None,
    importer: Importer,
) -> tuple[RuntimeCheck, Optional[Any]]:
    try:
        module = importer(module_name)
    except Exception as exc:
        return RuntimeCheck(name, _import_error(exc), "FAIL"), None
    version = getattr(module, "__version__", None)
    if not version:
        try:
            version = importlib.metadata.version(distribution or module_name)
        except (importlib.metadata.PackageNotFoundError, ValueError):
            version = "imported"
    return RuntimeCheck(name, str(version), "PASS"), module


def _paddle_device_checks(paddle: Optional[Any], requested_device: str) -> list[RuntimeCheck]:
    wants_gpu = _wants_gpu(requested_device)
    if paddle is None:
        return [
            RuntimeCheck("Paddle CUDA build", "unavailable: import failed", "FAIL"),
            RuntimeCheck("Paddle GPU available", "unavailable: import failed", "FAIL"),
            RuntimeCheck("Paddle device", "unavailable: import failed", "FAIL"),
        ]

    try:
        device_api = getattr(paddle, "device")
        compiled_fn = getattr(device_api, "is_compiled_with_cuda", None)
        if not callable(compiled_fn):
            compiled_fn = getattr(paddle, "is_compiled_with_cuda")
        compiled = bool(compiled_fn())
    except Exception as exc:
        message = f"check failed ({_error_name(exc)})"
        status = "FAIL" if wants_gpu else "INFO"
        return [
            RuntimeCheck("Paddle CUDA build", message, status),
            RuntimeCheck("Paddle GPU available", "unknown", status),
            RuntimeCheck("Paddle device", "unknown", "FAIL"),
        ]

    gpu_count = 0
    if compiled:
        try:
            gpu_count = int(device_api.cuda.device_count())
        except Exception:
            gpu_count = 0
    gpu_available = compiled and gpu_count > 0
    try:
        active_device = str(device_api.get_device())
        device_status = "PASS" if not wants_gpu or gpu_available else "FAIL"
    except Exception as exc:
        active_device = f"check failed ({_error_name(exc)})"
        device_status = "FAIL"

    optional_status = "PASS" if gpu_available else ("FAIL" if wants_gpu else "INFO")
    return [
        RuntimeCheck(
            "Paddle CUDA build",
            "yes" if compiled else "no",
            "PASS" if compiled else ("FAIL" if wants_gpu else "INFO"),
        ),
        RuntimeCheck(
            "Paddle GPU available",
            f"yes ({gpu_count})" if gpu_available else "no",
            optional_status,
        ),
        RuntimeCheck("Paddle device", active_device, device_status),
    ]


def _torch_device_checks(torch: Optional[Any], requested_device: str) -> list[RuntimeCheck]:
    wants_gpu = _wants_gpu(requested_device)
    if torch is None:
        return [
            RuntimeCheck("Torch CUDA available", "unavailable: import failed", "FAIL"),
            RuntimeCheck("Torch device", "unavailable: import failed", "FAIL"),
        ]
    try:
        available = bool(torch.cuda.is_available())
    except Exception as exc:
        return [
            RuntimeCheck("Torch CUDA available", f"check failed ({_error_name(exc)})", "FAIL"),
            RuntimeCheck("Torch device", "unknown", "FAIL"),
        ]
    cuda_status = "PASS" if available else ("FAIL" if wants_gpu else "INFO")
    if available:
        try:
            device = str(torch.cuda.get_device_name(0))
        except Exception as exc:
            device = f"check failed ({_error_name(exc)})"
            return [
                RuntimeCheck("Torch CUDA available", "yes", cuda_status),
                RuntimeCheck("Torch device", device, "FAIL"),
            ]
    else:
        device = "cpu"
    return [
        RuntimeCheck("Torch CUDA available", "yes" if available else "no", cuda_status),
        RuntimeCheck("Torch device", device, "PASS" if not wants_gpu or available else "FAIL"),
    ]


def _wants_gpu(device: str) -> bool:
    return device.strip().lower().startswith(("gpu", "cuda"))


def _import_error(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "no detail"
    return f"import failed ({_error_name(exc)}: {message[:160]})"


def _error_name(exc: Exception) -> str:
    return type(exc).__name__
