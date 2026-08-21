"""Programmatically generated real OpenCV images; no fake ``.shape`` objects."""

from __future__ import annotations

import cv2
import numpy as np


def sharp_label() -> np.ndarray:
    image = np.full((220, 520, 3), 190, dtype=np.uint8)
    cv2.rectangle(image, (8, 8), (511, 211), (30, 30, 30), 2)
    cv2.putText(image, "SKU: ABC123", (28, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.25, (20, 20, 20), 3, cv2.LINE_AA)
    cv2.putText(image, "LOT: 260821", (28, 165), cv2.FONT_HERSHEY_SIMPLEX, 1.25, (20, 20, 20), 3, cv2.LINE_AA)
    return image


def blurred_label() -> np.ndarray:
    return cv2.GaussianBlur(sharp_label(), (17, 17), 5.0)


def dark_label() -> np.ndarray:
    return np.clip(sharp_label().astype(np.float32) * 0.08, 0, 255).astype(np.uint8)


def overexposed_label() -> np.ndarray:
    image = np.full_like(sharp_label(), 255)
    cv2.putText(image, "SKU: ABC123", (28, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.25, (248, 248, 248), 2, cv2.LINE_AA)
    return image
