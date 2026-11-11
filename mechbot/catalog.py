"""Read-only runtime catalog with pinned user reviews and searchable public data."""
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
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
    return dest


class Catalog:
    def __init__(self, path):
        self.db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.weapon_names = [r['name'] for r in self.db.execute('SELECT name FROM weapons')]
        self.mech_names = [r['name'] for r in self.db.execute('SELECT name FROM mechs')]
        self._record_cache = {}

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
        # Numeric OCR values require a user-verified cell and an accepted screenshot identity.
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
        review = self._structured_match('database_weapons', 'name', item['family'])
        listing = self._structured_match('list', 'list', item['name'])
        if not self._listing_matches_weapon_variant(listing):listing=None
        return {'matches': results, 'verified_stats': stats, 'candidate_count': n,
                'review': review, 'listing': listing}

    def mech(self, name):
        return self.search(name, 'mech')

    def mech_profile(self, name):
        matches = self.search(name, 'mech')
        if len(matches) != 1:
            return {'matches': matches, 'review': None, 'listing': None}
        return {'matches': matches,
                'review': self._structured_match('database_mechs', 'name', matches[0]['name']),
                'listing': self._structured_match('list', 'list', matches[0]['name'])}

    def upgrade_cost(self, name, from_rank, to_rank, from_level=None, to_level=None):
        """Return verified calculator totals; a rank-only step means max level to next rank."""
        try:
            from_rank, to_rank = int(from_rank), int(to_rank)
        except (TypeError, ValueError):
            return None
        if not (1 <= from_rank <= 6 and 1 <= to_rank <= 6 and to_rank > from_rank):
            return None
        weapon_matches = self.search(name, 'weapon')
        mech_matches = self.search(name, 'mech')
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
        elif len(mech_matches) == 1 and self._same_name(mech_matches[0]['name'], name):
            listing = self._structured_match('list', 'list', mech_matches[0]['name'])
            if not listing or str(listing.get('type', '')).casefold() != 'mech':
                return None
            rarity = str(listing.get('rarity') or '').casefold()
            cost_key = f'{rarity}_mech'
            item_type = 'mech'
        else:
            return None
        if not rarity or rarity == 'none':
            return None
        if from_level is None and to_level is None:
            # Calculator rank transitions are the upgrade from L7 to L1 at the next star.
            from_level, to_level = 7, 1
        elif from_level is None or to_level is None:
            return None
        try:
            from_level, to_level = int(from_level), int(to_level)
        except (TypeError, ValueError):
            return None
        if not (1 <= from_level <= 7 and 1 <= to_level <= 7):
            return None
        costs = {(int(x['star']), int(x['level'])): x
                 for x in self._structured_records('mech_upgrade_costs')
                 if 'star' in x and 'level' in x}
        totals = {'credits': 0, 'acoins': 0, 'blueprints': 0}
        current_star = from_rank
        current_level_index = from_level - 1
        target_star = to_rank
        target_level_index = to_level - 1
        # Match the calculator's rollover convention: target star L1 is reached by step (old star, L7).
        if target_level_index == 0:
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
        return {'item': item['name'] if item_type == 'weapon' else mech_matches[0]['name'],
                'item_type': item_type, 'from_rank': from_rank, 'from_level': from_level,
                'to_rank': to_rank, 'to_level': to_level, 'cost': totals,
                'assumed_rank_transition': from_level == 7 and to_level == 1}

    def search_confirmed_archive(self, question, entities=(), limit=5):
        """Search only user-confirmed structured rows and non-code public pages/posts."""
        tokens = {x.casefold() for x in re.findall(r'[a-zA-Z][a-zA-Z0-9-]{2,}|\b\d{2,}\b', str(question))}
        stop = {'what', 'when', 'where', 'which', 'who', 'with', 'from', 'into', 'about', 'this', 'that',
                'does', 'doesnt', 'have', 'has', 'how', 'much', 'many', 'tell', 'show', 'give', 'please',
                'can', 'could', 'would', 'should', 'need', 'want', 'rank', 'level', 'star', 'stars',
                'upgrade', 'price', 'cost', 'weapon', 'weapons', 'mech', 'mechs', 'game', 'arena'}
        tokens -= stop
        entity_names = [str(x).casefold() for x in entities if x]
        if not tokens and not entity_names:
            return []
        hits = []
        collections = ('database_bots', 'database_ecrData', 'database_mechs', 'database_weapons',
                       'legendary_skins', 'list', 'mech_upgrade_costs', 'mod_cost', 'mod_list',
                       'pilot_list', 'pilot_upgrade_costs', 'skin_likes')
        for collection in collections:
            for record in self._structured_records(collection):
                if collection=='list' and not self._listing_matches_weapon_variant(record):continue
                text = json.dumps({k: v for k, v in record.items() if not k.startswith('_archive_')},
                                  ensure_ascii=False).casefold()
                score = sum(1 for token in tokens if token in text)
                for entity in entity_names:
                    if entity in text:
                        score += 10
                if score:
                    hits.append((score, {'kind': 'structured_data', 'collection': collection,
                        'title': record.get('_archive_title'), 'source': record.get('_archive_source'),
                        'author': record.get('_archive_author'), 'date': record.get('_archive_date'),
                        'data': {k: v for k, v in record.items() if not k.startswith('_archive_')}}))
        allowed_categories = ('page', 'database_bots', 'database_ecrData', 'database_mechs',
                              'database_weapons', 'news', 'announcements', 'release_notes',
                              'guides', 'game_discussion', 'tavern', 'rules', 'bug_reports', 'feedback')
        marks = ','.join('?' for _ in USER_CONFIRMED_PUBLIC_SOURCES)
        category_marks = ','.join('?' for _ in allowed_categories)
        try:
            docs = self.db.execute(f'''SELECT source,category,title,author_label,published_text,
                excerpt,content,url,first_seen_utc,last_checked_utc FROM archive_documents WHERE review_status='verified'
                AND source IN ({marks}) AND category IN ({category_marks}) ORDER BY id''',
                (*USER_CONFIRMED_PUBLIC_SOURCES, *allowed_categories)).fetchall()
        except sqlite3.Error:
            docs = []
        event_query=bool(tokens & {'update','updated','news','release','announced','announcement','happened','changed','fix','patch'})
        for doc in docs:
            title=str(doc['title'] or '')
            content=str(doc['content'] or '')
            haystack = ' '.join((title,str(doc['excerpt'] or ''),content)).casefold()
            score = sum(1 for token in tokens if token in haystack)
            title_fold=title.casefold()
            for entity in entity_names:
                if entity in haystack:score+=10
                if entity in title_fold:score+=15
            if event_query and doc['source']=='plarium_forum' and doc['category'] in ('news','announcements','release_notes'):
                score+=5
            author = str(doc['author_label'] or '')
            if score and not author.casefold().startswith('deleted '):
                excerpt=content or str(doc['excerpt'] or '')
                phrase=next((e for e in entity_names if e in excerpt.casefold()),None)
                if phrase:
                    start=max(0,excerpt.casefold().find(phrase)-180)
                    excerpt=excerpt[start:start+750]
                else:
                    excerpt=excerpt[:750]
                date=str(doc['published_text'] or '')
                if not re.search(r'\b(?:19|20)\d{2}\b',date):
                    date=str(doc['first_seen_utc'] or doc['last_checked_utc'] or '')
                hits.append((score, {'kind':'public_document','title':title,
                    'source':doc['source'],'category':doc['category'],'author':doc['author_label'],
                    'date':date,'url':doc['url'],'excerpt':excerpt}))
        hits.sort(key=lambda x: x[0], reverse=True)
        return [hit for _, hit in hits[:max(0, min(int(limit), 8))]]
