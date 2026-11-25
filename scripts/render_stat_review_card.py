#!/usr/bin/env python3
"""Render selected screenshot/OCR review cards on demand to keep workspace usage low."""
from __future__ import annotations
import argparse, csv, hashlib, io, json, re, zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "data/drive_ocr_review_cards/index.csv"
ARCHIVES = ROOT / "data/source_archives"
OUT = ROOT / "data/drive_ocr_stage/visual_review_tmp"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def natural_key(s: str):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", s)]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("card_ids",nargs="+")
    parser.add_argument("--output-dir",default=str(OUT))
    args=parser.parse_args()
    with INDEX.open(encoding="utf-8-sig",newline="") as f: idx=list(csv.DictReader(f))
    by_card={}
    for row in idx: by_card.setdefault(row["card_id"],[]).append(row)
    output=Path(args.output_dir);output.mkdir(parents=True,exist_ok=True)
    for card_id in args.card_ids:
        rows=by_card.get(card_id)
        if not rows: raise SystemExit(f"Unknown card id {card_id}")
        archive=rows[0]["archive"];source_path=rows[0]["source_path"];expected_sha=rows[0]["source_sha256"]
        if any(r["source_path"]!=source_path or r["archive"]!=archive or r["source_sha256"]!=expected_sha for r in rows):
            raise SystemExit(f"Inconsistent source metadata in {card_id}")
        with zipfile.ZipFile(ARCHIVES/archive) as zf: image_bytes=zf.read(source_path)
        actual=hashlib.sha256(image_bytes).hexdigest()
        if actual!=expected_sha: raise SystemExit(f"Screenshot hash mismatch {card_id}")
        im=Image.open(io.BytesIO(image_bytes)).convert("RGB");width,height=im.size
        rows.sort(key=lambda r:(float(r["baseline_y"]),r["stat_label_ocr"].casefold()))
        panel_title_h,line_h=54,34;panel_h=panel_title_h+line_h*len(rows)+18
        canvas=Image.new("RGB",(width,45+height+panel_h),"white");draw=ImageDraw.Draw(canvas)
        header=f"{card_id}  |  {archive}  |  {source_path}  |  SHA256 {actual[:16]}…"
        draw.text((14,8),header,font=ImageFont.truetype(FONT_BOLD,22),fill=(18,32,60));canvas.paste(im,(0,45))
        panel_top=45+height;draw.rectangle((0,panel_top,width,canvas.height),fill=(242,245,250))
        draw.text((14,panel_top+8),"RAW OCR CANDIDATES — compare each label and C1–C7 against the screenshot above",font=ImageFont.truetype(FONT_BOLD,20),fill=(110,45,35))
        font=ImageFont.truetype(FONT,19)
        for i,row in enumerate(rows):
            cells={str(k):str(v or "") for k,v in json.loads(row["raw_cells"]).items()}
            cell_str=" | ".join(f"C{n}:{cells.get(str(n)) or '∅'}" for n in range(1,8))
            label=row.get("stat_label_ocr","") or "[blank label]"
            draw.text((14,panel_top+panel_title_h+i*line_h),f"{i+1:02d}. {label}   {cell_str}",font=font,fill=(20,25,30))
        path=output/f"{card_id}.jpg";canvas.save(path,quality=95,subsampling=0,optimize=True)
        print(f"{path}\t{actual}\t{len(rows)} rows")
if __name__=="__main__":main()
