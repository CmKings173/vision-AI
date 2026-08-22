#!/usr/bin/env python3
"""Build a hardware-specific TensorRT engine from an ONNX model.

Example on GX10:

    python scripts/build_tensorrt_engine.py \
      --onnx models/ppocr/det.onnx \
      --engine models/ppocr/det.engine \
      --shape 1,3,960,960 \
      --fp16

For dynamic ONNX inputs, provide all three shapes:
``--min-shape``, ``--opt-shape`` and ``--max-shape``.
The generated engine is target-GPU specific and should not be copied from
Mac/x86 to GX10.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional


def _shape(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("shape must be comma-separated integers") from exc
    if not result or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("shape dimensions must be >= 1")
    return result


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onnx", required=True, help="source ONNX model")
    parser.add_argument("--engine", required=True, help="output TensorRT engine")
    parser.add_argument("--shape", type=_shape, help="static input shape, e.g. 1,3,960,960")
    parser.add_argument("--min-shape", type=_shape)
    parser.add_argument("--opt-shape", type=_shape)
    parser.add_argument("--max-shape", type=_shape)
    parser.add_argument("--workspace-mb", type=int, default=2048)
    parser.add_argument("--fp16", action="store_true")
    return parser.parse_args()


def build(
    onnx_path: str,
    engine_path: str,
    *,
    static_shape: Optional[tuple[int, ...]],
    min_shape: Optional[tuple[int, ...]],
    opt_shape: Optional[tuple[int, ...]],
    max_shape: Optional[tuple[int, ...]],
    workspace_mb: int,
    fp16: bool,
) -> None:
    import tensorrt as trt

    source = Path(onnx_path)
    if not source.is_file():
        raise FileNotFoundError(f"ONNX model not found: {source}")
    if workspace_mb < 1:
        raise ValueError("workspace_mb must be >= 1")

    logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(logger)
    flags = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    network = builder.create_network(flags)
    parser = trt.OnnxParser(network, logger)
    if not parser.parse(source.read_bytes()):
        errors = [str(parser.get_error(index)) for index in range(parser.num_errors)]
        raise RuntimeError("ONNX_PARSE_FAILED: " + " | ".join(errors))
    if network.num_inputs != 1:
        raise ValueError(f"expected one input, found {network.num_inputs}")

    input_tensor = network.get_input(0)
    input_shape = tuple(int(value) for value in input_tensor.shape)
    dynamic = any(value < 0 for value in input_shape)
    profiles_provided = (min_shape, opt_shape, max_shape)
    if dynamic and not all(profiles_provided):
        raise ValueError(
            "dynamic ONNX input requires --min-shape, --opt-shape and --max-shape"
        )
    if not dynamic and any(profiles_provided):
        raise ValueError("shape profiles are only valid for a dynamic ONNX input")
    if static_shape and dynamic:
        raise ValueError("use shape profiles instead of --shape for a dynamic input")
    if static_shape and tuple(static_shape) != input_shape:
        raise ValueError(f"--shape {static_shape} does not match ONNX input {input_shape}")

    config = builder.create_builder_config()
    config.set_memory_pool_limit(
        trt.MemoryPoolType.WORKSPACE,
        workspace_mb * 1024 * 1024,
    )
    if fp16:
        if not builder.platform_has_fast_fp16:
            raise RuntimeError("FP16 is not supported by this TensorRT platform")
        config.set_flag(trt.BuilderFlag.FP16)
    if dynamic:
        profile = builder.create_optimization_profile()
        profile.set_shape(
            input_tensor.name,
            min_shape,
            opt_shape,
            max_shape,
        )
        config.add_optimization_profile(profile)

    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError("TENSORRT_ENGINE_BUILD_FAILED")
    output = Path(engine_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bytes(serialized))
    print(f"Built {output} ({output.stat().st_size} bytes)")


def main() -> int:
    args = _args()
    build(
        args.onnx,
        args.engine,
        static_shape=args.shape,
        min_shape=args.min_shape,
        opt_shape=args.opt_shape,
        max_shape=args.max_shape,
        workspace_mb=args.workspace_mb,
        fp16=args.fp16,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

