"""Read-only runtime snapshot with an audited, dataset-pinned OCR review."""
import gzip
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / 'data' / 'mech_bot_staging.sqlite3.gz'
log=logging.getLogger('mechbot.catalog')

# Explicit user confirmation applies only to this exact reviewed screenshot dataset.
USER_CONFIRMED_ARCHIVE_SHA256='eda05a31d893ec11ce13f221833b68b19108aa322affb9c4a19ecf6064c30ae8'
USER_CONFIRMED_VALUES_SHA256='d41cc54efd214cc295a91c3f7fe179156f4e056c73850ebbc872ac83b2161edb'
USER_CONFIRMED_CANDIDATE_COUNT=22652
USER_CONFIRMED_REVIEWER='requesting user (chat confirmation)'
USER_CONFIRMED_REVIEW_DATE='2026-11-10'
USER_CONFIRMED_REVIEW_NOTE=(
    'The user stated in chat on 2026-11-10 that they personally verified all OCR candidate values '
    'from the 251-screenshot weapon dataset. This confirms detected cell values, not unresolved '
    'screenshot-to-weapon identities or missing/cropped cells.'
)


def _candidate_fingerprint(db):
    rows=db.execute('''SELECT s.sha256,s.weapon_id,w.name,sc.stat_name,sc.raw_label,sc.source_row_number,
        sc.rank_column,sc.rank,sc.level,sc.star_color,sc.candidate_value
        FROM weapon_stat_candidates sc JOIN screenshots s ON s.id=sc.screenshot_id
        LEFT JOIN weapons w ON w.id=s.weapon_id ORDER BY sc.id''').fetchall()
    digest=hashlib.sha256()
    for row in rows:
        digest.update('\x1f'.join('' if value is None else str(value) for value in row).encode('utf-8'))
        digest.update(b'\n')
    return len(rows),digest.hexdigest()


def apply_user_confirmed_ocr_review(path,*,archive_sha=USER_CONFIRMED_ARCHIVE_SHA256,
                                    values_sha=USER_CONFIRMED_VALUES_SHA256,
                                    candidate_count=USER_CONFIRMED_CANDIDATE_COUNT,
                                    reviewed_at=None):
    """Promote only the exact OCR snapshot the user explicitly confirmed; leave source DB untouched."""
    db=sqlite3.connect(path)
    try:
        archive=db.execute("SELECT value FROM metadata WHERE key='screenshot_zip_sha256'").fetchone()
        if not archive or archive[0]!=archive_sha:return 0
        count,fingerprint=_candidate_fingerprint(db)
        if count!=candidate_count or fingerprint!=values_sha:return 0
        statuses={row[0] for row in db.execute('SELECT DISTINCT status FROM weapon_stat_candidates')}
        if statuses=={'verified'}:return count
        if statuses!={'unverified'}:return 0
        reviewed_at=reviewed_at or USER_CONFIRMED_REVIEW_DATE
        note=USER_CONFIRMED_REVIEW_NOTE
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE weapon_stat_candidates SET status='verified',reviewed_by=?,reviewed_at=?,review_note=? WHERE status='unverified'",
                   (USER_CONFIRMED_REVIEWER,reviewed_at,note))
        db.execute("INSERT INTO review_events(candidate_id,action,reviewer,reviewed_at,note) SELECT id,'verified',?,?,? FROM weapon_stat_candidates",
                   (USER_CONFIRMED_REVIEWER,reviewed_at,note))
        db.execute("INSERT INTO metadata(key,value) VALUES('description',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ('Offline staging: all detected weapon-stat OCR cells in the user-reviewed 251-screenshot dataset were confirmed on 2026-11-10. Other archive content remains staged.',))
        db.execute("INSERT INTO metadata(key,value) VALUES('review_warning',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ('OCR cell values are user-confirmed. Screenshot-to-weapon identity_review rows and missing/cropped cells remain unresolved; website-derived archive data remains unverified.',))
        db.commit()
        return count
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_snapshot():
    # Work on a private runtime copy so review metadata never mutates a supplied/source DB.
    override=os.getenv('GAME_DATABASE_PATH')
    source=Path(override) if override else ARCHIVE
    if not source.is_file():
        raise RuntimeError('Game catalog snapshot is unavailable')
    dest=Path(os.getenv('SNAPSHOT_PATH','/tmp/mech_bot_game_snapshot.sqlite3'))
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_suffix(dest.suffix+'.tmp')
    if override:
        shutil.copyfile(source,temp)
    else:
        with gzip.open(source,'rb') as source_file,open(temp,'wb') as target:
            shutil.copyfileobj(source_file,target)
    temp.replace(dest)
    reviewed=apply_user_confirmed_ocr_review(dest)
    if reviewed:
        log.info('Loaded %s user-reviewed weapon-stat cells.',reviewed)
    else:
        log.warning('Catalog data did not match the reviewed OCR snapshot; unsupported OCR values remain hidden.')
    return dest


class Catalog:
    def __init__(self, path):
        self.db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.weapon_names = [r['name'] for r in self.db.execute('SELECT name FROM weapons')]
        self.mech_names = [r['name'] for r in self.db.execute('SELECT name FROM mechs')]

    def close(self):
        self.db.close()

    def search(self, name, kind='weapon', limit=10):
        table = 'weapons' if kind == 'weapon' else 'mechs'
        needle = name.strip().casefold()
        if not needle:
            return []
        # Local lookup only; no SQL string interpolation of input.
        rows = self.db.execute(f'SELECT * FROM {table} ORDER BY name').fetchall()
        exact = [r for r in rows if r['name'].casefold() == needle]
        found = exact or [r for r in rows if needle in r['name'].casefold()]
        return [dict(r) for r in found[:limit]]

    def weapon(self, name, rank=None, level=None):
        results = self.search(name, 'weapon')
        if len(results) != 1:
            return {'matches': results, 'verified_stats': [], 'candidate_count': 0}
        item = results[0]
        # Numeric answers come only from reviewed cells tied to a confirmed screenshot identity.
        sql = '''SELECT sc.stat_name,sc.rank,sc.level,sc.star_color,sc.candidate_value AS value,
            s.source_path,s.sha256 AS source_sha256,sc.reviewed_by,sc.reviewed_at
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
        n = self.db.execute("SELECT count(*) FROM weapon_stat_candidates x JOIN screenshots s ON s.id=x.screenshot_id WHERE s.weapon_id=? AND s.identity_status IN ('exact_catalog_match','user_confirmed_weapon_source_mismatch') AND x.status='unverified'", (item['id'],)).fetchone()[0]
        return {'matches': results, 'verified_stats': stats, 'candidate_count': n}

    def mech(self, name):
        return self.search(name, 'mech')
