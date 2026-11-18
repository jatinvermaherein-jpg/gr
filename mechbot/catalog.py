"""Read-only runtime catalog with pinned user reviews and searchable public data."""
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
import re
import shutil
import sqlite3
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ASSET_HOSTS={'database.mecharenadev.com','mecharena.infohubhq.in','forum.plarium.com'}
ARCHIVE = ROOT / 'data' / 'mech_bot_staging.sqlite3.gz'
log = logging.getLogger('mechbot.catalog')

# User confirmation for the exact screenshot snapshot, not for inferred identities/cells.
USER_CONFIRMED_ARCHIVE_SHA256 = 'eda05a31d893ec11ce13f221833b68b19108aa322affb9c4a19ecf6064c30ae8'
USER_CONFIRMED_VALUES_SHA256 = 'd41cc54efd214cc295a91c3f7fe179156f4e056c73850ebbc872ac83b2161edb'
USER_CONFIRMED_CANDIDATE_COUNT = 22652
USER_CONFIRMED_REVIEWER = 'requesting user (chat confirmation)'
USER_CONFIRMED_REVIEW_DATE = '2026-11-10'
USER_CONFIRMED_REVIEW_NOTE = (
    'The user stated in chat on 2026-11-10 that they personally verified all OCR candidate values '
    'from the 251-screenshot weapon dataset. This confirms detected cell values, not unresolved '
    'screenshot-to-weapon identities or missing/cropped cells.'
)

# The following manifest pins the website/forum/calculator/database material already archived.
# Later ingests are not automatically promoted by this confirmation.
USER_CONFIRMED_PUBLIC_SOURCES = ('mech_arena_database', 'plarium_forum', 'upgrade_calculator')
USER_CONFIRMED_PUBLIC_DOC_COUNT = 147
USER_CONFIRMED_PUBLIC_STRUCTURED_COUNT = 1338
USER_CONFIRMED_PUBLIC_FINGERPRINT = '5072a459d5e97ae93725e1215565fc5b3d4ba0b85af84266b19c0943539d34be'
USER_CONFIRMED_PUBLIC_REVIEWER = 'requesting user (chat confirmation)'
USER_CONFIRMED_PUBLIC_REVIEW_DATE = '2026-11-10'
USER_CONFIRMED_PUBLIC_REVIEW_NOTE = (
    'The user explicitly confirmed in chat on 2026-11-10 that all already-extracted website, forum, '
    'calculator, and database documents in this exact archive are verified and official for bot use. '
    'The original source, author, title, URL, and dates are retained; this confirmation applies only '
    'to the pinned archive snapshot.'
)

# This approval covers the pinned raw OCR manifest. The fresh visual pilot/mech stat overlay was
# separately reviewed and promoted to Supabase; the stale legacy SQLite candidate statuses remain untouched.
USER_CONFIRMED_DRIVE_OCR_MANIFEST_SHA256 = 'dfc42f5cab6e7ce74526c9700bc1aedd308c2f09f2e356fdb33331cafa5537c5'
USER_CONFIRMED_DRIVE_OCR_REVIEWER = 'requesting user (chat confirmation)'
USER_CONFIRMED_DRIVE_OCR_REVIEW_NOTE = (
    'The user explicitly directed that the 90 raw OCR records in the pinned Drive/chat-upload manifest '
    'be marked verified and available to the bot. This approval concerns those raw OCR records only. '
    'The legacy SQLite copy still has 523 derived candidate rows with stale needs_review statuses; a '
    'separate fresh 84-screenshot visual rebuild (3,520 cells) was user-confirmed and promoted to the '
    'private Supabase stat overlay. Gladitor remains user-attributed, not independently verified; this '
    'raw OCR record approval does not attest to its author attribution.'
)
USER_CONFIRMED_DRIVE_OCR_DOCUMENTS = {
    'PILOT.zip': {'id': 148, 'source': 'google_drive_archive', 'category': 'pilot_stats_screenshots',
                  'collection': 'drive_screenshot_ocr', 'count': 36, 'archive_id': '1o7-ceXZ8bQqhKK1kpVchnK28nE_XEH61'},
    'MA SS.zip': {'id': 149, 'source': 'google_drive_archive', 'category': 'mech_stats_screenshots',
                  'collection': 'drive_screenshot_ocr', 'count': 48, 'archive_id': '1TY_Mal19TN_5lkmvQkvSBHrDexHFqHcE'},
    'chat_uploads': {'id': 150, 'source': 'user_chat_upload', 'category': 'context_screenshots',
                     'collection': 'user_upload_screenshot_ocr', 'count': 6, 'archive_id': None},
}
USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS = {'drive_pilot_stat_candidates': 72, 'drive_mech_stat_candidates': 451}

# Transcribed from the pinned calculator source map's `aCoinCostPerBluePrint` table.
# Keep this separate from level-up costs: it is the A-Coin price to buy each missing BP.
BLUEPRINT_ACOINS_PER_RARITY = {
    'common': 2,
    'uncommon': 4,
    'rare': 12,
    'epic_a': 48,
    'epic_b': 48,
    'epic_c': 48,
    'legendary': 240,
}


def _candidate_fingerprint(db):
    rows = db.execute('''SELECT s.sha256,s.weapon_id,w.name,sc.stat_name,sc.raw_label,sc.source_row_number,
        sc.rank_column,sc.rank,sc.level,sc.star_color,sc.candidate_value
        FROM weapon_stat_candidates sc JOIN screenshots s ON s.id=sc.screenshot_id
        LEFT JOIN weapons w ON w.id=s.weapon_id ORDER BY sc.id''').fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update('\x1f'.join('' if value is None else str(value) for value in row).encode('utf-8'))
        digest.update(b'\n')
    return len(rows), digest.hexdigest()


def _public_archive_fingerprint(db, sources=USER_CONFIRMED_PUBLIC_SOURCES):
    marks = ','.join('?' for _ in sources)
    docs = db.execute(f'''SELECT id,url,source,category,title,author_label,published_text,content_hash
        FROM archive_documents WHERE source IN ({marks}) ORDER BY id''', sources).fetchall()
    rows = db.execute(f'''SELECT r.id,r.document_id,r.collection,r.row_key,r.data_json
        FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id
        WHERE d.source IN ({marks}) ORDER BY r.id''', sources).fetchall()
    digest = hashlib.sha256()
    for tag, values in ((b'D', docs), (b'R', rows)):
        for row in values:
            digest.update(tag + b'\x1e')
            digest.update('\x1f'.join('' if value is None else str(value) for value in row).encode('utf-8'))
            digest.update(b'\n')
    return len(docs), len(rows), digest.hexdigest()


def _set_metadata(db, key, value):
    db.execute('''INSERT INTO metadata(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, value))


def _safe_user_chat_ocr_excerpt(value):
    """Expose only game-relevant OCR snippets, not chat turns, handles, or image-generation prompts."""
    text=str(value or '')
    if re.search(r'(?i)generated\s*image|create\s+one\s+polished|transparent\s+png|currency\s+icon|supplied\s+.*?reference',text):
        return ''
    speaker=r'[\w@./-]+(?:\s+[\w@./-]+){0,2}\s+\d{1,2}:\d{2}\s*(?:AM|PM)'
    text=re.sub(r'(?is)'+speaker+r'.*?(?='+speaker+r'|$)','',text)
    game_term=re.compile(r'\b(?:mech(?:\s*arena)?|weapon|pilot|implant|stat|hp|damage|speed|energy|rank|ability|legendary|epic|rare|tier|redox|dreadnought|aoe|stasis|credits?|a[- ]coins|blueprints?)\b',re.I)
    output=[]
    for line in text.splitlines():
        line=re.sub(r'^\s*APP\s+@ARCH/?ANGLE[.:]?\s*','',line,flags=re.I).strip()
        if not line or not game_term.search(line):continue
        if re.search(r'(?i)\b(?:stop\s+emergency|emergency_stop|dm|username|who\s+inputted|data\s+in\s+the\s+catalog)\b',line):continue
        output.append(line)
    return '\n'.join(output)[:750]


def apply_user_confirmed_ocr_review(path, *, archive_sha=USER_CONFIRMED_ARCHIVE_SHA256,
                                    values_sha=USER_CONFIRMED_VALUES_SHA256,
                                    candidate_count=USER_CONFIRMED_CANDIDATE_COUNT,
                                    reviewed_at=None):
    """Promote only the exact OCR snapshot the user explicitly confirmed; leave source DB untouched."""
    db = sqlite3.connect(path)
    try:
        archive = db.execute("SELECT value FROM metadata WHERE key='screenshot_zip_sha256'").fetchone()
        if not archive or archive[0] != archive_sha:
            return 0
        count, fingerprint = _candidate_fingerprint(db)
        if count != candidate_count or fingerprint != values_sha:
            return 0
        statuses = {row[0] for row in db.execute('SELECT DISTINCT status FROM weapon_stat_candidates')}
        if statuses == {'verified'}:
            return count
        if statuses != {'unverified'}:
            return 0
        reviewed_at = reviewed_at or USER_CONFIRMED_REVIEW_DATE
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE weapon_stat_candidates SET status='verified',reviewed_by=?,reviewed_at=?,review_note=? WHERE status='unverified'",
                   (USER_CONFIRMED_REVIEWER, reviewed_at, USER_CONFIRMED_REVIEW_NOTE))
        db.execute("INSERT INTO review_events(candidate_id,action,reviewer,reviewed_at,note) SELECT id,'verified',?,?,? FROM weapon_stat_candidates",
                   (USER_CONFIRMED_REVIEWER, reviewed_at, USER_CONFIRMED_REVIEW_NOTE))
        _set_metadata(db, 'description',
                      'Offline staging: all detected weapon-stat OCR cells in the user-reviewed 251-screenshot dataset were confirmed on 2026-11-10.')
        _set_metadata(db, 'review_warning',
                      'OCR cell values are user-confirmed. Screenshot-to-weapon identities and missing/cropped cells remain gated; website archive data has a separate review record.')
        db.commit()
        return count
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_user_confirmed_public_review(path, *, expected_fingerprint=USER_CONFIRMED_PUBLIC_FINGERPRINT,
                                      document_count=USER_CONFIRMED_PUBLIC_DOC_COUNT,
                                      structured_count=USER_CONFIRMED_PUBLIC_STRUCTURED_COUNT,
                                      reviewed_at=None):
    """Promote the exact user-confirmed public-source archive on a runtime copy only."""
    db = sqlite3.connect(path)
    try:
        try:
            count, structured_count_actual, fingerprint = _public_archive_fingerprint(db)
        except sqlite3.Error:
            return 0
        if (count != document_count or structured_count_actual != structured_count
                or fingerprint != expected_fingerprint):
            return 0
        marks = ','.join('?' for _ in USER_CONFIRMED_PUBLIC_SOURCES)
        rows = db.execute(f'''SELECT review_status,trust FROM archive_documents
            WHERE source IN ({marks})''', USER_CONFIRMED_PUBLIC_SOURCES).fetchall()
        if len(rows) != document_count:
            return 0
        if any(row[0] not in ('pending', 'verified') for row in rows):
            return 0
        if any(row[1] not in ('user_designated_trusted', 'unverified', 'user_confirmed') for row in rows):
            return 0
        reviewed_at = reviewed_at or USER_CONFIRMED_PUBLIC_REVIEW_DATE
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(archive_documents)')}
        for name in ('reviewed_by', 'reviewed_at', 'review_note'):
            if name not in columns:
                db.execute(f'ALTER TABLE archive_documents ADD COLUMN {name} TEXT')
        db.execute(f'''UPDATE archive_documents SET review_status='verified',trust='user_confirmed',
            reviewed_by=?,reviewed_at=?,review_note=? WHERE source IN ({marks})''',
            (USER_CONFIRMED_PUBLIC_REVIEWER, reviewed_at, USER_CONFIRMED_PUBLIC_REVIEW_NOTE,
             *USER_CONFIRMED_PUBLIC_SOURCES))
        db.execute(f'''UPDATE archive_ingest_queue SET status='verified_by_user_confirmation'
            WHERE status='digested_pending_review' AND document_id IN
            (SELECT id FROM archive_documents WHERE source IN ({marks}))''', USER_CONFIRMED_PUBLIC_SOURCES)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_digests'").fetchone():
            digest_rows = db.execute(f'''SELECT g.rowid,g.digest_json FROM archive_digests g
                JOIN archive_documents d ON d.id=g.document_id WHERE d.source IN ({marks})''',
                USER_CONFIRMED_PUBLIC_SOURCES).fetchall()
            for rowid, digest_json in digest_rows:
                try:
                    value = json.loads(digest_json)
                except (TypeError, json.JSONDecodeError):
                    value = {'prior_digest': digest_json}
                if not isinstance(value, dict):
                    value = {'prior_digest': value}
                value.update({'verification': 'user_confirmed',
                              'reviewed_by': USER_CONFIRMED_PUBLIC_REVIEWER,
                              'reviewed_at': reviewed_at,
                              'review_note': USER_CONFIRMED_PUBLIC_REVIEW_NOTE})
                db.execute('UPDATE archive_digests SET digest_json=? WHERE rowid=?',
                           (json.dumps(value, ensure_ascii=False, sort_keys=True), rowid))
        _set_metadata(db, 'website_data_review', USER_CONFIRMED_PUBLIC_REVIEW_NOTE)
        _set_metadata(db, 'website_data_review_fingerprint', fingerprint)
        _set_metadata(db, 'description',
                      'Offline staging with user-confirmed weapon OCR and user-confirmed public website, forum, calculator, and database archive data.')
        _set_metadata(db, 'review_warning',
                      'The user-confirmed source review applies only to this pinned archive snapshot; dated source content can become stale. Screenshot identities and missing/cropped cells remain gated.')
        db.commit()
        return count
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_user_confirmed_drive_ocr_review(path, *, manifest_sha=USER_CONFIRMED_DRIVE_OCR_MANIFEST_SHA256,
                                          reviewed_at=None):
    """Promote only the user's exact 90-row raw OCR scope; derived stat candidates stay gated."""
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    try:
        try:metadata=dict(db.execute('SELECT key,value FROM metadata'))
        except sqlite3.Error:return 0
        if metadata.get('drive_ocr_manifest_sha256')!=manifest_sha or manifest_sha!=USER_CONFIRMED_DRIVE_OCR_MANIFEST_SHA256:
            return 0
        try:document_ids=json.loads(metadata.get('drive_ocr_document_ids','{}'))
        except (TypeError,json.JSONDecodeError):return 0
        expected_ids={name:info['id'] for name,info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.items()}
        if document_ids!=expected_ids:return 0
        docs=db.execute('SELECT id,source,category,trust,review_status,title,content,excerpt FROM archive_documents WHERE id IN (?,?,?)',
                        tuple(expected_ids.values())).fetchall()
        if len(docs)!=3:return 0
        docs_by_id={int(row['id']):row for row in docs}
        for name,info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.items():
            row=docs_by_id.get(info['id'])
            if (not row or row['source']!=info['source'] or row['category']!=info['category']
                    or row['review_status'] not in ('pending','verified')
                    or row['trust'] not in ('unverified','user_confirmed')):
                return 0
        if len(db.execute('SELECT 1 FROM archive_documents WHERE source IN (\'google_drive_archive\',\'user_chat_upload\')').fetchall())!=3:
            return 0

        candidate_count,candidate_fingerprint=_candidate_fingerprint(db)
        if candidate_count!=USER_CONFIRMED_CANDIDATE_COUNT or candidate_fingerprint!=USER_CONFIRMED_VALUES_SHA256:return 0
        public_count,public_structured,public_fingerprint=_public_archive_fingerprint(db)
        if (public_count!=USER_CONFIRMED_PUBLIC_DOC_COUNT or public_structured!=USER_CONFIRMED_PUBLIC_STRUCTURED_COUNT
                or public_fingerprint!=USER_CONFIRMED_PUBLIC_FINGERPRINT):return 0

        ocr_rows=db.execute('''SELECT rowid AS _rowid,archive_name,archive_id,source_sha256,ocr_text,review_status
            FROM archive_image_ocr WHERE archive_name IN (?,?,?)''',tuple(USER_CONFIRMED_DRIVE_OCR_DOCUMENTS)).fetchall()
        if len(ocr_rows)!=90:return 0
        ocr_counts={name:0 for name in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS}
        ocr_by_key={}
        for row in ocr_rows:
            name=row['archive_name']
            info=USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.get(name)
            key=(name,str(row['source_sha256'] or ''))
            if (not info or row['archive_id']!=info['archive_id'] or not key[1]
                    or key in ocr_by_key or row['review_status'] not in ('needs_review','verified')):
                return 0
            ocr_counts[name]+=1
            ocr_by_key[key]=row
        if any(ocr_counts[name]!=info['count'] for name,info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.items()):return 0

        collection_marks=','.join('?' for _ in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS)
        structured_rows=db.execute(f'''SELECT r.id,r.document_id,r.collection,r.row_key,r.data_json,d.source,d.category
            FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id
            WHERE r.collection IN ({collection_marks}) AND d.id IN (?,?,?)''',
            (*[info['collection'] for info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.values()],*expected_ids.values())).fetchall()
        if len(structured_rows)!=90:return 0
        structured_by_key={}
        for row in structured_rows:
            try:data=json.loads(row['data_json'])
            except (TypeError,json.JSONDecodeError):return 0
            name=data.get('archive_name')
            info=USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.get(name)
            key=(name,str(data.get('source_sha256') or ''))
            if (not info or int(row['document_id'])!=info['id'] or row['source']!=info['source']
                    or row['category']!=info['category'] or row['collection']!=info['collection']
                    or row['row_key']!=data.get('image_path') or key in structured_by_key
                    or data.get('review_status') not in ('needs_review','verified')):
                return 0
            source_row=ocr_by_key.get(key)
            if (not source_row or data.get('drive_file_id')!=info['archive_id']
                    or data.get('ocr_text')!=source_row['ocr_text']):return 0
            structured_by_key[key]=(row,data)
        if set(structured_by_key)!=set(ocr_by_key):return 0
        for name,info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.items():
            n=sum(1 for key in structured_by_key if key[0]==name)
            if n!=info['count']:return 0

        derived_rows=db.execute('''SELECT collection,data_json FROM archive_structured_rows
            WHERE collection IN (?,?) AND document_id IN (?,?,?)''',
            (*USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS.keys(),*expected_ids.values())).fetchall()
        derived_counts={name:0 for name in USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS}
        for row in derived_rows:
            try:data=json.loads(row['data_json'])
            except (TypeError,json.JSONDecodeError):return 0
            if row['collection'] not in derived_counts or data.get('candidate_status')!='needs_review':return 0
            derived_counts[row['collection']]+=1
        if derived_counts!=USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS or sum(derived_counts.values())!=523:return 0

        statuses={row['review_status'] for row in ocr_rows}
        prior_review=metadata.get('drive_ocr_user_review_manifest_sha256')
        docs_verified=all(docs_by_id[doc_id]['review_status']=='verified'
                          and docs_by_id[doc_id]['trust']=='user_confirmed' for doc_id in expected_ids.values())
        if statuses=={'verified'} and docs_verified and prior_review==manifest_sha:
            return 90
        if statuses!={'needs_review'} or any(row['review_status']!='pending' for row in docs):return 0
        queue=db.execute('SELECT status,count(*) AS n FROM archive_ingest_queue WHERE document_id IN (?,?,?) GROUP BY status',
                         tuple(expected_ids.values())).fetchall()
        if sum(row['n'] for row in queue)!=3 or any(row['status'] not in ('raw_ocr_pending_manual_review','verified_by_user_confirmation') for row in queue):
            return 0

        reviewed_at=reviewed_at or datetime.now(timezone.utc).isoformat(timespec='seconds')
        db.execute('BEGIN IMMEDIATE')
        for table in ('archive_documents','archive_image_ocr'):
            columns={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            for column in ('reviewed_by','reviewed_at','review_note'):
                if column not in columns:db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')

        for row in ocr_rows:
            db.execute('''UPDATE archive_image_ocr SET review_status='verified',reviewed_by=?,reviewed_at=?,review_note=?
                WHERE rowid=?''',(USER_CONFIRMED_DRIVE_OCR_REVIEWER,reviewed_at,USER_CONFIRMED_DRIVE_OCR_REVIEW_NOTE,row['_rowid']))
        for key,(row,data) in structured_by_key.items():
            data.update({'review_status':'verified','reviewed_by':USER_CONFIRMED_DRIVE_OCR_REVIEWER,
                         'reviewed_at':reviewed_at,'review_note':USER_CONFIRMED_DRIVE_OCR_REVIEW_NOTE,
                         'bot_visibility':'user_approved_raw_ocr'})
            db.execute('UPDATE archive_structured_rows SET data_json=? WHERE id=?',
                       (json.dumps(data,ensure_ascii=False,sort_keys=True),row['id']))
        for name,info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.items():
            body=(f"User-supplied screenshot archive containing {info['count']} images. The {info['count']} raw OCR "
                  f"records for this source were explicitly approved by the user for bot lookup. This approval "
                  "does not independently validate OCR accuracy, screenshot identity, game version/date, or author attribution. "
                  "Any derived pilot/mech stat-candidate rows from these images remain unverified and unavailable to the bot.")
            db.execute('''UPDATE archive_documents SET review_status='verified',trust='user_confirmed',
                reviewed_by=?,reviewed_at=?,review_note=?,content=?,excerpt=? WHERE id=?''',
                (USER_CONFIRMED_DRIVE_OCR_REVIEWER,reviewed_at,USER_CONFIRMED_DRIVE_OCR_REVIEW_NOTE,
                 body,body,info['id']))
        db.execute('''UPDATE archive_ingest_queue SET status='verified_by_user_confirmation'
            WHERE document_id IN (?,?,?) AND status='raw_ocr_pending_manual_review' ''',tuple(expected_ids.values()))
        _set_metadata(db,'drive_ocr_user_review_manifest_sha256',manifest_sha)
        _set_metadata(db,'drive_ocr_user_reviewed_at_utc',reviewed_at)
        _set_metadata(db,'drive_ocr_user_review',USER_CONFIRMED_DRIVE_OCR_REVIEW_NOTE)
        _set_metadata(db,'drive_ocr_promotion_policy',
                      'The user approved all 90 raw OCR records for bot lookup. The 523 legacy derived candidate rows in this SQLite snapshot retain stale needs_review statuses; a separate fresh visual rebuild of 3,520 pilot/mech stat cells was user-confirmed and promoted to the private Supabase overlay.')
        _set_metadata(db,'drive_ocr_ingest',
                      '84 Drive image OCR records + 6 chat-upload OCR records were user-approved for bot lookup. The 523 derived pilot/mech candidate rows in this legacy SQLite snapshot retain stale statuses; the separate fresh user-confirmed 3,520-cell visual overlay is retrieved from Supabase. Gladitor attribution remains user-attributed and independently unverified.')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('OCR promotion failed SQLite integrity check.')
        if db.execute("SELECT count(*) FROM archive_image_ocr WHERE archive_name IN (?,?,?) AND review_status='verified'",
                      tuple(USER_CONFIRMED_DRIVE_OCR_DOCUMENTS)).fetchone()[0]!=90:raise RuntimeError('OCR review status count mismatch.')
        check=db.execute('SELECT collection,data_json FROM archive_structured_rows WHERE collection IN (?,?) AND document_id IN (?,?,?)',
                         (*USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS.keys(),*expected_ids.values())).fetchall()
        check_counts={name:0 for name in USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS}
        for row in check:
            if row['collection'] not in check_counts or json.loads(row['data_json']).get('candidate_status')!='needs_review':
                raise RuntimeError('Derived candidate review gate changed unexpectedly.')
            check_counts[row['collection']]+=1
        if check_counts!=USER_CONFIRMED_DRIVE_CANDIDATE_COUNTS or sum(check_counts.values())!=523:
            raise RuntimeError('Derived candidate row count changed unexpectedly.')
        db.commit()
        return 90
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_snapshot():
    # Work on a private runtime copy so review metadata never mutates a supplied/source DB.
    override = os.getenv('GAME_DATABASE_PATH')
    source = Path(override) if override else ARCHIVE
    if not source.is_file():
        raise RuntimeError('Game catalog snapshot is unavailable')
    dest = Path(os.getenv('SNAPSHOT_PATH', '/tmp/mech_bot_game_snapshot.sqlite3'))
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + '.tmp')
    if override:
        shutil.copyfile(source, temp)
    else:
        with gzip.open(source, 'rb') as source_file, open(temp, 'wb') as target:
            shutil.copyfileobj(source_file, target)
    temp.replace(dest)
    reviewed = apply_user_confirmed_ocr_review(dest)
    if reviewed:
        log.info('Loaded %s user-reviewed weapon-stat cells.', reviewed)
    else:
        log.warning('OCR snapshot did not match the reviewed manifest; unsupported OCR values remain hidden.')
    public_reviewed = apply_user_confirmed_public_review(dest)
    if public_reviewed:
        log.info('Loaded %s user-confirmed website, forum, calculator, and database documents.', public_reviewed)
    else:
        log.warning('Public-source archive did not match its reviewed manifest; new or changed source data remains hidden.')
    drive_ocr_reviewed=apply_user_confirmed_drive_ocr_review(dest)
    if drive_ocr_reviewed:
        log.info('Loaded %s user-approved raw Drive/chat-upload OCR records; legacy derived candidates remain hidden in this SQLite copy (fresh user-confirmed stat cells load separately from Supabase).',drive_ocr_reviewed)
    else:
        log.warning('Raw Drive/chat-upload OCR did not match its pinned user-approved manifest; it remains hidden.')
    return dest


class Catalog:
    def __init__(self, path):
        self.db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.weapon_names = [r['name'] for r in self.db.execute('SELECT name FROM weapons')]
        self.mech_names = [r['name'] for r in self.db.execute('SELECT name FROM mechs')]
        self._record_cache = {}
        self._raw_ocr_records_cache=None
        self._verified_stat_overlay={'mech':{},'pilot':{}}
        self._verified_stat_overlay_run={}
        self.pilot_names=sorted({str(record.get('name')) for record in self._structured_records('pilot_list')
                                 if record.get('name')},key=str.casefold)
        self.mod_names=sorted({str(record.get('mod_label') or '').strip()
                               for record in self._structured_records('mod_list')
                               if str(record.get('mod_label') or '').strip()},key=str.casefold)
        self.mod_choices=sorted({f"{str(record.get('mod_label') or '').strip()} ({str(record.get('rarity') or '').title()})"
                                 for record in self._structured_records('mod_list')
                                 if record.get('mod_label') and record.get('rarity')},key=str.casefold)

    def load_verified_stat_overlay(self, rows):
        """Attach the complete, user-confirmed Supabase stat overlay without rewriting values."""
        rows=list(rows or [])
        if not rows:
            self._verified_stat_overlay={'mech':{},'pilot':{}}
            self._verified_stat_overlay_run={}
            return 0
        run_ids={str(row.get('run_id') or '') for row in rows}
        if len(run_ids)!=1 or '' in run_ids:
            log.error('Refusing screenshot stat overlay with missing or mixed run IDs.')
            return 0
        expected_values={row.get('expected_cell_count') for row in rows if row.get('expected_cell_count') is not None}
        if len(expected_values)>1:
            log.error('Refusing screenshot stat overlay with inconsistent run cell counts.')
            return 0
        if expected_values:
            try:expected=int(next(iter(expected_values)))
            except (TypeError,ValueError):
                log.error('Refusing screenshot stat overlay with an invalid expected cell count.')
                return 0
            if expected!=len(rows):
                log.error('Refusing incomplete screenshot stat overlay: expected %s cells, received %s.',expected,len(rows))
                return 0
        if any(row.get('run_user_confirmed') is not True or row.get('user_confirmed') is not True for row in rows):
            log.error('Refusing screenshot stat overlay without explicit run and cell confirmation.')
            return 0

        canonical={
            'mech':{str(name).strip().casefold():str(name) for name in self.mech_names},
            'pilot':{str(name).strip().casefold():str(name) for name in self.pilot_names},
        }
        grouped={'mech':{},'pilot':{}}
        seen=set()
        for row in rows:
            archive=str(row.get('archive_name') or '')
            kind={'MA SS.zip':'mech','PILOT.zip':'pilot'}.get(archive)
            if not kind:
                log.error('Refusing screenshot stat cell from an unexpected archive: %s.',archive)
                return 0
            entity=str(row.get('entity_title') or '').strip()
            canonical_name=canonical[kind].get(entity.casefold())
            label=str(row.get('stat_label') or '').strip()
            raw_value=row.get('value')
            try:rank=int(row.get('rank_column'))
            except (TypeError,ValueError):
                log.error('Refusing screenshot stat cell with an invalid rank column.')
                return 0
            if not canonical_name or not label or raw_value is None or not str(raw_value):
                log.error('Refusing screenshot stat cell without a catalog entity, label, or displayed value.')
                return 0
            if any(not row.get(field) for field in ('source_path','source_screenshot_sha256',
                                                     'source_archive_sha256','card_id','row_key','cell_key')):
                log.error('Refusing screenshot stat cell without complete source provenance.')
                return 0
            if not 1<=rank<=7 or row.get('row_type') not in ('direct','reconstructed'):
                log.error('Refusing screenshot stat cell with an invalid rank or row type.')
                return 0
            if row.get('value_unit') not in ('percentage','as_displayed'):
                log.error('Refusing screenshot stat cell with an unknown displayed unit.')
                return 0
            key=(kind,canonical_name.casefold(),label.casefold(),rank)
            if key in seen:
                log.error('Refusing screenshot stat overlay containing duplicate entity/stat/rank cells.')
                return 0
            seen.add(key)
            provenance={
                'run_id':str(row['run_id']),
                'archive_name':archive,
                'source_path':str(row.get('source_path') or ''),
                'source_screenshot_sha256':str(row.get('source_screenshot_sha256') or ''),
                'source_archive_sha256':str(row.get('source_archive_sha256') or ''),
                'card_id':str(row.get('card_id') or ''),
                'row_key':str(row.get('row_key') or ''),
                'cell_key':str(row.get('cell_key') or ''),
                'candidate_row_id':row.get('candidate_row_id'),
                'reconstruction_id':row.get('reconstruction_id'),
                'baseline_y':row.get('baseline_y'),
                'description':row.get('description'),
                'row_type':str(row.get('row_type') or ''),
                'cell_status':str(row.get('cell_status') or ''),
                'reviewed_on':str(row.get('cell_reviewed_on') or row.get('row_reviewed_on') or row.get('run_reviewed_on') or ''),
                'run_reviewed_on':str(row.get('run_reviewed_on') or ''),
                'user_confirmed':True,
                'user_confirmation_note':str(row.get('user_confirmation_note') or ''),
                'review_notes':str(row.get('review_notes') or ''),
                'source_fragment_candidate_row_ids':row.get('source_fragment_candidate_row_ids'),
            }
            cell={'stat_name':label,'stat_label':label,'rank':rank,'rank_column':rank,'level':None,
                  'value':str(raw_value),'value_unit':str(row['value_unit']),
                  'source_type':'verified_screenshot_overlay','provenance':provenance}
            grouped[kind].setdefault(canonical_name.casefold(),[]).append(cell)
        first=rows[0]
        try:
            direct_expected=int(first.get('direct_verified_cell_count'))
            reconstructed_expected=int(first.get('reconstructed_cell_count'))
        except (TypeError,ValueError):
            direct_expected=reconstructed_expected=None
        if expected_values and (direct_expected is None or reconstructed_expected is None):
            log.error('Refusing screenshot stat overlay without direct/reconstructed run counts.')
            return 0
        if direct_expected is not None and reconstructed_expected is not None:
            direct_actual=sum(1 for row in rows if row.get('row_type')=='direct')
            reconstructed_actual=sum(1 for row in rows if row.get('row_type')=='reconstructed')
            if (direct_actual!=direct_expected or reconstructed_actual!=reconstructed_expected
                    or direct_actual+reconstructed_actual!=len(rows)):
                log.error('Refusing screenshot stat overlay with mismatched direct/reconstructed cell counts.')
                return 0
        for kind in grouped:
            for cells in grouped[kind].values():
                cells.sort(key=lambda item:(item['stat_name'].casefold(),item['rank']))
        self._verified_stat_overlay=grouped
        self._verified_stat_overlay_run={
            'run_id':str(first.get('run_id') or ''),
            'reviewed_on':str(first.get('run_reviewed_on') or ''),
            'user_confirmed':True,
            'user_confirmation_note':first.get('user_confirmation_note'),
            'candidate_rows':first.get('candidate_rows'),
            'screenshot_count':first.get('screenshot_count'),
            'cell_count':len(rows),
            'direct_verified_cell_count':first.get('direct_verified_cell_count'),
            'reconstructed_cell_count':first.get('reconstructed_cell_count'),
            'combined_overlay_sha256':first.get('combined_overlay_sha256'),
            'manual_manifest_sha256':first.get('manual_manifest_sha256'),
            'raw_ocr_manifest_sha256':first.get('raw_ocr_manifest_sha256'),
            'correction_log_sha256':first.get('correction_log_sha256'),
            'candidate_jsonl_sha256':first.get('candidate_jsonl_sha256'),
            'source_archive_sha256':first.get('run_source_archive_sha256'),
        }
        return len(rows)

    def screenshot_stats(self, kind, name, rank=None, level=None):
        """Return exact displayed screenshot values; these columns record rank, not level."""
        if kind not in self._verified_stat_overlay or level is not None:
            return []
        needle=str(name or '').strip().casefold()
        if not needle:return []
        records=self._verified_stat_overlay[kind].get(needle,[])
        if rank is not None:
            try:rank=int(rank)
            except (TypeError,ValueError):return []
            records=[record for record in records if record['rank']==rank]
        return [dict(record) for record in records]

    def close(self):
        self.db.close()

    def search(self, name, kind='weapon', limit=10):
        table = 'weapons' if kind == 'weapon' else 'mechs'
        needle = name.strip().casefold()
        if not needle:
            return []
        rows = self.db.execute(f'SELECT * FROM {table} ORDER BY name').fetchall()
        exact = [r for r in rows if r['name'].casefold() == needle]
        found = exact or [r for r in rows if needle in r['name'].casefold()]
        return [dict(r) for r in found[:limit]]

    def _structured_records(self, collection):
        if collection in self._record_cache:
            return self._record_cache[collection]
        marks = ','.join('?' for _ in USER_CONFIRMED_PUBLIC_SOURCES)
        try:
            rows = self.db.execute(f'''SELECT r.row_key,r.data_json,d.source,d.title,d.url,
                d.category,d.author_label,d.published_text,d.last_checked_utc
                FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id
                WHERE r.collection=? AND d.source IN ({marks}) AND d.review_status='verified'
                ORDER BY r.id''', (collection, *USER_CONFIRMED_PUBLIC_SOURCES)).fetchall()
        except sqlite3.Error:
            self._record_cache[collection] = []
            return []
        records = []
        for row in rows:
            try:
                value = json.loads(row['data_json'])
            except (TypeError, json.JSONDecodeError):
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, dict):
                    continue
                record = dict(item)
                record['_archive_source'] = row['source']
                record['_archive_title'] = row['title']
                record['_archive_url'] = row['url']
                record['_archive_category'] = row['category']
                record['_archive_author'] = row['author_label']
                record['_archive_date'] = row['published_text'] or row['last_checked_utc']
                records.append(record)
        self._record_cache[collection] = records
        return records

    def _user_confirmed_raw_ocr_records(self):
        """Read only the pinned, explicitly approved raw-OCR collections, never stat candidates."""
        if hasattr(self,'_raw_ocr_records_cache') and self._raw_ocr_records_cache is not None:
            return self._raw_ocr_records_cache
        sources=tuple(dict.fromkeys(info['source'] for info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.values()))
        collections=tuple(dict.fromkeys(info['collection'] for info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.values()))
        doc_ids=tuple(info['id'] for info in USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.values())
        try:
            rows=self.db.execute('''SELECT r.row_key,r.data_json,d.source,d.title,d.url,d.category,
                d.author_label,d.published_text,d.last_checked_utc,d.id AS document_id
                FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id
                WHERE r.collection IN (?,?) AND d.source IN (?,?) AND d.review_status='verified'
                  AND d.id IN (?,?,?) ORDER BY r.id''',(*collections,*sources,*doc_ids)).fetchall()
        except sqlite3.Error:
            self._raw_ocr_records_cache=[]
            return []
        records=[]
        if len(rows)!=90:
            self._raw_ocr_records_cache=[]
            return []
        for row in rows:
            try:data=json.loads(row['data_json'])
            except (TypeError,json.JSONDecodeError):continue
            info=USER_CONFIRMED_DRIVE_OCR_DOCUMENTS.get(data.get('archive_name'))
            if (not info or data.get('review_status')!='verified'
                    or data.get('bot_visibility')!='user_approved_raw_ocr'
                    or int(row['document_id'])!=info['id'] or row['source']!=info['source']
                    or row['category']!=info['category'] or row['row_key']!=data.get('image_path')):
                continue
            record=dict(data)
            record['_archive_source']=row['source']
            record['_archive_title']=row['title']
            record['_archive_url']=row['url']
            record['_archive_category']=row['category']
            record['_archive_author']=row['author_label']
            record['_archive_date']=row['published_text'] or row['last_checked_utc']
            records.append(record)
        if len(records)!=90:records=[]
        self._raw_ocr_records_cache=records
        return records

    @staticmethod
    def _same_name(a, b):
        return str(a or '').strip().casefold() == str(b or '').strip().casefold()

    def _structured_match(self, collection, field, value):
        return next((record for record in self._structured_records(collection)
                     if self._same_name(record.get(field), value)), None)

    def _listing_matches_weapon_variant(self, listing):
        if not listing or str(listing.get('type','')).casefold()!='weapon':return True
        matches=self.search(str(listing.get('list') or ''),'weapon')
        if len(matches)!=1 or not self._same_name(matches[0]['name'],listing.get('list')):return True
        try:
            return (int(listing.get('energy'))==int(matches[0].get('variant_number'))
                    and int(matches[0].get('calculator_energy_field'))==int(matches[0].get('variant_number')))
        except (TypeError,ValueError):
            return False

    def weapon(self, name, rank=None, level=None):
        results = self.search(name, 'weapon')
        if len(results) != 1:
            return {'matches': results, 'verified_stats': [], 'candidate_count': 0,
                    'review': None, 'listing': None}
        item = results[0]
        # Values are exposed only after the pinned user confirmation and accepted identity gate.
        sql = '''SELECT sc.stat_name,sc.rank,sc.level,sc.star_color,sc.candidate_value AS value,
            s.source_path,s.sha256 AS source_sha256,s.identity_status,
            sc.reviewed_by,sc.reviewed_at,sc.review_note
            FROM weapon_stat_candidates sc JOIN screenshots s ON s.id=sc.screenshot_id
            WHERE s.weapon_id=? AND s.identity_status IN ('exact_catalog_match','user_confirmed_weapon_source_mismatch')
            AND sc.status='verified' '''
        params = [item['id']]
        if rank is not None:
            sql += ' AND sc.rank=?'
            params.append(rank)
        if level is not None:
            sql += ' AND sc.level=?'
            params.append(level)
        sql += ' ORDER BY sc.rank,sc.level,sc.stat_name'
        stats = [dict(r) for r in self.db.execute(sql, params)]
        for stat in stats:
            stat['source_type']='user_confirmed_weapon_screenshot'
            stat['provenance']={key:stat.get(key) for key in
                ('source_path','source_sha256','identity_status','reviewed_by','reviewed_at','review_note')}
        n = self.db.execute("SELECT count(*) FROM weapon_stat_candidates x JOIN screenshots s ON s.id=x.screenshot_id WHERE s.weapon_id=? AND s.identity_status IN ('exact_catalog_match','user_confirmed_weapon_source_mismatch') AND x.status='unverified'", (item['id'],)).fetchone()[0]
        review = self._structured_match('database_weapons', 'name', item['family'])
        listing = self._structured_match('list', 'list', item['name'])
        if not self._listing_matches_weapon_variant(listing):listing=None
        return {'matches': results, 'verified_stats': stats, 'candidate_count': n,
                'review': review, 'listing': listing}

    def mech(self, name):
        return self.search(name, 'mech')

    def mech_profile(self, name, rank=None):
        matches = self.search(name, 'mech')
        if len(matches) != 1:
            return {'matches': matches, 'review': None, 'listing': None,'verified_stats':[]}
        canonical_name=matches[0]['name']
        return {'matches': matches,
                'review': self._structured_match('database_mechs', 'name', canonical_name),
                'listing': self._structured_match('list', 'list', canonical_name),
                'verified_stats':self.screenshot_stats('mech',canonical_name,rank=rank)}

    def pilot(self, name, limit=10):
        """Search approved public pilot listings; never load derived OCR stat candidates."""
        needle=str(name or '').strip().casefold()
        if not needle:return []
        records=[record for record in self._structured_records('pilot_list') if record.get('name')]
        exact=[record for record in records if self._same_name(record.get('name'),needle)]
        found=exact or [record for record in records if needle in str(record.get('name') or '').casefold()]
        try:limit=max(0,min(int(limit),25))
        except (TypeError,ValueError):limit=10
        return [{key:value for key,value in record.items() if not key.startswith('_archive_')}
                for record in found[:limit]]

    def pilot_profile(self, name, rank=None):
        """Return the verified pilot listing plus the separately user-confirmed stat overlay."""
        matches=self.pilot(name)
        listing=matches[0] if len(matches)==1 else None
        return {'matches':matches,'listing':listing,
                'verified_stats':self.screenshot_stats('pilot',listing.get('name'),rank=rank) if listing else []}

    @staticmethod
    def _mod_key(value):
        return re.sub(r'[^a-z0-9]+',' ',str(value or '').casefold()).strip()

    def mod_profile(self, name, rarity=None):
        """Return verified mod purchase and per-level resource data from the archived calculator."""
        query=str(name or '').strip()
        rarity_query=str(rarity or '').strip().casefold()
        suffix=re.search(r'\s*\((epic|legendary)\)\s*$',query,re.I)
        if suffix:
            rarity_query=rarity_query or suffix[1].casefold()
            query=query[:suffix.start()].strip()
        prefix=re.match(r'^(epic|legendary)\s+(.+)$',query,re.I)
        if prefix:
            rarity_query=rarity_query or prefix[1].casefold()
            query=prefix[2].strip()
        needle=self._mod_key(query)
        if not needle:return {'matches':[]}
        records=[]
        for record in self._structured_records('mod_list'):
            label=str(record.get('mod_label') or '').strip()
            mod_name=str(record.get('mod_name') or '').strip()
            record_rarity=str(record.get('rarity') or '').casefold()
            if rarity_query and rarity_query!=record_rarity:continue
            if needle not in (self._mod_key(label),self._mod_key(mod_name.replace('_',' '))):continue
            key=f'{record_rarity}_{mod_name}'.casefold()
            level_costs={}
            for row in self._structured_records('mod_cost'):
                try:level=int(row.get('level'))
                except (TypeError,ValueError):continue
                amounts=row.get(key)
                if isinstance(amounts,dict):
                    level_costs[level]={resource:int(amounts.get(resource) or 0)
                                        for resource in ('basic_mod_parts','elite_mod_parts','power')}
            data={k:v for k,v in record.items() if not k.startswith('_archive_')}
            data['mod_label']=label
            data['level_costs']=level_costs
            data['source']={k:record.get('_archive_'+k) for k in ('source','title','url','date')}
            records.append(data)
        records.sort(key=lambda row:(row.get('rarity',''),row.get('mod_label','').casefold()))
        return {'matches':records}

    def mod_upgrade_cost(self, name, rarity, from_level, to_level):
        """Sum only the archived level entries for an exact mod rarity/level transition."""
        try:from_level,to_level=int(from_level),int(to_level)
        except (TypeError,ValueError):return None
        if not (1<=from_level<=6 and 1<=to_level<=6 and to_level>=from_level):return None
        matches=self.mod_profile(name,rarity).get('matches',[])
        if len(matches)!=1:return None
        item=matches[0]
        totals={'basic_mod_parts':0,'elite_mod_parts':0,'power':0}
        for level in range(from_level+1,to_level+1):
            amount=item.get('level_costs',{}).get(level)
            if not isinstance(amount,dict):return None
            for resource in totals:
                try:totals[resource]+=int(amount.get(resource) or 0)
                except (TypeError,ValueError):return None
        return {'item':item['mod_label'],'rarity':item['rarity'],'from_level':from_level,
                'to_level':to_level,'cost':totals,'source':item.get('source')}

    def upgrade_cost(self, name, from_rank, to_rank, from_level=None, to_level=None):
        """Return verified calculator totals; a rank-only step means max level to next rank."""
        try:
            from_rank, to_rank = int(from_rank), int(to_rank)
        except (TypeError, ValueError):
            return None
        if not (1 <= from_rank <= 6 and 1 <= to_rank <= 6 and to_rank >= from_rank):
            return None
        weapon_matches = self.search(name, 'weapon')
        mech_matches = self.search(name, 'mech')
        pilot_matches = self.pilot(name)
        if len(weapon_matches) == 1 and self._same_name(weapon_matches[0]['name'], name):
            item = weapon_matches[0]
            listing = self._structured_match('list', 'list', item['name'])
            if not listing or str(listing.get('type', '')).casefold() != 'weapon':
                return None
            try:
                listed_energy = int(listing.get('energy'))
                variant_energy = int(item.get('variant_number'))
                calculator_energy = int(item.get('calculator_energy_field'))
            except (TypeError, ValueError):
                return None
            # Do not silently calculate a different variant when a source's energy fields disagree.
            if listed_energy != variant_energy or calculator_energy != variant_energy:
                return None
            rarity = str(listing.get('rarity') or '').casefold()
            cost_key = f'{rarity}_weapon_{listed_energy}'
            item_type = 'weapon'
            item_name=item['name']
        elif len(mech_matches) == 1 and self._same_name(mech_matches[0]['name'], name):
            listing = self._structured_match('list', 'list', mech_matches[0]['name'])
            if not listing or str(listing.get('type', '')).casefold() != 'mech':
                return None
            rarity = str(listing.get('rarity') or '').casefold()
            cost_key = f'{rarity}_mech'
            item_type = 'mech'
            item_name=mech_matches[0]['name']
        elif len(pilot_matches)==1 and self._same_name(pilot_matches[0].get('name'),name):
            item=pilot_matches[0]
            rarity=str(item.get('rarity') or '').casefold()
            cost_key=rarity
            item_type='pilot'
            item_name=str(item['name'])
        else:
            return None
        if not rarity or rarity == 'none':
            return None
        if from_level is None and to_level is None:
            # Rank-only transitions mean L7 to L1 at the next star; a same-rank
            # transition must specify both levels so it cannot be guessed.
            if to_rank == from_rank:
                return None
            from_level, to_level = 7, 1
        elif from_level is None or to_level is None:
            return None
        try:
            from_level, to_level = int(from_level), int(to_level)
        except (TypeError, ValueError):
            return None
        if not (1 <= from_level <= 7 and 1 <= to_level <= 7):
            return None
        if to_rank == from_rank:
            if to_level < from_level:
                return None
            if to_level == from_level:
                zero_cost={'acoins':0,'marks':0,'xp':0} if item_type=='pilot' else {'credits':0,'acoins':0,'blueprints':0}
                return {'item':item_name,'item_type':item_type,'rarity':rarity,
                        'blueprint_acoins_per_blueprint':BLUEPRINT_ACOINS_PER_RARITY.get(rarity),
                        'from_rank':from_rank,'from_level':from_level,
                        'to_rank':to_rank,'to_level':to_level,'cost':zero_cost,
                        'assumed_rank_transition':False,'no_op':True}
        collection='pilot_upgrade_costs' if item_type=='pilot' else 'mech_upgrade_costs'
        costs = {(int(x['star']), int(x['level'])): x
                 for x in self._structured_records(collection)
                 if 'star' in x and 'level' in x}
        totals = {'acoins':0,'marks':0,'xp':0} if item_type=='pilot' else {'credits':0,'acoins':0,'blueprints':0}
        current_star = from_rank
        current_level_index = from_level - 1
        target_star = to_rank
        target_level_index = to_level - 1
        # Match the calculator's rollover convention: target star L1 is reached by step (old star, L7).
        if target_level_index == 0 and target_star > current_star:
            target_level_index = 7
            target_star -= 1
        steps = 0
        while current_star < target_star or (current_star == target_star and current_level_index < target_level_index):
            current_level_index += 1
            if current_level_index > 7:
                current_star += 1
                current_level_index = 1
            record = costs.get((current_star, current_level_index))
            amount = record.get(cost_key) if record else None
            if not isinstance(amount, dict):
                return None
            try:
                for key in totals:
                    value = amount.get(key, 0)
                    totals[key] += int(value or 0)
            except (TypeError, ValueError):
                return None
            steps += 1
            if steps > 42 or current_star > 6:
                return None
        if steps == 0:
            return None
        return {'item':item_name,'item_type':item_type,'rarity':rarity,
                'blueprint_acoins_per_blueprint':BLUEPRINT_ACOINS_PER_RARITY.get(rarity),
                'from_rank':from_rank,'from_level':from_level,
                'to_rank':to_rank,'to_level':to_level,'cost':totals,
                'assumed_rank_transition':from_rank<to_rank and from_level==7 and to_level==1,
                'no_op':False}

    def search_confirmed_archive(self, question, entities=(), limit=5):
        """Search confirmed public data plus only the user-approved raw OCR allowlist."""
        tokens={x.casefold() for x in re.findall(r'[a-zA-Z][a-zA-Z0-9-]{2,}|\b\d{2,}\b',str(question))}
        stop={'what','when','where','which','who','with','from','into','about','this','that','does','doesnt',
              'have','has','how','much','many','tell','show','give','please','can','could','would','should',
              'need','want','rank','level','star','stars','upgrade','price','cost','weapon','weapons','mech','mechs','game','arena'}
        tokens-=stop
        entity_names=[str(x).casefold() for x in entities if x]
        if not tokens and not entity_names:return []
        hits=[]
        marks=','.join('?' for _ in USER_CONFIRMED_PUBLIC_SOURCES)
        try:
            collections=[row[0] for row in self.db.execute(f'''SELECT DISTINCT r.collection
                FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id
                WHERE d.source IN ({marks}) AND d.review_status='verified' ORDER BY r.collection''',
                USER_CONFIRMED_PUBLIC_SOURCES)]
        except sqlite3.Error:collections=[]
        for collection in collections:
            for record in self._structured_records(collection):
                if collection=='list' and not self._listing_matches_weapon_variant(record):continue
                data={k:v for k,v in record.items() if not k.startswith('_archive_')}
                text=json.dumps(data,ensure_ascii=False).casefold()
                score=sum(1 for token in tokens if token in text)
                score+=10*sum(1 for entity in entity_names if entity in text)
                if score:
                    hits.append((score,{'kind':'structured_data','collection':collection,
                        'title':record.get('_archive_title'),'source':record.get('_archive_source'),
                        'author':record.get('_archive_author'),'date':record.get('_archive_date'),
                        'url':record.get('_archive_url'),'data':data}))
        try:
            docs=self.db.execute(f'''SELECT source,category,title,author_label,published_text,
                excerpt,content,url,first_seen_utc,last_checked_utc FROM archive_documents
                WHERE review_status='verified' AND source IN ({marks})
                AND COALESCE(category,'') NOT IN ('script','client_source','sitemap') ORDER BY id''',
                USER_CONFIRMED_PUBLIC_SOURCES).fetchall()
        except sqlite3.Error:docs=[]
        event_query=bool(tokens & {'update','updated','news','release','announced','announcement','happened','changed','fix','patch'})
        for doc in docs:
            title=str(doc['title'] or '')
            content=str(doc['content'] or '')
            haystack=' '.join((title,str(doc['excerpt'] or ''),content)).casefold()
            score=sum(1 for token in tokens if token in haystack)
            title_fold=title.casefold()
            for entity in entity_names:
                if entity in haystack:score+=10
                if entity in title_fold:score+=15
            if event_query and doc['source']=='plarium_forum' and doc['category'] in ('news','announcements','release_notes'):score+=5
            author=str(doc['author_label'] or '')
            if score and not author.casefold().startswith('deleted '):
                excerpt=content or str(doc['excerpt'] or '')
                phrase=next((e for e in entity_names if e in excerpt.casefold()),None)
                if phrase:
                    start=max(0,excerpt.casefold().find(phrase)-180)
                    excerpt=excerpt[start:start+750]
                else:excerpt=excerpt[:750]
                date=str(doc['published_text'] or '')
                if not re.search(r'\b(?:19|20)\d{2}\b',date):date=str(doc['first_seen_utc'] or doc['last_checked_utc'] or '')
                hits.append((score,{'kind':'public_document','title':title,'source':doc['source'],
                    'category':doc['category'],'author':doc['author_label'],'date':date,'url':doc['url'],'excerpt':excerpt}))
        # Public-image OCR and transcriptions remain unusable until explicitly reviewed.
        try:
            ocr_rows=self.db.execute("SELECT source_url,ocr_text,review_status,processed_at_utc FROM archive_image_ocr WHERE review_status='verified'").fetchall()
        except sqlite3.Error:ocr_rows=[]
        for row in ocr_rows:
            host=(urlparse(str(row['source_url'] or '')).hostname or '').casefold()
            if host not in PUBLIC_ASSET_HOSTS:continue
            excerpt=str(row['ocr_text'] or '')[:750]
            text=excerpt.casefold()
            score=sum(1 for token in tokens if token in text)+10*sum(1 for entity in entity_names if entity in text)
            if score:hits.append((score,{'kind':'public_document','title':'Reviewed public-image text',
                'source':'public_image_ocr','author':'','date':row['processed_at_utc'],'url':row['source_url'],'excerpt':excerpt}))
        try:
            transcriptions=self.db.execute("SELECT source_url,kind,entity,data_json,notes FROM archive_image_transcription_staging WHERE review_status='verified'").fetchall()
        except sqlite3.Error:transcriptions=[]
        for row in transcriptions:
            host=(urlparse(str(row['source_url'] or '')).hostname or '').casefold()
            if host not in PUBLIC_ASSET_HOSTS:continue
            excerpt=' '.join(str(row[key] or '') for key in ('kind','entity','data_json','notes'))[:750]
            text=excerpt.casefold()
            score=sum(1 for token in tokens if token in text)+10*sum(1 for entity in entity_names if entity in text)
            if score:hits.append((score,{'kind':'public_document','title':'Reviewed public-image transcription',
                'source':'public_image_transcription','author':'','date':'','url':row['source_url'],'excerpt':excerpt}))
        # The user-approved OCR collections are narrowly allowlisted; derived pilot/mech candidate
        # collections are intentionally not loaded here even though they share the same documents.
        for record in self._user_confirmed_raw_ocr_records():
            # Pilot values are exposed only from the reviewed pilot_list collection.
            if record.get('archive_name')=='PILOT.zip':continue
            excerpt=str(record.get('ocr_text') or '')[:750]
            if record.get('_archive_source')=='user_chat_upload':
                excerpt=_safe_user_chat_ocr_excerpt(excerpt)
                if not excerpt:continue
            text=excerpt.casefold()
            score=sum(1 for token in tokens if token in text)
            score+=20*sum(1 for entity in entity_names if entity in text)
            if score:
                hits.append((score,{'kind':'user_confirmed_raw_ocr','title':record.get('_archive_title'),
                    'source':record.get('_archive_source'),'category':record.get('_archive_category'),
                    'author':record.get('_archive_author'),'date':record.get('_archive_date'),
                    'url':record.get('_archive_url'),'excerpt':excerpt,
                    'archive_name':record.get('archive_name'),'image_path':record.get('image_path'),
                    'source_sha256':record.get('source_sha256')}))
        hits.sort(key=lambda item:item[0],reverse=True)
        return [hit for _,hit in hits[:max(0,min(int(limit),8))]]
