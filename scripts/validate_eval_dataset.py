#!/usr/bin/env python3
"""Validate a Phase 1 evaluation dataset without modifying it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.evaluation.dataset import validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Phase 1 evaluation dataset")
    parser.add_argument("--dataset", required=True, help="Dataset root containing config, ground truth, manifest and images")
    args = parser.parse_args()
    report = validate_dataset(args.dataset)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
