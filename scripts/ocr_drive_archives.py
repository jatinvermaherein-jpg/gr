#!/usr/bin/env python3
"""One-off, provenance-preserving OCR for the two supplied Drive ZIPs and six chat screenshots.

Requires rapidocr_onnxruntime in the execution environment (intentionally not a bot runtime dependency).
Output is raw OCR staging only; never promote these results to verified catalog facts without review.
"""
from __future__ import annotations

import csv
import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "drive_ocr_stage"
ARCHIVES = [
    (ROOT.parent / "google_drive" / "PILOT.zip", "1o7-ceXZ8bQqhKK1kpVchnK28nE_XEH61"),
    (ROOT.parent / "google_drive" / "MA SS.zip", "1TY_Mal19TN_5lkmvQkvSBHrDexHFqHcE"),
]
UPLOADS = ROOT.parent / "uploads"
UPLOAD_NAMES = [f"image-{n}.png" for n in range(25, 31)]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_text(words: list[dict]) -> str:
    """Create a readable approximation while preserving original boxes/order separately."""
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (w["top"] + w["height"] / 2, w["left"]))
    lines: list[dict] = []
    for word in ordered:
        cy = word["top"] + word["height"] / 2
        target = next((row for row in lines if abs(cy - row["cy"]) <= max(9, word["height"] * 0.55, row["h"] * 0.55)), None)
        if target is None:
            lines.append({"cy": cy, "h": word["height"], "words": [word]})
        else:
            target["words"].append(word)
            target["cy"] = sum(w["top"] + w["height"] / 2 for w in target["words"]) / len(target["words"])
            target["h"] = max(target["h"], word["height"])
    lines.sort(key=lambda row: row["cy"])
    return "\n".join(" ".join(w["text"] for w in sorted(row["words"], key=lambda w: w["left"])) for row in lines)


def ocr_bytes(engine: RapidOCR, data: bytes, *, archive_name: str, archive_id: str | None,
              image_path: str, source_url: str) -> dict:
    with Image.open(BytesIO(data)) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        result, _ = engine(np.asarray(rgb))
    words = []
    for order, entry in enumerate(result or []):
        box, text, confidence = entry
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        words.append({
            "order": order,
            "text": str(text),
            "confidence": round(float(confidence), 6),
            "left": round(left, 2),
            "top": round(top, 2),
            "width": round(right - left, 2),
            "height": round(bottom - top, 2),
        })
    confidence = sum(w["confidence"] for w in words) / len(words) if words else None
    return {
        "archive_name": archive_name,
        "archive_id": archive_id,
        "image_path": image_path,
        "source_url": source_url,
        "source_sha256": sha256(data),
        "image_width": width,
        "image_height": height,
        "ocr_engine": "rapidocr_onnxruntime/RapidOCR (default model)",
        "ocr_text": line_text(words),
        "words": words,
        "token_count": len(words),
        "mean_confidence": round(confidence, 6) if confidence is not None else None,
        "assessment": "raw_ocr_staged_not_manually_reviewed",
        "review_status": "needs_review",
        "error": None,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    engine = RapidOCR()
    records: list[dict] = []
    for archive_path, archive_id in ARCHIVES:
        with ZipFile(archive_path) as archive:
            images = [entry for entry in archive.infolist() if not entry.is_dir() and entry.filename.lower().endswith(".png")]
            images.sort(key=lambda entry: entry.filename.casefold())
            for number, entry in enumerate(images, start=1):
                data = archive.read(entry)
                rec = ocr_bytes(
                    engine, data, archive_name=archive_path.name, archive_id=archive_id,
                    image_path=entry.filename,
                    source_url=f"drive://{archive_id}/{entry.filename}",
                )
                records.append(rec)
                if number % 10 == 0 or number == len(images):
                    print(f"{archive_path.name}: OCR {number}/{len(images)}", flush=True)
    for name in UPLOAD_NAMES:
        path = UPLOADS / name
        if not path.is_file():
            print(f"Missing upload; skipped: {path}", flush=True)
            continue
        rec = ocr_bytes(
            engine, path.read_bytes(), archive_name="chat_uploads", archive_id=None,
            image_path=name, source_url=f"upload://{name}",
        )
        records.append(rec)
        print(f"chat uploads: OCR {name}", flush=True)

    jsonl = OUT / "drive_and_upload_ocr.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = OUT / "image_manifest.csv"
    fields = ["archive_name", "archive_id", "image_path", "source_url", "source_sha256", "image_width",
              "image_height", "ocr_engine", "token_count", "mean_confidence", "assessment", "review_status", "error"]
    with manifest.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: record.get(field) for field in fields} for record in records)
    tokens = OUT / "ocr_tokens.csv"
    with tokens.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["archive_name", "archive_id", "image_path", "source_sha256", "order", "text", "confidence", "left", "top", "width", "height"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for record in records:
            for word in record["words"]:
                writer.writerow({"archive_name": record["archive_name"], "archive_id": record["archive_id"],
                                 "image_path": record["image_path"], "source_sha256": record["source_sha256"], **word})
    print(f"Saved {len(records)} image records to {jsonl}", flush=True)
    print(f"Saved image manifest to {manifest} and token table to {tokens}", flush=True)


if __name__ == "__main__":
    main()
