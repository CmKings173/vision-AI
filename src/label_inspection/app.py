"""Configuration-backed factories for the local V1 pipeline."""

from __future__ import annotations

import logging

from .barcode.zxing import ZXingBarcodeDecoder
from .camera.selector import FrameSelector
from .config import Settings, settings
from .detection.contour import ContourDetector
from .detection.fixed_roi import FixedROIDetector
from .detection.ultralytics_detector import UltralyticsLabelDetector
from .extraction.fields import FieldExtractor
from .ocr.ppocr import PPOCRAdapter
from .ocr.tensorrt_ocr import TensorRTOCRAdapter
from .pipeline.inspection import InspectionPipeline
from .pipeline.ranking import CandidateScorer, CandidateScoreWeights
from .preprocessing.quality import QualityChecker
from .validation.rules import LabelValidator


def build_pipeline(config: Settings = settings) -> InspectionPipeline:
    config.validate()
    detector_name = config.detector.strip().lower().replace("_", "-")
    if detector_name in {"fixedroi", "fixed-roi", "roi"}:
        detector = FixedROIDetector.parse_roi(config.label_roi)
        detector = FixedROIDetector(detector, normalized=config.roi_normalized)
    elif detector_name in {"contour", "contours"}:
        logging.getLogger(__name__).warning(
            "Contour detector is EXPERIMENTAL and is not accepted for GX10 V1"
        )
        detector = ContourDetector()
    elif detector_name in {"ultralytics", "yolo"}:
        detector = UltralyticsLabelDetector(
            config.detector_model,
            device=config.detector_device,
        )
    else:
        raise ValueError(f"unsupported detector: {config.detector}")

    # Keep SKU and LOT in the JSON even when only SKU is a PASS requirement;
    # this preserves evidence for later business-rule changes.
    extractor = FieldExtractor(fields=("sku", "lot"))
    validator = LabelValidator(
        required_fields=config.required_fields,
        barcode_required=config.barcode_required,
        min_field_confidence=config.ocr_confidence,
    )
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
    ocr_name = config.ocr_engine.strip().lower().replace("_", "-")
    if ocr_name in {"tensorrt", "tensor-rt"}:
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

    logging.getLogger(__name__).info(
        "Detector engine=%s device=%s; OCR engine=%s device=%s; Barcode engine=%s",
        detector_name,
        config.detector_device,
        config.ocr_engine,
        config.ocr_device,
        config.barcode_engine,
    )
    return InspectionPipeline(
        detector=detector,
        ocr=ocr,
        barcode=ZXingBarcodeDecoder(),
        extractor=extractor,
        validator=validator,
        selector=FrameSelector(
            top_k=config.top_k,
            max_frame_age_ms=config.max_frame_age_ms,
            preview_long_edge=config.frame_preview_long_edge,
        ),
        quality_checker=quality_checker,
        candidate_scorer=scorer,
        camera_id=config.camera_id,
        bbox_padding_ratio=config.bbox_padding_ratio,
    )
