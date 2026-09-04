"""Configuration-backed station, worker, and compatibility factories."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .camera.selector import FrameSelector
from .config import Settings, settings
from .detection.contour import ContourDetector
from .detection.fixed_roi import FixedROIDetector
from .detection.ultralytics_detector import UltralyticsLabelDetector
from .pipeline.ranking import CandidateScorer, CandidateScoreWeights
from .preprocessing.quality import QualityChecker
from .station.preparation import StationPreparer

if TYPE_CHECKING:
    from .pipeline.inspection import InspectionPipeline
    from .station.spool import LocalSpool
    from .worker.processor import InspectionProcessor


def build_station_preparer(config: Settings = settings) -> StationPreparer:
    """Build station-only preparation without importing inference runtimes."""

    config.validate_station()
    return _build_station_preparer(
        config,
        rotate_degrees=config.camera_rotate_degrees,
    )


def build_local_spool(config: Settings = settings) -> LocalSpool:
    """Build the station-owned durable outbox without inference dependencies."""

    config.validate_station()
    from .station.spool import LocalSpool, SpoolLimits

    return LocalSpool(
        config.spool_root,
        bucket=config.artifact_bucket,
        limits=SpoolLimits(
            max_pending_events=config.spool_max_pending_events,
            max_pending_bytes=config.spool_max_pending_bytes,
            min_free_disk_bytes=config.spool_min_free_disk_bytes,
        ),
    )


def build_processor(config: Settings = settings) -> InspectionProcessor:
    """Build worker-owned OCR/barcode/business processing components."""

    config.validate_worker()

    # Inference imports remain inside the worker factory. Importing or building
    # station-service therefore never imports model/decoder runtimes.
    from .barcode.zxing import ZXingBarcodeDecoder
    from .extraction.profiles import build_extractor, normalize_profile
    from .ocr.ppocr import PPOCRAdapter
    from .ocr.ppocr_v6 import PPOCRV6TransformersAdapter
    from .ocr.tensorrt_ocr import TensorRTOCRAdapter
    from .validation.rules import LabelValidator
    from .worker.processor import InspectionProcessor

    profile = normalize_profile(config.extraction_profile)
    extractor = build_extractor(profile)
    # Approval is an explicit profile contract state. It must never be
    # inferred from the absence of semantic blockers.
    profile_binding = extractor.profile_binding
    profile_approved = profile_binding.allows_automated_pass

    if profile_approved:
        # Phase 1 has no approved-profile resolver yet. In particular, the
        # process-wide VISION_REQUIRED_FIELDS / VISION_BARCODE_REQUIRED values
        # are not profile-owned policy and must never authorize a named
        # profile. Fail startup until a later profile boundary supplies its
        # own reviewed validation policy together with the approved binding.
        raise ValueError(
            "approved profile requires a profile-owned validation policy"
        )

    validator = LabelValidator(
        required_fields=(),
        barcode_required=False,
        min_field_confidence=config.ocr_confidence,
        profile_binding=profile_binding,
    )
    ocr_name = config.ocr_engine.strip().lower().replace("_", "-")
    if ocr_name == "ppocr-v6":
        ocr = PPOCRV6TransformersAdapter(
            device=config.ocr_device,
            ocr_version=config.ocr_version,
        )
    elif ocr_name in {"tensorrt", "tensor-rt"}:
        ocr = TensorRTOCRAdapter(
            det_engine=config.ocr_det_engine or "",
            rec_engine=config.ocr_rec_engine or "",
            cls_engine=config.ocr_cls_engine,
            char_dict=config.ocr_char_dict or "",
            det_input_size=(config.ocr_det_input_height, config.ocr_det_input_width),
            rec_input_size=(config.ocr_rec_image_height, config.ocr_rec_image_width),
            det_threshold=config.ocr_det_threshold,
            det_box_threshold=config.ocr_det_box_threshold,
            det_min_box_size=config.ocr_det_min_box_size,
        )
    else:
        ocr = PPOCRAdapter(lang=config.ocr_lang, device=config.ocr_device)

    return InspectionProcessor(
        ocr=ocr,
        barcode=ZXingBarcodeDecoder(),
        extractor=extractor,
        validator=validator,
    )


def build_pipeline(config: Settings = settings) -> InspectionPipeline:
    """Build the existing local synchronous compatibility façade."""

    config.validate()
    from .pipeline.inspection import InspectionPipeline

    # Existing scripts normalize orientation before calling the local pipeline.
    # Keep rotation at zero here to avoid changing their observable behavior.
    preparer = _build_station_preparer(config, rotate_degrees=0)
    processor = build_processor(config)
    logging.getLogger(__name__).info(
        "Detector engine=%s device=%s; OCR engine=%s device=%s; Barcode engine=%s",
        config.detector,
        config.detector_device,
        config.ocr_engine,
        config.ocr_device,
        config.barcode_engine,
    )
    return InspectionPipeline(preparer=preparer, processor=processor)


def _build_station_preparer(
    config: Settings,
    *,
    rotate_degrees: int,
) -> StationPreparer:
    detector_name = config.detector.strip().lower().replace("_", "-")
    if detector_name in {"fixedroi", "fixed-roi", "roi"}:
        roi = FixedROIDetector.parse_roi(config.label_roi)
        detector = FixedROIDetector(roi, normalized=config.roi_normalized)
    elif detector_name in {"contour", "contours"}:
        logging.getLogger(__name__).warning(
            "Contour detector is EXPERIMENTAL and is not accepted for GX10 V1"
        )
        detector = ContourDetector()
    elif detector_name in {"ultralytics", "yolo"}:
        detector = UltralyticsLabelDetector(
            config.detector_model,
            device=config.detector_device,
            confidence=config.detector_confidence,
            iou=config.detector_iou,
            image_size=config.detector_image_size,
            max_det=config.detector_max_det,
        )
    else:
        raise ValueError(f"unsupported detector: {config.detector}")

    quality_checker = QualityChecker(
        min_width=config.quality_min_width,
        min_height=config.quality_min_height,
        min_sharpness=config.quality_min_sharpness,
        min_brightness=config.quality_min_brightness,
        max_brightness=config.quality_max_brightness,
        max_underexposed_ratio=config.quality_max_underexposed_ratio,
        max_overexposed_ratio=config.quality_max_overexposed_ratio,
        max_glare_ratio=config.quality_max_glare_ratio,
    )
    scorer = CandidateScorer(
        weights=CandidateScoreWeights(
            detection=config.score_weight_detection,
            sharpness=config.score_weight_sharpness,
            exposure=config.score_weight_exposure,
            area=config.score_weight_area,
            freshness=config.score_weight_freshness,
            glare=config.score_weight_glare,
            validity=config.score_weight_validity,
        ),
        sharpness_reference=config.candidate_sharpness_reference,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    return StationPreparer(
        detector=detector,
        selector=FrameSelector(
            top_k=config.top_k,
            max_frame_age_ms=config.max_frame_age_ms,
            preview_long_edge=config.frame_preview_long_edge,
        ),
        quality_checker=quality_checker,
        candidate_scorer=scorer,
        station_id=config.station_id,
        camera_id=config.camera_id,
        rotate_degrees=rotate_degrees,
        bbox_padding_ratio=config.bbox_padding_ratio,
    )
