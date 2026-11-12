#!/usr/bin/env python3
"""Build a human-review workbook from raw Drive/archive OCR; all candidate values stay unverified."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "data" / "drive_ocr_stage"
INPUT = STAGE / "drive_and_upload_ocr.jsonl"
OUTPUT = STAGE / "drive_ocr_review.xlsx"


def title_of(record: dict) -> str:
    candidates = [w for w in record["words"] if w["top"] < 115 and w["left"] > 900]
    return max(candidates, key=lambda w: w["width"] * w["height"])["text"] if candidates else "(title not detected)"


def group_lines(words: list[dict]) -> list[dict]:
    ordered = sorted(words, key=lambda w: (w["top"] + w["height"] / 2, w["left"]))
    lines: list[dict] = []
    for word in ordered:
        cy = word["top"] + word["height"] / 2
        # Fixed-height game tables; allow for separate OCR boxes on a shared baseline.
        target = next((line for line in lines if abs(cy - line["cy"]) <= max(10, min(18, max(word["height"], line["height"]) * 0.52))), None)
        if target is None:
            lines.append({"cy": cy, "height": word["height"], "words": [word]})
        else:
            target["words"].append(word)
            target["cy"] = sum(w["top"] + w["height"] / 2 for w in target["words"]) / len(target["words"])
            target["height"] = max(target["height"], word["height"])
    lines.sort(key=lambda line: line["cy"])
    return lines


def candidate_rows(record: dict) -> list[dict]:
    rows = []
    centers = [427 + 213 * i for i in range(7)]
    for line in group_lines(record["words"]):
        if line["cy"] < 175:
            continue
        label_words = sorted([w for w in line["words"] if w["left"] < 385], key=lambda w: w["left"])
        label = " ".join(w["text"] for w in label_words).strip()
        if re.fullmatch(r"rank", label, flags=re.I):
            continue
        value_cells = [[] for _ in range(7)]
        for word in line["words"]:
            cx = word["left"] + word["width"] / 2
            if cx < 385:
                continue
            index = min(range(7), key=lambda i: abs(cx - centers[i]))
            if abs(cx - centers[index]) <= 112:
                value_cells[index].append(word)
        if not label and not any(value_cells):
            continue
        cell_texts, cell_scores = [], []
        for cell in value_cells:
            cell.sort(key=lambda w: w["left"])
            cell_texts.append(" ".join(w["text"] for w in cell))
            cell_scores.append(round(sum(w["confidence"] for w in cell) / len(cell), 3) if cell else None)
        flags = []
        if not label:
            flags.append("row label not detected")
        if any(not value for value in cell_texts):
            flags.append("one or more OCR cells missing")
        if any(score is not None and score < 0.60 for score in cell_scores):
            flags.append("low-confidence OCR cell")
        rows.append({"y": round(line["cy"], 1), "label": label, "values": cell_texts,
                     "scores": cell_scores, "flags": "; ".join(flags) or "no automatic flag; still requires review"})
    return rows


def add_header(ws, headers):
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="244A69")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def main() -> None:
    records = [json.loads(line) for line in INPUT.open(encoding="utf-8") if line.strip()]
    wb = Workbook()
    readme = wb.active
    readme.title = "Read me"
    for row in [
        ["Drive archive OCR review", "NOT VERIFIED CATALOG DATA"],
        ["Coverage", "PILOT.zip: 36 PNGs; MA SS.zip: 48 PNGs; chat attachments image-25.png through image-30.png: 6."],
        ["Attribution", "Drive archive author label is recorded as ‘Gladitor (user-attributed)’ at the user’s request; this attribution is not independently verified."],
        ["OCR method", "RapidOCR on original PNGs. Token text, confidence and original-image bounding boxes are preserved. No OCR value was promoted to verified."],
        ["Candidate sheets", "Rows are grouped by approximate text baseline; table cells are mapped left-to-right by fixed screenshot geometry. Raw candidates are unchanged OCR text. Empty means OCR did not return a token, not zero."],
        ["Ranks", "Columns C1–C6 are the six yellow rank columns; C7 is the pink final column. This is a positional label only; do not infer progression details beyond the screenshot/user convention."],
        ["Confidence", "RapidOCR confidence is a model score (0–1), not probability of correctness or human verification."],
        ["Source integrity", "Per-file SHA-256 and original ZIP paths are recorded on the Images sheet. The source ZIPs remain in /home/user/google_drive/."],
        ["Next review", "Compare every candidate with its image, correct OCR errors, confirm entity identity/version/source/date, then explicitly approve a reviewed snapshot before bot use."],
        ["Context uploads", "The six prior chat attachments are OCR’d separately; they are not treated as authoritative game data. image-26 has no detected text."],
    ]:
        readme.append(row)
    readme.column_dimensions["A"].width = 25
    readme.column_dimensions["B"].width = 110
    for row in readme.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    readme.freeze_panes = "A2"

    images = wb.create_sheet("Images")
    add_header(images, ["Archive", "Drive file ID", "Original image path", "Source reference", "Entity/title OCR",
                        "Width", "Height", "Token count", "Mean OCR confidence", "OCR text by row",
                        "Image SHA-256", "Review status"])
    for rec in records:
        images.append([rec["archive_name"], rec["archive_id"], rec["image_path"], rec["source_url"], title_of(rec),
                       rec["image_width"], rec["image_height"], rec["token_count"], rec["mean_confidence"], rec["ocr_text"],
                       rec["source_sha256"], rec["review_status"]])
    for col, width in enumerate([20, 42, 48, 68, 26, 10, 10, 12, 20, 100, 66, 22], 1):
        images.column_dimensions[get_column_letter(col)].width = width
    images.freeze_panes = "A2"
    for row in images.iter_rows(min_row=2):
        row[9].alignment = Alignment(wrap_text=True, vertical="top")

    pilots = wb.create_sheet("Pilot candidates")
    pilot_headers = ["Archive", "Original image path", "Pilot OCR title", "Stat label", "Baseline Y"]
    for i in range(1, 8):
        pilot_headers.extend([f"C{i} raw OCR", f"C{i} confidence"])
    pilot_headers.extend(["OCR flags", "Image SHA-256", "Review status"])
    add_header(pilots, pilot_headers)
    mechs = wb.create_sheet("Mech candidates")
    mech_headers = ["Archive", "Original image path", "Mech OCR title", "Stat label", "Baseline Y"]
    for i in range(1, 8):
        mech_headers.extend([f"C{i} raw OCR", f"C{i} confidence"])
    mech_headers.extend(["OCR flags", "Image SHA-256", "Review status"])
    add_header(mechs, mech_headers)
    uploads = wb.create_sheet("Chat uploads OCR")
    add_header(uploads, ["Original upload", "Image dimensions", "Token count", "Mean OCR confidence", "OCR text", "Image SHA-256", "Review status"])

    tokens = wb.create_sheet("OCR tokens")
    add_header(tokens, ["Archive", "Drive file ID", "Original image path", "Image SHA-256", "Detector order",
                        "Text", "Confidence (0–1)", "Left px", "Top px", "Width px", "Height px"])

    candidate_records = []
    for rec in records:
        if rec["archive_name"] == "chat_uploads":
            uploads.append([rec["image_path"], f"{rec['image_width']}×{rec['image_height']}", rec["token_count"],
                            rec["mean_confidence"], rec["ocr_text"], rec["source_sha256"], rec["review_status"]])
        else:
            row_target = pilots if rec["archive_name"] == "PILOT.zip" else mechs
            for row in candidate_rows(rec):
                candidate_records.append({
                    "archive_name": rec["archive_name"], "archive_id": rec["archive_id"],
                    "source_path": rec["image_path"], "source_url": rec["source_url"],
                    "source_sha256": rec["source_sha256"], "entity_title_ocr": title_of(rec),
                    "stat_label_ocr": row["label"], "baseline_y": row["y"],
                    "cells": [{"column": i + 1, "raw_ocr": value, "confidence": score}
                              for i, (value, score) in enumerate(zip(row["values"], row["scores"]))],
                    "flags": row["flags"], "review_status": "needs_review",
                })
                out = [rec["archive_name"], rec["image_path"], title_of(rec), row["label"], row["y"]]
                for value, score in zip(row["values"], row["scores"]):
                    out.extend([value, score])
                out.extend([row["flags"], rec["source_sha256"], "needs_review"])
                row_target.append(out)
        for word in rec["words"]:
            tokens.append([rec["archive_name"], rec["archive_id"], rec["image_path"], rec["source_sha256"],
                           word["order"], word["text"], word["confidence"], word["left"], word["top"],
                           word["width"], word["height"]])
    for ws in (pilots, mechs):
        ws.freeze_panes = "F2"
        ws.column_dimensions["A"].width = 18
        ws.column_dimensions["B"].width = 45
        ws.column_dimensions["C"].width = 24
        ws.column_dimensions["D"].width = 36
        ws.column_dimensions["E"].width = 13
        for i in range(6, 20):
            ws.column_dimensions[get_column_letter(i)].width = 16
        ws.column_dimensions[get_column_letter(20)].width = 38
        ws.column_dimensions[get_column_letter(21)].width = 66
        ws.column_dimensions[get_column_letter(22)].width = 20
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    for col, width in enumerate([30, 22, 14, 20, 100, 66, 20], 1):
        uploads.column_dimensions[get_column_letter(col)].width = width
    for col, width in enumerate([18, 42, 48, 66, 14, 38, 18, 12, 12, 12, 12], 1):
        tokens.column_dimensions[get_column_letter(col)].width = width
    for ws in (uploads, tokens):
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    candidates_path = STAGE / "stat_candidate_rows.jsonl"
    with candidates_path.open("w", encoding="utf-8") as f:
        for item in candidate_records:
            f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    wb.save(OUTPUT)
    print(f"Wrote {OUTPUT}: {len(records)} images, {pilots.max_row - 1} pilot candidate rows, "
          f"{mechs.max_row - 1} mech candidate rows, {tokens.max_row - 1} OCR tokens")


if __name__ == "__main__":
    main()
