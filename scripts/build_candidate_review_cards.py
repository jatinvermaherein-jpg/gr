#!/usr/bin/env python3
"""Render original source screenshots alongside their unedited OCR candidate rows."""
from __future__ import annotations
import csv, hashlib, io, json, re, zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ROWS = ROOT / "data/drive_ocr_stage/stat_candidate_rows.jsonl"
ARCHIVES = ROOT / "data/source_archives"
OUT = ROOT / "data/drive_ocr_review_cards"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def natural_key(s: str):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", s)]

def main():
    rows = [json.loads(line) for line in ROWS.open(encoding="utf-8") if line.strip()]
    grouped = {}
    for row in rows:
        grouped.setdefault((row["archive_name"], row["source_path"]), []).append(row)
    OUT.mkdir(parents=True, exist_ok=True)
    index_rows = []
    by_archive = {"PILOT.zip": 0, "MA SS.zip": 0}
    for archive in ("PILOT.zip", "MA SS.zip"):
        group_items = [(k, v) for k, v in grouped.items() if k[0] == archive]
        group_items.sort(key=lambda kv: natural_key(kv[0][1]))
        with zipfile.ZipFile(ARCHIVES / archive) as zf:
            for (arc, source_path), candidates in group_items:
                by_archive[archive] += 1
                seq = by_archive[archive]
                prefix = "PILOT" if archive == "PILOT.zip" else "MECH"
                card_id = f"{prefix}-{seq:03d}"
                image_bytes = zf.read(source_path)
                source_sha = hashlib.sha256(image_bytes).hexdigest()
                expected_sha = candidates[0]["source_sha256"]
                if source_sha != expected_sha:
                    raise ValueError(f"Source SHA mismatch for {archive}/{source_path}: {source_sha} != {expected_sha}")
                im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                width, height = im.size
                panel_title_h, line_h = 54, 34
                panel_h = panel_title_h + line_h * len(candidates) + 18
                canvas = Image.new("RGB", (width, 45 + height + panel_h), "white")
                draw = ImageDraw.Draw(canvas)
                header = f"{card_id}  |  {archive}  |  {source_path}  |  SHA256 {source_sha[:16]}…"
                draw.text((14, 8), header, font=ImageFont.truetype(FONT_BOLD, 22), fill=(18, 32, 60))
                canvas.paste(im, (0, 45))
                panel_top = 45 + height
                draw.rectangle((0, panel_top, width, canvas.height), fill=(242, 245, 250))
                draw.text((14, panel_top + 8), "RAW OCR CANDIDATES — compare each label and C1–C7 against the screenshot above",
                          font=ImageFont.truetype(FONT_BOLD, 20), fill=(110, 45, 35))
                font = ImageFont.truetype(FONT, 19)
                for i, candidate in enumerate(sorted(candidates, key=lambda x: (float(x.get("baseline_y") or 0), x.get("stat_label_ocr", "")))):
                    cells = {int(c["column"]): c.get("raw_ocr") for c in candidate.get("cells", [])}
                    cell_str = " | ".join(f"C{n}:{cells.get(n) if cells.get(n) not in (None, '') else '∅'}" for n in range(1, 8))
                    label = candidate.get("stat_label_ocr", "") or "[blank label]"
                    line = f"{i+1:02d}. {label}   {cell_str}"
                    draw.text((14, panel_top + panel_title_h + i * line_h), line, font=font, fill=(20, 25, 30))
                    index_rows.append({
                        "card_id": card_id, "archive": archive, "source_path": source_path,
                        "source_sha256": source_sha, "entity_title_ocr": candidate.get("entity_title_ocr"),
                        "stat_label_ocr": label, "baseline_y": candidate.get("baseline_y"),
                        "raw_cells": json.dumps(cells, ensure_ascii=False, sort_keys=True),
                        "flags": candidate.get("flags"), "review_status": candidate.get("review_status"),
                    })
                out_path = OUT / f"{card_id}.jpg"
                canvas.save(out_path, quality=95, subsampling=0, optimize=True)
    with (OUT / "index.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(index_rows[0]))
        w.writeheader(); w.writerows(index_rows)
    print(f"Rendered {sum(by_archive.values())} screenshots / {len(index_rows)} candidate rows")
    print(f"Cards: {OUT}")
    print(f"Index: {OUT / 'index.csv'}")

if __name__ == "__main__": main()
