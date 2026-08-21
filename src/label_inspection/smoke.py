"""Deterministic process exit semantics for local smoke commands."""

from __future__ import annotations

from enum import IntEnum
from typing import Optional


class SmokeExitCode(IntEnum):
    OK = 0
    FAILURE = 1
    USAGE_OR_RUNTIME = 2


def inspection_exit_code(
    validation_status: Optional[str],
    *,
    frames_read: Optional[int] = None,
) -> int:
    if frames_read is not None and frames_read <= 0:
        return int(SmokeExitCode.FAILURE)
    if validation_status == "ERROR":
        return int(SmokeExitCode.FAILURE)
    return int(SmokeExitCode.OK)
