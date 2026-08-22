"""Resident PP-OCR inference through native TensorRT.

The adapter intentionally accepts serialized TensorRT engines instead of
loading Paddle at runtime.  Engine building is kept in
``scripts/build_tensorrt_engine.py`` because the engine is hardware-specific
and must be built for the target GPU.

The expected engine contract is the standard PP-OCR split model:

* detection: one image tensor in, one DB probability map out;
* recognition: one normalized text crop in, CTC logits out;
* optional classification: one crop in, two-class angle logits out.

The runner uses TensorRT's modern tensor API and cuda-python for device
memory.  It loads each engine once and returns the same ``RawOCRResult``
contract as the PaddleOCR adapter.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from ..schemas import OCRLine, RawOCRResult


RunnerFactory = Callable[[str], "TensorRTEngineRunner"]


class TensorRTEngineRunner:
    """Small TensorRT engine runner with per-call CUDA buffers."""

    def __init__(self, engine_path: str) -> None:
        self.engine_path = str(engine_path)
        self._trt: Any = None
        self._cudart: Any = None
        self._runtime: Any = None
        self._engine: Any = None
        self._context: Any = None
        self._stream: Any = None
        self._input_names: list[str] = []
        self._output_names: list[str] = []
        self._load()

    def _load(self) -> None:
        if not Path(self.engine_path).is_file():
            raise FileNotFoundError(f"TensorRT engine does not exist: {self.engine_path}")
        try:
            import tensorrt as trt
        except Exception as exc:  # pragma: no cover - target-runtime branch
            raise RuntimeError("TENSORRT_DEPENDENCY_MISSING") from exc
        try:
            # cuda-python 13.x exposes the low-level runtime as
            # ``cuda.bindings.runtime``. Older releases exposed the same API
            # as ``cuda.cudart``; keep both layouts working for GX10 images.
            from cuda.bindings import runtime as cudart
        except Exception:
            try:
                from cuda import cudart  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - target-runtime branch
                raise RuntimeError("CUDA_PYTHON_DEPENDENCY_MISSING") from exc

        self._trt = trt
        self._cudart = cudart
        logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(logger)
        with open(self.engine_path, "rb") as handle:
            serialized = handle.read()
        self._engine = self._runtime.deserialize_cuda_engine(serialized)
        if self._engine is None:
            raise RuntimeError("TENSORRT_ENGINE_DESERIALIZE_FAILED")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise RuntimeError("TENSORRT_CONTEXT_CREATE_FAILED")
        self._input_names = []
        self._output_names = []
        for index in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(index)
            mode = self._engine.get_tensor_mode(name)
            if mode == trt.TensorIOMode.INPUT:
                self._input_names.append(name)
            else:
                self._output_names.append(name)
        if len(self._input_names) != 1 or not self._output_names:
            raise RuntimeError("TENSORRT_ENGINE_IO_CONTRACT_INVALID")
        error, stream = cudart.cudaStreamCreate()
        _check_cuda(error)
        self._stream = stream

    def infer(self, image: np.ndarray) -> list[np.ndarray]:
        if self._context is None or self._engine is None or self._stream is None:
            raise RuntimeError("TENSORRT_RUNNER_NOT_READY")
        tensor = np.ascontiguousarray(image)
        input_name = self._input_names[0]
        if not self._context.set_input_shape(input_name, tensor.shape):
            raise RuntimeError("TENSORRT_INPUT_SHAPE_REJECTED")

        device_buffers: list[int] = []
        host_outputs: list[np.ndarray] = []
        try:
            input_ptr = _cuda_malloc(self._cudart, tensor.nbytes)
            device_buffers.append(input_ptr)
            self._context.set_tensor_address(input_name, input_ptr)
            _cuda_memcpy_async(
                self._cudart,
                input_ptr,
                tensor.ctypes.data,
                tensor.nbytes,
                self._stream,
                host_to_device=True,
            )

            for name in self._output_names:
                shape = tuple(int(value) for value in self._context.get_tensor_shape(name))
                if not shape or any(value < 0 for value in shape):
                    raise RuntimeError("TENSORRT_OUTPUT_SHAPE_UNRESOLVED")
                dtype = self._trt.nptype(self._engine.get_tensor_dtype(name))
                output = np.empty(shape, dtype=dtype)
                output_ptr = _cuda_malloc(self._cudart, output.nbytes)
                device_buffers.append(output_ptr)
                self._context.set_tensor_address(name, output_ptr)
                host_outputs.append(output)

            if not self._context.execute_async_v3(self._stream):
                raise RuntimeError("TENSORRT_EXECUTION_FAILED")
            for name, output, output_ptr in zip(
                self._output_names, host_outputs, device_buffers[1:]
            ):
                del name
                _cuda_memcpy_async(
                    self._cudart,
                    output.ctypes.data,
                    output_ptr,
                    output.nbytes,
                    self._stream,
                    host_to_device=False,
                )
            _check_cuda(self._cudart.cudaStreamSynchronize(self._stream)[0])
            return host_outputs
        finally:
            for pointer in device_buffers:
                try:
                    _check_cuda(self._cudart.cudaFree(pointer))
                except Exception:
                    pass

    def close(self) -> None:
        if self._stream is not None and self._cudart is not None:
            try:
                _check_cuda(self._cudart.cudaStreamDestroy(self._stream))
            except Exception:
                pass
        self._stream = None
        self._context = None
        self._engine = None
        self._runtime = None

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        self.close()


class TensorRTOCRAdapter:
    """PP-OCR det/rec/(optional)cls adapter backed by resident TRT engines."""

    engine = "tensorrt-ppocr"

    def __init__(
        self,
        *,
        det_engine: str,
        rec_engine: str,
        char_dict: str,
        cls_engine: Optional[str] = None,
        det_input_size: tuple[int, int] = (960, 960),
        rec_input_size: tuple[int, int] = (48, 320),
        det_threshold: float = 0.30,
        det_box_threshold: float = 0.60,
        det_min_box_size: int = 3,
        runner_factory: RunnerFactory = TensorRTEngineRunner,
    ) -> None:
        self.det_engine = det_engine
        self.rec_engine = rec_engine
        self.cls_engine = cls_engine
        self.char_dict_path = char_dict
        self.det_input_size = det_input_size
        self.rec_input_size = rec_input_size
        self.det_threshold = det_threshold
        self.det_box_threshold = det_box_threshold
        self.det_min_box_size = det_min_box_size
        self._runner_factory = runner_factory
        self._detector: Optional[TensorRTEngineRunner] = None
        self._recognizer: Optional[TensorRTEngineRunner] = None
        self._classifier: Optional[TensorRTEngineRunner] = None
        self._characters: Optional[list[str]] = None
        self._load_error: Optional[str] = None

    def _load(self) -> bool:
        if self._detector is not None and self._recognizer is not None:
            return True
        if self._load_error is not None:
            return False
        try:
            self._characters = _read_character_dict(self.char_dict_path)
            self._detector = self._runner_factory(self.det_engine)
            self._recognizer = self._runner_factory(self.rec_engine)
            if self.cls_engine:
                self._classifier = self._runner_factory(self.cls_engine)
            return True
        except FileNotFoundError:
            self._load_error = "TENSORRT_MODEL_MISSING"
        except RuntimeError as exc:
            code = str(exc).split(":", 1)[0]
            self._load_error = code if code.startswith(("TENSORRT_", "CUDA_")) else "TENSORRT_LOAD_ERROR"
        except Exception:
            self._load_error = "TENSORRT_LOAD_ERROR"
        self._detector = None
        self._recognizer = None
        self._classifier = None
        return False

    def recognize(self, image: object) -> RawOCRResult:
        started = time.perf_counter()
        if not self._load():
            return _failure(
                self._load_error or "TENSORRT_NOT_AVAILABLE",
                (time.perf_counter() - started) * 1000,
            )
        try:
            import cv2

            source = np.asarray(image)
            if source.ndim != 3 or source.shape[2] != 3:
                raise ValueError("OCR image must be an HxWx3 image")
            boxes = self._detect(source)
            lines: list[OCRLine] = []
            for polygon in boxes:
                crop = _perspective_crop(source, polygon)
                if crop is None or crop.size == 0:
                    continue
                if self._classifier is not None and _should_rotate(crop, self._classifier, self.rec_input_size):
                    crop = cv2.rotate(crop, cv2.ROTATE_180)
                tensor = _prepare_recognition(crop, self.rec_input_size)
                outputs = self._recognizer.infer(tensor)  # type: ignore[union-attr]
                text, confidence = decode_ctc(outputs[0], self._characters or [])
                if text.strip():
                    lines.append(
                        OCRLine(
                            text=text,
                            confidence=confidence,
                            polygon=[[float(x), float(y)] for x, y in polygon],
                        )
                    )
            return RawOCRResult(
                engine=self.engine,
                lines=lines,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=True,
                raw={"line_count": len(lines), "backend": "TensorRT"},
            )
        except Exception:
            return _failure(
                "TENSORRT_INFERENCE_ERROR",
                (time.perf_counter() - started) * 1000,
            )

    def _detect(self, image: np.ndarray) -> list[list[tuple[float, float]]]:
        import cv2

        height, width = image.shape[:2]
        input_height, input_width = self.det_input_size
        tensor = _prepare_detection(image, self.det_input_size)
        outputs = self._detector.infer(tensor)  # type: ignore[union-attr]
        probability = _probability_map(outputs[0])
        probability = cv2.resize(probability, (input_width, input_height))
        bitmap = (probability > self.det_threshold).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        found: list[list[tuple[float, float]]] = []
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            if box_width < self.det_min_box_size or box_height < self.det_min_box_size:
                continue
            mask = np.zeros_like(bitmap)
            cv2.drawContours(mask, [contour], -1, 1, thickness=-1)
            score = float(probability[mask.astype(bool)].mean()) if mask.any() else 0.0
            if score < self.det_box_threshold:
                continue
            points = cv2.boxPoints(cv2.minAreaRect(contour))
            polygon = [
                (
                    float(point[0]) * width / input_width,
                    float(point[1]) * height / input_height,
                )
                for point in points
            ]
            found.append(_order_polygon(polygon))
        return sorted(found, key=lambda item: (min(point[1] for point in item), min(point[0] for point in item)))


def decode_ctc(output: np.ndarray, characters: list[str]) -> tuple[str, float]:
    """Decode the common PP-OCR CTC output with blank index zero."""

    logits = np.asarray(output)
    while logits.ndim > 2:
        logits = logits[0]
    if logits.ndim != 2 or not characters:
        return "", 0.0
    if logits.shape[-1] not in {len(characters), len(characters) + 1} and logits.shape[0] in {
        len(characters),
        len(characters) + 1,
    }:
        logits = logits.T
    probabilities = _softmax(logits, axis=-1)
    indices = probabilities.argmax(axis=-1)
    scores = probabilities.max(axis=-1)
    blank_index = 0 if logits.shape[-1] == len(characters) + 1 else logits.shape[-1] - 1
    decoded: list[str] = []
    confidence: list[float] = []
    previous = blank_index
    for index, score in zip(indices.tolist(), scores.tolist()):
        if index == blank_index or index == previous:
            previous = index
            continue
        char_index = index - 1 if blank_index == 0 else index
        if 0 <= char_index < len(characters):
            decoded.append(characters[char_index])
            confidence.append(float(score))
        previous = index
    return "".join(decoded), float(np.mean(confidence)) if confidence else 0.0


def _prepare_detection(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    height, width = size
    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    rgb = resized[:, :, ::-1].astype(np.float32) / 255.0
    normalized = (rgb - 0.5) / 0.5
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None, ...])


def _prepare_recognition(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    height, width = size
    source_height, source_width = image.shape[:2]
    target_width = min(width, max(1, int(round(source_width * height / source_height))))
    resized = cv2.resize(image, (target_width, height), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    canvas[:, :target_width] = resized
    rgb = canvas[:, :, ::-1].astype(np.float32) / 255.0
    normalized = (rgb - 0.5) / 0.5
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None, ...])


def _probability_map(output: np.ndarray) -> np.ndarray:
    value = np.asarray(output)
    while value.ndim > 2:
        value = value[0]
    if value.size and (float(value.min()) < 0.0 or float(value.max()) > 1.0):
        value = 1.0 / (1.0 + np.exp(-value))
    return value.astype(np.float32, copy=False)


def _perspective_crop(image: np.ndarray, polygon: list[tuple[float, float]]) -> Optional[np.ndarray]:
    import cv2

    ordered = np.asarray(_order_polygon(polygon), dtype=np.float32)
    width = max(int(np.linalg.norm(ordered[1] - ordered[0])), int(np.linalg.norm(ordered[2] - ordered[3])))
    height = max(int(np.linalg.norm(ordered[3] - ordered[0])), int(np.linalg.norm(ordered[2] - ordered[1])))
    if width < 2 or height < 2:
        return None
    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(image, matrix, (width, height))


def _order_polygon(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    values = list(points)
    if len(values) != 4:
        x_values = [point[0] for point in values]
        y_values = [point[1] for point in values]
        return [(min(x_values), min(y_values)), (max(x_values), min(y_values)), (max(x_values), max(y_values)), (min(x_values), max(y_values))]
    values.sort(key=lambda point: (point[1], point[0]))
    top = sorted(values[:2], key=lambda point: point[0])
    bottom = sorted(values[2:], key=lambda point: point[0], reverse=True)
    return [top[0], top[1], bottom[0], bottom[1]]


def _should_rotate(image: np.ndarray, classifier: TensorRTEngineRunner, size: tuple[int, int]) -> bool:
    output = classifier.infer(_prepare_recognition(image, size))[0]
    logits = np.asarray(output).reshape(-1)
    return logits.size >= 2 and int(np.argmax(logits)) == 1 and float(_softmax(logits)[1]) >= 0.90


def _read_character_dict(path: str) -> list[str]:
    values = [line.rstrip("\r\n") for line in Path(path).read_text(encoding="utf-8").splitlines()]
    values = [value for value in values if value]
    if not values:
        raise ValueError("OCR character dictionary is empty")
    return values


def _softmax(values: np.ndarray, axis: int = -1) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    shifted = values - values.max(axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=axis, keepdims=True)


def _failure(code: str, elapsed_ms: float) -> RawOCRResult:
    return RawOCRResult(
        engine="tensorrt-ppocr",
        elapsed_ms=elapsed_ms,
        success=False,
        error=code,
        error_code=code,
        error_message="TensorRT OCR runtime is unavailable or inference failed.",
    )


def _check_cuda(error: Any) -> None:
    if isinstance(error, tuple):
        error = error[0]
    if int(error) != 0:
        raise RuntimeError(f"CUDA call failed: {error}")


def _cuda_malloc(cudart: Any, size: int) -> int:
    error, pointer = cudart.cudaMalloc(size)
    _check_cuda(error)
    return int(pointer)


def _cuda_memcpy_async(
    cudart: Any,
    destination: int,
    source: int,
    size: int,
    stream: Any,
    *,
    host_to_device: bool,
) -> None:
    kind = (
        cudart.cudaMemcpyKind.cudaMemcpyHostToDevice
        if host_to_device
        else cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost
    )
    _check_cuda(cudart.cudaMemcpyAsync(destination, source, size, kind, stream)[0])
