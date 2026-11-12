#!/usr/bin/env python3
"""Apply the user's explicit verification decision to the pinned 90 raw OCR records only."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STAGING_GZ = DATA / "mech_bot_staging.sqlite3.gz"
REVIEW_XLSX = DATA / "drive_ocr_stage" / "drive_ocr_review.xlsx"
BACKUPS = DATA / "backups"
DB_BACKUP = BACKUPS / "mech_bot_staging.before_raw_ocr_verification.sqlite3.gz"
XLSX_BACKUP = BACKUPS / "drive_ocr_review.before_raw_ocr_verification.xlsx"
EXPECTED_MANIFEST = "dfc42f5cab6e7ce74526c9700bc1aedd308c2f09f2e356fdb33331cafa5537c5"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def update_review_workbook(path: Path, output: Path) -> None:
    workbook = load_workbook(path)
    expected_sheets = {"Read me", "Images", "Pilot candidates", "Mech candidates", "Chat uploads OCR"}
    if not expected_sheets.issubset(set(workbook.sheetnames)):
        raise RuntimeError("OCR review workbook is missing an expected sheet.")

    images = workbook["Images"]
    headers = [cell.value for cell in images[1]]
    if headers.count("Review status") != 1 or images.max_row != 91:
        raise RuntimeError("The raw OCR worksheet does not match the verified 90-image manifest.")
    status_col = headers.index("Review status") + 1
    for row in range(2, 92):
        if images.cell(row, status_col).value not in ("needs_review", "verified"):
            raise RuntimeError("Unexpected status in a raw OCR workbook row.")
        images.cell(row, status_col).value = "verified"

    uploads = workbook["Chat uploads OCR"]
    upload_headers = [cell.value for cell in uploads[1]]
    if upload_headers.count("Review status") != 1 or uploads.max_row != 7:
        raise RuntimeError("The chat-upload OCR worksheet does not match its six-image scope.")
    upload_status_col = upload_headers.index("Review status") + 1
    for row in range(2, 8):
        if uploads.cell(row, upload_status_col).value not in ("needs_review", "verified"):
            raise RuntimeError("Unexpected status in a chat-upload OCR workbook row.")
        uploads.cell(row, upload_status_col).value = "verified"

    for sheet_name, expected_rows in (("Pilot candidates", 73), ("Mech candidates", 452)):
        sheet = workbook[sheet_name]
        headers = [cell.value for cell in sheet[1]]
        if headers.count("Review status") != 1 or sheet.max_row != expected_rows:
            raise RuntimeError(f"The {sheet_name} candidate worksheet does not match its pinned row count.")
        candidate_status_col = headers.index("Review status") + 1
        for row in range(2, expected_rows + 1):
            if sheet.cell(row, candidate_status_col).value != "needs_review":
                raise RuntimeError("A derived stat candidate is not in the separate review queue.")

    readme = workbook["Read me"]
    readme["B1"] = "90 raw OCR records user-approved; 523 derived candidates remain unverified"
    readme["B4"] = (
        "RapidOCR text, confidence and image coordinates are preserved. The 90 raw OCR records were "
        "marked verified for bot lookup at the user's direction; OCR accuracy was not independently rechecked."
    )
    readme["B5"] = (
        "The Pilot candidates and Mech candidates sheets contain 523 derived stat candidates. "
        "All remain needs_review and are not available as verified game stats."
    )
    readme["B9"] = (
        "The raw OCR records are approved for lookup. Before using any derived candidate values, "
        "compare each cell with its image and separately verify entity identity, version and source."
    )
    readme["B10"] = (
        "The six chat-upload OCR records are marked verified at the user's direction. Shared bot answers "
        "only use relevant game snippets and omit conversation text, usernames and image-generation prompts. "
        "image-26 has no detected text."
    )
    workbook.save(output)
    workbook.close()


def main() -> None:
    if not STAGING_GZ.is_file() or not REVIEW_XLSX.is_file():
        raise SystemExit("Missing the pinned staging snapshot or OCR review workbook.")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    if not DB_BACKUP.exists():
        shutil.copy2(STAGING_GZ, DB_BACKUP)
    if not XLSX_BACKUP.exists():
        shutil.copy2(REVIEW_XLSX, XLSX_BACKUP)

    fd, db_name = tempfile.mkstemp(prefix="raw-ocr-review-", suffix=".sqlite3")
    os.close(fd)
    temp_db = Path(db_name)
    fd, xlsx_name = tempfile.mkstemp(prefix="raw-ocr-review-", suffix=".xlsx", dir=REVIEW_XLSX.parent)
    os.close(fd)
    temp_xlsx = Path(xlsx_name)
    temp_gz = STAGING_GZ.with_name(STAGING_GZ.name + ".verified.tmp")
    try:
        with gzip.open(STAGING_GZ, "rb") as source, temp_db.open("wb") as target:
            shutil.copyfileobj(source, target)

        sys.path.insert(0, str(ROOT))
        from mechbot.catalog import (
            USER_CONFIRMED_DRIVE_OCR_DOCUMENTS,
            USER_CONFIRMED_DRIVE_OCR_MANIFEST_SHA256,
            USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS,
            apply_user_confirmed_drive_ocr_review,
        )
        if USER_CONFIRMED_DRIVE_OCR_MANIFEST_SHA256 != EXPECTED_MANIFEST:
            raise RuntimeError("The code's pinned raw OCR manifest does not match this operation.")
        promoted = apply_user_confirmed_drive_ocr_review(temp_db, manifest_sha=EXPECTED_MANIFEST)
        if promoted != 90:
            raise RuntimeError("The exact 90-record verification guard did not pass; source files were not replaced.")

        db = sqlite3.connect(f"file:{temp_db}?mode=ro", uri=True)
        ocr_counts = dict(db.execute(
            "SELECT archive_name,count(*) FROM archive_image_ocr "
            "WHERE archive_name IN ('PILOT.zip','MA SS.zip','chat_uploads') AND review_status='verified' "
            "GROUP BY archive_name"
        ).fetchall())
        if ocr_counts != {"PILOT.zip": 36, "MA SS.zip": 48, "chat_uploads": 6}:
            raise RuntimeError(f"Raw OCR post-check failed: {ocr_counts}")
        candidate_counts = dict(db.execute(
            "SELECT collection,count(*) FROM archive_structured_rows "
            "WHERE collection IN (?,?) GROUP BY collection", tuple(USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS)
        ).fetchall())
        if candidate_counts != USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS:
            raise RuntimeError(f"Derived candidate scope changed: {candidate_counts}")
        candidates = db.execute(
            "SELECT data_json FROM archive_structured_rows WHERE collection IN (?,?)",
            tuple(USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS),
        ).fetchall()
        if len(candidates) != 523 or any(json.loads(row[0]).get("candidate_status") != "needs_review" for row in candidates):
            raise RuntimeError("A derived stat candidate left its review gate.")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed.")
        db.close()

        update_review_workbook(REVIEW_XLSX, temp_xlsx)
        with temp_gz.open("wb") as raw_stream:
            with gzip.GzipFile(fileobj=raw_stream, mode="wb", compresslevel=9, mtime=0) as compressed, temp_db.open("rb") as source:
                shutil.copyfileobj(source, compressed)
        # Verify the compressed artifact before replacing the staging snapshot.
        with gzip.open(temp_gz, "rb") as check, temp_db.open("rb") as expected:
            if hashlib.sha256(check.read()).digest() != hashlib.sha256(expected.read()).digest():
                raise RuntimeError("Compressed SQLite round-trip check failed.")

        os.replace(temp_gz, STAGING_GZ)
        os.replace(temp_xlsx, REVIEW_XLSX)
        print(json.dumps({
            "result": "verified_user_approved_raw_ocr",
            "raw_ocr_records": ocr_counts,
            "raw_ocr_total": sum(ocr_counts.values()),
            "derived_candidates": candidate_counts,
            "derived_candidate_total": sum(candidate_counts.values()),
            "staging_database_sha256": sha256(STAGING_GZ),
            "raw_ocr_manifest_sha256": EXPECTED_MANIFEST,
            "backups": [str(DB_BACKUP.relative_to(ROOT)), str(XLSX_BACKUP.relative_to(ROOT))],
        }, indent=2))
    finally:
        for path in (temp_db, temp_xlsx, temp_gz):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
