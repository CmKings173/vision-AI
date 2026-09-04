#!/usr/bin/env python3
"""Persistent direct IP-camera POC with warmed OCR and manual triggers."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.artifacts import save_inspection_artifacts, write_result_json
from label_inspection.camera.acquisition import CameraAcquisition
from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.rtsp import RTSPCamera
from label_inspection.camera.security import mask_url_credentials, resolve_camera_source
from label_inspection.config import settings
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.extraction.profiles import DGX_SPARK_LABEL_FIELDS
from label_inspection.preprocessing.orientation import normalize_orientation


def _emit(payload: dict, code: int) -> int:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        print(json.dumps({"status": "ERROR", "reason": "JSON_SERIALIZATION_FAILED"}))
        return 1
    return code


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


def _new_runtime_metrics() -> dict[str, object]:
    """Create counters whose denominators match the stages actually run."""

    return {
        "total_triggers": 0,
        "stale_trigger_count": 0,
        "accepted_frame_triggers": 0,
        "detection_attempts": 0,
        "detection_hits": 0,
        "detection_misses": 0,
        "ocr_attempts": 0,
        "ocr_successes": 0,
        "ocr_failures": 0,
        "barcode_attempts": 0,
        "barcode_successes": 0,
        "barcode_failures": 0,
        "full_pipeline_attempts": 0,
        "full_pipeline_passes": 0,
        "detection_timings": [],
        "ocr_timings": [],
        "barcode_timings": [],
        "parallel_inference_timings": [],
        "total_inspection_timings": [],
        "successful_e2e_timings": [],
    }


def _record_stale_metrics(metrics: dict[str, object]) -> None:
    metrics["total_triggers"] = int(metrics["total_triggers"]) + 1
    metrics["stale_trigger_count"] = int(metrics["stale_trigger_count"]) + 1


def _record_inspection_metrics(
    metrics: dict[str, object],
    *,
    detector_attempts: list[dict],
    result: object,
    inspection_ms: float,
) -> None:
    metrics["total_triggers"] = int(metrics["total_triggers"]) + 1
    metrics["accepted_frame_triggers"] = int(metrics["accepted_frame_triggers"]) + 1
    for attempt in detector_attempts:
        metrics["detection_attempts"] = int(metrics["detection_attempts"]) + 1
        accepted = int(attempt.get("accepted_detection_count", 0) or 0) > 0
        key = "detection_hits" if accepted else "detection_misses"
        metrics[key] = int(metrics[key]) + 1
        inference_ms = attempt.get("inference_ms")
        if isinstance(inference_ms, (int, float)) and inference_ms >= 0:
            metrics["detection_timings"].append(float(inference_ms))  # type: ignore[union-attr]

    raw_ocr = result.raw_ocr
    barcode = getattr(result, "barcode", None)
    ocr_ran = getattr(raw_ocr, "state", "NOT_RUN") != "NOT_RUN"
    barcode_ran = getattr(barcode, "state", "NOT_RUN") != "NOT_RUN"
    if ocr_ran:
        metrics["ocr_attempts"] = int(metrics["ocr_attempts"]) + 1
        if bool(getattr(raw_ocr, "success", False)):
            metrics["ocr_successes"] = int(metrics["ocr_successes"]) + 1
        else:
            metrics["ocr_failures"] = int(metrics["ocr_failures"]) + 1
        ocr_ms = result.timing.get("ocr_ms")
        if isinstance(ocr_ms, (int, float)) and ocr_ms >= 0:
            metrics["ocr_timings"].append(float(ocr_ms))  # type: ignore[union-attr]
    if barcode_ran:
        metrics["barcode_attempts"] = int(metrics["barcode_attempts"]) + 1
        if bool(getattr(barcode, "success", False)):
            metrics["barcode_successes"] = int(metrics["barcode_successes"]) + 1
        else:
            metrics["barcode_failures"] = int(metrics["barcode_failures"]) + 1
        barcode_ms = result.timing.get("barcode_ms")
        if isinstance(barcode_ms, (int, float)) and barcode_ms >= 0:
            metrics["barcode_timings"].append(float(barcode_ms))  # type: ignore[union-attr]
    if not (ocr_ran or barcode_ran):
        return
    parallel_ms = result.timing.get("parallel_inference_ms")
    if isinstance(parallel_ms, (int, float)) and parallel_ms >= 0:
        metrics["parallel_inference_timings"].append(float(parallel_ms))  # type: ignore[union-attr]
    metrics["full_pipeline_attempts"] = int(metrics["full_pipeline_attempts"]) + 1
    metrics["total_inspection_timings"].append(float(inspection_ms))  # type: ignore[union-attr]
    if getattr(result.validation, "status", None) == "PASS":
        metrics["full_pipeline_passes"] = int(metrics["full_pipeline_passes"]) + 1
        metrics["successful_e2e_timings"].append(float(inspection_ms))  # type: ignore[union-attr]


def _runtime_metrics_summary(metrics: dict[str, object]) -> dict[str, object]:
    return {
        "total_triggers": int(metrics["total_triggers"]),
        "stale_trigger_count": int(metrics["stale_trigger_count"]),
        "accepted_frame_triggers": int(metrics["accepted_frame_triggers"]),
        "detection_attempts": int(metrics["detection_attempts"]),
        "detection_hits": int(metrics["detection_hits"]),
        "detection_misses": int(metrics["detection_misses"]),
        "ocr_attempts": int(metrics["ocr_attempts"]),
        "ocr_successes": int(metrics["ocr_successes"]),
        "ocr_failures": int(metrics["ocr_failures"]),
        "barcode_attempts": int(metrics["barcode_attempts"]),
        "barcode_successes": int(metrics["barcode_successes"]),
        "barcode_failures": int(metrics["barcode_failures"]),
        "full_pipeline_attempts": int(metrics["full_pipeline_attempts"]),
        "full_pipeline_passes": int(metrics["full_pipeline_passes"]),
        "detection_p50_ms": _percentile(metrics["detection_timings"], 0.50),  # type: ignore[arg-type]
        "detection_p95_ms": _percentile(metrics["detection_timings"], 0.95),  # type: ignore[arg-type]
        "ocr_p50_ms": _percentile(metrics["ocr_timings"], 0.50),  # type: ignore[arg-type]
        "ocr_p95_ms": _percentile(metrics["ocr_timings"], 0.95),  # type: ignore[arg-type]
        "barcode_p50_ms": _percentile(metrics["barcode_timings"], 0.50),  # type: ignore[arg-type]
        "barcode_p95_ms": _percentile(metrics["barcode_timings"], 0.95),  # type: ignore[arg-type]
        "parallel_inference_p50_ms": _percentile(
            metrics["parallel_inference_timings"], 0.50  # type: ignore[arg-type]
        ),
        "parallel_inference_p95_ms": _percentile(
            metrics["parallel_inference_timings"], 0.95  # type: ignore[arg-type]
        ),
        "total_inspection_p50_ms": _percentile(
            metrics["total_inspection_timings"], 0.50  # type: ignore[arg-type]
        ),
        "total_inspection_p95_ms": _percentile(
            metrics["total_inspection_timings"], 0.95  # type: ignore[arg-type]
        ),
        "successful_e2e_p50_ms": _percentile(
            metrics["successful_e2e_timings"], 0.50  # type: ignore[arg-type]
        ),
        "successful_e2e_p95_ms": _percentile(
            metrics["successful_e2e_timings"], 0.95  # type: ignore[arg-type]
        ),
    }


def _fresh_packets(buffer: FrameBuffer, now: float, monotonic_now: float):
    return buffer.snapshot(now=now, monotonic_now=monotonic_now)


def _wait_for_ready(
    camera: RTSPCamera,
    buffer: FrameBuffer,
    timeout_s: float,
) -> tuple[bool, list, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        now = time.time()
        monotonic_now = time.monotonic()
        fresh = _fresh_packets(buffer, now, monotonic_now)
        health = camera.health
        if health.connected and not health.stale and fresh:
            return True, fresh, health
        time.sleep(0.05)
    return False, [], camera.health


def _rotate_packets(packets: list, rotate_degrees: int) -> list:
    if rotate_degrees == 0:
        return packets
    return [
        replace(packet, frame=normalize_orientation(packet.frame, rotate_degrees))
        for packet in packets
    ]


def _result_payload(
    source: str,
    result: object,
    telemetry: dict,
) -> dict:
    label = result.label
    barcode = result.barcode.to_dict()
    decoded_barcodes = getattr(result, "barcodes", [])
    barcode_items = [item.to_dict() for item in decoded_barcodes if item.value]
    if not barcode_items and barcode.get("value"):
        barcode_items = [barcode]
    return {
        "status": result.validation.status,
        "event_id": result.event_id,
        "camera_id": result.camera_id,
        "source": mask_url_credentials(source),
        "telemetry": telemetry,
        "selected_frame_id": result.frame_id,
        "selected_frame_timestamp": result.frame_timestamp,
        "label_bbox": list(label.bbox) if label is not None else None,
        "crop_bbox": list(result.crop_bbox) if result.crop_bbox is not None else None,
        "crop_score": result.candidate_score.to_dict()
        if result.candidate_score is not None
        else None,
        "quality": result.quality.to_dict(),
        "evidence": [item.to_dict() for item in getattr(result, "evidence", [])],
        "ocr": result.raw_ocr.to_dict(),
        "barcode": {
            "status": barcode.get("status"),
            "number_found": len(barcode_items),
            "items": barcode_items,
            "selected": barcode,
        },
        "fields": {key: value.to_dict() for key, value in result.extracted.items()},
        "validation": result.validation.to_dict(),
        "timings": dict(result.timing),
    }


def _error_payload(source: str, event_id: str, reason: str, telemetry: dict) -> dict:
    return {
        "status": "ERROR",
        "event_id": event_id,
        "source": mask_url_credentials(source),
        "reason": reason,
        "telemetry": telemetry,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persistent direct RTSP/HTTP IP camera label inspection POC"
    )
    parser.add_argument("--source", help="Direct RTSP or HTTP camera URL")
    parser.add_argument("--camera-id")
    parser.add_argument(
        "--roi",
        help="Calibrated FixedROI x1,y1,x2,y2; normalized unless --roi-absolute",
    )
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument(
        "--detector",
        choices=("fixed-roi", "yolo", "ultralytics"),
        default="fixed-roi",
        help="Label detector; YOLO uses --detector-model and does not need --roi",
    )
    parser.add_argument("--detector-model")
    parser.add_argument(
        "--detector-device",
        help="YOLO device; defaults to --device when --detector=yolo",
    )
    parser.add_argument("--detector-confidence", type=float)
    parser.add_argument("--detector-iou", type=float)
    parser.add_argument("--detector-imgsz", type=int)
    parser.add_argument("--detector-max-det", type=int)
    parser.add_argument(
        "--rotate-deg",
        type=int,
        default=settings.camera_rotate_degrees,
        help="Clockwise camera rotation before FixedROI/OCR: 0, 90, 180, or 270",
    )
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--connect-timeout-s", type=float, default=15.0)
    parser.add_argument("--triggers", type=int, default=10)
    parser.add_argument(
        "--trigger-after-s",
        type=float,
        help="Use a timed trigger for every inspection instead of waiting for Enter",
    )
    parser.add_argument(
        "--debug-dir",
        default="artifacts/manual_rtsp_inspection",
        help="Root directory for <event_id>/selected_frame.jpg, label_crop.jpg, result.json",
    )
    args = parser.parse_args()
    if (
        args.connect_timeout_s <= 0
        or args.triggers < 1
        or args.rotate_deg not in {0, 90, 180, 270}
        or (args.trigger_after_s is not None and args.trigger_after_s < 0)
    ):
        return _emit({"status": "ERROR", "reason": "INVALID_ARGUMENT"}, 2)

    try:
        source = resolve_camera_source(args.source, settings.rtsp_url)
        detector_name = args.detector.strip().lower().replace("_", "-")
        roi = args.roi if args.roi is not None else settings.label_roi
        roi_normalized = (
            not args.roi_absolute if args.roi is not None else settings.roi_normalized
        )
        if detector_name in {"fixed-roi", "fixedroi", "roi"}:
            if not roi:
                raise ValueError(
                    "A calibrated --roi or VISION_LABEL_ROI is required; full-frame ROI is not accepted"
                )
            parsed_roi = FixedROIDetector.parse_roi(roi)
            if roi_normalized and parsed_roi == (0.0, 0.0, 1.0, 1.0):
                raise ValueError("Full-frame ROI 0,0,1,1 is not accepted for DGX Spark label POC")
        elif detector_name not in {"yolo", "ultralytics"}:
            raise ValueError(f"Unsupported detector: {args.detector}")
        config = replace(
            settings,
            camera_id=args.camera_id or settings.camera_id,
            detector=detector_name,
            detector_model=args.detector_model or settings.detector_model,
            detector_device=(
                args.detector_device
                or (args.device if detector_name in {"yolo", "ultralytics"} else settings.detector_device)
            ),
            detector_confidence=(
                settings.detector_confidence
                if args.detector_confidence is None
                else args.detector_confidence
            ),
            detector_iou=settings.detector_iou if args.detector_iou is None else args.detector_iou,
            detector_image_size=(
                settings.detector_image_size
                if args.detector_imgsz is None
                else args.detector_imgsz
            ),
            detector_max_det=(
                settings.detector_max_det
                if args.detector_max_det is None
                else args.detector_max_det
            ),
            label_roi=roi,
            roi_normalized=roi_normalized,
            camera_rotate_degrees=args.rotate_deg,
            ocr_engine="ppocr_v6",
            ocr_backend="transformers",
            ocr_version="PP-OCRv6",
            ocr_device=args.device,
            barcode_engine="zxing",
            extraction_profile="dgx_spark_label",
            required_fields=DGX_SPARK_LABEL_FIELDS,
        )
        pipeline = build_pipeline(config)
    except ValueError as exc:
        return _emit({"status": "ERROR", "stage": "setup", "reason": str(exc)}, 2)
    except Exception as exc:  # noqa: BLE001 - setup boundary serializes runtime failures
        return _emit({"status": "ERROR", "stage": "setup", "reason": str(exc)}, 1)

    startup_started = time.perf_counter()
    print("STARTUP: loading and warming detector/OCR...", file=sys.stderr)
    detector_ready = True
    detector_warmup_ms = 0.0
    try:
        detector_warmup = getattr(pipeline.detector, "warmup", None)
        if callable(detector_warmup):
            detector_warmup_ms = float(detector_warmup())
            detector_ready = bool(getattr(pipeline.detector, "ready", False))
        warmup = pipeline.ocr.warmup()
        ocr_ready = bool(warmup.success and getattr(pipeline.ocr, "ready", False))
        zxing_ready = bool(pipeline.barcode.prepare())
    except Exception as exc:  # noqa: BLE001 - startup boundary serializes runtime failures
        return _emit(
            {"status": "ERROR", "stage": "startup", "reason": str(exc)},
            1,
        )
    startup_ms = (time.perf_counter() - startup_started) * 1000
    if not detector_ready or not ocr_ready or not zxing_ready:
        return _emit(
            {
                "status": "ERROR",
                "stage": "startup",
                "reason": "RUNTIME_NOT_READY",
                "ocr_ready": ocr_ready,
                "ocr_warmup": warmup.to_dict(),
                "zxing_ready": zxing_ready,
                "detector_ready": detector_ready,
                "detector_runtime": getattr(
                    pipeline.detector, "runtime_metadata", {}
                ),
            },
            1,
        )
    print(
        f"STARTUP: detector ready (warmup_ms={detector_warmup_ms:.2f}); "
        f"OCR ready (warmup_ms={getattr(pipeline.ocr, 'warmup_ms', 0.0):.2f}); "
        f"ZXing ready; startup_ms={startup_ms:.2f}",
        file=sys.stderr,
    )

    camera = RTSPCamera(
        source,
        open_timeout_ms=config.rtsp_open_timeout_ms,
        read_timeout_ms=config.rtsp_read_timeout_ms,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    buffer = FrameBuffer(max_size=config.buffer_size, window_ms=config.buffer_window_ms)
    acquisition = CameraAcquisition(camera, buffer)
    capture_started_at = time.time()
    acquisition.start()
    metrics = _new_runtime_metrics()
    inspection_summaries: list[dict] = []
    try:
        ready, fresh, health = _wait_for_ready(camera, buffer, args.connect_timeout_s)
        if not ready:
            return _emit(
                {
                    "status": "ERROR",
                    "stage": "camera_connect",
                    "reason": health.last_error or "NO_FRESH_FRAME",
                    "source": mask_url_credentials(source),
                    "camera_health": health.__dict__,
                    "frames_received": acquisition.captured_count,
                },
                1,
            )

        print(
            f"SYSTEM READY camera_connected={health.connected}; "
            f"fresh_frames={len(fresh)}; ocr_ready={ocr_ready}; zxing_ready={zxing_ready}; "
            f"rotate_deg={args.rotate_deg}",
            file=sys.stderr,
        )
        for trigger_index in range(1, args.triggers + 1):
            if args.trigger_after_s is None:
                input(f"ENTER trigger {trigger_index}/{args.triggers}: press Enter to inspect... ")
                trigger_mode = "manual_enter"
            else:
                print(
                    f"Timed trigger {trigger_index}/{args.triggers} in "
                    f"{args.trigger_after_s:.3f}s; press Ctrl-C to cancel.",
                    file=sys.stderr,
                )
                time.sleep(args.trigger_after_s)
                trigger_mode = "timed"

            event_id = f"INS-{uuid.uuid4().hex[:12].upper()}"
            triggered_at = time.time()
            monotonic_now = time.monotonic()
            raw_packets = buffer.snapshot()
            fresh_packets = _fresh_packets(buffer, triggered_at, monotonic_now)
            health_at_trigger = camera.health
            telemetry = {
                "capture_started_at": capture_started_at,
                "triggered_at": triggered_at,
                "trigger_mode": trigger_mode,
                "camera_connected": health_at_trigger.connected,
                "frames_received": acquisition.captured_count,
                "buffered_frames": len(raw_packets),
                "fresh_frames_at_trigger": len(fresh_packets),
                "stale_frames_at_trigger": max(0, len(raw_packets) - len(fresh_packets)),
                "camera_health": health_at_trigger.__dict__,
                "ocr_ready_before_trigger": ocr_ready,
                "zxing_ready_before_trigger": zxing_ready,
                "ocr_warmup_ms_excluded": getattr(pipeline.ocr, "warmup_ms", 0.0),
                "detector": {
                    "name": config.detector,
                    "model": (
                        config.detector_model
                        if config.detector in {"yolo", "ultralytics"}
                        else None
                    ),
                    "device": config.detector_device,
                    "ready": detector_ready,
                    "warmup_ms_excluded": detector_warmup_ms,
                    "runtime": getattr(pipeline.detector, "runtime_metadata", {}),
                },
                "orientation": {
                    "rotate_degrees_clockwise": args.rotate_deg,
                    "normalized_before_detector": True,
                    "normalized_before_fixed_roi": config.detector == "fixed-roi",
                },
                "extraction_profile": config.extraction_profile,
                "required_fields": list(config.required_fields),
            }
            if not fresh_packets:
                _record_stale_metrics(metrics)
                payload = _error_payload(
                    source, event_id, "NO_FRESH_FRAME_AT_TRIGGER", telemetry
                )
                payload["artifacts"] = {
                    "directory": str(
                        Path(args.debug_dir).expanduser().resolve() / event_id
                    ),
                    "result": write_result_json(args.debug_dir, event_id, payload),
                }
                _emit(payload, 1)
                inspection_summaries.append(
                    {"event_id": event_id, "status": "ERROR", "reason": payload["reason"]}
                )
                ready, fresh, health = _wait_for_ready(
                    camera, buffer, args.connect_timeout_s
                )
                if not ready:
                    print(
                        f"SYSTEM NOT READY after stale event_id={event_id}: "
                        f"{health.last_error or 'NO_FRESH_FRAME'}",
                        file=sys.stderr,
                    )
                    break
                if trigger_index < args.triggers:
                    print(
                        f"SYSTEM READY after stale recovery camera_connected={health.connected}; "
                        f"fresh_frames={len(fresh)}; next_trigger={trigger_index + 1}",
                        file=sys.stderr,
                    )
                continue

            processed_packets = _rotate_packets(fresh_packets, args.rotate_deg)
            inspection_started = time.perf_counter()
            execution = pipeline.execute_packets(
                processed_packets,
                event_id=event_id,
                camera_id=config.camera_id,
            )
            result = execution.result
            pipeline_finished = time.perf_counter()
            telemetry["pipeline_wall_ms"] = (pipeline_finished - inspection_started) * 1000
            payload = _result_payload(source, result, telemetry)
            preparation_debug = dict(getattr(pipeline.preparer, "last_debug", {}))
            selected_ids = preparation_debug.get("detector_input_frame_ids", [])
            detector_input_packet = None
            if selected_ids:
                detector_input_packet = next(
                    (
                        packet
                        for packet in processed_packets
                        if packet.frame_id == selected_ids[0]
                    ),
                    None,
                )
            if detector_input_packet is None:
                detector_input_packet = processed_packets[0]
            artifact_paths = None
            try:
                artifact_selected_frame = (
                    execution.prepared.selected_frame
                    if execution.prepared is not None
                    else detector_input_packet.frame
                )
                detector_debug = {
                    "event_id": event_id,
                    "detector": preparation_debug,
                }
                artifact_paths = save_inspection_artifacts(
                    args.debug_dir,
                    event_id,
                    selected_frame=artifact_selected_frame,
                    label_crop=execution.label_crop_snapshot,
                    detector_input=detector_input_packet.frame,
                    detector_debug=detector_debug,
                    result_payload=payload,
                )
            except Exception as exc:  # noqa: BLE001 - artifact boundary must preserve result
                payload["artifact_error"] = str(exc)
                payload["artifacts"] = {
                    "directory": str(
                        Path(args.debug_dir).expanduser().resolve() / event_id
                    ),
                    "result": write_result_json(args.debug_dir, event_id, payload),
                }
            if artifact_paths is None and "artifacts" not in payload:
                payload["artifacts"] = {
                    "directory": str(
                        Path(args.debug_dir).expanduser().resolve() / event_id
                    ),
                    "result": write_result_json(args.debug_dir, event_id, payload),
                }
            artifact_paths = payload.get("artifacts")
            telemetry["artifact_write_ms"] = (time.perf_counter() - pipeline_finished) * 1000
            telemetry["inspection_wall_ms"] = (time.perf_counter() - inspection_started) * 1000
            payload["telemetry"] = telemetry
            if payload.get("artifacts", {}).get("result"):
                write_result_json(args.debug_dir, event_id, payload)
            _emit(payload, 0 if result.validation.status in {"PASS", "REVIEW", "FAIL"} else 1)
            total_inspection_ms = float(telemetry["inspection_wall_ms"])
            detector_attempts = preparation_debug.get("detector_attempts", [])
            _record_inspection_metrics(
                metrics,
                detector_attempts=detector_attempts,
                result=result,
                inspection_ms=total_inspection_ms,
            )
            inspection_summaries.append(
                {
                    "event_id": event_id,
                    "status": result.validation.status,
                    "selected_frame_id": result.frame_id,
                    "artifacts": artifact_paths,
                }
            )
            ready, fresh, health = _wait_for_ready(camera, buffer, args.connect_timeout_s)
            if not ready:
                print(
                    f"SYSTEM NOT READY after event_id={event_id}: "
                    f"{health.last_error or 'NO_FRESH_FRAME'}",
                    file=sys.stderr,
                )
                break
            if trigger_index < args.triggers:
                print(
                    f"SYSTEM READY camera_connected={health.connected}; "
                    f"fresh_frames={len(fresh)}; next_trigger={trigger_index + 1}",
                    file=sys.stderr,
                )
    except (EOFError, KeyboardInterrupt):
        print("Manual trigger loop cancelled.", file=sys.stderr)
    finally:
        # OpenCV's FFmpeg backend may finish a native read after close() has
        # returned.  Wait long enough for that deferred release before Python
        # exits, otherwise the daemon cleanup thread can abort the process.
        shutdown_timeout_s = max(2.0, config.rtsp_read_timeout_ms / 1000.0 + 0.5)
        acquisition.stop(join_timeout_s=shutdown_timeout_s)
        if not camera.wait_closed(timeout_s=shutdown_timeout_s):
            print(
                "WARNING: RTSP native capture release did not finish before shutdown timeout",
                file=sys.stderr,
            )

    summary = {
        "status": "COMPLETED"
        if int(metrics["total_triggers"]) == args.triggers
        else "INCOMPLETE",
        "triggers_requested": args.triggers,
        "triggers_completed": int(metrics["total_triggers"]),
        "same_process_model_reuse": True,
        "startup": {
            "ocr_ready_before_camera": ocr_ready,
            "ocr_warmup_ms_excluded_from_ocr_ms": getattr(
                pipeline.ocr, "warmup_ms", 0.0
            ),
            "detector_ready_before_camera": detector_ready,
            "detector_warmup_ms_excluded_from_detection_ms": detector_warmup_ms,
            "detector_runtime": getattr(pipeline.detector, "runtime_metadata", {}),
            "zxing_ready_before_camera": zxing_ready,
        },
        "benchmark": {
            "warmup_excluded": True,
            **_runtime_metrics_summary(metrics),
        },
        "metrics": _runtime_metrics_summary(metrics),
        "results": inspection_summaries,
    }
    return _emit(
        summary,
        0 if int(metrics["total_triggers"]) == args.triggers else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
