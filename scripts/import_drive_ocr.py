#!/usr/bin/env python3
"""Add supplied Drive screenshot OCR to the offline catalog snapshot as unverified staging."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_GZ = ROOT / "data" / "mech_bot_staging.sqlite3.gz"
OCR_JSONL = ROOT / "data" / "drive_ocr_stage" / "drive_and_upload_ocr.jsonl"
CANDIDATE_JSONL = ROOT / "data" / "drive_ocr_stage" / "stat_candidate_rows.jsonl"
DRIVE = ROOT.parent / "google_drive"
AUTHOR = "Gladitor (user-attributed; independent verification pending)"
SOURCES = {
    "PILOT.zip": {
        "id": "1o7-ceXZ8bQqhKK1kpVchnK28nE_XEH61",
        "url": "https://drive.google.com/file/d/1o7-ceXZ8bQqhKK1kpVchnK28nE_XEH61/view",
        "category": "pilot_stats_screenshots",
        "title": "PILOT.zip — pilot bonus screenshots (36 PNGs)",
        "modified": "2026-11-11T20:03:34.763Z",
    },
    "MA SS.zip": {
        "id": "1TY_Mal19TN_5lkmvQkvSBHrDexHFqHcE",
        "url": "https://drive.google.com/file/d/1TY_Mal19TN_5lkmvQkvSBHrDexHFqHcE/view",
        "category": "mech_stats_screenshots",
        "title": "MA SS.zip — mech stats screenshots (48 PNGs)",
        "modified": "2026-11-11T19:37:46.367Z",
    },
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    if not DB_GZ.is_file() or not OCR_JSONL.is_file() or not CANDIDATE_JSONL.is_file():
        raise SystemExit("Missing staging database or OCR review files; run the OCR and review-workbook scripts first.")
    records = [json.loads(line) for line in OCR_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]
    candidate_records = [json.loads(line) for line in CANDIDATE_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_archive: dict[str, list[dict]] = {}
    for rec in records:
        by_archive.setdefault(rec["archive_name"], []).append(rec)
    expected = {"PILOT.zip": 36, "MA SS.zip": 48, "chat_uploads": 6}
    candidate_counts = {name: sum(row["archive_name"] == name for row in candidate_records) for name in SOURCES}
    if ({name: len(by_archive.get(name, [])) for name in expected} != expected or len(records) != 90
            or candidate_counts != {"PILOT.zip": 72, "MA SS.zip": 451} or len(candidate_records) != 523):
        raise SystemExit("OCR manifest/candidates do not match 36 + 48 Drive images, six chat uploads, and 523 review rows.")
    ocr_by_path = {(rec["archive_name"], rec["image_path"]): rec for rec in records}
    for row in candidate_records:
        source = ocr_by_path.get((row["archive_name"], row["source_path"]))
        if (not source or row["source_sha256"] != source["source_sha256"]
                or row["review_status"] != "needs_review" or len(row["cells"]) != 7
                or [cell["column"] for cell in row["cells"]] != list(range(1, 8))):
            raise SystemExit(f"Candidate provenance/schema mismatch: {row.get('source_path')} {row.get('stat_label_ocr')}")
    for name, source in SOURCES.items():
        path = DRIVE / name
        if not path.is_file():
            raise SystemExit(f"Missing source archive: {path}")
        if any(rec["archive_id"] != source["id"] for rec in by_archive[name]):
            raise SystemExit(f"Drive file ID mismatch for {name}; refusing import.")
        actual_paths = {rec["image_path"] for rec in by_archive[name]}
        if len(actual_paths) != expected[name] or any(not p.lower().endswith(".png") for p in actual_paths):
            raise SystemExit(f"Image paths are incomplete or unexpected for {name}.")

    # Capture one real UTC audit timestamp for this batch; never infer an ingest time from the date.
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fd, temp_db_name = tempfile.mkstemp(prefix="mech-drive-ocr-", suffix=".sqlite3")
    os.close(fd)
    temp_db = Path(temp_db_name)
    temp_gz = DB_GZ.with_suffix(".sqlite3.gz.tmp")
    try:
        with gzip.open(DB_GZ, "rb") as source, temp_db.open("wb") as dest:
            while chunk := source.read(1024 * 1024):
                dest.write(chunk)
        db = sqlite3.connect(temp_db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        for table in ("metadata", "archive_documents", "archive_structured_rows", "archive_image_ocr", "archive_ingest_queue"):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                raise RuntimeError(f"Required catalog table missing: {table}")

        # Guard existing confirmed content: only add documents in our dedicated source groups.
        before_mech_count = db.execute("SELECT count(*) FROM weapon_stat_candidates").fetchone()[0]
        before_public_docs = db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('mech_arena_database','plarium_forum','upgrade_calculator')").fetchone()[0]
        if before_public_docs != 147 or before_mech_count != 22652:
            raise RuntimeError("Unexpected base catalog snapshot; aborting instead of modifying a different dataset.")
        already = db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('google_drive_archive','user_chat_upload')").fetchone()[0]
        if already:
            raise RuntimeError(f"Found {already} prior Drive/user-upload OCR document(s); check idempotence before retrying.")

        source_docs: dict[str, int] = {}
        max_doc_id = db.execute("SELECT COALESCE(MAX(id),0) FROM archive_documents").fetchone()[0]
        docs_to_add = []
        for archive_name, source in SOURCES.items():
            archive_bytes = (DRIVE / archive_name).read_bytes()
            archive_hash = digest(archive_bytes)
            image_records = by_archive[archive_name]
            doc_id = max_doc_id + len(docs_to_add) + 1
            source_docs[archive_name] = doc_id
            body = (
                f"User-supplied Google Drive screenshot archive. Contains {len(image_records)} PNG images. "
                f"Raw RapidOCR output and word coordinates are staged in archive_image_ocr; image-level "
                f"OCR records are in archive_structured_rows. This is OCR staging only: text, image identity, "
                f"game version/date, accuracy, and author attribution are not independently verified. "
                f"Attribution requested by the user: {AUTHOR}. All records remain pending review and are not "
                f"eligible for bot answers."
            )
            urls = [r["source_url"] for r in image_records]
            docs_to_add.append((doc_id, source["url"], "google_drive_archive", source["category"], source["title"],
                                AUTHOR, source["modified"], body, body[:500], json.dumps(urls, ensure_ascii=False),
                                archive_hash, imported_at, imported_at, source["modified"], "unverified", "pending"))

        uploads = by_archive["chat_uploads"]
        upload_hash = digest("\n".join(sorted(r["source_sha256"] for r in uploads)).encode("utf-8"))
        upload_doc_id = max_doc_id + len(docs_to_add) + 1
        source_docs["chat_uploads"] = upload_doc_id
        upload_body = (
            "Six previously uploaded chat screenshots transcribed as contextual OCR only. These images are not "
            "treated as authoritative game sources; generated-asset prompts and other conversation context are "
            "not catalog facts. OCR, source meaning, and any attribution remain unverified."
        )
        docs_to_add.append((upload_doc_id, "upload://chat-screenshots/image-25.png-image-30.png", "user_chat_upload",
                            "context_screenshots", "User chat screenshots 25–30 (OCR context only)",
                            "requesting user (uploaded screenshots)", None, upload_body, upload_body,
                            json.dumps([r["source_url"] for r in uploads], ensure_ascii=False), upload_hash,
                            imported_at, imported_at, None, "unverified", "pending"))
        db.executemany("""INSERT INTO archive_documents
            (id,url,source,category,title,author_label,published_text,content,excerpt,image_urls_json,
             content_hash,first_seen_utc,last_checked_utc,source_modified,trust,review_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", docs_to_add)

        max_struct_id = db.execute("SELECT COALESCE(MAX(id),0) FROM archive_structured_rows").fetchone()[0]
        structured_rows = []
        image_ocr_rows = []
        for rec in records:
            archive_name = rec["archive_name"]
            parent_id = source_docs[archive_name]
            collection = "drive_screenshot_ocr" if archive_name in SOURCES else "user_upload_screenshot_ocr"
            data = {
                "archive_name": archive_name,
                "drive_file_id": rec["archive_id"],
                "image_path": rec["image_path"],
                "source_url": rec["source_url"],
                "source_sha256": rec["source_sha256"],
                "image_width": rec["image_width"],
                "image_height": rec["image_height"],
                "ocr_engine": rec["ocr_engine"],
                "ocr_text": rec["ocr_text"],
                "token_count": rec["token_count"],
                "mean_confidence": rec["mean_confidence"],
                "review_status": "needs_review",
                "author_attribution": AUTHOR if archive_name in SOURCES else None,
            }
            structured_rows.append((max_struct_id + len(structured_rows) + 1, parent_id, collection,
                                    rec["image_path"], json.dumps(data, ensure_ascii=False, sort_keys=True)))
            image_ocr_rows.append((rec["source_url"], rec["archive_name"], rec["archive_id"], rec["source_sha256"],
                                   rec["image_width"], rec["image_height"], rec["ocr_engine"], rec["ocr_text"],
                                   json.dumps(rec["words"], ensure_ascii=False, separators=(",", ":")),
                                   rec["token_count"], rec["mean_confidence"], rec["assessment"], "needs_review",
                                   imported_at, rec.get("error")))
        for candidate in candidate_records:
            archive_name = candidate["archive_name"]
            collection = "drive_pilot_stat_candidates" if archive_name == "PILOT.zip" else "drive_mech_stat_candidates"
            data = dict(candidate)
            data["author_attribution"] = AUTHOR
            data["candidate_status"] = "needs_review"
            data["candidate_method"] = "RapidOCR token row grouping and fixed-column positional mapping; values are unverified OCR candidates."
            structured_rows.append((max_struct_id + len(structured_rows) + 1, source_docs[archive_name], collection,
                                    f"{candidate['source_path']}#y{candidate['baseline_y']:.1f}",
                                    json.dumps(data, ensure_ascii=False, sort_keys=True)))
        db.executemany("""INSERT INTO archive_structured_rows (id,document_id,collection,row_key,data_json)
            VALUES (?,?,?,?,?)""", structured_rows)
        db.executemany("""INSERT INTO archive_image_ocr
            (source_url,archive_name,archive_id,source_sha256,image_width,image_height,ocr_engine,ocr_text,
             words_json,token_count,mean_confidence,assessment,review_status,processed_at_utc,error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", image_ocr_rows)

        max_queue_id = db.execute("SELECT COALESCE(MAX(id),0) FROM archive_ingest_queue").fetchone()[0]
        queue_rows = [(max_queue_id + i, row[0], row[10], "raw_ocr_pending_manual_review", imported_at)
                      for i, row in enumerate(docs_to_add, start=1)]
        db.executemany("INSERT INTO archive_ingest_queue (id,document_id,content_hash,status,created_at_utc) VALUES (?,?,?,?,?)", queue_rows)

        manifest_hash = digest(OCR_JSONL.read_bytes())
        meta = {
            "drive_ocr_ingest": "84 Drive PNGs + 6 chat uploads; raw RapidOCR text/boxes plus 72 pilot and 451 mech positional row candidates imported; all need review; user-specified Gladitor attribution is not independently verified.",
            "drive_ocr_manifest_sha256": manifest_hash,
            "drive_ocr_imported_at_utc": imported_at,
            "drive_ocr_document_ids": json.dumps(source_docs, sort_keys=True),
            "drive_ocr_promotion_policy": "Do not serve to the bot or mark verified until an explicitly reviewed data snapshot is confirmed.",
        }
        db.executemany("""INSERT INTO metadata(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", meta.items())
        db.commit()

        counts = {
            "drive_ocr_rows": db.execute("SELECT count(*) FROM archive_image_ocr WHERE archive_name IN ('PILOT.zip','MA SS.zip')").fetchone()[0],
            "chat_upload_ocr_rows": db.execute("SELECT count(*) FROM archive_image_ocr WHERE archive_name='chat_uploads'").fetchone()[0],
            "new_archive_documents": db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('google_drive_archive','user_chat_upload')").fetchone()[0],
            "new_structured_rows": db.execute("SELECT count(*) FROM archive_structured_rows WHERE collection IN ('drive_screenshot_ocr','user_upload_screenshot_ocr','drive_pilot_stat_candidates','drive_mech_stat_candidates')").fetchone()[0],
            "pilot_candidate_rows": db.execute("SELECT count(*) FROM archive_structured_rows WHERE collection='drive_pilot_stat_candidates'").fetchone()[0],
            "mech_candidate_rows": db.execute("SELECT count(*) FROM archive_structured_rows WHERE collection='drive_mech_stat_candidates'").fetchone()[0],
            "new_ocr_needs_review": db.execute("SELECT count(*) FROM archive_image_ocr WHERE review_status='needs_review' AND (source_url LIKE 'drive://%' OR source_url LIKE 'upload://%')").fetchone()[0],
            "unverified_archive_docs": db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('google_drive_archive','user_chat_upload') AND review_status='pending' AND trust='unverified'").fetchone()[0],
            "mech_candidates": db.execute("SELECT count(*) FROM weapon_stat_candidates").fetchone()[0],
            "public_source_documents": db.execute("SELECT count(*) FROM archive_documents WHERE source IN ('mech_arena_database','plarium_forum','upgrade_calculator')").fetchone()[0],
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
        }
        if (counts["drive_ocr_rows"] != 84 or counts["chat_upload_ocr_rows"] != 6
                or counts["new_archive_documents"] != 3 or counts["new_structured_rows"] != 613
                or counts["pilot_candidate_rows"] != 72 or counts["mech_candidate_rows"] != 451
                or counts["new_ocr_needs_review"] != 90 or counts["unverified_archive_docs"] != 3):
            raise RuntimeError(f"Post-import count check failed: {counts}")
        if counts["integrity"] != "ok" or counts["mech_candidates"] != 22652 or counts["public_source_documents"] != 147:
            raise RuntimeError(f"Snapshot integrity or base archive guard failed: {counts}")
        db.close()

        # Verify the pinned pre-existing weapon/public-source fingerprints still match.
        import sys
        sys.path.insert(0, str(ROOT))
        from mechbot.catalog import _candidate_fingerprint, _public_archive_fingerprint
        verify = sqlite3.connect(f"file:{temp_db}?mode=ro", uri=True)
        candidate_count, candidate_fingerprint = _candidate_fingerprint(verify)
        doc_count, structured_count, public_fingerprint = _public_archive_fingerprint(verify)
        verify.close()
        if candidate_count != 22652 or doc_count != 147 or structured_count != 1338:
            raise RuntimeError("Pre-existing pinned database row counts changed.")

        with temp_gz.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed, temp_db.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    compressed.write(chunk)
        # Round-trip check the artifact before swapping the runtime snapshot.
        with gzip.open(temp_gz, "rb") as compressed:
            if digest(compressed.read()) != digest(temp_db.read_bytes()):
                raise RuntimeError("Compressed database round-trip SHA-256 mismatch.")
        os.replace(temp_gz, DB_GZ)
        print(json.dumps({
            "result": "imported_unverified_staging",
            "counts": counts,
            "base_candidate_fingerprint": candidate_fingerprint,
            "base_public_fingerprint": public_fingerprint,
            "expected_public_rows": [doc_count, structured_count],
            "archive_doc_ids": source_docs,
            "ocr_manifest_sha256": manifest_hash,
            "review_status": "All 90 image OCR records and 523 derived stat rows need manual review; none was marked verified or served to the bot.",
        }, ensure_ascii=False, indent=2))
    finally:
        if temp_db.exists():
            temp_db.unlink()
        if temp_gz.exists():
            temp_gz.unlink()


if __name__ == "__main__":
    main()
