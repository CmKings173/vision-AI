#!/usr/bin/env python3
"""Inventory configured public datasets without downloading or mixing them."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LOCAL_DIRS = {
    "roboflow_shipping_label_v2": "public/roboflow_shipping_label_v2",
    "roboflow_parcel_label_detection": "public/roboflow_parcel_label_detection",
    "dynamsoft_skewed_datamatrix": "public/dynamsoft_skewed_datamatrix",
    "dynamsoft_challenging": "public/dynamsoft_challenging_images",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory Phase 1 public dataset sources")
    parser.add_argument(
        "--sources",
        default="phase1_input/phase1_eval_bundle/phase1_eval_bundle/sources/public_sources.json",
    )
    parser.add_argument("--root", default="phase1_input/phase1_eval_bundle/phase1_eval_bundle")
    parser.add_argument("--output", default="datasets/public_inventory.json")
    args = parser.parse_args()

    source_path = Path(args.sources).expanduser().resolve()
    root = Path(args.root).expanduser().resolve()
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    datasets = []
    for source in payload.get("public_datasets", []):
        dataset_id = str(source.get("id"))
        relative = LOCAL_DIRS.get(dataset_id, f"public/{dataset_id}")
        local_path = root / relative
        files = [
            path for path in local_path.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ] if local_path.exists() else []
        datasets.append({
            **source,
            "local_path": str(local_path),
            "local_exists": local_path.exists(),
            "local_image_count": len(files),
            "download_attempted": False,
            "status": "AVAILABLE_LOCAL" if files else "NOT_DOWNLOADED_NETWORK_OR_CREDENTIALS",
        })

    report = {
        "generated_at": str(date.today()),
        "source_file": str(source_path),
        "network_download_attempted": False,
        "production_accuracy_inclusion": "NEVER",
        "datasets": datasets,
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
