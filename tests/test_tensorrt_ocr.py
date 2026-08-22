from pathlib import Path

import numpy as np

from label_inspection.ocr.tensorrt_ocr import TensorRTOCRAdapter, decode_ctc


def test_decode_ctc_collapses_repeated_characters_and_blank():
    logits = np.array(
        [
            [
                [8.0, 0.0, 0.0],  # blank
                [0.0, 8.0, 0.0],  # A
                [0.0, 8.0, 0.0],  # repeated A
                [0.0, 0.0, 8.0],  # B
            ]
        ],
        dtype=np.float32,
    )

    text, confidence = decode_ctc(logits, ["A", "B"])

    assert text == "AB"
    assert confidence > 0.99


def test_tensorrt_adapter_fails_structured_when_model_files_are_missing(tmp_path: Path):
    adapter = TensorRTOCRAdapter(
        det_engine=str(tmp_path / "det.engine"),
        rec_engine=str(tmp_path / "rec.engine"),
        char_dict=str(tmp_path / "keys.txt"),
    )

    result = adapter.recognize(np.zeros((64, 128, 3), dtype=np.uint8))

    assert result.success is False
    assert result.error_code == "TENSORRT_MODEL_MISSING"
    assert result.engine == "tensorrt-ppocr"

