#!/usr/bin/env python3
"""Build a self-contained, checksummed Mech Arena data pack for sharing.

The pack contains every game-data table the bot uses, the user-confirmed
screenshot-stat overlay exactly as it is stored in Supabase, the raw source
evidence behind those numbers, and a README + data dictionary that label the
verification status of each dataset.

Live Discord user data (messages to the bot, notes, reminders, profiles,
payout state, announcement cache) is deliberately EXCLUDED: the pack carries
those tables as empty schemas only, so the shape is documented but no rows
leave the operator's account.

Usage:
    python scripts/build_mech_arena_data_pack.py \
        [--out exports/mech-arena-database-pack-20261110] \
        [--supabase-evidence /tmp/supabase_evidence.json] [--zip]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT_GZ = ROOT / 'data' / 'mech_bot_staging.sqlite3.gz'
STAGE = ROOT / 'data' / 'drive_ocr_stage'
BATCHES = STAGE / 'supabase_promotion_batches'
FORUM_JSON = ROOT / 'data' / 'mech_arena_last_150_threads.json'
SOURCE_ARCHIVES = ROOT / 'data' / 'source_archives'
MIGRATIONS = ROOT / 'migrations'

REVIEW_DATE = '2026-11-10'
EXPECTED_RUN_ID = 'drive_stat_overlay_20261110_99555d68e4d4'
EXPECTED_COUNTS = {'sources': 84, 'rows': 505, 'cells': 3520,
                   'direct_cells': 3506, 'reconstructed_cells': 14, 'pilot_percent_cells': 504}
STATUS_CODES = {
    'visually_matched': 'm',
    'visually_matched_after_whitespace_normalization': 'w',
    'visually_corrected': 'c',
    'visually_recovered_from_ocr_blank': 'r',
    'visually_confirmed': 'v',
    'reconstructed_from_original_screenshot': 'x',
}
CODE_STATUS = {v: k for k, v in STATUS_CODES.items()}
PRIVATE_TABLES = ('profiles', 'reminders', 'notes', 'worker_campaign', 'worker_reminders',
                  'payout_alerts', 'announcements', 'conversation_turns')

TABLE_NOTES = {
    'weapons': 'Weapon families plus the variant/energy split the bot searches on; identity_status records how each name was tied to its screenshots.',
    'mechs': 'Mech names with their calculator and database spellings; identity_status records the name-matching result.',
    'screenshots': 'One row per stat screenshot: file path, SHA-256, the weapon it was matched to and the identity decision.',
    'weapon_stat_candidates': 'Every stat cell read from weapon screenshots, one row per stat/rank/level cell. status marks verified vs unverified; unverified rows are candidates, not confirmed game facts.',
    'archive_image_ocr': 'Raw OCR records for the 90 user-approved Drive/chat-upload screenshots (84 + 6). review_status reflects the explicit user approval of these raw records only.',
    'archive_image_transcription_staging': 'Transcription staging rows kept for traceability; not an approved dataset.',
    'archive_structured_rows': 'The structured game-data collections the bot answers from: database_weapons, database_mechs, list, pilot_list, mod_list, mod_cost, pilot_upgrade_costs, mech_upgrade_costs, database_ecrData, legendary_skins, skin_likes, database_bots, drive_* stat candidates and drive_* screenshot OCR.',
    'archive_entities': 'Entity index over structured rows (collection, key, name, kind) used to resolve mentions.',
    'archive_documents': 'Archived public pages: Plarium forum threads, patch notes, guides and wiki pages, with content_hash, trust and review_status.',
    'archive_document_versions': 'Captured versions of the same document, so a change over time is visible.',
    'archive_digests': 'Per-document digest JSON summarising what was captured at that hash.',
    'archive_assets': 'Downloaded asset manifest (files, sizes, SHA-256) behind the archives.',
    'archive_archive_batches': 'Ingest batch ledger for the archive loader.',
    'archive_fetch_log': 'One row per fetch attempt: URL, time, HTTP status, note.',
    'archive_ingest_queue': 'Queue state for documents pending structured ingest.',
    'archive_user_inventory_corrections': 'User-supplied corrections to inventory-dependent answers, with the evidence line the user gave.',
    'archive_user_inventory_correction_history': 'Prior versions of the corrections above.',
    'mechs': 'Mech names with their calculator and database spellings; identity_status records the name-matching result.',
    'user_profiles': 'Prototype table for saved timezones. Empty in this snapshot.',
    'personal_notes': 'Prototype table for saved notes. Empty in this snapshot.',
    'review_events': 'Audit log for stat-cell review actions. Empty in this snapshot.',
    'metadata': 'Snapshot-level provenance: scope, descriptions, warnings, Drive URLs, SHA-256 pins and the OCR promotion policy.',
    'bot_verified_weapon_stats': 'View over weapon_stat_candidates exposing only status=verified cells joined to their screenshot.',
    'stat_rebuild_runs': 'One row per stat-overlay run: pinned SHA-256 of every input file, the counts it promised, and the user confirmation it carries.',
    'stat_rebuild_sources': 'One row per source screenshot in the overlay: card id, path, image SHA-256, parent-archive SHA-256, cell counts and the reviewer note.',
    'stat_rebuild_rows': 'One row per stat line (entity + stat label): direct or reconstructed, baseline y, description, fragment provenance.',
    'verified_stat_cells': 'The 3,520 user-confirmed screenshot cells (value exactly as displayed, rank column, unit, review status). Rebuilt directly from the promotion payload and fingerprint-checked against the live Supabase copy.',
}

OVERLAY_DDL = '''
CREATE TABLE stat_rebuild_runs (
    run_id TEXT PRIMARY KEY,
    reviewed_on TEXT NOT NULL,
    candidate_rows INTEGER NOT NULL CHECK (candidate_rows = 523),
    screenshot_count INTEGER NOT NULL CHECK (screenshot_count = 84),
    direct_cell_slots_compared INTEGER NOT NULL,
    direct_verified_cell_count INTEGER NOT NULL,
    reconstructed_cell_count INTEGER NOT NULL,
    combined_cell_count INTEGER NOT NULL,
    prior_pinned_direct_cell_count INTEGER NOT NULL,
    historical_direct_count_delta INTEGER NOT NULL,
    candidate_jsonl_sha256 TEXT NOT NULL,
    raw_ocr_manifest_sha256 TEXT NOT NULL,
    combined_overlay_sha256 TEXT NOT NULL,
    manual_manifest_sha256 TEXT NOT NULL,
    correction_log_sha256 TEXT NOT NULL,
    source_archive_sha256 TEXT NOT NULL,
    user_confirmed INTEGER NOT NULL CHECK (user_confirmed IN (0,1)),
    user_confirmation_note TEXT NOT NULL,
    pilot_unit_convention TEXT NOT NULL,
    pilot_percent_cells INTEGER NOT NULL,
    raw_ocr_records_imported INTEGER NOT NULL DEFAULT 0 CHECK (raw_ocr_records_imported = 0),
    unrelated_public_documents_imported INTEGER NOT NULL DEFAULT 0 CHECK (unrelated_public_documents_imported = 0),
    created_at TEXT NOT NULL,
    CHECK (combined_cell_count = direct_verified_cell_count + reconstructed_cell_count),
    CHECK (historical_direct_count_delta = direct_verified_cell_count - prior_pinned_direct_cell_count)
);
CREATE TABLE stat_rebuild_sources (
    rebuild_id TEXT NOT NULL REFERENCES stat_rebuild_runs(run_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    archive_name TEXT NOT NULL CHECK (archive_name IN ('PILOT.zip','MA SS.zip')),
    source_path TEXT NOT NULL,
    source_screenshot_sha256 TEXT NOT NULL,
    source_archive_sha256 TEXT NOT NULL,
    candidate_rows INTEGER NOT NULL,
    direct_rows_visually_reviewed INTEGER NOT NULL,
    direct_cell_slots_compared INTEGER NOT NULL,
    wrapped_reconstruction_id TEXT,
    review_notes TEXT NOT NULL,
    reviewed_on TEXT NOT NULL,
    PRIMARY KEY (rebuild_id, card_id),
    UNIQUE (rebuild_id, source_path)
);
CREATE TABLE stat_rebuild_rows (
    rebuild_id TEXT NOT NULL REFERENCES stat_rebuild_runs(run_id) ON DELETE CASCADE,
    row_key TEXT NOT NULL,
    card_id TEXT NOT NULL,
    archive_name TEXT NOT NULL CHECK (archive_name IN ('PILOT.zip','MA SS.zip')),
    entity_title TEXT,
    stat_label TEXT,
    row_type TEXT NOT NULL CHECK (row_type IN ('direct','reconstructed')),
    candidate_row_id TEXT,
    reconstruction_id TEXT,
    baseline_y NUMERIC,
    description TEXT,
    source_fragment_candidate_row_ids TEXT,
    reviewed_on TEXT NOT NULL,
    PRIMARY KEY (rebuild_id, row_key),
    FOREIGN KEY (rebuild_id, card_id) REFERENCES stat_rebuild_sources(rebuild_id, card_id) ON DELETE CASCADE,
    CHECK ((row_type='direct' AND candidate_row_id IS NOT NULL AND reconstruction_id IS NULL)
        OR (row_type='reconstructed' AND reconstruction_id IS NOT NULL))
);
CREATE TABLE verified_stat_cells (
    rebuild_id TEXT NOT NULL,
    cell_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    rank_column INTEGER NOT NULL CHECK (rank_column BETWEEN 1 AND 7),
    value TEXT NOT NULL,
    value_unit TEXT NOT NULL CHECK (value_unit IN ('percentage','as_displayed')),
    cell_status TEXT NOT NULL,
    user_confirmed INTEGER NOT NULL DEFAULT 1 CHECK (user_confirmed IN (0,1)),
    reviewed_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rebuild_id, cell_key),
    FOREIGN KEY (rebuild_id, row_key) REFERENCES stat_rebuild_rows(rebuild_id, row_key) ON DELETE CASCADE
);
CREATE UNIQUE INDEX verified_stat_cells_row_rank ON verified_stat_cells (rebuild_id, row_key, rank_column);
CREATE INDEX verified_stat_cells_rank ON verified_stat_cells (rebuild_id, rank_column);
'''


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def payloads(sql_path: Path):
    """Pull the $overlay_payload$ JSON arrays out of a promotion batch file."""
    text = sql_path.read_text(encoding='utf-8')
    found = re.findall(r'\$overlay_payload\$(.+?)\$overlay_payload\$::jsonb', text, re.S)
    if not found:
        raise SystemExit(f'No overlay payload found in {sql_path.name}')
    out = []
    for blob in found:
        out.extend(json.loads(blob))
    return out


def or_empty(value):
    return '' if value in (None, 'null') else str(value)


def apply_live_source_notes(source_rows, override_path: Path):
    """Prefer the review_notes the live database actually holds over the local batch wording."""
    if not override_path or not override_path.exists():
        return source_rows, []
    payload = json.loads(override_path.read_text(encoding='utf-8'))
    mapping = payload['review_notes']
    out, applied = [], []
    for row in source_rows:
        live = mapping.get(row[1])
        if live is not None and live != row[10]:
            applied.append({'card_id': row[1], 'local_batch_wording': row[10], 'live_wording': live})
            row = (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], live, row[11])
        out.append(tuple(row))
    return out, applied


def rebuild_overlay():
    """Recreate the four Supabase overlay tables from the promotion payload."""
    if not BATCHES.is_dir():
        raise SystemExit(f'Missing promotion batches at {BATCHES}; cannot rebuild the overlay.')
    sources = payloads(BATCHES / '01_sources.sql')
    rows = []
    for path in sorted(BATCHES.glob('rows_*.sql')):
        rows.extend(payloads(path))
    cells = []
    for path in sorted(BATCHES.glob('cells_*.sql')):
        cells.extend(payloads(path))
    if len(sources) != EXPECTED_COUNTS['sources']:
        raise SystemExit(f'Expected {EXPECTED_COUNTS["sources"]} sources, got {len(sources)}.')
    if len(rows) != EXPECTED_COUNTS['rows']:
        raise SystemExit(f'Expected {EXPECTED_COUNTS["rows"]} stat rows, got {len(rows)}.')
    if len(cells) != EXPECTED_COUNTS['cells']:
        raise SystemExit(f'Expected {EXPECTED_COUNTS["cells"]} cells, got {len(cells)}.')

    audit = json.loads((STAGE / 'fresh_visual_rebuild_audit.json').read_text(encoding='utf-8'))
    summary = audit['summary']
    overlay_sha = sha256(STAGE / 'verified_stat_overlay_fresh.jsonl')
    run_id = 'drive_stat_overlay_' + REVIEW_DATE.replace('-', '') + '_' + overlay_sha[:12]
    if run_id != EXPECTED_RUN_ID:
        raise SystemExit(f'Overlay hash moved: rebuilt {run_id}, expected {EXPECTED_RUN_ID}.')
    if summary.get('source_candidate_jsonl_sha256') != sha256(STAGE / 'stat_candidate_rows.jsonl'):
        raise SystemExit('stat_candidate_rows.jsonl no longer matches the pinned audit hash.')
    if summary.get('source_raw_ocr_manifest_sha256') != sha256(STAGE / 'drive_and_upload_ocr.jsonl'):
        raise SystemExit('drive_and_upload_ocr.jsonl no longer matches the pinned audit hash.')

    archives = json.dumps(summary['source_archive_sha256'], ensure_ascii=False, separators=(',', ':'))
    run = {
        'run_id': run_id, 'reviewed_on': REVIEW_DATE, 'candidate_rows': 523, 'screenshot_count': 84,
        'direct_cell_slots_compared': 3521, 'direct_verified_cell_count': EXPECTED_COUNTS['direct_cells'],
        'reconstructed_cell_count': EXPECTED_COUNTS['reconstructed_cells'], 'combined_cell_count': EXPECTED_COUNTS['cells'],
        'prior_pinned_direct_cell_count': 3505, 'historical_direct_count_delta': 1,
        'candidate_jsonl_sha256': summary['source_candidate_jsonl_sha256'],
        'raw_ocr_manifest_sha256': summary['source_raw_ocr_manifest_sha256'],
        'combined_overlay_sha256': overlay_sha,
        'manual_manifest_sha256': sha256(STAGE / 'manual_visual_review_manifest.json'),
        'correction_log_sha256': sha256(STAGE / 'stat_cell_correction_log_fresh.jsonl'),
        'source_archive_sha256': archives, 'user_confirmed': 1,
        'user_confirmation_note': ('User confirmed all records in the fresh rebuild are verified; PILOT stats, '
                                   'including Mech HP and weapon DMG, are percentages.'),
        'pilot_unit_convention': 'percentage', 'pilot_percent_cells': EXPECTED_COUNTS['pilot_percent_cells'],
        'raw_ocr_records_imported': 0, 'unrelated_public_documents_imported': 0,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }

    source_rows = []
    for s in sources:
        source_rows.append((run_id, s[0], s[1], s[2], s[3], s[4], int(s[5]), int(s[6]), int(s[7]),
                            or_empty(s[8]) or None, s[9], s[10]))
    row_rows = []
    row_archive = {}
    for r in rows:
        row_archive[r[0]] = r[2]
        row_rows.append((run_id, r[0], r[1], r[2], or_empty(r[3]) or None, or_empty(r[4]) or None, r[5],
                         or_empty(r[6]) or None, or_empty(r[7]) or None, r[8],
                         or_empty(r[9]) or None,
                         None if r[10] in (None, 'null') else json.dumps(r[10], ensure_ascii=False, separators=(',', ':')),
                         REVIEW_DATE))
    cell_rows = []
    for c in cells:
        row_key, rank, value, code = c[0], int(c[1]), c[2], c[3]
        if code not in CODE_STATUS:
            raise SystemExit(f'Unknown cell status code {code!r} for {row_key}.')
        unit = 'percentage' if (row_archive.get(row_key) == 'PILOT.zip' or '%' in str(value)) else 'as_displayed'
        cell_rows.append((run_id, f'{row_key}:c{rank}', row_key, rank, value, unit, CODE_STATUS[code], 1,
                          REVIEW_DATE, run['created_at']))
    pilot_percent = sum(1 for row in cell_rows if row[5] == 'percentage' and row[1].startswith('PILOT'))
    if pilot_percent != EXPECTED_COUNTS['pilot_percent_cells']:
        raise SystemExit(f'Pilot percentage-cell count moved: {pilot_percent} vs {EXPECTED_COUNTS["pilot_percent_cells"]}.')
    if len({row[1] for row in cell_rows}) != len(cell_rows):
        raise SystemExit('Duplicate cell_key in the rebuilt overlay.')
    return run, source_rows, row_rows, cell_rows


def install_overlay(db: sqlite3.Connection, run, source_rows, row_rows, cell_rows):
    db.executescript(OVERLAY_DDL)
    columns = list(run)
    db.execute(f'INSERT INTO stat_rebuild_runs ({",".join(columns)}) VALUES ({",".join("?" * len(columns))})',
               [run[c] for c in columns])
    db.executemany('INSERT INTO stat_rebuild_sources (rebuild_id,card_id,archive_name,source_path,'
                   'source_screenshot_sha256,source_archive_sha256,candidate_rows,direct_rows_visually_reviewed,'
                   'direct_cell_slots_compared,wrapped_reconstruction_id,review_notes,reviewed_on) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', source_rows)
    db.executemany('INSERT INTO stat_rebuild_rows (rebuild_id,row_key,card_id,archive_name,entity_title,stat_label,'
                   'row_type,candidate_row_id,reconstruction_id,baseline_y,description,'
                   'source_fragment_candidate_row_ids,reviewed_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', row_rows)
    db.executemany('INSERT INTO verified_stat_cells (rebuild_id,cell_key,row_key,rank_column,value,value_unit,'
                   'cell_status,user_confirmed,reviewed_on,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', cell_rows)


def install_empty_runtime_schema(db: sqlite3.Connection):
    """Create the live operational tables with no rows, under mech_bot_app_ names."""
    from mechbot.store import PG_DDL
    created = []
    for name, body in re.findall(r'CREATE TABLE IF NOT EXISTS mech_bot_app\.(\w+) \((.*?)\);', PG_DDL, re.S):
        definitions = []
        for clause in split_top_level(body):
            clause = ' '.join(clause.split())
            if not clause or re.match(r'^(PRIMARY KEY|UNIQUE \(|FOREIGN KEY|CHECK)\b', clause):
                continue
            clause = clause.replace(' GENERATED BY DEFAULT AS IDENTITY', '')
            for old, new in (('BIGINT', 'INTEGER'), ('BOOLEAN', 'INTEGER')):
                clause = re.sub(rf'\b{old}\b', new, clause)
            definitions.append(re.sub(r'\bTIMESTAMPTZ\b', 'TEXT', clause))
        if not definitions:
            raise SystemExit(f'No columns parsed for mech_bot_app.{name}')
        db.execute(f'CREATE TABLE IF NOT EXISTS mech_bot_app_{name} ({", ".join(definitions)});')
        created.append(name)
    for ddl in ('CREATE INDEX IF NOT EXISTS mech_bot_app_announcement_posted ON mech_bot_app_announcements(posted_at DESC)',
                'CREATE INDEX IF NOT EXISTS mech_bot_app_conversation_scope ON mech_bot_app_conversation_turns(user_id,guild_id,channel_id,created_at DESC,id DESC)',
                'CREATE INDEX IF NOT EXISTS mech_bot_app_conversation_expiry ON mech_bot_app_conversation_turns(expires_at)',
                'CREATE INDEX IF NOT EXISTS mech_bot_app_reminder_due ON mech_bot_app_reminders(status,due_at)',
                'CREATE INDEX IF NOT EXISTS mech_bot_app_worker_reminder_due ON mech_bot_app_worker_reminders(status,due_at)'):
        db.execute(ddl)
    if set(created) != set(PRIVATE_TABLES):
        raise SystemExit(f'Runtime schema mirror incomplete: {sorted(created)}')
    for name in created:
        if db.execute(f'SELECT count(*) FROM mech_bot_app_{name}').fetchone()[0] != 0:
            raise SystemExit(f'Private table mech_bot_app_{name} is not empty; refusing to ship it.')
    return created


def split_top_level(text: str):
    """Split a column list on commas that are not nested inside parentheses."""
    out, depth, current = [], 0, ''
    for char in text:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            out.append(current)
            current = ''
        else:
            current += char
    if current.strip():
        out.append(current)
    return out


def write_csvs(db: sqlite3.Connection, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for name in names:
        cur = db.execute(f'SELECT * FROM "{name}"')
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
        counts[name] = len(rows)
        with (out_dir / f'{name}.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv_writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
    return counts


def csv_writer(handle):
    import csv as _csv
    return _csv.writer(handle, quoting=_csv.QUOTE_MINIMAL, lineterminator='\n')


def write_json_collections(db: sqlite3.Connection, out_dir: Path, json_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    made = {}
    for collection, n in db.execute('SELECT collection, count(*) FROM archive_structured_rows GROUP BY collection ORDER BY collection'):
        rows = [json.loads(r[0]) for r in db.execute(
            'SELECT data_json FROM archive_structured_rows WHERE collection=? ORDER BY rowid', (collection,))]
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', collection)
        (out_dir / f'{safe}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        made[collection] = {'file': f'json/structured/{safe}.json', 'records': n}
    meta = {r[0]: r[1] for r in db.execute('SELECT key, value FROM metadata')}
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / 'snapshot_metadata.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    return made


def fingerprint_cells(cell_rows):
    parts = sorted(f'{r[1]}|{r[3]}|{r[4]}|{r[5]}|{r[6]}' for r in cell_rows)
    return hashlib.md5(','.join(parts).encode('utf-8')).hexdigest(), len(parts)


def fingerprint_rows(row_rows):
    parts = sorted(f'{r[1]}|{r[2]}|{r[3]}|{or_empty(r[4])}|{or_empty(r[5])}|{r[6]}|{or_empty(r[7])}|{or_empty(r[8])}'
                   for r in row_rows)
    return hashlib.md5(','.join(parts).encode('utf-8')).hexdigest(), len(parts)


def fingerprint_sources(source_rows):
    parts = sorted(f'{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}|{r[6]}|{r[7]}|{r[8]}|{or_empty(r[9])}|{r[10]}|{r[11]}'
                   for r in source_rows)
    return hashlib.md5(','.join(parts).encode('utf-8')).hexdigest(), len(parts)


def check_against_supabase(evidence_path: Path | None, run, source_rows, row_rows, cell_rows):
    checks = {
        'run_id': run['run_id'],
        'cells': {'rows': len(cell_rows), 'fingerprint': fingerprint_cells(cell_rows)[0]},
        'rows': {'rows': len(row_rows), 'fingerprint': fingerprint_rows(row_rows)[0]},
        'sources': {'rows': len(source_rows), 'fingerprint': fingerprint_sources(source_rows)[0]},
        'overlay_sha256': run['combined_overlay_sha256'],
        'status': 'not checked',
    }
    if evidence_path is None or not evidence_path.exists():
        checks['status'] = 'skipped (no captured Supabase evidence provided)'
        return checks, True
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    remote = evidence['observed']
    problems = []
    for key in ('cells', 'rows', 'sources'):
        if str(remote[key]['n']) != str(checks[key]['rows']):
            problems.append(f"{key} row count {remote[key]['n']} != {checks[key]['rows']}")
        if remote[key]['fp'] != checks[key]['fingerprint']:
            problems.append(f'{key} md5 fingerprint mismatch')
    for column in ('combined_overlay_sha256', 'candidate_jsonl_sha256', 'raw_ocr_manifest_sha256',
                   'manual_manifest_sha256', 'correction_log_sha256'):
        if remote['run'][0][column] != run[column]:
            problems.append(f'run.{column} mismatch')
    if str(remote['run'][0]['user_confirmed']).lower() not in ('true', '1'):
        problems.append('live run is not flagged user_confirmed')
    if remote['run'][0]['reviewed_on'] != run['reviewed_on']:
        problems.append('live run reviewed_on mismatch')
    checks['status'] = 'match' if not problems else 'MISMATCH: ' + '; '.join(problems)
    checks['queries'] = evidence.get('queries', {})
    checks['captured_at'] = evidence.get('captured_at')
    return checks, not problems


def build(out_root: Path, evidence_path: Path | None, make_zip: bool, source_notes_path: Path | None = None):
    if out_root.exists():
        raise SystemExit(f'{out_root} already exists; refusing to overwrite a pack.')
    pack = out_root / 'mech-arena-database-pack'
    (pack / 'database').mkdir(parents=True)
    (pack / 'source_evidence' / 'screenshot_archives').mkdir(parents=True)
    (pack / 'source_evidence' / 'rebuild_artifacts').mkdir(parents=True)
    (pack / 'csv').mkdir(parents=True)
    (pack / 'json' / 'structured').mkdir(parents=True)
    (pack / 'supabase_schema').mkdir(parents=True)
    (pack / 'verification').mkdir(parents=True)

    db_path = pack / 'database' / 'mech_arena_database.sqlite3'
    with gzip.open(SNAPSHOT_GZ, 'rb') as src, db_path.open('wb') as dst:
        shutil.copyfileobj(src, dst)
    db = sqlite3.connect(db_path)
    db.execute('PRAGMA foreign_keys=ON')

    run, source_rows, row_rows, cell_rows = rebuild_overlay()
    source_rows, note_divergence = apply_live_source_notes(source_rows, source_notes_path)
    install_overlay(db, run, source_rows, row_rows, cell_rows)
    runtime = install_empty_runtime_schema(db)

    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise SystemExit(f'Pack database failed integrity check: {integrity}')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise SystemExit('Pack database has overlay foreign-key violations.')

    checks, ok = check_against_supabase(evidence_path, run, source_rows, row_rows, cell_rows)
    checks['live_source_note_divergence'] = {
        'cards': [item['card_id'] for item in note_divergence],
        'replaced': note_divergence,
        'meaning': ('These source-capture review_notes were taken from the live database because the promotion '
                    'insert used ON CONFLICT DO NOTHING and an earlier attempt had already written those rows. '
                    'Stat values, counts, rows and cells are unaffected.'),
    }
    if not ok:
        raise SystemExit('Rebuilt overlay does not match the live Supabase copy: ' + checks['status'])

    counts = write_csvs(db, pack / 'csv')
    collections = write_json_collections(db, pack / 'json' / 'structured', pack / 'json')

    db.execute('DROP TABLE IF EXISTS pack_dataset')
    db.execute('''CREATE TABLE pack_dataset (dataset TEXT PRIMARY KEY, tier TEXT NOT NULL, source TEXT NOT NULL,
                  rows INTEGER NOT NULL, description TEXT NOT NULL)''')
    db.execute('DROP TABLE IF EXISTS pack_file')
    db.execute('''CREATE TABLE pack_file (path TEXT PRIMARY KEY, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL)''')
    db.execute('DROP TABLE IF EXISTS pack_meta')
    db.execute('CREATE TABLE pack_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    for key, value in [('pack_built_utc', datetime.now(timezone.utc).isoformat(timespec='seconds')),
                       ('pack_build_date', str(date.today())),
                       ('screenshot_overlay_reviewed_on', REVIEW_DATE),
                       ('screenshot_overlay_run_id', run['run_id']),
                       ('snapshot_gz_sha256', sha256(SNAPSHOT_GZ)),
                       ('snapshot_gz_bytes', str(SNAPSHOT_GZ.stat().st_size)),
                       ('live_database', 'Supabase project hq bot / schema mech_bot_app (read-only; no rows copied from user-data tables)'),
                       ('excluded_tables', ', '.join(f'mech_bot_app.{t}' for t in runtime)),
                       ('excluded_reason', 'Live Discord user data (messages, notes, reminders, profiles, payout state) and the announcement cache: schema only, zero rows.')]:
        db.execute('INSERT INTO pack_meta VALUES (?,?)', (key, value))

    tiers = {'verified_overlay': ('user-confirmed', 'supabase:mech_bot_app overlay + pinned screenshot archive'),
             'raw_ocr': ('user-approved raw records', 'data/drive_ocr_stage'),
             'archive': ('archived public sources', 'data/mech_bot_staging.sqlite3.gz'),
             'candidates': ('unverified candidates', 'data/mech_bot_staging.sqlite3.gz'),
             'empty_schema': ('excluded content, schema only', 'supabase:mech_bot_app (deliberately empty)')}
    overlay_tables = {'stat_rebuild_runs', 'stat_rebuild_sources', 'stat_rebuild_rows', 'verified_stat_cells'}
    candidate_tables = {'weapon_stat_candidates', 'archive_image_transcription_staging'}
    ocr_tables = {'archive_image_ocr'}
    for name, rows in counts.items():
        if name in overlay_tables:
            tier, source = tiers['verified_overlay']
        elif name in ocr_tables:
            tier, source = tiers['raw_ocr']
        elif name in candidate_tables:
            tier, source = tiers['candidates']
        elif name.startswith('mech_bot_app_'):
            tier, source = tiers['empty_schema']
        else:
            tier, source = tiers['archive']
        db.execute('INSERT INTO pack_dataset VALUES (?,?,?,?,?)',
                   (name, tier, source, rows, TABLE_NOTES.get(name, '')))
    db.commit()

    # Raw evidence: the screenshot archives and every rebuild/audit artifact.
    shutil.copy2(SOURCE_ARCHIVES / 'MA SS.zip', pack / 'source_evidence' / 'screenshot_archives' / 'MA SS.zip')
    shutil.copy2(SOURCE_ARCHIVES / 'PILOT.zip', pack / 'source_evidence' / 'screenshot_archives' / 'PILOT.zip')
    for item in sorted(STAGE.rglob('*')):
        if item.is_file():
            target = pack / 'source_evidence' / 'rebuild_artifacts' / item.relative_to(STAGE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    review_cards = ROOT / 'data' / 'drive_ocr_review_cards'
    if review_cards.is_dir():
        shutil.copytree(review_cards, pack / 'source_evidence' / 'review_cards')
    shutil.copy2(SNAPSHOT_GZ, pack / 'source_evidence' / 'mech_bot_staging.sqlite3.gz')
    if FORUM_JSON.exists():
        shutil.copy2(FORUM_JSON, pack / 'json' / 'plarium_forum_last_150_threads.json')

    for migration in sorted(MIGRATIONS.glob('*.sql')):
        shutil.copy2(migration, pack / 'supabase_schema' / migration.name)
    from mechbot.store import PG_DDL
    (pack / 'supabase_schema' / 'mech_bot_app_runtime_schema.sql').write_text(
        '-- The live schema the bot runs against. These tables exist in the pack only as empty\n'
        '-- mirrors named mech_bot_app_<table>; their rows were deliberately not exported.\n\n' + PG_DDL,
        encoding='utf-8')
    (pack / 'supabase_schema' / 'how_to_verify_overlay.sql').write_text(VERIFICATION_SQL, encoding='utf-8')
    (pack / 'verification' / 'overlay_vs_live_supabase.json').write_text(
        json.dumps(checks, ensure_ascii=False, indent=1), encoding='utf-8')

    # Reproducibility without shipping operational code: the bot's own source holds guild,
    # channel and member identifiers and deployment wiring, which do not belong in a public pack.
    builder_dir = pack / 'builder'
    builder_dir.mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), builder_dir / 'build_mech_arena_data_pack.py')
    (builder_dir / 'NOTES.md').write_text(
        '# How this pack was built\n\n'
        '`build_mech_arena_data_pack.py` is the exact script that produced this folder: it decompresses the pinned\n'
        'game snapshot, rebuilds the four Supabase overlay tables from the promotion payload, mirrors the live\n'
        'runtime schema as empty tables, writes the CSV/JSON mirrors, and checksums everything.\n\n'
        'The bot itself is not included on purpose: its source and config carry Discord guild, channel and member\n'
        'identifiers plus deployment wiring, which have no place in a shareable data pack. Run the script from a\n'
        'checkout of the bot repository (it reads `data/`, `migrations/` and `mechbot/store.py` from there).\n',
        encoding='utf-8')

    # Input provenance: what this pack was built from, with the hash of each input file.
    db.execute('DELETE FROM pack_file')
    inputs = [SNAPSHOT_GZ, FORUM_JSON, *sorted(STAGE.rglob('*.json*')), *sorted(STAGE.rglob('*.xlsx')),
              *sorted(STAGE.rglob('*.md')), *sorted(SOURCE_ARCHIVES.glob('*.zip'))]
    for item in inputs:
        if item.is_file():
            db.execute('INSERT INTO pack_file VALUES (?,?,?)',
                       (item.relative_to(ROOT).as_posix(), item.stat().st_size, sha256(item)))
    db.execute("INSERT INTO pack_meta (key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               ('pack_file_meaning', 'sha256 of each input file under the bot repo, computed when the pack was built'))
    db.commit()

    dictionary = build_dictionary(db, counts)
    counts_table = db.execute('SELECT dataset, tier, rows FROM pack_dataset ORDER BY dataset').fetchall()
    total_rows = sum(r[2] for r in counts_table if not r[0].startswith('mech_bot_app_')
                     and r[0] not in ('pack_dataset', 'pack_file', 'pack_meta'))
    table_count = len([name for name in counts if name != 'bot_verified_weapon_stats'])
    readme = build_readme(run, counts, checks, total_rows, out_root.name, table_count)
    db.close()

    (pack / 'README.md').write_text(readme, encoding='utf-8')
    (pack / 'DATA_DICTIONARY.md').write_text(dictionary, encoding='utf-8')
    for suffix in ('-wal', '-shm'):
        (pack / 'database' / f'mech_arena_database.sqlite3{suffix}').unlink(missing_ok=True)

    def digests():
        return {item.relative_to(pack).as_posix(): sha256(item)
                for item in sorted(p for p in pack.rglob('*') if p.is_file() and p.name != 'MANIFEST.sha256')}

    shipped = digests()
    (pack / 'manifest.json').write_text(json.dumps({
        'pack': 'mech-arena-database-pack',
        'built_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'overlay_run_id': run['run_id'],
        'overlay_reviewed_on': REVIEW_DATE,
        'tables': {name: rows for name, rows in sorted(counts.items())},
        'structured_collections': collections,
        'live_database_match': checks['status'],
        'excluded_private_tables': list(PRIVATE_TABLES),
        'database_sha256': shipped['database/mech_arena_database.sqlite3'],
        'files': len(shipped),
        'sha256': shipped,
    }, ensure_ascii=False, indent=1), encoding='utf-8')
    # MANIFEST is written last so every file it lists, manifest.json included, is final.
    final = digests()
    (pack / 'MANIFEST.sha256').write_text(
        '\n'.join(f'{digest}  {rel}' for rel, digest in final.items()) + '\n', encoding='utf-8')

    archive = None
    if make_zip:
        archive = out_root / (out_root.name + '.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for item in sorted(p for p in pack.rglob('*') if p.is_file()):
                zf.write(item, item.relative_to(out_root).as_posix())
        bad = zipfile.ZipFile(archive).testzip()
        if bad:
            raise SystemExit(f'Zip integrity failed at {bad}')
    return out_root, archive, checks, counts, total_rows


VERIFICATION_SQL = '''-- Run these read-only queries against the live bot database to confirm the
-- pack matches what the bot serves. Each one must equal the value recorded in
-- verification/overlay_vs_live_supabase.json (order by ... COLLATE "C" keeps
-- the string order identical to a plain byte sort in Python).

SELECT count(*) AS cells,
       md5(string_agg(cell_key || '|' || rank_column::text || '|' || value || '|' || value_unit || '|'
                       || cell_status, ',' ORDER BY cell_key COLLATE "C")) AS fp
FROM mech_bot_app.verified_stat_cells
WHERE rebuild_id = 'drive_stat_overlay_20261110_99555d68e4d4';

SELECT count(*) AS rows,
       md5(string_agg(row_key || '|' || card_id || '|' || archive_name || '|' || coalesce(entity_title, '')
                      || '|' || coalesce(stat_label, '') || '|' || row_type || '|'
                      || coalesce(candidate_row_id, '') || '|' || coalesce(reconstruction_id, ''),
                      ',' ORDER BY row_key COLLATE "C")) AS fp
FROM mech_bot_app.stat_rebuild_rows
WHERE rebuild_id = 'drive_stat_overlay_20261110_99555d68e4d4';

SELECT count(*) AS rows,
       md5(string_agg(card_id || '|' || archive_name || '|' || source_path || '|' || source_screenshot_sha256
                      || '|' || source_archive_sha256 || '|' || candidate_rows::text
                      || '|' || direct_rows_visually_reviewed::text || '|' || direct_cell_slots_compared::text
                      || '|' || coalesce(wrapped_reconstruction_id, '') || '|' || review_notes || '|'
                      || reviewed_on::text, ',' ORDER BY card_id COLLATE "C")) AS fp
FROM mech_bot_app.stat_rebuild_sources
WHERE rebuild_id = 'drive_stat_overlay_20261110_99555d68e4d4';

SELECT run_id, reviewed_on, user_confirmed, combined_cell_count, direct_verified_cell_count,
       reconstructed_cell_count, combined_overlay_sha256, candidate_jsonl_sha256,
       raw_ocr_manifest_sha256, manual_manifest_sha256, correction_log_sha256
FROM mech_bot_app.stat_rebuild_runs
WHERE run_id = 'drive_stat_overlay_20261110_99555d68e4d4';

-- These tables must be present but were intentionally exported empty:
SELECT 'profiles' t, count(*) FROM mech_bot_app.profiles
UNION ALL SELECT 'reminders', count(*) FROM mech_bot_app.reminders
UNION ALL SELECT 'notes', count(*) FROM mech_bot_app.notes
UNION ALL SELECT 'conversation_turns', count(*) FROM mech_bot_app.conversation_turns
UNION ALL SELECT 'announcements', count(*) FROM mech_bot_app.announcements
UNION ALL SELECT 'payout_alerts', count(*) FROM mech_bot_app.payout_alerts
UNION ALL SELECT 'worker_campaign', count(*) FROM mech_bot_app.worker_campaign
UNION ALL SELECT 'worker_reminders', count(*) FROM mech_bot_app.worker_reminders;
'''


def build_dictionary(db: sqlite3.Connection, counts) -> str:
    lines = ['# Data dictionary', '',
             'Open `database/mech_arena_database.sqlite3` (or use the matching CSV in `csv/<table>.csv`).',
             '`tier` tells you how much to trust a table: **user-confirmed** values were checked against the',
             'screenshot; **unverified candidates** were machine-read and not signed off.', '']
    for name in sorted(counts):
        cols = db.execute(f'PRAGMA table_info("{name}")').fetchall()
        kind = db.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
        tier = db.execute('SELECT tier FROM pack_dataset WHERE dataset=?', (name,)).fetchone()
        note = TABLE_NOTES.get(name, '')
        lines += [f'## {name}', '',
                  f'- kind: {kind[0] if kind else "table"} · tier: {tier[0] if tier else "n/a"} · rows: {counts[name]}',
                  f'- {note}' if note else '- no curated note for this table',
                  '', '| column | type | not null |', '| --- | --- | --- |']
        lines += [f'| {c[1]} | {c[2] or ""} | {"yes" if c[3] else ""} |' for c in cols]
        lines.append('')
    return '\n'.join(lines)


def build_readme(run, counts, checks, total_rows, pack_name, table_count) -> str:
    overlay_rows = counts.get('verified_stat_cells', 0)
    fmt = lambda n: f'{n:,}'
    return f'''# Mech Arena database pack

Everything the ARCHΛNGLE Mech Arena Discord bot knows, in one folder. Built {date.today().isoformat()}; the
screenshot-stat overlay inside it was reviewed on **{REVIEW_DATE}**.

- **{fmt(total_rows)} data rows across {table_count} tables** (plus the `bot_verified_weapon_stats` view), in one SQLite file
- every game-data collection the bot answers from (weapons, mechs, pilots, mods, upgrade costs, ECR data, skins, guides)
- the **{fmt(overlay_rows)} user-confirmed screenshot stat cells** (pilot + mech rank 1→7), each tied back to its
  source PNG by SHA-256
- the **raw evidence**: the two screenshot archives the numbers came from, the OCR/rebuild audit trail, and
  the promotion receipt
- CSV and JSON mirrors of every table, a data dictionary, and a checksummed `MANIFEST.sha256`

## Open it

```bash
unzip {pack_name}.zip
cd {pack_name}/mech-arena-database-pack
sqlite3 database/mech_arena_database.sqlite3
```

```sql
-- confirmed Acantha stat progression, rank by rank
SELECT r.entity_title, r.stat_label, c.rank_column, c.value
FROM verified_stat_cells c JOIN stat_rebuild_rows r ON r.row_key = c.row_key
WHERE lower(r.entity_title) = 'acantha'
ORDER BY r.stat_label, c.rank_column;

-- every mech the catalog knows, with its confirmed stat-cell count
SELECT m.name, count(c.cell_key) AS confirmed_cells
FROM mechs m
LEFT JOIN stat_rebuild_rows r ON lower(r.entity_title) = lower(m.name) AND r.archive_name = 'MA SS.zip'
LEFT JOIN verified_stat_cells c ON c.row_key = r.row_key
GROUP BY m.name ORDER BY confirmed_cells DESC, m.name;
```

No SQLite? `csv/<table>.csv` and `json/structured/<collection>.json` hold the same rows.

## What each tier means

| tier | meaning |
| --- | --- |
| **user-confirmed** (`verified_stat_cells`, `stat_rebuild_*`) | Each cell was visually checked against the pinned screenshot and approved by the data owner. Values are displayed *exactly* as in the game — signs, decimals, units and `%` included. The rank columns are **ranks, not levels**. Pilot bonuses and PILOT-overlay mech stats are percentages. |
| **user-approved raw records** (`archive_image_ocr`) | The 90 raw OCR records the owner approved for lookup. The approval covers the OCR text, not game truth: screenshot identity, OCR accuracy and game version are not independently verified. |
| **archived public sources** (`archive_*`, `weapons`, `mechs`, `screenshots`) | Scraped from the in-game calculator, community wiki/database pages and the Plarium forum. Forum posts are **community claims**: useful, not official, and sometimes stale or wrong. |
| **unverified candidates** (`weapon_stat_candidates`, `archive_image_transcription_staging`) | Machine-read stat cells that were never signed off. **Do not quote these as facts.** `status` marks what happened to each row. |
| **excluded content, schema only** (`mech_bot_app_*`) | Live operational tables (see below) — structure only, zero rows. |

## What is deliberately *not* in here

The bot's live database also stores Discord user data: `conversation_turns` (people's messages to the bot),
`notes`, `reminders`, `profiles` (user IDs + timezones), `payout_alerts`, `worker_campaign`,
`worker_reminders`, and a cached copy of announcement channels. **None of those rows are in this pack.** Their
`CREATE TABLE` definitions are reproduced as empty `mech_bot_app_*` tables and in
`supabase_schema/mech_bot_app_runtime_schema.sql` so you can see the shape without getting anyone's data.

## Verifying a number back to a pixel

`verified_stat_cells.cell_key` → `stat_rebuild_rows.row_key` → `stat_rebuild_sources.source_path`
+ `source_screenshot_sha256`. The two archives under `source_evidence/screenshot_archives/` contain those PNGs:

| archive | SHA-256 |
| --- | --- |
| `MA SS.zip` | `{json.loads(run['source_archive_sha256'])['MA SS.zip']}` |
| `PILOT.zip` | `{json.loads(run['source_archive_sha256'])['PILOT.zip']}` |

{f"**Note on 5 pilot source cards:** their review-note wording is copied from the live database "
   f"({', '.join(checks['live_source_note_divergence']['cards'])}) because the original insert used "
   "ON CONFLICT DO NOTHING and kept an earlier phrasing. Only the note text differs; every stat value, "
   "count, row and cell is identical to the pinned artifacts.\n\n" if checks.get('live_source_note_divergence', {}).get('cards') else ''}Overlay run: `{run['run_id']}` · overlay SHA-256 `{run['combined_overlay_sha256']}` · match against the
live database: **{checks['status']}** (see `verification/overlay_vs_live_supabase.json`, queries in
`supabase_schema/how_to_verify_overlay.sql`).

The 14 reconstructed cells were rebuilt from a neighbouring screenshot row where the game UI wrapped; they are
flagged `row_type='reconstructed'` / `cell_status='reconstructed_from_original_screenshot'`.

## Folder map

| path | contents |
| --- | --- |
| `database/mech_arena_database.sqlite3` | the whole pack in one SQLite file (game data + overlay + `pack_dataset`/`pack_file`/`pack_meta`) |
| `csv/` | one CSV per table and the `bot_verified_weapon_stats` view |
| `json/structured/` | every `archive_structured_rows` collection as a JSON array |
| `json/snapshot_metadata.json` | snapshot provenance: scope, warnings, pinned hashes, Drive URLs |
| `json/plarium_forum_last_150_threads.json` | the archived Plarium forum crawl the `/forum` feature reads |
| `source_evidence/screenshot_archives/` | `MA SS.zip` (mech stats) and `PILOT.zip` (pilot stats) — the PNGs behind the overlay |
| `source_evidence/rebuild_artifacts/` | OCR records, candidate rows, correction log, validation report, Supabase promotion batches and receipts |
| `source_evidence/mech_bot_staging.sqlite3.gz` | the exact snapshot the bot loads, as shipped |
| `supabase_schema/` | the overlay + runtime DDL and the verification queries |
| `builder/` | the script that produced this pack, plus notes on what was withheld and why |
| `MANIFEST.sha256`, `manifest.json` | checksums and row counts for every file above |

## Rebuild it yourself

```bash
# from a checkout of the bot repo (needs data/ + migrations/ + mechbot/store.py present)
python scripts/build_mech_arena_data_pack.py --out /tmp/pack --zip \\
    --supabase-evidence exports/supabase_evidence_20261118.json
```

Mech Arena is Plarium's game. This is a fan-made dataset: no numbers here are official, and a game update can make
any of it stale.
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'exports' / f'mech-arena-database-pack-{date.today():%Y%m%d}'))
    parser.add_argument('--supabase-evidence', default=None)
    parser.add_argument('--zip', action='store_true')
    parser.add_argument('--live-source-notes', default=str(ROOT / 'exports' / 'live_source_review_notes.json'))
    args = parser.parse_args()
    notes = Path(args.live_source_notes) if args.live_source_notes else None
    out_root, archive, checks, counts, total_rows = build(
        Path(args.out), Path(args.supabase_evidence) if args.supabase_evidence else None, args.zip, notes)
    print(f'pack: {out_root}')
    if archive:
        print(f'zip: {archive} ({archive.stat().st_size:,} bytes)')
    print(f'tables: {len(counts)} · game rows: {total_rows:,} · overlay: {checks["cells"]["rows"]:,} cells')
    print(f'supabase match: {checks["status"]}')


if __name__ == '__main__':
    main()
