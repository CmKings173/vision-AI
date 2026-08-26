"""Minimal secret-safe JSON lifecycle logging."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..contracts import epoch_ms_now

_SENSITIVE_KEY_PARTS = (
    "access_key",
    "credential",
    "password",
    "secret",
    "token",
    "url",
)


class StructuredLifecycleLogger:
    def __init__(self, *, sink: Callable[[str], None] | None = None) -> None:
        self._sink = sink or print

    def emit(
        self,
        *,
        event_id: str,
        component: str,
        stage: str,
        status: str,
        **fields: Any,
    ) -> None:
        for key in fields:
            normalized = key.lower()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError("sensitive fields are forbidden in lifecycle logs")
        payload = {
            "timestamp_ms": epoch_ms_now(),
            "event_id": event_id,
            "component": component,
            "stage": stage,
            "status": status,
            **fields,
        }
        self._sink(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
