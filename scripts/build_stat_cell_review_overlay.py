#!/usr/bin/env python3
"""Build a provenance-pinned, cell-level visual-review overlay without promoting candidates.

The raw candidate JSONL and the 90 separately approved raw-OCR records are read-only inputs.
Only manually entered screenshot observations from manual_visual_review_manifest.json are
used to replace/fill cells; all other nonblank cells are retained as visually matched values.
Candidate-row review_status values are never changed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "data/drive_ocr_stage"
ROWS_PATH = STAGE / "stat_candidate_rows.jsonl"
INDEX_PATH = ROOT / "data/drive_ocr_review_cards/index.csv"
MANIFEST_PATH = STAGE / "manual_visual_review_manifest.json"
ARCHIVES_DIR = ROOT / "data/source_archives"
SOURCE_WORKBOOK = STAGE / "drive_ocr_review.xlsx"
OUTPUT_WORKBOOK = STAGE / "drive_ocr_review_with_verified_cells.xlsx"
VERIFIED_CELLS_PATH = STAGE / "verified_stat_cells.jsonl"
CORRECTION_LOG_PATH = STAGE / "stat_cell_correction_log.jsonl"
ROW_REVIEW_LOG_PATH = STAGE / "candidate_row_visual_review_log.jsonl"
RECONSTRUCTIONS_PATH = STAGE / "verified_row_reconstructions.jsonl"
SUMMARY_PATH = STAGE / "stat_cell_review_summary.json"

EXPECTED_COUNTS = {"PILOT.zip": 72, "MA SS.zip": 451}
EXPECTED_ROW_COUNT = 523


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def label_norm(value) -> str:
    text = str(value or "").strip()
    return "" if text == "[blank label]" else text


def baseline_norm(value) -> float:
    return round(float(value or 0), 1)


def candidate_cells(row: dict) -> dict[str, str]:
    cells = {str(i): "" for i in range(1, 8)}
    for cell in row.get("cells", []):
        col = str(int(cell["column"]))
        cells[col] = "" if cell.get("raw_ocr") is None else str(cell.get("raw_ocr"))
    return cells


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _read_inputs():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    with ROWS_PATH.open(encoding="utf-8") as f:
        candidates = [json.loads(line) for line in f if line.strip()]
    with INDEX_PATH.open(encoding="utf-8-sig", newline="") as f:
        index_rows = list(csv.DictReader(f))
    if len(candidates) != EXPECTED_ROW_COUNT or len(index_rows) != EXPECTED_ROW_COUNT:
        raise ValueError(f"Expected {EXPECTED_ROW_COUNT} rows; got JSONL={len(candidates)}, index={len(index_rows)}")
    if Counter(row["archive_name"] for row in candidates) != Counter(EXPECTED_COUNTS):
        raise ValueError("Candidate archive row counts differ from the pinned 72-pilot/451-mech manifest.")
    if any(row.get("review_status") != "needs_review" for row in candidates):
        raise ValueError("At least one source candidate row no longer has needs_review status; refusing to proceed.")
    if any(row.get("review_status") != "needs_review" for row in index_rows):
        raise ValueError("At least one review-card index row no longer has needs_review status; refusing to proceed.")
    if not SOURCE_WORKBOOK.is_file():
        raise FileNotFoundError(SOURCE_WORKBOOK)
    return manifest, candidates, index_rows


def _join_candidates_to_index(candidates: list[dict], index_rows: list[dict]):
    index_by_key = defaultdict(list)
    for index_row in index_rows:
        key = (index_row["archive"], index_row["source_path"], baseline_norm(index_row["baseline_y"]), label_norm(index_row["stat_label_ocr"]))
        index_by_key[key].append(index_row)

    joined = []
    candidate_ids = set()
    index_usage = Counter()
    for source_index, row in enumerate(candidates, start=1):
        key = (row["archive_name"], row["source_path"], baseline_norm(row.get("baseline_y")), label_norm(row.get("stat_label_ocr")))
        matches = index_by_key.get(key, [])
        row_cells = candidate_cells(row)
        matches = [x for x in matches if {str(k): str(v or "") for k, v in json.loads(x["raw_cells"]).items()} == row_cells]
        if len(matches) != 1:
            raise ValueError(f"Could not uniquely join source candidate row {source_index}: {key} ({len(matches)} matches)")
        index_row = matches[0]
        index_usage[index_row["card_id"]] += 1
        if row.get("source_sha256") != index_row["source_sha256"]:
            raise ValueError(f"Candidate/index screenshot SHA mismatch: {index_row['card_id']} {row['source_path']}")
        identity = {
            "archive_id": row["archive_id"],
            "source_path": row["source_path"],
            "source_sha256": row["source_sha256"],
            "baseline_y": baseline_norm(row.get("baseline_y")),
            "stat_label_ocr": label_norm(row.get("stat_label_ocr")),
            "raw_cells": row_cells,
        }
        candidate_id = "cand-" + sha256_bytes(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8"))[:24]
        if candidate_id in candidate_ids:
            raise ValueError(f"Duplicate candidate id {candidate_id}")
        candidate_ids.add(candidate_id)
        joined.append({
            "source_index": source_index,
            "row": row,
            "index": index_row,
            "candidate_id": candidate_id,
            "raw_label": label_norm(row.get("stat_label_ocr")),
            "raw_cells": row_cells,
        })
    if len(index_usage) != 84:
        raise ValueError(f"Expected 84 screenshot cards; found {len(index_usage)}")
    if any(count < 1 for count in index_usage.values()):
        raise ValueError("At least one review card has no candidate row.")
    return joined


def _verify_archive_images(joined: list[dict]):
    zips = {}
    hashes = {}
    try:
        for entry in joined:
            row, idx = entry["row"], entry["index"]
            archive_name = row["archive_name"]
            if archive_name not in zips:
                archive_path = ARCHIVES_DIR / archive_name
                if not archive_path.is_file():
                    raise FileNotFoundError(archive_path)
                zips[archive_name] = zipfile.ZipFile(archive_path)
                hashes[archive_name] = sha256_file(archive_path)
            image_bytes = zips[archive_name].read(row["source_path"])
            actual = sha256_bytes(image_bytes)
            if actual != row["source_sha256"] or actual != idx["source_sha256"]:
                raise ValueError(f"Screenshot SHA-256 mismatch for {archive_name}/{row['source_path']}")
    finally:
        for zf in zips.values():
            zf.close()
    return hashes


def _make_override_maps(manifest: dict, joined: list[dict]):
    by_card_label = defaultdict(list)
    for entry in joined:
        by_card_label[(entry["index"]["card_id"], entry["raw_label"])].append(entry)

    cell_map = {}
    for card_id, labels in manifest.get("cell_overrides", {}).items():
        for raw_label, cols in labels.items():
            matches = by_card_label.get((card_id, label_norm(raw_label)), [])
            if len(matches) != 1:
                raise ValueError(f"Cell override does not resolve uniquely: {card_id} / {raw_label!r} ({len(matches)} rows)")
            entry = matches[0]
            for col, value in cols.items():
                if str(col) not in {str(i) for i in range(1, 8)} or not isinstance(value, str) or not value:
                    raise ValueError(f"Invalid cell override: {card_id} / {raw_label!r} / C{col}")
                cell_map[(entry["candidate_id"], str(col))] = value

    label_map = {}
    for card_id, labels in manifest.get("label_overrides", {}).items():
        for raw_label, verified_label in labels.items():
            matches = by_card_label.get((card_id, label_norm(raw_label)), [])
            if len(matches) != 1:
                raise ValueError(f"Label override does not resolve uniquely: {card_id} / {raw_label!r} ({len(matches)} rows)")
            entry = matches[0]
            if not isinstance(verified_label, str) or not verified_label.strip():
                raise ValueError(f"Invalid verified label: {card_id} / {raw_label!r}")
            label_map[entry["candidate_id"]] = verified_label.strip()
    return cell_map, label_map, by_card_label


def _resolve_reconstructions(manifest: dict, joined: list[dict], by_card_label):
    records = []
    excluded = set()
    fragment_ids_by_recon = {}
    for spec in manifest.get("row_reconstructions", []):
        card_id = spec["card_id"]
        fragments = []
        for fragment in spec["source_fragments"]:
            raw_label = label_norm(fragment.get("stat_label_ocr"))
            target_y = baseline_norm(fragment.get("baseline_y"))
            options = [
                entry for entry in joined
                if entry["index"]["card_id"] == card_id
                and entry["raw_label"] == raw_label
                and baseline_norm(entry["row"].get("baseline_y")) == target_y
            ]
            if len(options) != 1:
                raise ValueError(f"Reconstruction source fragment does not resolve uniquely: {card_id} y={target_y} label={raw_label!r}")
            entry = options[0]
            fragments.append(entry)
            excluded.add(entry["candidate_id"])
        if len(fragments) != len(spec["source_fragments"]):
            raise ValueError(f"Incomplete source fragments for {card_id} / {spec['stat_label']}")
        hashes = {f["row"]["source_sha256"] for f in fragments}
        paths = {f["row"]["source_path"] for f in fragments}
        if len(hashes) != 1 or len(paths) != 1:
            raise ValueError(f"Reconstruction mixes screenshots for {card_id} / {spec['stat_label']}")
        source = fragments[0]
        recon_identity = f"{card_id}\0{spec['stat_label']}\0{source['row']['source_sha256']}\0{','.join(f['candidate_id'] for f in fragments)}"
        recon_id = "recon-" + hashlib.sha256(recon_identity.encode("utf-8")).hexdigest()[:24]
        cells = {str(i): str(spec["cells"][str(i)]) for i in range(1, 8)}
        record = {
            "record_type": "verified_reconstructed_stat_row",
            "reconstruction_id": recon_id,
            "card_id": card_id,
            "archive_name": source["row"]["archive_name"],
            "archive_id": source["row"]["archive_id"],
            "source_path": source["row"]["source_path"],
            "source_url": source["row"]["source_url"],
            "source_sha256": source["row"]["source_sha256"],
            "entity": spec["entity"],
            "stat_label": spec["stat_label"],
            "baseline_y": float(spec["baseline_y"]),
            "verified_cells": cells,
            "source_candidate_ids": [f["candidate_id"] for f in fragments],
            "source_fragment_text": [
                {"candidate_id": f["candidate_id"], "baseline_y": f["row"].get("baseline_y"),
                 "stat_label_ocr": f["row"].get("stat_label_ocr") or "",
                 "raw_cells": f["raw_cells"]}
                for f in fragments
            ],
            "evidence_note": spec["evidence_note"],
            "reviewed_on": manifest["reviewed_on"],
            "reviewer": manifest["reviewer"],
            "review_status": "visually_verified_reconstruction",
            "source_candidate_status": "needs_review (unchanged)",
        }
        records.append(record)
        fragment_ids_by_recon[recon_id] = [f["candidate_id"] for f in fragments]
    return records, excluded, fragment_ids_by_recon


def _build_overlays(manifest: dict, joined: list[dict], cell_map: dict, label_map: dict, reconstructions: list[dict], excluded: set[str]):
    verified_cells = []
    correction_log = []
    row_review_log = []
    correction_counts = Counter()
    by_candidate_recon = defaultdict(list)
    for recon in reconstructions:
        for candidate_id in recon["source_candidate_ids"]:
            by_candidate_recon[candidate_id].append(recon["reconstruction_id"])

    for entry in joined:
        row, idx = entry["row"], entry["index"]
        candidate_id = entry["candidate_id"]
        card_id = idx["card_id"]
        raw_label = entry["raw_label"]
        verified_label = label_map.get(candidate_id, raw_label)
        is_fragment = candidate_id in excluded
        verified_count = 0
        corrected_cell_count = 0
        filled_count = 0
        whitespace_count = 0
        raw_blanks = []
        if not is_fragment:
            for col in range(1, 8):
                col_key = str(col)
                raw_value = entry["raw_cells"].get(col_key, "")
                explicit = (candidate_id, col_key) in cell_map
                if explicit:
                    verified_value = cell_map[(candidate_id, col_key)]
                elif raw_value:
                    verified_value = raw_value.strip()
                else:
                    raw_blanks.append(f"C{col}")
                    continue
                if not verified_value:
                    raise ValueError(f"Empty verified value for {candidate_id} C{col}")
                verified_count += 1
                if not raw_value:
                    filled_count += 1
                if raw_value and verified_value != raw_value:
                    corrected_cell_count += 1
                if raw_value and verified_value == raw_value.strip() and verified_value != raw_value:
                    whitespace_count += 1

                cell_record = {
                    "record_type": "verified_candidate_cell",
                    "candidate_id": candidate_id,
                    "source_candidate_index": entry["source_index"],
                    "card_id": card_id,
                    "archive_name": row["archive_name"],
                    "archive_id": row["archive_id"],
                    "source_path": row["source_path"],
                    "source_url": row["source_url"],
                    "source_sha256": row["source_sha256"],
                    "entity_title_ocr": row.get("entity_title_ocr"),
                    "stat_label_ocr": row.get("stat_label_ocr") or "",
                    "verified_stat_label": verified_label,
                    "baseline_y": float(row.get("baseline_y") or 0),
                    "rank_column": col,
                    "raw_ocr_value": raw_value if raw_value else None,
                    "verified_value": verified_value,
                    "reviewed_on": manifest["reviewed_on"],
                    "reviewer": manifest["reviewer"],
                    "verification_basis": "Manual visual comparison to the original screenshot; screenshot SHA-256 revalidated against the source ZIP.",
                    "source_candidate_status": row["review_status"],
                }
                verified_cells.append(cell_record)

                if not raw_value:
                    correction_type = "missing_ocr_value_recovered"
                    correction_counts["filled_missing_cells"] += 1
                elif verified_value == raw_value:
                    correction_type = None
                elif verified_value == raw_value.strip():
                    correction_type = "surrounding_whitespace_normalized"
                    correction_counts["whitespace_normalizations"] += 1
                else:
                    correction_type = "ocr_value_corrected_from_screenshot"
                    correction_counts["ocr_value_corrections"] += 1
                if correction_type:
                    correction_log.append({
                        **{k: cell_record[k] for k in (
                            "candidate_id", "source_candidate_index", "card_id", "archive_name", "archive_id",
                            "source_path", "source_url", "source_sha256", "entity_title_ocr", "stat_label_ocr",
                            "verified_stat_label", "baseline_y", "rank_column", "raw_ocr_value", "verified_value",
                            "reviewed_on", "reviewer", "source_candidate_status"
                        )},
                        "record_type": "cell_correction",
                        "correction_type": correction_type,
                        "evidence_note": f"Source screenshot {row['source_path']} (SHA-256 {row['source_sha256']}) visibly shows {verified_value!r} in C{col} for {verified_label!r}.",
                    })

        if verified_label != raw_label:
            correction_counts["label_corrections"] += 1
            correction_log.append({
                "record_type": "label_correction",
                "candidate_id": candidate_id,
                "source_candidate_index": entry["source_index"],
                "card_id": card_id,
                "archive_name": row["archive_name"],
                "archive_id": row["archive_id"],
                "source_path": row["source_path"],
                "source_url": row["source_url"],
                "source_sha256": row["source_sha256"],
                "entity_title_ocr": row.get("entity_title_ocr"),
                "stat_label_ocr": row.get("stat_label_ocr") or "",
                "verified_stat_label": verified_label,
                "baseline_y": float(row.get("baseline_y") or 0),
                "reviewed_on": manifest["reviewed_on"],
                "reviewer": manifest["reviewer"],
                "source_candidate_status": row["review_status"],
                "correction_type": "screenshot_label_transcription",
                "evidence_note": f"The screenshot visibly labels this row {verified_label!r}; OCR label {raw_label!r} included a spacing/neighbor-cell artifact.",
            })

        if is_fragment:
            overlay_result = "source_fragment_reconstructed_separately"
        else:
            overlay_result = "visually_compared_cell_overlay"
        row_review_log.append({
            "candidate_id": candidate_id,
            "source_candidate_index": entry["source_index"],
            "card_id": card_id,
            "archive_name": row["archive_name"],
            "archive_id": row["archive_id"],
            "source_path": row["source_path"],
            "source_url": row["source_url"],
            "source_sha256": row["source_sha256"],
            "entity_title_ocr": row.get("entity_title_ocr"),
            "stat_label_ocr": row.get("stat_label_ocr") or "",
            "verified_stat_label": verified_label,
            "baseline_y": float(row.get("baseline_y") or 0),
            "source_candidate_status": row["review_status"],
            "overlay_review_result": overlay_result,
            "verified_cell_count": verified_count,
            "corrected_cell_count": corrected_cell_count,
            "filled_missing_cell_count": filled_count,
            "whitespace_normalized_cell_count": whitespace_count,
            "source_blank_columns_not_emitted": ", ".join(raw_blanks),
            "reconstruction_ids": ", ".join(by_candidate_recon.get(candidate_id, [])),
            "reviewed_on": manifest["reviewed_on"],
            "reviewer": manifest["reviewer"],
        })

    reconstructed_cells = []
    reconstruction_log = []
    for recon in reconstructions:
        reconstruction_log.append({
            "record_type": "row_reconstruction",
            "reconstruction_id": recon["reconstruction_id"],
            "card_id": recon["card_id"],
            "archive_name": recon["archive_name"],
            "archive_id": recon["archive_id"],
            "source_path": recon["source_path"],
            "source_url": recon["source_url"],
            "source_sha256": recon["source_sha256"],
            "entity": recon["entity"],
            "verified_stat_label": recon["stat_label"],
            "baseline_y": recon["baseline_y"],
            "source_candidate_ids": ", ".join(recon["source_candidate_ids"]),
            "reviewed_on": recon["reviewed_on"],
            "reviewer": recon["reviewer"],
            "source_candidate_status": recon["source_candidate_status"],
            "correction_type": "logical_row_reconstruction_from_wrapped_ocr_fragments",
            "evidence_note": recon["evidence_note"],
        })
        for col in range(1, 8):
            col_key = str(col)
            fragments = []
            for fragment in recon["source_fragment_text"]:
                raw_fragment = fragment["raw_cells"].get(col_key, "")
                if raw_fragment:
                    fragments.append({
                        "candidate_id": fragment["candidate_id"],
                        "baseline_y": fragment["baseline_y"],
                        "raw_ocr_value": raw_fragment,
                    })
            cell = {
                "record_type": "verified_reconstructed_cell",
                "candidate_id": recon["reconstruction_id"] + f"-C{col}",
                "reconstruction_id": recon["reconstruction_id"],
                "card_id": recon["card_id"],
                "archive_name": recon["archive_name"],
                "archive_id": recon["archive_id"],
                "source_path": recon["source_path"],
                "source_url": recon["source_url"],
                "source_sha256": recon["source_sha256"],
                "entity_title_ocr": recon["entity"],
                "stat_label_ocr": "(wrapped OCR fragments)",
                "verified_stat_label": recon["stat_label"],
                "baseline_y": recon["baseline_y"],
                "rank_column": col,
                "raw_ocr_fragments": fragments,
                "verified_value": recon["verified_cells"][col_key],
                "reviewed_on": recon["reviewed_on"],
                "reviewer": recon["reviewer"],
                "verification_basis": "Manual visual reconstruction from the complete wrapped row in the original screenshot; all source-fragment candidate IDs and the image SHA-256 are retained.",
                "source_candidate_status": recon["source_candidate_status"],
            }
            reconstructed_cells.append(cell)

    verified_cells.extend(reconstructed_cells)
    correction_log.extend(reconstruction_log)
    correction_log.sort(key=lambda x: (x.get("card_id", ""), x.get("baseline_y", 0), x.get("rank_column", 0), x.get("record_type", "")))
    row_review_log.sort(key=lambda x: x["source_candidate_index"])
    reconstructions.sort(key=lambda x: x["card_id"])
    return verified_cells, correction_log, row_review_log, reconstructions, correction_counts


def _sheet_from_records(wb, title: str, headers: list[str], records: list[dict], *, max_width: int = 52):
    if title in wb.sheetnames:
        del wb[title]
    ws = wb.create_sheet(title)
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(name="Aptos", size=10, bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for record in records:
        values = []
        for header in headers:
            value = record.get(header, "")
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            values.append(value)
        ws.append(values)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 32
    for col_index, header in enumerate(headers, start=1):
        values = [str(header)] + [str(ws.cell(row=r, column=col_index).value or "") for r in range(2, min(ws.max_row, 150) + 1)]
        width = min(max_width, max(12, min(48, max((len(v) for v in values), default=12) + 2)))
        if header in {"verified_value", "evidence_note", "source_blank_columns_not_emitted", "raw_ocr_fragments", "source_candidate_ids", "verified_cells"}:
            width = max(width, min(max_width, 34))
        ws.column_dimensions[get_column_letter(col_index)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.column in (1, 2):
                cell.font = Font(name="Aptos", size=9, color="17365D")
    return ws


def _write_workbook(verified_cells: list[dict], correction_log: list[dict], row_review_log: list[dict], reconstructions: list[dict]):
    wb = load_workbook(SOURCE_WORKBOOK)
    readme = wb["Read me"]
    readme.append(("Cell-level review overlay", "A separate screenshot-pinned overlay is included in the added review sheets. Original candidate sheets and their needs_review statuses were not changed."))
    readme.append(("Verified values", f"{sum(1 for x in verified_cells if x['record_type'] == 'verified_candidate_cell')} directly compared candidate cells plus {sum(1 for x in verified_cells if x['record_type'] == 'verified_reconstructed_cell')} reconstructed cells; see Verified stat cells and Row reconstructions."))
    readme.append(("Correction policy", "Only screenshot-readable values are included. Missing/ambiguous cells not present in the overlay remain blank or gated; no progression-based guesses were added. The 90 approved raw OCR records remain separate."))
    readme.append(("Source identity", "Each reviewed cell includes the archive path and screenshot SHA-256. The generation script rehashed each image from the source ZIP before writing this workbook."))

    cell_headers = [
        "record_type", "candidate_id", "reconstruction_id", "source_candidate_index", "card_id", "archive_name", "archive_id",
        "source_path", "source_url", "source_sha256", "entity_title_ocr", "stat_label_ocr", "verified_stat_label",
        "baseline_y", "rank_column", "raw_ocr_value", "verified_value", "raw_ocr_fragments", "source_candidate_status",
        "review_status", "reviewed_on", "reviewer", "verification_basis"
    ]
    correction_headers = [
        "record_type", "correction_type", "candidate_id", "reconstruction_id", "source_candidate_index", "card_id",
        "archive_name", "archive_id", "source_path", "source_url", "source_sha256", "entity_title_ocr",
        "stat_label_ocr", "verified_stat_label", "baseline_y", "rank_column", "raw_ocr_value", "verified_value",
        "source_candidate_ids", "source_candidate_status", "reviewed_on", "reviewer", "evidence_note"
    ]
    row_headers = [
        "candidate_id", "source_candidate_index", "card_id", "archive_name", "archive_id", "source_path", "source_url",
        "source_sha256", "entity_title_ocr", "stat_label_ocr", "verified_stat_label", "baseline_y",
        "source_candidate_status", "overlay_review_result", "verified_cell_count", "corrected_cell_count",
        "filled_missing_cell_count", "whitespace_normalized_cell_count", "source_blank_columns_not_emitted",
        "reconstruction_ids", "reviewed_on", "reviewer"
    ]
    recon_headers = [
        "reconstruction_id", "card_id", "archive_name", "archive_id", "source_path", "source_url", "source_sha256",
        "entity", "stat_label", "baseline_y", "C1", "C2", "C3", "C4", "C5", "C6", "C7",
        "source_candidate_ids", "source_fragment_text", "source_candidate_status", "review_status", "reviewed_on", "reviewer", "evidence_note"
    ]
    _sheet_from_records(wb, "Verified stat cells", cell_headers, verified_cells)
    _sheet_from_records(wb, "Cell correction log", correction_headers, correction_log)
    _sheet_from_records(wb, "Candidate row review", row_headers, row_review_log)
    recon_rows = []
    for r in reconstructions:
        recon_rows.append({
            "reconstruction_id": r["reconstruction_id"], "card_id": r["card_id"], "archive_name": r["archive_name"],
            "archive_id": r["archive_id"], "source_path": r["source_path"], "source_url": r["source_url"],
            "source_sha256": r["source_sha256"], "entity": r["entity"], "stat_label": r["stat_label"],
            "baseline_y": r["baseline_y"], **{f"C{i}": r["verified_cells"][str(i)] for i in range(1, 8)},
            "source_candidate_ids": json.dumps(r["source_candidate_ids"], ensure_ascii=False),
            "source_fragment_text": json.dumps(r["source_fragment_text"], ensure_ascii=False),
            "source_candidate_status": r["source_candidate_status"], "review_status": r["review_status"],
            "reviewed_on": r["reviewed_on"], "reviewer": r["reviewer"], "evidence_note": r["evidence_note"],
        })
    _sheet_from_records(wb, "Row reconstructions", recon_headers, recon_rows)
    wb.save(OUTPUT_WORKBOOK)

    check = load_workbook(OUTPUT_WORKBOOK, read_only=True, data_only=True)
    required = {"Verified stat cells", "Cell correction log", "Candidate row review", "Row reconstructions"}
    if not required.issubset(set(check.sheetnames)):
        raise ValueError("Output workbook missing review sheets")
    for sheet_name, expected in (("Pilot candidates", EXPECTED_COUNTS["PILOT.zip"]), ("Mech candidates", EXPECTED_COUNTS["MA SS.zip"])):
        ws = check[sheet_name]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        review_col = next((i + 1 for i, name in enumerate(headers) if str(name or "").strip().casefold() == "review status"), None)
        if review_col is None:
            raise ValueError(f"No Review status column in {sheet_name}")
        status_values = [ws.cell(row=r, column=review_col).value for r in range(2, ws.max_row + 1)]
        if len(status_values) != expected or any(v != "needs_review" for v in status_values):
            raise ValueError(f"Candidate rows/statuses changed in {sheet_name}")
    check.close()


def main():
    manifest, candidates, index_rows = _read_inputs()
    joined = _join_candidates_to_index(candidates, index_rows)
    archive_hashes = _verify_archive_images(joined)
    cell_map, label_map, by_card_label = _make_override_maps(manifest, joined)
    reconstructions, excluded, _ = _resolve_reconstructions(manifest, joined, by_card_label)
    verified_cells, correction_log, row_review_log, reconstructions, correction_counts = _build_overlays(
        manifest, joined, cell_map, label_map, reconstructions, excluded
    )

    # Source candidate rows remain gated, and all 523 row identities must still be represented in the audit log.
    if len(row_review_log) != EXPECTED_ROW_COUNT:
        raise ValueError(f"Expected {EXPECTED_ROW_COUNT} candidate review rows; got {len(row_review_log)}")
    if len({r["candidate_id"] for r in row_review_log}) != EXPECTED_ROW_COUNT:
        raise ValueError("Candidate review log has duplicate/missing IDs")
    if any(r["source_candidate_status"] != "needs_review" for r in row_review_log):
        raise ValueError("A source candidate status was unexpectedly changed")

    # The existing workbook is never overwritten; a reviewed copy is written as a new deliverable.
    source_jsonl_sha = sha256_file(ROWS_PATH)
    source_workbook_sha = sha256_file(SOURCE_WORKBOOK)
    write_jsonl(VERIFIED_CELLS_PATH, verified_cells)
    write_jsonl(CORRECTION_LOG_PATH, correction_log)
    write_jsonl(ROW_REVIEW_LOG_PATH, row_review_log)
    write_jsonl(RECONSTRUCTIONS_PATH, reconstructions)
    _write_workbook(verified_cells, correction_log, row_review_log, reconstructions)

    # Ensure read-only inputs still match the exact byte fingerprints captured before output creation.
    if sha256_file(ROWS_PATH) != source_jsonl_sha or sha256_file(SOURCE_WORKBOOK) != source_workbook_sha:
        raise ValueError("A source input changed during overlay generation")
    summary = {
        "schema_version": 1,
        "reviewed_on": manifest["reviewed_on"],
        "reviewer": manifest["reviewer"],
        "candidate_rows": EXPECTED_ROW_COUNT,
        "candidate_rows_by_archive": EXPECTED_COUNTS,
        "screenshot_count": len({(x["row"]["archive_name"], x["row"]["source_path"]) for x in joined}),
        "screenshot_archives_rehashed": True,
        "archive_sha256": archive_hashes,
        "source_candidate_jsonl_sha256": source_jsonl_sha,
        "source_review_workbook_sha256": source_workbook_sha,
        "verified_cell_records": len(verified_cells),
        "verified_source_candidate_cells": sum(x["record_type"] == "verified_candidate_cell" for x in verified_cells),
        "verified_reconstructed_cells": sum(x["record_type"] == "verified_reconstructed_cell" for x in verified_cells),
        "cell_and_label_correction_log_records": len(correction_log),
        "changed_cell_counts": dict(correction_counts),
        "reconstructed_rows": len(reconstructions),
        "source_fragment_candidate_rows_kept_needs_review": len(excluded),
        "source_candidate_status_counts": dict(Counter(r["source_candidate_status"] for r in row_review_log)),
        "source_files_unchanged": True,
        "source_candidate_rows_promoted": 0,
        "raw_ocr_approved_records_changed": 0,
        "output_files": {
            "verified_cells_jsonl": str(VERIFIED_CELLS_PATH.relative_to(ROOT)),
            "correction_log_jsonl": str(CORRECTION_LOG_PATH.relative_to(ROOT)),
            "candidate_row_review_jsonl": str(ROW_REVIEW_LOG_PATH.relative_to(ROOT)),
            "row_reconstructions_jsonl": str(RECONSTRUCTIONS_PATH.relative_to(ROOT)),
            "reviewed_workbook": str(OUTPUT_WORKBOOK.relative_to(ROOT)),
        },
        "source_note": manifest["scope"],
        "unresolved_notes": manifest.get("unresolved_notes", []),
    }
    summary["output_sha256"] = {
        str(path.relative_to(ROOT)): sha256_file(path)
        for path in (VERIFIED_CELLS_PATH, CORRECTION_LOG_PATH, ROW_REVIEW_LOG_PATH, RECONSTRUCTIONS_PATH, OUTPUT_WORKBOOK)
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "candidate_rows": summary["candidate_rows"],
        "screenshots": summary["screenshot_count"],
        "verified_cell_records": summary["verified_cell_records"],
        "correction_log_records": summary["cell_and_label_correction_log_records"],
        "changed_cell_counts": summary["changed_cell_counts"],
        "reconstructed_rows": summary["reconstructed_rows"],
        "source_statuses": summary["source_candidate_status_counts"],
        "output_workbook": str(OUTPUT_WORKBOOK),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
