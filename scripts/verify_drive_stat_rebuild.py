#!/usr/bin/env python3
"""Validate recovered Drive screenshot-stat inputs without promoting OCR candidates.

This checks the source ZIPs, candidate JSONL, screenshot references, review-card index,
raw OCR manifest, and the staging catalog. It deliberately does not treat a hash/count
check as visual verification and does not write to SQLite or Supabase.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "data/drive_ocr_stage"
ARCHIVES = ROOT / "data/source_archives"
APPROVAL_PATH = STAGE / "drive_stat_cell_user_approval.json"
CANDIDATES_PATH = STAGE / "stat_candidate_rows.jsonl"
RAW_OCR_PATH = STAGE / "drive_and_upload_ocr.jsonl"
WORKBOOK_PATH = STAGE / "drive_ocr_review.xlsx"
INDEX_PATH = ROOT / "data/drive_ocr_review_cards/index.csv"
STAGING_DB_GZ = ROOT / "data/mech_bot_staging.sqlite3.gz"
REPORT_PATH = STAGE / "rebuild_validation_report.json"
PROMOTION_AUDIT_PATH = STAGE / "supabase_promotion_batches/promotion_verification.json"
PROMOTION_EXPORT_MANIFEST_PATH = STAGE / "supabase_promotion_batches/export_manifest.json"
BACKUP_RECEIPT_PATH = STAGE / "backup_upload_receipt.json"
MANUAL_REVIEW_PATH = STAGE / "manual_visual_review_manifest.json"
FRESH_AUDIT_PATH = STAGE / "fresh_visual_rebuild_audit.json"
FRESH_OUTPUTS = {
    "direct_verified_cells": STAGE / "verified_stat_cells_fresh.jsonl",
    "combined_overlay": STAGE / "verified_stat_overlay_fresh.jsonl",
    "provenance": STAGE / "cell_level_provenance_fresh.jsonl",
    "corrections": STAGE / "stat_cell_correction_log_fresh.jsonl",
    "candidate_row_log": STAGE / "candidate_row_visual_review_log_fresh.jsonl",
    "reconstructions": STAGE / "verified_row_reconstructions_fresh.jsonl",
    "reconstructed_cells": STAGE / "reconstructed_stat_cells_fresh.jsonl",
    "gated_cells": STAGE / "gated_ambiguous_cells_fresh.jsonl",
    "screenshot_manifest": STAGE / "fresh_visual_source_screenshot_manifest.jsonl",
}

PINNED_RAW_OCR_SHA256 = "dfc42f5cab6e7ce74526c9700bc1aedd308c2f09f2e356fdb33331cafa5537c5"
EXPECTED_SOURCE_ROWS = {"PILOT.zip": 72, "MA SS.zip": 451}
EXPECTED_ARCHIVE_IDS = {
    "PILOT.zip": "1o7-ceXZ8bQqhKK1kpVchnK28nE_XEH61",
    "MA SS.zip": "1TY_Mal19TN_5lkmvQkvSBHrDexHFqHcE",
}
MISSING_REVIEW_FILES = [
    "manual_visual_review_manifest.json",
    "verified_stat_cells.jsonl",
    "stat_cell_correction_log.jsonl",
    "candidate_row_visual_review_log.jsonl",
    "verified_row_reconstructions.jsonl",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def report_local_date() -> str:
    """Use an explicit reproducibility date when supplied; otherwise today's local date."""
    override = os.environ.get("MECHBOT_REPORT_LOCAL_DATE")
    return date.fromisoformat(override).isoformat() if override else date.today().isoformat()


def md5_of_lines(lines: list[str]) -> str:
    return hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()


def label_norm(value) -> str:
    text = str(value or "").strip()
    return "" if text == "[blank label]" else text


def baseline_norm(value) -> float:
    return round(float(value or 0), 1)


def candidate_cells(row: dict) -> dict[str, str]:
    cells = {str(i): "" for i in range(1, 8)}
    for cell in row.get("cells", []):
        col = str(int(cell["column"]))
        if col not in cells:
            raise ValueError(f"Invalid rank column {col}")
        cells[col] = "" if cell.get("raw_ocr") is None else str(cell.get("raw_ocr"))
    return cells


def main() -> None:
    approval = json.loads(APPROVAL_PATH.read_text(encoding="utf-8"))
    candidates = [json.loads(line) for line in CANDIDATES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    raw_ocr = [json.loads(line) for line in RAW_OCR_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    candidate_sha = sha256_file(CANDIDATES_PATH)
    raw_ocr_sha = sha256_file(RAW_OCR_PATH)
    archive_hashes = {name: sha256_file(ARCHIVES / name) for name in EXPECTED_SOURCE_ROWS}
    expected_archives = approval["pinned_inputs"]["source_archive_sha256"]
    if candidate_sha != approval["pinned_inputs"]["source_candidate_jsonl_sha256"]:
        raise SystemExit("Candidate JSONL SHA-256 does not match the user approval pin.")
    if archive_hashes != expected_archives:
        raise SystemExit(f"Source ZIP SHA-256 mismatch: {archive_hashes}")
    if raw_ocr_sha != PINNED_RAW_OCR_SHA256:
        raise SystemExit(f"Raw OCR manifest SHA-256 mismatch: {raw_ocr_sha}")

    counts = Counter(row["archive_name"] for row in candidates)
    if counts != Counter(EXPECTED_SOURCE_ROWS):
        raise SystemExit(f"Candidate archive counts do not match: {counts}")
    if len(candidates) != approval["scope"]["candidate_rows"]:
        raise SystemExit(f"Candidate row count mismatch: {len(candidates)}")
    if any(row.get("review_status") != "needs_review" for row in candidates):
        raise SystemExit("Recovered source candidate status changed; refusing validation.")

    # Verify archive integrity and every distinct screenshot reference against the actual bytes.
    zips = {}
    archive_entries = {}
    image_hashes = {}
    try:
        for name in EXPECTED_SOURCE_ROWS:
            zf = zipfile.ZipFile(ARCHIVES / name)
            bad = zf.testzip()
            if bad:
                raise SystemExit(f"ZIP integrity check failed for {name}: {bad}")
            zips[name] = zf
            archive_entries[name] = set(zf.namelist())

        for row in candidates:
            archive_name = row["archive_name"]
            source_path = row["source_path"]
            if source_path not in archive_entries[archive_name]:
                raise SystemExit(f"Missing screenshot in source ZIP: {archive_name}/{source_path}")
            key = (archive_name, source_path)
            if key not in image_hashes:
                image_hashes[key] = sha256_bytes(zips[archive_name].read(source_path))
            if image_hashes[key] != row["source_sha256"]:
                raise SystemExit(f"Screenshot SHA-256 mismatch: {archive_name}/{source_path}")
            if row.get("archive_id") != EXPECTED_ARCHIVE_IDS[archive_name]:
                raise SystemExit(f"Unexpected Drive archive ID on candidate: {archive_name}")
            if row.get("source_url") != f"drive://{row['archive_id']}/{source_path}":
                raise SystemExit(f"Source URL/path mismatch: {archive_name}/{source_path}")
            cols = [int(c["column"]) for c in row.get("cells", [])]
            if sorted(cols) != list(range(1, 8)):
                raise SystemExit(f"Candidate does not contain exactly C1-C7: {archive_name}/{source_path}")
    finally:
        for zf in zips.values():
            zf.close()

    # Confirm a unique candidate identity for each source row and exact linkage to review cards.
    index_rows = list(csv.DictReader(INDEX_PATH.open(encoding="utf-8-sig", newline="")))
    by_index_key = defaultdict(list)
    for idx in index_rows:
        key = (idx["archive"], idx["source_path"], baseline_norm(idx["baseline_y"]), label_norm(idx["stat_label_ocr"]))
        by_index_key[key].append(idx)

    candidate_ids = set()
    for row in candidates:
        key = (row["archive_name"], row["source_path"], baseline_norm(row.get("baseline_y")), label_norm(row.get("stat_label_ocr")))
        matches = [idx for idx in by_index_key.get(key, [])
                   if {str(k): str(v or "") for k, v in json.loads(idx["raw_cells"]).items()} == candidate_cells(row)]
        if len(matches) != 1:
            raise SystemExit(f"Review-card index join is not unique for {key}: {len(matches)}")
        if matches[0]["source_sha256"] != row["source_sha256"]:
            raise SystemExit(f"Review-card index screenshot hash mismatch: {key}")
        identity = {
            "archive_id": row["archive_id"], "source_path": row["source_path"],
            "source_sha256": row["source_sha256"], "baseline_y": baseline_norm(row.get("baseline_y")),
            "stat_label_ocr": label_norm(row.get("stat_label_ocr")), "raw_cells": candidate_cells(row),
        }
        cid = "cand-" + sha256_bytes(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8"))[:24]
        if cid in candidate_ids:
            raise SystemExit(f"Duplicate candidate identity: {cid}")
        candidate_ids.add(cid)

    if len(index_rows) != 523 or len({row["card_id"] for row in index_rows}) != 84:
        raise SystemExit("Review-card index count/card count mismatch.")
    raw_nonempty = sum(bool(cell.get("raw_ocr")) for row in candidates for cell in row.get("cells", []))
    raw_blank = 523 * 7 - raw_nonempty
    if len(raw_ocr) != 90:
        raise SystemExit(f"Expected the exact 90-record raw OCR manifest; found {len(raw_ocr)}")

    # Read-only guard on the existing staged catalog: no writes, and unrelated scope remains unchanged.
    db_blob = gzip.open(STAGING_DB_GZ, "rb").read()
    db_sha = sha256_file(STAGING_DB_GZ)
    db = sqlite3.connect(":memory:")
    db.deserialize(db_blob)
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    raw_status = dict(db.execute("SELECT review_status,count(*) FROM archive_image_ocr WHERE archive_name IN ('PILOT.zip','MA SS.zip','chat_uploads') GROUP BY review_status"))
    candidate_status = dict(db.execute("SELECT json_extract(data_json,'$.candidate_status'),count(*) FROM archive_structured_rows WHERE collection IN ('drive_pilot_stat_candidates','drive_mech_stat_candidates') GROUP BY json_extract(data_json,'$.candidate_status')"))
    pending_public_docs = db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('mech_arena_database','plarium_forum','upgrade_calculator') AND review_status='pending'").fetchone()[0]
    db.close()
    if integrity != "ok" or raw_status != {"verified": 90} or candidate_status != {"needs_review": 523} or pending_public_docs != 147:
        raise SystemExit(f"Staging scope guard failed: integrity={integrity}, raw={raw_status}, candidates={candidate_status}, pending={pending_public_docs}")

    # Independently validate the newly rebuilt visual overlay, not merely its inputs.
    missing_fresh_files = [str(path.relative_to(ROOT)) for path in [MANUAL_REVIEW_PATH,FRESH_AUDIT_PATH,*FRESH_OUTPUTS.values()] if not path.is_file()]
    if missing_fresh_files:
        raise SystemExit(f"Fresh visual rebuild artifacts are incomplete: {missing_fresh_files}")
    manual = json.loads(MANUAL_REVIEW_PATH.read_text(encoding="utf-8"))
    fresh_audit = json.loads(FRESH_AUDIT_PATH.read_text(encoding="utf-8"))
    direct_cells = read_jsonl(FRESH_OUTPUTS["direct_verified_cells"])
    combined_overlay = read_jsonl(FRESH_OUTPUTS["combined_overlay"])
    provenance = read_jsonl(FRESH_OUTPUTS["provenance"])
    corrections = read_jsonl(FRESH_OUTPUTS["corrections"])
    candidate_row_log = read_jsonl(FRESH_OUTPUTS["candidate_row_log"])
    reconstructions = read_jsonl(FRESH_OUTPUTS["reconstructions"])
    reconstructed_cells = read_jsonl(FRESH_OUTPUTS["reconstructed_cells"])
    gated_cells = read_jsonl(FRESH_OUTPUTS["gated_cells"])
    screenshot_manifest = read_jsonl(FRESH_OUTPUTS["screenshot_manifest"])

    index_by_card = defaultdict(list)
    source_to_card = {}
    for idx in index_rows:
        index_by_card[idx["card_id"]].append(idx)
        source_to_card[(idx["archive"],idx["source_path"])] = idx["card_id"]
    manual_log = {e["card_id"]:e for e in manual["visual_review_log"]}
    if len(manual_log)!=len(manual["visual_review_log"]) or set(manual_log)!=set(index_by_card):
        raise SystemExit("Fresh visual-review manifest does not cover every screenshot exactly once")
    if set(manual.get("visually_reviewed_card_ids",[]))!=set(index_by_card) or len(manual.get("visually_reviewed_card_ids",[]))!=84:
        raise SystemExit("Fresh visual-review card ID list does not match the screenshot index")
    if any("nonblank_cells_visually_compared" in e for e in manual["visual_review_log"]):
        raise SystemExit("Misleading nonblank_cells_visually_compared field remains in the manifest")
    candidates_by_card = defaultdict(list)
    for row in candidates:
        cid=source_to_card[(row["archive_name"],row["source_path"])]
        candidates_by_card[cid].append(row)
    fragments_by_card={r["card_id"]:r["source_fragment_baselines_y"] for r in manual["row_reconstructions"]}
    total_compared_slots=0
    for cid, entries in index_by_card.items():
        card_hash=entries[0]["source_sha256"]
        log=manual_log[cid]
        if any(r["source_sha256"]!=card_hash for r in entries) or log["source_sha256"]!=card_hash:
            raise SystemExit(f"Fresh manifest screenshot hash mismatch for {cid}")
        if log["candidate_rows"]!=len(candidates_by_card[cid]):
            raise SystemExit(f"Fresh manifest candidate row count mismatch for {cid}")
        fragment_count=len(fragments_by_card.get(cid,[]))
        expected_slots=(len(candidates_by_card[cid])-fragment_count)*7
        if log["cell_slots_visually_compared"]!=expected_slots:
            raise SystemExit(f"Fresh manifest rank-slot comparison count mismatch for {cid}")
        total_compared_slots+=expected_slots

    user_confirmation=manual.get("user_confirmation",{})
    unit_conventions=manual.get("unit_conventions",{})
    pilot_direct=[r for r in direct_cells if r["archive"]=="PILOT.zip"]
    pilot_unit=unit_conventions.get("PILOT.zip",{})
    if not user_confirmation.get("all_rebuilt_records_confirmed_verified"):
        raise SystemExit("Latest user confirmation of the fresh rebuild is not recorded in the manifest")
    if len(pilot_direct)!=504 or any("%" not in str(r["value"]) for r in pilot_direct):
        raise SystemExit("PILOT direct values do not all retain percentage units")
    if pilot_unit.get("unit_type")!="percentage" or pilot_unit.get("verified_direct_cell_count")!=len(pilot_direct) or not pilot_unit.get("all_verified_value_strings_contain_percent_sign"):
        raise SystemExit("PILOT percentage unit note does not match the direct overlay")
    direct_keys={(r["candidate_row_id"],int(r["column"])) for r in direct_cells}
    combined_direct={(r["candidate_row_id"],int(r["column"])) for r in combined_overlay if r.get("overlay_record_type")=="direct_visual_verification"}
    combined_recon=[r for r in combined_overlay if r.get("overlay_record_type")=="approved_wrapped_row_reconstruction"]
    if len(direct_keys)!=len(direct_cells) or len(combined_direct)!=len(combined_overlay)-len(combined_recon) or direct_keys!=combined_direct:
        raise SystemExit("Direct cell overlay records are duplicated or differ from the consolidated overlay")
    if any(not str(r.get("value") or "").strip() for r in direct_cells):
        raise SystemExit("Direct verified-cell overlay contains an empty value")
    if len(candidates)!=523 or len(candidate_row_log)!=523 or len(screenshot_manifest)!=84:
        raise SystemExit("Fresh row/screenshot review outputs do not cover the full candidate scope")
    if len(provenance)!=523*7+14 or len(reconstructions)!=2 or len(reconstructed_cells)!=14 or len(gated_cells)!=0:
        raise SystemExit("Fresh cell provenance/reconstruction/gating output counts do not match the rebuild scope")
    if len(combined_overlay)!=len(direct_cells)+len(reconstructed_cells):
        raise SystemExit("Consolidated overlay is missing direct or reconstructed records")
    if total_compared_slots!=fresh_audit["summary"]["direct_cell_slots_visually_compared"]:
        raise SystemExit("Fresh audit rank-cell-position count disagrees with the manual review manifest")
    direct_pin_expected=approval["scope"]["direct_verified_cell_records"]
    review_summary=manual.get("review_summary",{})
    expected_review_summary={
        "candidate_rows":len(candidates),
        "screenshots_reviewed":84,
        "direct_candidate_rows_visually_reviewed":fresh_audit["summary"]["direct_rows_visually_reviewed"],
        "direct_rank_cell_positions_compared_including_blank_slots":total_compared_slots,
        "direct_nonblank_cells_verified_by_fresh_visual_review":len(direct_cells),
        "prior_approval_direct_verified_cell_count":direct_pin_expected,
        "direct_cell_count_delta_vs_prior_approval":len(direct_cells)-direct_pin_expected,
        "reconstruction_rows":len(reconstructions),
        "reconstructed_cell_records":len(reconstructed_cells),
        "unreadable_or_gated_cells":len(gated_cells),
    }
    for key,value in expected_review_summary.items():
        if review_summary.get(key)!=value:
            raise SystemExit(f"Manual review summary mismatch for {key}: {review_summary.get(key)!r} != {value!r}")
    if fresh_audit["summary"]["source_candidate_jsonl_sha256"]!=candidate_sha or fresh_audit["summary"]["source_raw_ocr_manifest_sha256"]!=raw_ocr_sha:
        raise SystemExit("Fresh visual audit source input hashes do not match the validated inputs")
    for rec in direct_cells+reconstructed_cells:
        card_id=rec["card_id"]
        if card_id not in index_by_card or rec["source_sha256"]!=index_by_card[card_id][0]["source_sha256"]:
            raise SystemExit(f"Fresh verified cell has invalid screenshot provenance for {card_id}")
        if rec["source_archive_sha256"]!=archive_hashes[rec["archive"]]:
            raise SystemExit(f"Fresh verified cell has invalid source-archive provenance for {card_id}")
    direct_pin_delta=len(direct_cells)-direct_pin_expected
    if fresh_audit["approval_pin_reconciliation"]["direct_cell_count_delta"]!=direct_pin_delta:
        raise SystemExit("Fresh audit approval-count variance does not match the rebuilt overlay")
    current_date = report_local_date()
    as_text = lambda value: "" if value is None else str(value)

    source_fingerprint = md5_of_lines([
        "|".join((r["card_id"], r["source_path"], r["source_sha256"]))
        for r in sorted(screenshot_manifest, key=lambda r: r["card_id"])
    ])
    direct_row_fingerprint_records = []
    for row in candidate_row_log:
        if row.get("visual_review_status") != "visually_reviewed_direct_row":
            continue
        direct_row_fingerprint_records.append([
            row["candidate_row_id"], row["card_id"], row["archive"],
            as_text(row.get("entity_title_ocr")), as_text(row.get("verified_stat_label")),
            "direct", row["candidate_row_id"], "", as_text(row.get("baseline_y")), "",
        ])
    reconstruction_row_fingerprint_records = [[
        "recon:" + row["reconstruction_id"], row["card_id"], row["archive"],
        as_text(row.get("entity_title")), as_text(row.get("stat_label")),
        "reconstructed", "", row["reconstruction_id"], "", as_text(row.get("description")),
    ] for row in reconstructions]
    row_fingerprint_records = sorted(direct_row_fingerprint_records + reconstruction_row_fingerprint_records, key=lambda r: r[0])
    row_fingerprint = md5_of_lines(["|".join(row) for row in row_fingerprint_records])

    cell_fingerprint_records = []
    direct_overlay_cells = [r for r in combined_overlay if r.get("overlay_record_type") == "direct_visual_verification"]
    reconstructed_overlay_cells = [r for r in combined_overlay if r.get("overlay_record_type") == "approved_wrapped_row_reconstruction"]
    for cell in combined_overlay:
        row_key = cell.get("candidate_row_id") or ("recon:" + str(cell.get("reconstruction_id")))
        rank = str(int(cell["column"]))
        value = str(cell["value"])
        value_unit = "percentage" if cell["archive"] == "PILOT.zip" or "%" in value else "as_displayed"
        cell_fingerprint_records.append([
            f"{row_key}:c{rank}", row_key, rank, value, value_unit,
            str(cell["cell_status"]), "true", str(cell["reviewed_on_local_date"]),
        ])
    cell_fingerprint_records.sort(key=lambda r: r[0])
    cell_fingerprint = md5_of_lines(["|".join(row) for row in cell_fingerprint_records])
    row_keys_for_cells = {row[0] for row in row_fingerprint_records}
    orphan_cells = sum(1 for row in cell_fingerprint_records if row[1] not in row_keys_for_cells)
    pilot_overlay_cells = [r for r in direct_overlay_cells if r["archive"] == "PILOT.zip"]
    pilot_percent_suffix = sum(str(r["value"]).endswith("%") for r in pilot_overlay_cells)
    expected_cell_counts = {
        "cells": len(combined_overlay),
        "distinct_keys": len({row[0] for row in cell_fingerprint_records}),
        "direct_cells": len(direct_overlay_cells),
        "reconstructed_cells": len(reconstructed_overlay_cells),
        "pilot_cells": len(pilot_overlay_cells),
        "pilot_percent_suffix": pilot_percent_suffix,
        "pilot_missing_percent": len(pilot_overlay_cells) - pilot_percent_suffix,
        "percentage_unit_cells": sum(row[4] == "percentage" for row in cell_fingerprint_records),
        "reconstructed_status_cells": sum(row[5] == "reconstructed_from_original_screenshot" for row in cell_fingerprint_records),
        "not_user_confirmed": 0 if user_confirmation.get("all_rebuilt_records_confirmed_verified") else len(combined_overlay),
        "wrong_review_date": sum(row[7] != manual["reviewed_on"] for row in cell_fingerprint_records),
        "orphan_cells": orphan_cells,
    }

    promotion_evidence = None
    promotion_verified = False
    if PROMOTION_AUDIT_PATH.is_file():
        promotion_evidence = json.loads(PROMOTION_AUDIT_PATH.read_text(encoding="utf-8"))
        export_manifest = json.loads(PROMOTION_EXPORT_MANIFEST_PATH.read_text(encoding="utf-8"))
        if promotion_evidence.get("status") != "verified":
            raise SystemExit("Supabase promotion evidence is not marked verified")
        if promotion_evidence.get("verified_on_local_date") != date.fromisoformat(promotion_evidence["verified_on_local_date"]).isoformat():
            raise SystemExit("Supabase promotion evidence date is not a valid ISO local date")
        if promotion_evidence.get("project") != {"name": "hq bot", "project_ref": "urrygvuogdpjimxfolwq", "schema": "mech_bot_app"}:
            raise SystemExit("Supabase promotion evidence points to an unexpected project or schema")
        if promotion_evidence.get("run_id") != export_manifest.get("run_id"):
            raise SystemExit("Supabase promotion run ID differs from the local export manifest")
        migration = promotion_evidence.get("migration", {})
        if migration.get("version") != "20261112100959" or migration.get("applied") is not True:
            raise SystemExit("Expected Supabase migration 20261112100959 is not verified as applied")
        design = promotion_evidence.get("design", {})
        expected_tables = ["stat_rebuild_runs", "stat_rebuild_sources", "stat_rebuild_rows", "verified_stat_cells"]
        if design.get("tables") != expected_tables or design.get("table_count") != 4:
            raise SystemExit("Applied Supabase destination does not match the disclosed four-table design")
        if promotion_evidence.get("run_records") != 1:
            raise SystemExit("Supabase promotion run record count is not one")

        source_evidence = promotion_evidence.get("source_verification", {})
        if (source_evidence.get("rows"), source_evidence.get("unique_paths"), source_evidence.get("fingerprint_md5"), source_evidence.get("expected_manifest_fingerprint_md5"), source_evidence.get("matches_expected")) != (len(screenshot_manifest), len({r["source_path"] for r in screenshot_manifest}), source_fingerprint, source_fingerprint, True):
            raise SystemExit("Supabase source count/path/fingerprint evidence differs from the fresh screenshot manifest")
        row_evidence = promotion_evidence.get("row_verification", {})
        if (row_evidence.get("rows"), row_evidence.get("direct_rows"), row_evidence.get("reconstructed_rows"), row_evidence.get("fingerprint_md5"), row_evidence.get("expected_rebuild_fingerprint_md5"), row_evidence.get("matches_expected")) != (len(row_fingerprint_records), len(direct_row_fingerprint_records), len(reconstruction_row_fingerprint_records), row_fingerprint, row_fingerprint, True):
            raise SystemExit("Supabase stat-row count/fingerprint evidence differs from the fresh rebuild")
        cell_evidence = promotion_evidence.get("cell_verification", {})
        for key, expected in expected_cell_counts.items():
            if cell_evidence.get(key) != expected:
                raise SystemExit(f"Supabase cell verification mismatch for {key}: {cell_evidence.get(key)!r} != {expected!r}")
        if (cell_evidence.get("fingerprint_md5"), cell_evidence.get("expected_overlay_fingerprint_md5"), cell_evidence.get("matches_expected")) != (cell_fingerprint, cell_fingerprint, True):
            raise SystemExit("Supabase verified-cell fingerprint differs from the fresh overlay")
        if cell_evidence.get("reviewed_on_local_date") != manual["reviewed_on"]:
            raise SystemExit("Supabase cells were not written with the actual visual-review date")
        expected_writes = {
            "sources_expected_and_verified": len(screenshot_manifest),
            "stat_rows_expected_and_verified": len(row_fingerprint_records),
            "cells_expected_and_verified": len(combined_overlay),
            "post_write_counts_and_fingerprints_verified": True,
            "raw_ocr_records_imported": 0,
            "unrelated_public_documents_imported": 0,
        }
        writes_evidence = promotion_evidence.get("writes", {})
        if any(writes_evidence.get(key) != expected for key, expected in expected_writes.items()):
            raise SystemExit("Supabase promotion write-scope evidence is incomplete or differs from the validated overlay")
        if export_manifest.get("source_rows") != len(screenshot_manifest) or export_manifest.get("effective_stat_rows") != len(row_fingerprint_records) or export_manifest.get("cell_rows") != len(combined_overlay):
            raise SystemExit("Local Supabase export manifest counts differ from the validated rebuild")
        promotion_verified = True

    fresh_output_hashes={key:{"path":str(path.relative_to(ROOT)),"sha256":sha256_file(path),"jsonl_records":len(read_jsonl(path))} for key,path in FRESH_OUTPUTS.items()}
    missing_legacy_outputs=[name for name in MISSING_REVIEW_FILES if name!="manual_visual_review_manifest.json" and not (STAGE/name).is_file()]
    current_workbook_sha = sha256_file(WORKBOOK_PATH)
    pinned_workbook_sha = approval["pinned_inputs"]["source_review_workbook_sha256"]
    backup_receipt = json.loads(BACKUP_RECEIPT_PATH.read_text(encoding="utf-8")) if BACKUP_RECEIPT_PATH.is_file() else None
    report = {
        "report_version": 3,
        "generated_on_local_date": current_date,
        "checked_on_local_date": current_date,
        "visual_reviewed_on_local_date": manual["reviewed_on"],
        "automated_consistency_checks": "passed_with_user_confirmed_fresh_overlay_historical_pin_delta_and_supabase_promotion_verified" if promotion_verified else ("passed_with_user_confirmed_fresh_overlay_and_historical_pin_delta" if direct_pin_delta else "passed"),
        "visual_verification_claimed_by_this_report": True,
        "supabase_write_performed": promotion_verified,
        "database_promotion_performed": promotion_verified,
        "database_promotion_verified": promotion_verified,
        "database_promotion_state": "verified_complete" if promotion_verified else "not_verified",
        "supabase_write_performed_by_validator": False,
        "drive_backup_uploaded": bool(backup_receipt),
        "drive_backup_receipt": backup_receipt,
        "approval_artifact": {
            "path": str(APPROVAL_PATH.relative_to(ROOT)),
            "approval_status": approval["approval_status"],
            "reviewed_on": approval["reviewed_on"],
            "reviewer": approval["reviewer"],
            "scope": approval["scope"],
            "approval_json_modified": False,
        },
        "external_checks": {
            "supabase": {
                "prior_destination_inspection": {
                    "name": "hq bot",
                    "project_ref": "urrygvuogdpjimxfolwq",
                    "schema": "mech_bot_app",
                    "preexisting_verified_stat_destination_found": False,
                },
                "database_write_performed": promotion_verified,
                "promotion_evidence_path": str(PROMOTION_AUDIT_PATH.relative_to(ROOT)) if promotion_verified else None,
                "promotion_evidence": promotion_evidence,
                "validator_connected_to_or_wrote_to_supabase": False,
            },
            "other_project_inspection": {
                "name": "jatin",
                "project_ref": "efatnpyhlgfipcslzfab",
                "matching_ocr_archive_stat_tables": [],
                "catalog_records_found": False,
                "unrelated_matching_name": "portico.mad_pilots",
            },
            "google_drive_review_artifact_search": {
                "named_overlay_or_visual_review_artifacts_found": 0,
                "search_result_note": "No files matched the pinned review artifact name fragments or full-text artifact markers before the fresh rebuild was created.",
            },
        },
        "sources": {
            "candidate_jsonl": {"path": str(CANDIDATES_PATH.relative_to(ROOT)), "sha256": candidate_sha, "matches_approval_pin": True},
            "raw_ocr_jsonl": {"path": str(RAW_OCR_PATH.relative_to(ROOT)), "records": len(raw_ocr), "sha256": raw_ocr_sha, "matches_raw_ocr_pin": True},
            "archives": {name: {"path": str((ARCHIVES / name).relative_to(ROOT)), "sha256": archive_hashes[name], "matches_approval_pin": True, "zip_integrity": "passed", "entries": len(archive_entries[name])} for name in EXPECTED_SOURCE_ROWS},
        },
        "candidate_validation": {
            "rows": len(candidates),
            "rows_by_archive": dict(counts),
            "unique_screenshots": len(image_hashes),
            "review_card_index_rows": len(index_rows),
            "review_cards": len({row["card_id"] for row in index_rows}),
            "unique_candidate_ids": len(candidate_ids),
            "candidate_source_status": "needs_review (unchanged)",
            "raw_nonempty_ocr_cells": raw_nonempty,
            "raw_blank_cell_slots": raw_blank,
            "pinned_direct_verified_cells_expected": direct_pin_expected,
        },
        "fresh_visual_review": {
            "manual_manifest_path": str(MANUAL_REVIEW_PATH.relative_to(ROOT)),
            "manual_manifest_sha256": sha256_file(MANUAL_REVIEW_PATH),
            "reviewed_screenshots": len(manual_log),
            "candidate_rows_reviewed": len(candidate_row_log),
            "direct_rows_visually_reviewed": fresh_audit["summary"]["direct_rows_visually_reviewed"],
            "direct_rank_cell_positions_compared_including_blank_slots": total_compared_slots,
            "direct_nonblank_verified_cells_rebuilt": len(direct_cells),
            "corrected_and_recovered_cell_count": fresh_audit["summary"]["corrected_nonblank_ocr_cells"]+fresh_audit["summary"]["ocr_blank_cells_visually_recovered"],
            "confirmed_blank_cell_count": fresh_audit["summary"]["visually_confirmed_blank_cells"],
            "reconstructed_cell_count": len(reconstructed_cells),
            "gated_ambiguous_cell_count": len(gated_cells),
            "review_manifest_source_hashes_match_index": True,
            "misleading_nonblank_cells_visually_compared_field_removed": True,
            "fresh_audit_status": fresh_audit["status"],
            "user_confirmation": user_confirmation,
            "pilot_unit_type": pilot_unit["unit_type"],
            "pilot_direct_cells_with_percent_sign": len(pilot_direct),
            "pilot_percent_suffix_check_passed": True,
            "pilot_value_strings_changed_after_user_note": pilot_unit.get("value_strings_changed_after_clarification",False),
        },
        "fresh_overlay_outputs": fresh_output_hashes,
        "approval_pin_reconciliation": {
            "pinned_direct_verified_cells_expected": direct_pin_expected,
            "rebuilt_direct_verified_cells_actual": len(direct_cells),
            "direct_cell_count_delta": direct_pin_delta,
            "pinned_reconstructed_cell_records_expected": approval["scope"]["reconstructed_cell_records"],
            "rebuilt_reconstructed_cell_records_actual": len(reconstructed_cells),
            "pinned_total_records_expected": direct_pin_expected+approval["scope"]["reconstructed_cell_records"],
            "rebuilt_total_records_actual": len(combined_overlay),
            "exact_count_match": direct_pin_delta==0,
            "all_current_rebuilt_records_user_confirmed": bool(user_confirmation.get("all_rebuilt_records_confirmed_verified",False)),
            "approval_pins_modified": False,
            "note": "The original approved overlay hash targets remain unavailable. The user has confirmed all records in the fresh rebuild. Its 3,506 direct-cell count still differs by one from the historical 3,505 pin; both figures remain recorded and the approval JSON was not modified.",
        },
        "legacy_review_artifact_recovery": {
            "missing_legacy_pinned_outputs": missing_legacy_outputs,
            "pinned_output_hashes_expected": {
                "verified_stat_cells_jsonl": approval["pinned_inputs"]["verified_stat_cells_jsonl_sha256"],
                "candidate_row_visual_review_log_jsonl": approval["pinned_inputs"]["candidate_row_visual_review_log_sha256"],
                "verified_row_reconstructions_jsonl": approval["pinned_inputs"]["verified_row_reconstructions_jsonl_sha256"],
            },
            "source_workbook_current_sha256": current_workbook_sha,
            "source_workbook_pinned_sha256": pinned_workbook_sha,
            "source_workbook_hash_matches": current_workbook_sha==pinned_workbook_sha,
            "fresh_rebuilt_replacements_exist": not missing_fresh_files,
        },
        "existing_staging_catalog": {
            "sqlite_gzip_sha256": db_sha,
            "integrity_check": integrity,
            "raw_ocr_records": raw_status,
            "derived_candidate_rows": candidate_status,
            "unrelated_public_documents_pending": pending_public_docs,
            "modified_by_validator": False,
        },
        "database_promotion_ready": promotion_verified,
        "blocker": None if promotion_verified else "No matching live Supabase promotion verification is present. This validator is read-only and cannot promote the overlay itself.",
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "result": "fresh_visual_overlay_user_confirmed_with_historical_count_delta" if direct_pin_delta else "fresh_visual_overlay_validated",
        "candidate_rows": len(candidates), "screenshots": len(image_hashes),
        "candidate_sha256": candidate_sha, "raw_ocr_sha256": raw_ocr_sha,
        "reviewed_rank_cell_slots": total_compared_slots,
        "direct_verified_cells_rebuilt": len(direct_cells),
        "direct_verified_cells_pinned": direct_pin_expected,
        "direct_cell_count_delta": direct_pin_delta,
        "reconstructed_cells": len(reconstructed_cells),
        "correction_log_records": len(corrections),
        "gated_cells": len(gated_cells),
        "missing_legacy_pinned_outputs": missing_legacy_outputs,
        "fresh_output_hashes": fresh_output_hashes,
        "staging_catalog_sha256": db_sha,
        "supabase_write_performed": promotion_verified,
        "database_promotion_ready": promotion_verified,
        "database_promotion_verified": promotion_verified,
        "database_promotion_state": "verified_complete" if promotion_verified else "not_verified",
        "supabase_write_performed_by_validator": False,
        "source_fingerprint_md5": source_fingerprint,
        "row_fingerprint_md5": row_fingerprint,
        "cell_fingerprint_md5": cell_fingerprint,
        "report": str(REPORT_PATH),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
