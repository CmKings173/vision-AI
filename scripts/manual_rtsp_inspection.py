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
from label_inspection.preprocessing.crop import crop_image
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
        roi = args.roi if args.roi is not None else settings.label_roi
        if not roi:
            raise ValueError(
                "A calibrated --roi or VISION_LABEL_ROI is required; full-frame ROI is not accepted"
            )
        parsed_roi = FixedROIDetector.parse_roi(roi)
        roi_normalized = (
            not args.roi_absolute if args.roi is not None else settings.roi_normalized
        )
        if roi_normalized and parsed_roi == (0.0, 0.0, 1.0, 1.0):
            raise ValueError("Full-frame ROI 0,0,1,1 is not accepted for DGX Spark label POC")
        config = replace(
            settings,
            camera_id=args.camera_id or settings.camera_id,
            detector="fixed-roi",
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
    except Exception as exc:
        return _emit({"status": "ERROR", "stage": "setup", "reason": str(exc)}, 1)

    startup_started = time.perf_counter()
    print("STARTUP: loading and warming PP-OCRv6...", file=sys.stderr)
    try:
        warmup = pipeline.ocr.warmup()
        ocr_ready = bool(warmup.success and getattr(pipeline.ocr, "ready", False))
        zxing_ready = bool(pipeline.barcode.prepare())
    except Exception as exc:
        return _emit(
            {"status": "ERROR", "stage": "startup", "reason": str(exc)},
            1,
        )
    startup_ms = (time.perf_counter() - startup_started) * 1000
    if not ocr_ready or not zxing_ready:
        return _emit(
            {
                "status": "ERROR",
                "stage": "startup",
                "reason": "RUNTIME_NOT_READY",
                "ocr_ready": ocr_ready,
                "ocr_warmup": warmup.to_dict(),
                "zxing_ready": zxing_ready,
            },
            1,
        )
    print(
        f"STARTUP: OCR ready (warmup_ms={getattr(pipeline.ocr, 'warmup_ms', 0.0):.2f}); "
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
    pipeline_timings: list[float] = []
    total_timings: list[float] = []
    ocr_timings: list[float] = []
    inspection_summaries: list[dict] = []
    completed = 0
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
                "orientation": {
                    "rotate_degrees_clockwise": args.rotate_deg,
                    "normalized_before_fixed_roi": True,
                },
                "extraction_profile": config.extraction_profile,
                "required_fields": list(config.required_fields),
            }
            if not fresh_packets:
                payload = _error_payload(
                    source, event_id, "NO_FRESH_FRAME_AT_TRIGGER", telemetry
                )
                payload["artifacts"] = {
                    "directory": str(
                        (Path(args.debug_dir).expanduser().resolve() / event_id)
                    ),
                    "result": write_result_json(args.debug_dir, event_id, payload),
                }
                _emit(payload, 1)
                inspection_summaries.append(
                    {"event_id": event_id, "status": "ERROR", "reason": payload["reason"]}
                )
                continue

            processed_packets = _rotate_packets(fresh_packets, args.rotate_deg)
            inspection_started = time.perf_counter()
            result = pipeline.inspect_packets(
                processed_packets,
                event_id=event_id,
                camera_id=config.camera_id,
            )
            pipeline_finished = time.perf_counter()
            telemetry["pipeline_wall_ms"] = (pipeline_finished - inspection_started) * 1000
            payload = _result_payload(source, result, telemetry)
            selected_packet = next(
                (packet for packet in processed_packets if packet.frame_id == result.frame_id),
                None,
            )
            artifact_paths = None
            if selected_packet is not None and result.label is not None:
                try:
                    label_crop = crop_image(
                        selected_packet.frame,
                        tuple(result.label.bbox),
                        padding_ratio=config.bbox_padding_ratio,
                    ).image
                    artifact_paths = save_inspection_artifacts(
                        args.debug_dir,
                        event_id,
                        selected_frame=selected_packet.frame,
                        label_crop=label_crop,
                        result_payload=payload,
                    )
                except Exception as exc:
                    payload["artifact_error"] = str(exc)
                    payload["artifacts"] = {
                        "directory": str(
                            (Path(args.debug_dir).expanduser().resolve() / event_id)
                        ),
                        "result": write_result_json(args.debug_dir, event_id, payload),
                    }
            if "artifacts" not in payload:
                payload["artifacts"] = {
                    "directory": str(
                        (Path(args.debug_dir).expanduser().resolve() / event_id)
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
            completed += 1
            pipeline_total_ms = float(result.timing.get("total_ms", telemetry["pipeline_wall_ms"]))
            total_inspection_ms = float(telemetry["inspection_wall_ms"])
            ocr_ms = float(result.timing.get("ocr_ms", 0.0))
            pipeline_timings.append(pipeline_total_ms)
            total_timings.append(total_inspection_ms)
            ocr_timings.append(ocr_ms)
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
        "status": "COMPLETED" if completed == args.triggers else "INCOMPLETE",
        "triggers_requested": args.triggers,
        "triggers_completed": completed,
        "same_process_model_reuse": True,
        "startup": {
            "ocr_ready_before_camera": ocr_ready,
            "ocr_warmup_ms_excluded_from_ocr_ms": getattr(
                pipeline.ocr, "warmup_ms", 0.0
            ),
            "zxing_ready_before_camera": zxing_ready,
        },
        "benchmark": {
            "warmup_excluded": True,
            "ocr_p50_ms": _percentile(ocr_timings, 0.50),
            "ocr_p95_ms": _percentile(ocr_timings, 0.95),
            "pipeline_total_p50_ms": _percentile(pipeline_timings, 0.50),
            "pipeline_total_p95_ms": _percentile(pipeline_timings, 0.95),
            "total_inspection_p50_ms": _percentile(total_timings, 0.50),
            "total_inspection_p95_ms": _percentile(total_timings, 0.95),
        },
        "results": inspection_summaries,
    }
    return _emit(summary, 0 if completed == args.triggers else 1)


if __name__ == "__main__":
    raise SystemExit(main())
