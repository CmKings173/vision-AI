#!/usr/bin/env python3
"""Report local/GX10 runtime readiness without installing anything."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.runtime import collect_runtime_checks, unsupported_python_message


def main() -> int:
    checks = collect_runtime_checks()
    for check in checks:
        print(f"{check.name:<16} {check.value:<24} {check.status}")
    message = unsupported_python_message()
    if message:
        print(message)
    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
