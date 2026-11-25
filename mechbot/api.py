"""Share what the bot has collected: announcements, curated facts, ECR views and the forum crawl.

The bot is already the thing that reads those announcement channels, distils each post into a fact row and
keeps the Plarium forum snapshot current. This module puts a read-only HTTP face on exactly that data, so
the website, a spreadsheet or another bot can read the same source instead of a second copy of it that
drifts.

Three rules, and they are the reason this file is not just `select *` behind a route:

* **Originals stay original.** `/announcements` and `/forum` hand back the stored message text and the
  crawled original post exactly as they were captured - no re-summarising, no "improved" wording, none of
  the decoration that makes an ECR post parseable stripped out. Derived material is always a separate,
  labelled field or a separate route.
* **Nothing private, ever.** Reminders, notes, conversation turns, per-user payout rows, timezones and
  worker campaigns live in the same database and are unreachable here: the route table is the only way to
  query, no route takes a table name, and `PRIVATE_TABLES` is asserted against every body by a test.
* **Numbers are traced or absent.** Each response carries the source it came from (a table, a message link,
  a pack file plus its manifest hash) and a caveat list where the source is weak - e.g. the tracker
  history's missing cost lines, which stay missing instead of being averaged away.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import time
from datetime import datetime, timezone

from aiohttp import web

from . import api_view
from . import ecr_digest
from . import facts as fact_lib
# The channel answers and this API must not disagree about whether a rotation is still live, so the
# standing is read through the same two functions the bot's own replies use instead of a second
# implementation of "how long does a post speak for the game".
from .facts import _ecr_state, _first_stated_window, _window_state, dedupe_facts, refresh_ecr_status
from .forum_archive import FORUM_ARCHIVE_PATH, _read_archive
from .store import Store   # imported for Store.FACT_COLUMNS: one list, one truth

API_PATH = '/api/mech-arena/public'
# The only paths that answer without a reader token are the health checks: they carry counts and state, no
# rows. The public page and the index are deliberately not exempt — the page is the data in another skin, so a
# deployment that sets a token locks it exactly like the JSON.
EXEMPT_PATHS = frozenset({'/health', '/api/mech-arena/public/health'})
DEFAULT_LIMIT = 50
EXPORT_ROW_CAP = 20000        # a ceiling on /export so a bug cannot turn into an unbounded read
REVISION_TTL = 5.0            # the fingerprint query is cheap but not free; cache it briefly

# Tables that exist in this database and must never be read through this API.
PRIVATE_TABLES = ('reminders', 'notes', 'conversation_turns', 'worker_campaign', 'worker_reminders',
                  'payout_alerts', 'profiles')

# The two tables this API is allowed to read, spelled out rather than discovered: a route must not be able
# to name a table, so the column lists are constants here.
ANNOUNCEMENT_COLUMNS = ('channel_id', 'message_id', 'guild_id', 'guild_name', 'channel_name', 'content',
                        'message_url', 'posted_at', 'edited_at', 'fetched_at')
FACT_COLUMNS = tuple(Store.FACT_COLUMNS)

APP_CONFIG = web.AppKey('config', dict)
APP_AUTH = web.AppKey('auth_required', bool)

BOT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ECR_HISTORY_PATH = os.path.join(BOT_ROOT, 'data', 'advisor', 'ecr_history_261.json')
ADVISOR_MANIFEST = os.path.join(BOT_ROOT, 'data', 'advisor', 'MANIFEST.json')

ANNOUNCEMENT_FIELDS = ANNOUNCEMENT_COLUMNS + ('posted_at_utc', 'posted_at_date', 'edited_at_utc',
                                              'fetched_at_utc', 'source', 'content_characters', 'facts')
FACT_FIELDS = FACT_COLUMNS + ('items', 'sections', 'source', 'date_confirmed', 'date_note', 'posted_at_utc',
                             'updated_at_utc')
ECR_FIELDS = ('fact_id', 'message_id', 'channel_id', 'channel_name', 'guild_name', 'message_url', 'posted_at',
              'posted_at_utc', 'title', 'summary', 'ecr_kind', 'status', 'status_basis', 'age', 'entities',
              'items', 'item_count', 'codes', 'stated_dates', 'prize_mentions', 'rank_note', 'event_date_label',
              'date_basis', 'date_confirmed', 'date_note', 'merged_attachment_messages', 'orphan_attachment_messages',
              'provenance', 'extractor', 'model', 'run_id')
FORUM_FIELDS = ('rank', 'topic_id', 'title', 'url', 'category', 'author', 'created', 'last_activity', 'replies',
                'views', 'likes', 'original_post', 'source')
HISTORY_FIELDS = ('name', 'type', 'start', 'end', 'items', 'cps', 'rank', 'skewed', 'span_days', 'source')


# Defaults a reader can rely on: off, loopback-ish, no token, a page cap that cannot hurt the bot's
# own loops. `config()` only widens this when the environment asks it to.
SAFE_DEFAULTS = {'enabled': False, 'host': '0.0.0.0', 'port': 8080, 'token': '', 'allow_origin': '*',
                 'max_limit': 200, 'cache_seconds': 30, 'path': API_PATH}


def config():
    """Where and how the API listens, from the environment.

    Off unless asked for: `python -m mechbot.bot` on a laptop must not open a port, while a Railway-style
    service that sets `PORT` and turns the flag on gets the API for free on the same process.
    """
    port = str(os.getenv('DATA_API_PORT') or os.getenv('PORT') or '').strip()
    raw_flag = str(os.getenv('DATA_API_ENABLED') or '').strip().lower()
    mode = str(os.getenv('DATA_API_MODE') or 'auto').strip().lower()
    enabled = raw_flag in ('1', 'true', 'yes', 'on') or (not raw_flag and bool(port) and mode != 'off')
    def _number(name, default):
        try:
            return int(str(os.getenv(name) or '').strip() or default)
        except ValueError:
            return default
    return {
        'enabled': bool(enabled),
        'host': str(os.getenv('DATA_API_HOST') or '0.0.0.0').strip() or '0.0.0.0',
        'port': _number('DATA_API_PORT', _number('PORT', 8080)),
        'token': str(os.getenv('DATA_API_TOKEN') or '').strip(),
        'allow_origin': str(os.getenv('DATA_API_ALLOW_ORIGIN') or '*').strip() or '*',
        'max_limit': max(10, min(_number('DATA_API_MAX_LIMIT', 200), EXPORT_ROW_CAP)),
        'cache_seconds': max(0, _number('DATA_API_CACHE_SECONDS', 30)),
        'path': API_PATH,
    }


def _iso(epoch):
    try:
        value = int(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _epoch_from_date(text):
    """`1732521600`, `2026-03-01` or `2026-03-01 09:00` (always read as UTC) to an epoch."""
    raw = str(text or '').strip()
    if raw.isdigit():
        return int(raw)
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?', raw)
    if not match:
        return None
    parts = [int(group) if group else 0 for group in match.groups()]
    try:
        return int(datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5],
                            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _page_args(request, default_limit=DEFAULT_LIMIT, ceiling=None):
    """`limit`/`offset`/`order`, clamped, so a typo cannot ask for the world."""
    cap = ceiling or config()['max_limit']
    raw = str(request.query.get('limit') or '').strip().lower()
    limit = cap if raw in ('all', 'every', '0') else _as_int(raw, default_limit)
    limit = max(1, min(limit, cap))
    offset = max(0, _as_int(request.query.get('offset'), 0))
    order = 'ASC' if str(request.query.get('order') or '').strip().lower() == 'asc' else 'DESC'
    return limit, offset, order


def _escape_like(text):
    out = str(text or '')
    for char in ('\\', '%', '_'):
        out = out.replace(char, '\\' + char)
    return out


def _project(row, request, allowed):
    """`?fields=a,b` for the wide payloads; an unknown name is a 400 that lists what is allowed.

    Silently dropping a misspelled field would return an almost-empty object, which reads like missing data
    rather than a bad query.
    """
    wanted = str(request.query.get('fields') or '').strip()
    if not wanted:
        return row
    names = [name.strip() for name in wanted.split(',') if name.strip()]
    bad = [name for name in names if name not in allowed]
    if bad:
        raise web.HTTPBadRequest(reason='Unknown field(s): ' + ', '.join(bad) + '. Allowed: ' + ', '.join(allowed))
    return {name: row[name] for name in names if name in row}


def _decoded(row):
    """`entities`/`details` are stored as JSON text; hand them over decoded."""
    out = dict(row or {})
    for name in ('entities', 'details'):
        if name in out and isinstance(out[name], str):
            try:
                out[name] = json.loads(out[name] or 'null')
            except (TypeError, ValueError):
                out[name] = [] if name == 'entities' else {}
    if 'entities' in out and not isinstance(out['entities'], list):
        out['entities'] = []
    if 'details' in out and not isinstance(out['details'], dict):
        out['details'] = {}
    return out


# ---------------------------------------------------------------------------------------------
# query building (one code path for sqlite and Postgres, because Store._query rewrites placeholders)
# ---------------------------------------------------------------------------------------------
async def _select(store, table, columns, where, args, order, limit, offset):
    clauses = ' AND '.join(where) if where else '1=1'
    # Both tables carry `posted_at` and `message_id`, and a snowflake id is a tiebreaker that agrees with
    # the clock, so newest-first is stable even when two posts land in the same second.
    tail = ' DESC,CAST(message_id AS BIGINT) DESC' if order == 'DESC' else ' ASC,CAST(message_id AS BIGINT) ASC'
    sql = (f'SELECT {",".join(columns)} FROM {table} WHERE {clauses} ORDER BY posted_at{tail} '
           f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}')
    rows = await store.fetch(sql, *args, int(limit), int(offset))
    return [_decoded(row) for row in rows]


async def _count(store, table, where, args):
    if store is None:
        return 0
    clauses = ' AND '.join(where) if where else '1=1'
    rows = await store.fetch(f'SELECT COUNT(*) AS total FROM {table} WHERE {clauses}', *args)
    return _as_int((rows or [{}])[0].get('total'), 0)


def _time_filters(request, args, where, column='posted_at'):
    """Accept epoch seconds or `YYYY-MM-DD` for `since`/`until`, and reject anything else."""
    for name, operator in (('since', '>='), ('after', '>='), ('until', '<='), ('before', '<=')):
        raw = str(request.query.get(name) or '').strip()
        if not raw:
            continue
        value = _epoch_from_date(raw)
        if value is None:
            raise web.HTTPBadRequest(reason=f'`{name}` must be a unix timestamp or a YYYY-MM-DD date (UTC).')
        where.append(f'{column} {operator} ${len(args) + 1}')
        args.append(int(value))
    return args


def _in_filter(request, args, where, name, column, allowed=None):
    values = [value.strip() for value in str(request.query.get(name) or '').split(',') if value.strip()]
    if not values:
        return []
    if allowed is not None:
        bad = [value for value in values if value not in allowed]
        if bad:
            raise web.HTTPBadRequest(reason=f'Unknown {name}: ' + ', '.join(bad) + '. Allowed: ' + ', '.join(allowed))
    placeholders = ','.join(f'${len(args) + index + 1}' for index in range(len(values)))
    where.append(f'{column} IN ({placeholders})')
    args.extend(values)
    return values


# ---------------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------------
def _announcement_view(row, facts=None):
    """The stored Discord message plus convenience date fields; `content` is the original text."""
    out = dict(row or {})
    out['posted_at_utc'] = _iso(out.get('posted_at'))
    out['posted_at_date'] = (out.get('posted_at_utc') or '')[:10] or None
    out['edited_at_utc'] = _iso(out.get('edited_at'))
    out['fetched_at_utc'] = _iso(out.get('fetched_at'))
    out['content_characters'] = len(str(out.get('content') or ''))
    out['source'] = {'kind': 'discord_message', 'table': 'announcements',
                     'channel_id': str(out.get('channel_id') or ''), 'message_id': str(out.get('message_id') or ''),
                     'url': out.get('message_url') or '', 'verbatim_field': 'content'}
    if facts is not None:
        out['facts'] = facts
    return out


def _fact_view(row):
    out = _decoded(row)
    out['posted_at_utc'] = _iso(out.get('posted_at'))
    out['posted_at_date'] = (out.get('posted_at_utc') or '')[:10] or None
    out['updated_at_utc'] = _iso(out.get('updated_at'))
    details = out.get('details') or {}
    entries = details.get('entries') or []
    out['items'] = [entry for entry in entries if isinstance(entry, dict) and not entry.get('section')]
    out['sections'] = [entry.get('label') for entry in entries
                       if isinstance(entry, dict) and entry.get('section')]
    out['source'] = {'kind': 'announcement_message', 'table': 'announcement_facts',
                     'message_id': str(out.get('message_id') or ''), 'url': out.get('message_url') or '',
                     'extractor': out.get('extractor'), 'model': out.get('model')}
    # The bot refuses to claim a date it did not read; the API keeps that distinction visible instead of
    # flattening `event_date_label` into something a consumer would trust as confirmed.
    # `date_basis` only ever says whether the post carried a date at all; what a reader needs to know is
    # whether that date names a year, because a yearless "March 1 to March 8" is a *mention*, and a
    # window built on it would be this module guessing a year the game never published.
    stated = [str(value) for value in ((out.get('details') or {}).get('stated_dates') or [])]
    out['date_confirmed'] = bool(stated) and any(re.search(r'\b20\d{2}\b', value) for value in stated)
    out['date_note'] = ('the post stated a full date' if out['date_confirmed'] else
                        'the post mentioned a date without a year' if stated else
                        'no event date is stated; only the post date is known')
    return out


def _ecr_view(fact, now, pool=None):
    """One ECR post as the bot answers it: the item blocks, the post's own line on each, its standing.

    `status` comes from `refresh_ecr_status` (already applied to `pool`), and `standing` from
    `_ecr_state`, so a caller reading the API sees exactly what a player sees in the channel.
    """
    state, why = _ecr_state(fact, now=now, facts=pool)
    details = fact.get('details') or {}
    items = []
    for entry in fact.get('items') or []:
        note = str(entry.get('note') or '')
        bullets = [str(line) for line in (entry.get('bullets') or [])]
        # `advice_for` answers (verdict, quote) or None, and the quote is the whole point of exposing it
        # here: a reader can see which words in the post the verdict came from instead of trusting it.
        verdict = fact_lib.advice_for(' '.join([note] + bullets)) if (note or bullets) else None
        items.append({'label': entry.get('label'), 'note': note, 'bullets': bullets,
                      'advice': (verdict or (None,))[0], 'advice_quote': (verdict or (None, None))[1],
                      'advice_from': 'the announcement post' if verdict else None, 'verbatim': True})
    return {'fact_id': fact.get('fact_id'), 'message_id': fact.get('message_id'),
            'channel_id': fact.get('channel_id'), 'channel_name': fact.get('channel_name'),
            'guild_name': fact.get('guild_name'), 'message_url': fact.get('message_url'),
            'posted_at': fact.get('posted_at'), 'posted_at_utc': fact.get('posted_at_utc'),
            'title': fact.get('title'), 'summary': fact.get('summary'),
            'ecr_kind': str(fact.get('ecr_kind') or 'unknown'),
            'status': fact.get('status'), 'status_basis': details.get('status_basis') or why,
            'state': state, 'standing': ('Live' if state == 'live' else 'Outdated' if state == 'outdated'
                                         else 'Unknown') + f' — {why}',
            'age': fact_lib.age_label(fact.get('posted_at'), now),
            'entities': fact.get('entities') or [], 'items': items, 'item_count': len(items),
            'codes': details.get('codes') or [], 'stated_dates': details.get('stated_dates') or [],
            'prize_mentions': details.get('prize_mentions') or [], 'rank_note': details.get('rank_note'),
            'event_date_label': fact.get('event_date_label') or '', 'date_basis': fact.get('date_basis') or '',
            'date_confirmed': fact.get('date_confirmed'),
            'merged_attachment_messages': len(details.get('merged_attachment_messages') or []),
            'orphan_attachment_messages': len(details.get('orphan_attachment_messages') or []),
            'provenance': fact.get('provenance'), 'extractor': fact.get('extractor'), 'model': fact.get('model'),
            'run_id': fact.get('run_id')}


_HISTORY_CACHE = {'path': None, 'mtime': None, 'rows': [], 'error': None}


def _ecr_history(path=None):
    """The vendored Crate Rush tracker history, read from the pack the bot already ships.

    Deliberately not merged with the live announcements: the pack is a historical record with its own
    gaps, and blending the two would hide which one a number came from.
    """
    target = os.path.abspath(str(path or ECR_HISTORY_PATH))
    try:
        mtime = os.path.getmtime(target)
    except OSError as exc:
        _HISTORY_CACHE.update({'path': target, 'mtime': None, 'rows': [], 'error': type(exc).__name__})
        return []
    if _HISTORY_CACHE['path'] == target and _HISTORY_CACHE['mtime'] == mtime:
        return _HISTORY_CACHE['rows']
    try:
        with open(target, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        rows = data if isinstance(data, list) else (data.get('history') or data.get('rows') or [])
        clean = [row for row in rows if isinstance(row, dict)]
        error = None
    except (OSError, UnicodeError, ValueError) as exc:
        clean, error = [], type(exc).__name__
    _HISTORY_CACHE.update({'path': target, 'mtime': mtime, 'rows': clean, 'error': error})
    return clean


def _history_view(row, path=None):
    view = dict(row or {})
    start = _epoch_from_date(str(view.get('start') or ''))
    end = _epoch_from_date(str(view.get('end') or ''))
    view['span_days'] = (int((end - start) // 86400) + 1) if (start is not None and end is not None) else None
    view['source'] = {'kind': 'vendored_pack_file', 'file': 'ecr_history_261.json', 'pack': _manifest_note(path)}
    return view


_MANIFEST_CACHE = {'path': None, 'mtime': None, 'note': None}


def _manifest_note(path=None):
    """Provenance for the pack file: its recorded hash and the pack build date, or None if unreadable."""
    target = os.path.abspath(str(path or ADVISOR_MANIFEST))
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return None
    if _MANIFEST_CACHE['path'] == target and _MANIFEST_CACHE['mtime'] == mtime:
        return _MANIFEST_CACHE['note']
    note = None
    try:
        with open(target, 'r', encoding='utf-8') as handle:
            manifest = json.load(handle)
        files = manifest.get('files') if isinstance(manifest, dict) else None
        entry = None
        if isinstance(files, dict):
            entry = files.get('ecr_history_261.json')
        elif isinstance(files, list):
            entry = next((row for row in files if isinstance(row, dict)
                          and 'ecr_history' in str(row.get('name') or row)), None)
        entry = entry or {}
        note = {'pack_build_date': manifest.get('pack_build_date') if isinstance(manifest, dict) else None,
                'sha256': entry.get('sha256'), 'bytes': entry.get('bytes'),
                'trust': entry.get('trust') or entry.get('trust_tier')}
    except (OSError, UnicodeError, ValueError):
        note = None
    _MANIFEST_CACHE.update({'path': target, 'mtime': mtime, 'note': note})
    return note


def _forum_view(thread):
    """A crawled forum thread with its original post text, flattened just enough to be usable."""
    row = thread or {}
    category = row.get('category') or {}
    post = row.get('original_post') or {}
    text = str(post.get('text') or '')
    return {'rank': row.get('rank'), 'topic_id': row.get('topic_id'), 'title': row.get('title'), 'url': row.get('url'),
            'category': {'name': category.get('name'), 'id': category.get('id'), 'url': category.get('url'),
                         'also_in': category.get('also_in') or []},
            'author': {'name': (row.get('author') or {}).get('name'), 'profile_url': (row.get('author') or {}).get('profile_url')},
            'created': row.get('created') or {}, 'last_activity': row.get('last_activity') or {},
            'replies': (row.get('stats') or {}).get('replies'), 'views': (row.get('stats') or {}).get('views'),
            'likes': (row.get('stats') or {}).get('likes'),
            'original_post': {'text': text, 'characters': len(text), 'verbatim': True},
            'source': {'kind': 'plarium_forum_crawl', 'file': os.path.basename(str(FORUM_ARCHIVE_PATH)),
                       'scope': 'the crawl the bot refreshes; see /forum/meta for its window'}}


def _forum_match(thread, needle):
    blob = ' '.join(str(thread.get(name) or '') for name in ('title', 'url')) + ' ' + \
        str(((thread.get('original_post') or {}).get('text')) or '') + ' ' + \
        str(((thread.get('category') or {}).get('name')) or '')
    return needle in blob.casefold()


# ---------------------------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------------------------
def _meta(request, endpoint, total, returned, offset, limit, source, caveats=None, extra=None):
    now = int(time.time())
    out = {'endpoint': endpoint, 'as_of': now, 'as_of_utc': _iso(now), 'total': int(total),
           'returned': int(returned), 'offset': int(offset), 'limit': int(limit),
           'next_offset': (int(offset) + int(returned)) if (offset + returned) < total else None,
           'source': source, 'caveats': list(caveats or []),
           'authenticated': bool(request.app[APP_AUTH]),
           'terms': 'Read-only snapshot of what the ARCHΛNGLE bot stores about the game. Player-owned rows '
                    '(schedules, saved text, chat memory, per-account payouts) are never served here.'}
    out.update(extra or {})
    return out


def _filename(endpoint):
    return endpoint.split('?')[0].strip('/').replace('/', '-') or 'index'


def _respond(request, endpoint, data, total, offset, limit, source, caveats=None, extra=None, status=200,
             fmt=None):
    fmt = str(fmt if fmt is not None else (request.query.get('format') or '')).strip().lower()
    name = _filename(endpoint)
    if fmt in ('ndjson', 'jsonl'):
        body = ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n' for row in data)
        return web.Response(text=body, content_type='application/x-ndjson', status=status,
                            headers={'Content-Disposition': f'inline; filename="{name}.ndjson"'})
    if fmt == 'csv':
        buffer = io.StringIO()
        columns = list(dict.fromkeys(key for row in data[:500] for key in row))
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in data:
            writer.writerow({key: (json.dumps(value, ensure_ascii=False)
                                   if isinstance(value, (dict, list)) else value)
                             for key, value in row.items() if key in columns})
        return web.Response(text=buffer.getvalue(), content_type='text/csv', status=status,
                            headers={'Content-Disposition': f'inline; filename="{name}.csv"'})
    return web.json_response({'data': data,
                              'meta': _meta(request, endpoint, total, len(data), offset, limit, source,
                                            caveats, extra)}, status=status)


# ---------------------------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------------------------
class DataApi:
    """One aiohttp app, started by the bot, answering read-only GETs from the bot's own storage.

    It is given the bot rather than a store so the index can also report the ingestion loops' state:
    "how current is this?" is a question about the collector, not about the database.
    """

    def __init__(self, bot, forum_path=None, history_path=None, manifest_path=None, settings=None):
        self.bot = bot
        self.forum_path = forum_path
        self.history_path = history_path
        self.manifest_path = manifest_path
        self.config = settings or config()
        self.runner = None
        self.site = None
        self.started_at = None
        self._revision_cache = {'value': None, 'at': 0.0}   # not `self._revision`: that is the coroutine

    # -- access ---------------------------------------------------------------------------
    @property
    def store(self):
        return getattr(self.bot, 'store', None)

    def _history(self):
        return _ecr_history(self.history_path)

    def _threads(self):
        return _read_archive(self.forum_path)

    async def _facts(self, where=(), args=(), limit=500, offset=0, order='DESC'):
        store = self.store
        if store is None or not getattr(store, 'supports_announcement_facts', False):
            return []
        return await _select(store, 'announcement_facts', FACT_COLUMNS, list(where), list(args), order, limit, offset)

    async def _revision(self):
        """A fingerprint over row counts and newest timestamps, so an ETag changes exactly when data does."""
        now = time.monotonic()
        cache = self._revision_cache
        if cache['value'] and now - cache['at'] < REVISION_TTL:
            return cache['value']
        store = self.store
        bits = []
        if store is not None:
            for table in ('announcements', 'announcement_facts'):
                try:
                    rows = await store.fetch(f'SELECT COUNT(*) AS total,MAX(posted_at) AS latest FROM {table}')
                    first = (rows or [{}])[0]
                    bits.append(f'{table}:{_as_int(first.get("total"))}:{_as_int(first.get("latest"))}')
                except Exception:
                    bits.append(f'{table}:unavailable')
        else:
            bits.append('store:none')
        bits.append(f'forum:{len(self._threads())}')
        value = hashlib.sha256('|'.join(bits).encode('utf-8')).hexdigest()[:16]
        cache.update({'value': value, 'at': now})
        return value

    def _base_url(self, request=None):
        if request is not None:
            return str(request.url.with_path(API_PATH).with_query(''))
        host = str(os.getenv('DATA_API_PUBLIC_HOST') or '').strip()
        if host:
            return host.rstrip('/') + API_PATH
        cfg = self.config
        return f'http://{cfg["host"] if cfg["host"] not in ("0.0.0.0",) else "localhost"}:{cfg["port"]}{API_PATH}'

    async def _counts(self):
        out = {'announcements': 0, 'facts': 0, 'ecr': 0, 'events': 0, 'forum_threads': 0, 'ecr_history': 0,
               'rotations': 0, 'channels': 0, 'database': 'none', 'by_category': {}}
        store = self.store
        if store is None:
            out['forum_threads'] = len(self._threads())
            out['ecr_history'] = len(self._history())
            out['rotations'] = len(ecr_digest.rotations(out['ecr_history'] and self._history() or []))
            return out
        out['database'] = 'postgres' if getattr(store, 'pool', None) else 'sqlite'
        if getattr(store, 'supports_announcements', False):
            try:
                rows = await store.fetch('SELECT COUNT(*) AS total,COUNT(DISTINCT channel_id) AS chans '
                                        'FROM announcements')
                first = (rows or [{}])[0]
                out['announcements'] = _as_int(first.get('total'))
                out['channels'] = _as_int(first.get('chans'))
            except Exception:
                pass
        if getattr(store, 'supports_announcement_facts', False):
            try:
                rows = await store.fetch('SELECT category,COUNT(*) AS total FROM announcement_facts GROUP BY category')
                tally = {str(row.get('category')): _as_int(row.get('total')) for row in (rows or [])}
                out['by_category'] = tally
                out['facts'] = sum(tally.values())
                out['ecr'] = tally.get('ecr', 0)
                out['events'] = sum(value for key, value in tally.items()
                                    if key in ('event', 'balance_change', 'store_offer', 'gear_hub', 'promo_code'))
            except Exception:
                pass
        out['forum_threads'] = len(self._threads())
        history = self._history()
        out['ecr_history'] = len(history)
        out['rotations'] = len(ecr_digest.rotations(history))
        return out

    async def summary(self):
        """What `/data_api` in Discord prints: what is served, how much of it, and how fresh.

        It is the same numbers the index answers with, so the command and the API cannot describe the
        collection differently. The reader token is reported as present or absent and never printed.
        """
        return {'counts': await self._counts(), 'ingestion': await self._ingestion_state(),
                'base_url': self._base_url(), 'readers': 'required' if self.config['token'] else 'open read',
                'page_ceiling': self.config['max_limit'], 'cache_seconds': self.config['cache_seconds'],
                'allow_origin': self.config['allow_origin']}

    async def _ingestion_state(self):
        state = {}
        store = self.store
        if store is not None and getattr(store, 'supports_announcements', False):
            try:
                rows = await store.fetch('SELECT channel_id,channel_name,COUNT(*) AS total,MAX(posted_at) AS latest '
                                         'FROM announcements GROUP BY channel_id,channel_name ORDER BY latest DESC')
                for row in (rows or [])[:12]:
                    channel_id = str(row.get('channel_id'))
                    cursor = None
                    try:
                        cursor = await store.announcement_cursor(channel_id)
                    except Exception:
                        cursor = None
                    state[f'channel {channel_id}'] = (f"{row.get('channel_name') or '?'} · "
                                                       f"{_as_int(row.get('total'))} posts · newest "
                                                       f"{_iso(row.get('latest')) or 'unknown'} · cursor "
                                                       f"{cursor if cursor is not None else 'unreadable'}")
            except Exception as exc:
                state['channels'] = f'unreadable ({type(exc).__name__})'
        for name, label in (('announcement_runner', 'announcement ingestion loop'),
                            ('forum_archive_runner', 'forum crawl loop'),
                            ('conversation_runner', 'chat-memory expiry loop')):
            task = getattr(self.bot, name, None)
            if task is None:
                state[label] = 'not started'
            else:
                state[label] = 'running' if not getattr(task, 'done', None) or not task.done() else 'stopped'
        if self.started_at:
            state['api up for'] = f'{int(time.time() - self.started_at)}s'
        return state

    # -- index ----------------------------------------------------------------------------
    async def root(self, request):
        """`/` is the page for a person and the catalogue for a script.

        A browser asking for `/` wants to see what is in there today; `curl` asking the same path wants the
        machine-readable index, because that is what a probe expects to find at a root. Both read the same
        counts, so nobody gets a different number depending on how they asked.
        """
        if 'text/html' in str(request.headers.get('Accept') or '').lower():
            return await self.view(request)
        return await self.index(request)

    async def view(self, request):
        """The public page: newest ECR, the posts themselves, dated events, the forum crawl.

        Every value comes from the helpers the JSON routes already use — `_fact_view`, `_ecr_view`,
        `refresh_ecr_status`, `dedupe_facts`, `_forum_view` — so the page cannot call a rotation live while
        `/ecr` calls it outdated. Only the "give me the six newest" plumbing is repeated here, and it holds
        no judgement of its own.
        """
        now = int(time.time())
        store = self.store
        ecr_rows, event_rows, posts = [], [], []
        if store is not None and getattr(store, 'supports_announcement_facts', False):
            facts = dedupe_facts(refresh_ecr_status(
                [_fact_view(row) for row in
                 await self._facts((), (), self.config['max_limit'] * 5, 0, 'DESC')], now=now))
            pool = [row for row in facts if row.get('category') == 'ecr']
            ecr_rows = [_ecr_view(row, now, pool) for row in pool[:6]]
            for row in [item for item in facts if item.get('category') not in ('ecr', 'other')][:8]:
                window = _first_stated_window(row)
                event_rows.append({'title': row.get('title'), 'category': row.get('category'),
                                   'channel_name': row.get('channel_name'),
                                   'message_url': row.get('message_url'),
                                   'event_date_label': row.get('event_date_label') or '',
                                   'date_note': row.get('date_note'),
                                   'window': (None if not (window and row.get('date_confirmed')) else
                                              {'label': fact_lib._window_label(row, now),
                                               'state': _window_state(row, now)}),
                                   'standing': fact_lib._fact_standing(row, now),
                                   'source': row.get('source') or {}})
        if store is not None and getattr(store, 'supports_announcements', False):
            posts = [_announcement_view(row) for row in
                     await _select(store, 'announcements', ANNOUNCEMENT_COLUMNS, [], [], 'DESC', 5, 0)]
        threads = self._threads() or []
        forum_meta = {}
        if threads:
            forum_meta = {'last_created': ((threads[0].get('created') or {}).get('display')),
                          'first_created': ((threads[-1].get('created') or {}).get('display')),
                          'threads_in_file': len(threads)}
        blocks = {'counts': await self._counts(), 'as_of_utc': _iso(now), 'revision': await self._revision(),
                  'ingestion': await self._ingestion_state(), 'ecr': ecr_rows, 'events': event_rows,
                  'announcements': posts, 'forum': [_forum_view(thread) for thread in threads[:14]],
                  'forum_meta': forum_meta, 'links': {
                      'JSON index': API_PATH, 'Announcements': API_PATH + '/announcements',
                      'ECR': API_PATH + '/ecr', 'Forum': API_PATH + '/forum',
                      'Export NDJSON': API_PATH + '/export?dataset=announcements', 'Health': '/health'},
                  'caveats': ['“Live”, “outdated” and “unknown” are this bot’s own reading of how long a post '
                              'of that kind speaks for the game, plus how it compares with the other stored '
                              'posts — the same derivation the Discord answers use.',
                              'A buy or skip line inside a folded block is the guide post’s own words about '
                              'its item, attributed to the post. It is not a recommendation from this API.',
                              'Forum timestamps are reproduced exactly as the site displayed them; the forum '
                              'publishes no timezone, so no UTC conversion is claimed.',
                              'Tracker history comes from the vendored community pack and is never merged '
                              'with the live announcements.',
                              'Post text is the stored message, unedited; a table that has not been migrated '
                              'shows as empty rather than filled in.']}
        return web.Response(text=api_view.render(blocks), content_type='text/html', charset='utf-8')

    async def index(self, request):
        """A machine-readable catalogue, and a human one in the browser.

        A browser gets HTML because the first question about a data API is "what is in there today", and
        that answer should not take a curl command to find out.
        """
        counts = await self._counts()
        endpoints = [
            {'path': API_PATH + '/announcements', 'gives': 'every stored announcement post, original text',
             'rows': counts['announcements'],
             'params': 'limit, offset, order, since, until, channel_id, guild_id, category, q, fields, '
                       'include=facts, format=json|ndjson|csv'},
            {'path': API_PATH + '/announcements/{message_id}', 'gives': 'one post with its curated facts',
             'rows': None, 'params': '—'},
            {'path': API_PATH + '/facts', 'gives': 'the curated fact per post: category, kind, entities, items',
             'rows': counts['facts'],
             'params': 'limit, offset, order, since, until, category, ecr_kind, status, channel_id, extractor, '
                       'q, fields, format'},
            {'path': API_PATH + '/ecr', 'gives': 'ECR posts with one block per featured item, the guide’s own buy/skip line, live status',
             'rows': counts['ecr'], 'params': 'limit, offset, kind, status, state, since, until, q, fields, format'},
            {'path': API_PATH + '/ecr/current', 'gives': 'the newest saved ECR of each kind', 'rows': None,
             'params': '—'},
            {'path': API_PATH + '/ecr/history', 'gives': 'the vendored Crate Rush tracker history, verbatim',
             'rows': counts['ecr_history'],
             'params': 'limit, offset, order, type, name, q, since, until, fields, format'},
            {'path': API_PATH + '/ecr/rotations', 'gives': 'that history grouped into rotations, with observed cadence',
             'rows': counts['rotations'], 'params': 'limit, offset, order'},
            {'path': API_PATH + '/events', 'gives': 'dated event/patch/store facts with how current they are',
             'rows': counts['events'], 'params': 'limit, offset, category, since, until, q, fields, format'},
            {'path': API_PATH + '/forum', 'gives': 'every crawled Plarium thread with its full original post',
             'rows': counts['forum_threads'],
             'params': 'limit, offset, order, category, q, fields, format'},
            {'path': API_PATH + '/forum/meta', 'gives': 'what that crawl covered and when it ran', 'rows': None,
             'params': '—'},
            {'path': API_PATH + '/stats', 'gives': 'counts, per-channel cursors, per-extractor fact stats',
             'rows': None, 'params': '—'},
            {'path': API_PATH + '/export', 'gives': 'a whole dataset as NDJSON, for rebuilding a local copy',
             'rows': None, 'params': 'dataset=announcements|facts|ecr_history|forum'},
            {'path': API_PATH + '/view',
             'gives': 'the same rows as a web page: current ECR, the originals, events, the forum crawl',
             'rows': None, 'params': '—'},
        ]
        payload = {'service': 'ARCHΛNGLE data API', 'version': 1, 'read_only': True,
                   'private_data': ('player-owned rows — schedules, saved text, chat memory, per-account '
                                    'payouts, stored timezones — are not exposed here: the routes below are the '
                                    'only data this API can answer with.'),
                   'base_url': self._base_url(request), 'endpoints': endpoints, 'counts': counts,
                   'ingestion': await self._ingestion_state(), 'revision': await self._revision(),
                   'docs': 'append ?format=ndjson or ?format=csv to any list route; ?fields=a,b to trim a row'}
        accepts = str(request.headers.get('Accept') or '')
        if 'text/html' in accepts and 'json' not in accepts:
            return web.Response(text=self._render_html(payload), content_type='text/html')
        return web.json_response(payload)

    def _render_html(self, payload):
        counts = payload['counts']
        tiles = ''.join(f'<div class="k"><b>{value}</b><span>{label}</span></div>' for label, value in
                        (('announcements', counts['announcements']), ('curated facts', counts['facts']),
                         ('ECR posts', counts['ecr']), ('dated events', counts['events']),
                         ('forum threads', counts['forum_threads']), ('tracker rows', counts['ecr_history'])))
        rows = ''.join(
            f'<tr><td><a href="{row["path"]}">{row["path"].replace(API_PATH, "") or "/"}</a></td>'
            f'<td>{row["gives"]}</td><td class="n">{row["rows"] if row["rows"] is not None else "—"}</td>'
            f'<td class="p">{row["params"]}</td></tr>' for row in payload['endpoints'])
        ingest = ''.join(f'<li><b>{key}</b><span>{value}</span></li>'
                         for key, value in (payload['ingestion'] or {}).items()) or '<li><span>no collector state</span></li>'
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ARCHΛNGLE · data API</title>
<style>
:root{{color-scheme:dark;--bg:#080918;--tx:#f1f0ff;--mu:#aeacd0;--ln:#ffffff26;--a:#8b7bff;--b:#3ee0ff}}
*{{box-sizing:border-box}}body{{margin:0;color:var(--tx);min-height:100vh;background:
radial-gradient(120vw 60vw at 12% -10%,#8b7bff2e,transparent 60%),
radial-gradient(90vw 50vw at 90% 6%,#3ee0ff22,transparent 60%),var(--bg);
font:400 15px/1.6 Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.w{{max-width:1120px;margin:0 auto;padding:clamp(28px,6vw,72px) max(20px,4vw)}}
h1{{font:700 clamp(30px,6vw,60px)/1.02 'Space Grotesk',ui-sans-serif,sans-serif;letter-spacing:-.03em;margin:0}}
h1 b{{color:var(--a)}}.sub{{color:var(--mu);max-width:66ch;margin:14px 0 26px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:28px}}
.k{{padding:14px 16px;border:1px solid var(--ln);border-radius:16px;background:#ffffff0f;backdrop-filter:blur(10px)}}
.k b{{display:block;font:700 26px/1 'Space Grotesk',ui-sans-serif,sans-serif;color:var(--b)}}
.k span{{color:var(--mu);font-size:12px;letter-spacing:.4px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:11px 10px;border-bottom:1px solid var(--ln);vertical-align:top}}
th{{font:500 10px/1 ui-monospace,'JetBrains Mono',monospace;letter-spacing:1.6px;text-transform:uppercase;color:var(--mu)}}
td a{{color:var(--b);text-decoration:none;font-family:ui-monospace,'JetBrains Mono',monospace;font-size:13px}}
td a:hover{{text-decoration:underline}}
.n{{text-align:right;font-family:ui-monospace,'JetBrains Mono',monospace;color:var(--a)}}
.p{{color:var(--mu);font-size:12px;font-family:ui-monospace,'JetBrains Mono',monospace}}
.note{{margin-top:24px;padding:16px 18px;border:1px solid var(--ln);border-radius:16px;background:#ffffff0d}}
.note h2{{margin:0 0 8px;font:600 13px/1 ui-monospace,monospace;letter-spacing:1.6px;text-transform:uppercase;color:var(--mu)}}
.note ul{{margin:0;padding-left:16px;color:var(--mu);font-size:13px}}.note li b{{color:var(--tx)}}
.foot{{margin-top:28px;padding-top:16px;border-top:1px solid var(--ln);color:var(--mu);font-size:12px;
font-family:ui-monospace,'JetBrains Mono',monospace;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
</style></head><body><div class="w">
<h1>ARCHΛNGLE <b>·</b> data API</h1>
<p class="sub">Everything the bot has read from the announcement channels, the ECR guides and the Plarium forum —
originals, not summaries. Read-only JSON, NDJSON and CSV. Private player data is not in here.</p>
<div class="cards">{tiles}</div>
<table><thead><tr><th>route</th><th>what it gives</th><th>rows</th><th>parameters</th></tr></thead><tbody>{rows}</tbody></table>
<div class="note"><h2>collector state</h2><ul>{ingest}</ul></div>
<div class="foot"><span>revision {payload['revision']}</span><span>{payload['private_data']}</span></div>
</div></body></html>"""

    # -- routes ---------------------------------------------------------------------------
    async def announcements(self, request):
        store = self.store
        if store is None or not getattr(store, 'supports_announcements', False):
            return _respond(request, '/announcements', [], 0, 0, 0, {'table': 'announcements'},
                            ['The announcements table has not been migrated in this deployment, so there is '
                             'nothing to read yet.'])
        where, args = [], []
        channel = str(request.query.get('channel_id') or '').strip()
        guild = str(request.query.get('guild_id') or '').strip()
        if channel:
            where.append(f'channel_id = ${len(args) + 1}'); args.append(channel)
        if guild:
            where.append(f'guild_id = ${len(args) + 1}'); args.append(guild)
        _time_filters(request, args, where)
        query = str(request.query.get('q') or '').strip()
        if query:
            where.append(f"(LOWER(content) LIKE LOWER(${len(args) + 1}) ESCAPE '\\' OR "
                         f"LOWER(channel_name) LIKE LOWER(${len(args) + 2}) ESCAPE '\\')")
            args.extend([f'%{_escape_like(query)}%', f'%{_escape_like(query)}%'])
        category = str(request.query.get('category') or '').strip()
        wanted_ids = None
        if category:
            if not getattr(store, 'supports_announcement_facts', False):
                raise web.HTTPBadRequest(reason='`category` needs the announcement_facts table.')
            wanted_ids = {str(row.get('message_id')) for row in await store.announcement_facts(categories=(category,),
                                                                                               limit=2000)}
            if not wanted_ids:
                return _respond(request, '/announcements', [], 0, 0, 0, {'table': 'announcements'},
                                [f'No stored post is classified as `{category}`, so the filter is empty by '
                                 f'records, not by omission.'])
        limit, offset, order = _page_args(request, ceiling=self.config['max_limit'])
        rows = await _select(store, 'announcements', ANNOUNCEMENT_COLUMNS, where, args, order, limit, offset)
        by_message = {}
        if str(request.query.get('include') or '').strip() == 'facts' and getattr(store, 'supports_announcement_facts', False):
            for fact in await store.announcement_facts(limit=2000):
                by_message.setdefault(str(fact.get('message_id')), []).append(_fact_view(fact))
        data = [_project(_announcement_view(row, by_message.get(str(row.get('message_id')))), request,
                         ANNOUNCEMENT_FIELDS) for row in rows]
        if wanted_ids is not None:
            data = [row for row in data if str(row.get('message_id')) in wanted_ids]
        total = len(data) if wanted_ids is not None else await _count(store, 'announcements', where, args)
        return _respond(request, '/announcements', data, total, offset, limit,
                        {'table': 'announcements', 'content': 'the message text exactly as fetched from Discord'},
                        ['A post that was only an image is stored as its stub text plus the merged attachment; '
                         'check facts.details.merged_attachment_messages before concluding the post said nothing.'])

    async def announcement_one(self, request):
        store = self.store
        message_id = str(request.match_info.get('message_id') or '').strip()
        if store is None or not getattr(store, 'supports_announcements', False) or not message_id.isdigit():
            raise web.HTTPNotFound(reason='`message_id` is the numeric Discord message id.')
        rows = await store.fetch('SELECT ' + ','.join(ANNOUNCEMENT_COLUMNS) + ' FROM announcements '
                                 'WHERE message_id = $1', message_id)
        if not rows:
            raise web.HTTPNotFound(reason=f'No stored announcement with message_id {message_id}.')
        facts = []
        if getattr(store, 'supports_announcement_facts', False):
            facts = [_fact_view(fact) for fact in await store.announcement_facts(limit=2000)
                     if str(fact.get('message_id')) == message_id]
        data = _project(_announcement_view(dict(rows[0]), facts), request, ANNOUNCEMENT_FIELDS)
        return web.json_response({'data': data,
                                  'meta': _meta(request, '/announcements/{message_id}', 1, 1, 0, 1,
                                                 {'table': 'announcements'},
                                                 ['`facts` is the curated reading of this post; `content` is what '
                                                  'the post said.'])})

    async def fact_rows(self, request):
        store = self.store
        if store is None or not getattr(store, 'supports_announcement_facts', False):
            return _respond(request, '/facts', [], 0, 0, 0, {'table': 'announcement_facts'},
                            ['The announcement_facts table has not been migrated in this deployment.'])
        where, args = [], []
        _in_filter(request, args, where, 'category', 'category', fact_lib.CATEGORIES)
        _in_filter(request, args, where, 'ecr_kind', 'ecr_kind', fact_lib.ECR_KINDS)
        _in_filter(request, args, where, 'status', 'status')
        _in_filter(request, args, where, 'channel_id', 'channel_id')
        _in_filter(request, args, where, 'extractor', 'extractor')
        _time_filters(request, args, where)
        query = str(request.query.get('q') or '').strip()
        if query:
            where.append(f"(LOWER(title) LIKE LOWER(${len(args) + 1}) ESCAPE '\\' OR LOWER(summary) LIKE "
                         f"LOWER(${len(args) + 2}) ESCAPE '\\' OR LOWER(entities) LIKE LOWER(${len(args) + 3}) "
                         f"ESCAPE '\\')")
            args.extend([f'%{_escape_like(query)}%'] * 3)
        limit, offset, order = _page_args(request, ceiling=self.config['max_limit'])
        rows = await _select(store, 'announcement_facts', FACT_COLUMNS, where, args, order, limit, offset)
        data = [_project(_fact_view(row), request, FACT_FIELDS) for row in rows]
        total = await _count(store, 'announcement_facts', where, args)
        return _respond(request, '/facts', data, total, offset, limit,
                        {'table': 'announcement_facts',
                         'extractors': sorted({str(row.get('extractor') or '?') for row in rows})},
                        ['`title` and `summary` are the extractor’s words about the post, not the post’s words; '
                         'read /announcements for the original.'])

    async def ecr(self, request):
        where, args = [], []
        where.append('category = $1'); args.append('ecr')
        _in_filter(request, args, where, 'kind', 'ecr_kind', fact_lib.ECR_KINDS)
        _time_filters(request, args, where)
        limit, offset, order = _page_args(request, ceiling=self.config['max_limit'])
        now = int(time.time())
        # Read the whole stored set of ECR posts, not just this page: cadence, supersession and re-post
        # collapsing are judgements about the set, and computing them from a 50-row slice would lie.
        rows = [_fact_view(row) for row in await self._facts(where, args, self.config['max_limit'] * 5, 0, order)]
        rows = dedupe_facts(refresh_ecr_status(rows, now=now))
        wanted_status = str(request.query.get('status') or '').strip().lower()
        wanted_state = str(request.query.get('state') or '').strip().lower()
        query = str(request.query.get('q') or '').strip().lower()
        views = []
        for row in rows:
            view = _ecr_view(row, now, rows)
            if wanted_status and view['status'] != wanted_status:
                continue
            if wanted_state and view['state'] != wanted_state:
                continue
            if query and query not in json.dumps(view, ensure_ascii=False).casefold():
                continue
            views.append(view)
        page = [_project(view, request, ECR_FIELDS) for view in views[offset:offset + limit]]
        return _respond(request, '/ecr', page, len(views), offset, limit,
                        {'table': 'announcement_facts', 'category': 'ecr',
                         'advice': ('the guide post’s own words about the item, read from its text; a clause the '
                                    'advisor treats as a non-verdict is removed, so quote and verdict travel '
                                    'together'),
                         'standing': 'mechbot/facts.py, the same derivation the channel answers use'},
                        ['`advice` is what the post said about the item, attributed to the post; `advice_quote` '
                         'is cut from the item block with any clause the advisor reads as a non-verdict '
                         'removed, so it can join two adjacent lines — and it is not a '
                         'recommendation from this bot.',
                         'A one-day ECR is marked ended 24 hours after the post plus a grace margin, because that '
                         'is the rule the post states rather than a timestamp it gave.'])

    async def ecr_current(self, request):
        rows = [_fact_view(row) for row in await self._facts(('category = $1',), ('ecr',),
                                                              self.config['max_limit'] * 5, 0)]
        now = int(time.time())
        rows = dedupe_facts(refresh_ecr_status(rows, now=now))
        data = [_ecr_view(row, now, rows) for row in fact_lib.current_ecrs(rows, now)]
        return _respond(request, '/ecr/current', data, len(data), 0, len(data),
                        {'table': 'announcement_facts', 'rule': 'newest saved post per ECR kind'},
                        (['No saved ECR post of any kind is stored yet.'] if not data else
                         ['One row per kind; /ecr lists every saved ECR post.']))

    async def ecr_history(self, request):
        rows = self._history()
        kind = str(request.query.get('type') or '').strip().lower()
        name = str(request.query.get('name') or '').strip().lower()
        query = str(request.query.get('q') or '').strip().lower()
        since = _epoch_from_date(str(request.query.get('since') or '')) if request.query.get('since') else None
        until = _epoch_from_date(str(request.query.get('until') or '')) if request.query.get('until') else None
        for field, value in (('since', request.query.get('since')), ('until', request.query.get('until'))):
            if value and _epoch_from_date(str(value)) is None:
                raise web.HTTPBadRequest(reason=f'`{field}` must be a unix timestamp or a YYYY-MM-DD date (UTC).')
        data = []
        for row in rows:
            view = _history_view(row, self.manifest_path)
            if kind and str(view.get('type') or '').lower() != kind:
                continue
            if name and name not in str(view.get('name') or '').lower():
                continue
            if query and query not in json.dumps(view, ensure_ascii=False).lower():
                continue
            start = _epoch_from_date(str(view.get('start') or ''))
            if start is not None and ((since is not None and start < since) or (until is not None and start > until)):
                continue
            data.append(_project(view, request, HISTORY_FIELDS))
        total = len(data)
        limit, offset, order = _page_args(request, default_limit=25, ceiling=self.config['max_limit'])
        if order == 'ASC':
            data = list(reversed(data))
        missing = sum(1 for row in rows if not str(row.get('cps') or '').strip())
        return _respond(request, '/ecr/history', data[offset:offset + limit], total, offset, limit,
                        {'file': 'data/advisor/ecr_history_261.json', 'trust': 'community tracker history',
                         'pack': _manifest_note(self.manifest_path)},
                        ['A missing `cps` means the tracker recorded no cost line; it stays empty rather than '
                         f'estimated ({missing} of {len(rows)} rows are like this).',
                         'This is the historical record the bot ships with, not a live feed.'])

    async def ecr_rotations(self, request):
        rows = self._history()
        rotations = ecr_digest.rotations(rows)
        cadence = ecr_digest.cadence(rows)
        limit, offset, order = _page_args(request, default_limit=10, ceiling=self.config['max_limit'])
        data = [{'start': rotation.get('start_text'), 'end': rotation.get('end_text'), 'days': rotation.get('days'),
                 'inconsistent_dates': bool(rotation.get('inconsistent')), 'rows': len(rotation.get('rows') or []),
                 'named_items': len(rotation.get('named') or []), 'type_breakdown': rotation.get('types'),
                 'items': [{'name': row.get('name'), 'type': row.get('type'), 'items': row.get('items'),
                            'cps': row.get('cps'), 'rank': row.get('rank')}
                           for row in (rotation.get('rows') or [])][:40]}
                for rotation in rotations]
        if order == 'ASC':
            data = list(reversed(data))
        caveats = ['A rotation whose own start/end dates disagree is flagged `inconsistent_dates` rather than '
                   're-timed: silently re-dating a date would be inventing a schedule.']
        if cadence:
            caveats.append(f'Observed rhythm: median gap {cadence["median_days"]} days over {cadence["samples"]} '
                           f'samples; gaps over 60 days are recording gaps, so they are excluded.')
        else:
            caveats.append('No cadence is reported: the history does not contain enough consecutive start dates.')
        return _respond(request, '/ecr/rotations', data[offset:offset + limit], len(data), offset, limit,
                        {'file': 'data/advisor/ecr_history_261.json', 'grouping': 'mechbot/ecr_digest.rotations'},
                        caveats, extra={'cadence': cadence})

    async def events(self, request):
        store = self.store
        if store is None or not getattr(store, 'supports_announcement_facts', False):
            return _respond(request, '/events', [], 0, 0, 0, {'table': 'announcement_facts'},
                            ['The announcement_facts table has not been migrated in this deployment.'])
        allowed = tuple(category for category in fact_lib.CATEGORIES if category not in ('ecr', 'other'))
        where, args = [], []
        chosen = _in_filter(request, args, where, 'category', 'category', allowed)
        if not chosen:
            placeholders = ','.join(f'${len(args) + index + 1}' for index in range(len(allowed)))
            where.append(f'category IN ({placeholders})')
            args.extend(allowed)
            chosen = list(allowed)
        _time_filters(request, args, where)
        limit, offset, order = _page_args(request, default_limit=25, ceiling=self.config['max_limit'])
        rows = [_fact_view(row) for row in await self._facts(where, args, self.config['max_limit'] * 5, 0, order)]
        now = int(time.time())
        rows = dedupe_facts(refresh_ecr_status(rows, now=now))
        query = str(request.query.get('q') or '').strip().lower()
        data = []
        for row in rows:
            days = int((now - _as_int(row.get('posted_at'))) // 86400)
            window = _first_stated_window(row)
            view = {'fact_id': row.get('fact_id'), 'category': row.get('category'), 'title': row.get('title'),
                    'summary': row.get('summary'), 'entities': row.get('entities') or [],
                    'event_date_label': row.get('event_date_label') or '', 'date_basis': row.get('date_basis') or '',
                    'date_confirmed': row.get('date_confirmed'), 'posted_at': row.get('posted_at'),
                    'posted_at_utc': row.get('posted_at_utc'),
                    'age': fact_lib.age_label(row.get('posted_at'), now),
                    'standing': fact_lib._fact_standing(row, now),
                    # A yearless "March 1 to March 8" is a mention, not a window: turning it into timestamps
                    # would put a year here that the game never published, so `date_note` says why instead.
                    'date_note': row.get('date_note'),
                    'window': (None if not (window and row.get('date_confirmed')) else
                               {'start_utc': _iso(int(window[0])), 'end_utc': _iso(int(window[1])),
                                'state': _window_state(row, now), 'label': fact_lib._window_label(row, now)}),
                    'post_age_days': days, 'status': row.get('status'), 'channel_name': row.get('channel_name'),
                    'message_url': row.get('message_url'), 'details': row.get('details') or {}}
            if query and query not in json.dumps(view, ensure_ascii=False).casefold():
                continue
            data.append(view)
        total = len(data)
        return _respond(request, '/events', data[offset:offset + limit], total, offset, limit,
                        {'table': 'announcement_facts', 'categories': list(allowed)},
                        ['`standing` is derived by the same rule the bot’s channel answers use: the post’s age '
                         'against how long that kind of post speaks for the game, plus any window the post '
                         'states itself. `window` is only present when the post gave dates.',
                         'A date with `date_confirmed: false` was mentioned without a year; that is not presented '
                         'as a confirmed event date.'])

    async def forum(self, request):
        threads = self._threads()
        limit, offset, order = _page_args(request, default_limit=25, ceiling=self.config['max_limit'])
        category = str(request.query.get('category') or '').strip().lower()
        query = str(request.query.get('q') or '').strip().casefold()
        rows = []
        for thread in threads:
            if category:
                names = [str(((thread.get('category') or {}).get('name')) or '').lower()]
                names += [str(value).lower() for value in (((thread.get('category') or {}).get('also_in')) or [])]
                if not any(category in name for name in names):
                    continue
            if query and not _forum_match(thread, query):
                continue
            rows.append(_forum_view(thread))
        if order == 'ASC':
            rows = list(reversed(rows))
        data = [_project(row, request, FORUM_FIELDS) for row in rows[offset:offset + limit]]
        return _respond(request, '/forum', data, len(rows), offset, limit,
                        {'file': os.path.basename(str(self.forum_path or FORUM_ARCHIVE_PATH)),
                         'crawl': 'see /forum/meta for the window this covers'},
                        ['Forum timestamps are reproduced exactly as the site displayed them; the forum publishes '
                         'no timezone, so no UTC conversion is claimed.',
                         'This is each thread’s original post only - the crawl does not collect replies.',
                         'Community posts are reports, not verified game data: a number inside one is the author’s, '
                         'nobody else’s confirmation.'])

    async def forum_meta(self, request):
        threads = self._threads()
        path = os.path.abspath(str(self.forum_path or FORUM_ARCHIVE_PATH))
        file_note = {'path': path}
        metadata = {}
        try:
            stat = os.stat(path)
            file_note.update({'bytes': stat.st_size, 'modified_utc': _iso(int(stat.st_mtime))})
            with open(path, 'r', encoding='utf-8') as handle:
                raw = json.load(handle)
            if isinstance(raw, dict) and isinstance(raw.get('metadata'), dict):
                metadata = raw['metadata']
        except (OSError, UnicodeError, ValueError) as exc:
            file_note.update({'error': type(exc).__name__})
        sections = {}
        for thread in threads:
            name = str(((thread.get('category') or {}).get('name')) or 'uncategorised')
            sections[name] = sections.get(name, 0) + 1
        return _respond(request, '/forum/meta', [dict(metadata or {}, **{'threads_in_file': len(threads),
                                                                        'file': file_note})], 1, 0, 1,
                        {'file': os.path.basename(str(self.forum_path or FORUM_ARCHIVE_PATH))},
                        ['The bot refreshes this crawl on a schedule; the loop state is in the collector block of '
                         '/stats and of the index.'], extra={'sections': sections})

    async def stats(self, request):
        counts = await self._counts()
        store = self.store
        per_channel = []
        if store is not None and getattr(store, 'supports_announcements', False):
            try:
                rows = await store.fetch('SELECT channel_id,channel_name,COUNT(*) AS total,MAX(posted_at) AS latest '
                                         'FROM announcements GROUP BY channel_id,channel_name ORDER BY total DESC')
                for row in rows or []:
                    per_channel.append({'channel_id': str(row.get('channel_id')),
                                        'channel_name': row.get('channel_name'), 'posts': _as_int(row.get('total')),
                                        'newest_post_utc': _iso(row.get('latest'))})
            except Exception as exc:
                per_channel.append({'error': type(exc).__name__})
        fact_stats = []
        if store is not None and getattr(store, 'supports_announcement_facts', False):
            try:
                for row in await store.announcement_fact_stats():
                    fact_stats.append({'category': row.get('category'), 'extractor': row.get('extractor'),
                                       'total': _as_int(row.get('total')), 'latest_utc': _iso(row.get('latest'))})
            except Exception as exc:
                fact_stats.append({'error': type(exc).__name__})
        payload = {'counts': counts, 'per_channel': per_channel, 'facts_by_category': fact_stats,
                   'forum': {'threads': counts['forum_threads'],
                             'file': os.path.basename(str(self.forum_path or FORUM_ARCHIVE_PATH))},
                   'pack': {'history_rows': counts['ecr_history'], 'manifest': _manifest_note(self.manifest_path)},
                   'collector': await self._ingestion_state(), 'revision': await self._revision()}
        return web.json_response({'data': payload,
                                  'meta': _meta(request, '/stats', counts['announcements'] + counts['facts'], 1, 0,
                                                1, {'tables': ('announcements', 'announcement_facts')})})

    async def export(self, request):
        """Everything in one response, NDJSON by default, for anyone rebuilding a local copy."""
        dataset = str(request.query.get('dataset') or 'announcements').strip().lower()
        limit, offset, order = _page_args(request, default_limit=EXPORT_ROW_CAP, ceiling=EXPORT_ROW_CAP)
        if dataset == 'announcements':
            store = self.store
            if store is None or not getattr(store, 'supports_announcements', False):
                return _respond(request, '/export?dataset=announcements', [], 0, 0, 0, {'table': 'announcements'},
                                ['Nothing stored to export.'])
            rows = await _select(store, 'announcements', ANNOUNCEMENT_COLUMNS, [], [], order, limit, offset)
            data = [_announcement_view(row) for row in rows]
            source = {'table': 'announcements'}
            note = ['Original message text, one JSON object per line.']
        elif dataset == 'facts':
            data = [_fact_view(row) for row in await self._facts((), (), limit, offset, order)]
            source = {'table': 'announcement_facts'}
            note = ['Curated facts with their `details` blocks, one JSON object per line.']
        elif dataset == 'ecr_history':
            data = [_history_view(row, self.manifest_path) for row in self._history()]
            source = {'file': 'data/advisor/ecr_history_261.json'}
            note = ['Verbatim pack rows.']
        elif dataset == 'forum':
            data = [_forum_view(thread) for thread in self._threads()]
            source = {'file': os.path.basename(str(self.forum_path or FORUM_ARCHIVE_PATH))}
            note = ['Full original post text for every crawled thread.']
        else:
            raise web.HTTPBadRequest(reason='`dataset` must be one of: announcements, facts, ecr_history, forum.')
        # A dump is for a machine reading it line by line, so NDJSON is the default; `?format=json` still
        # works for a person who wants the meta block with it.
        wanted = str(request.query.get('format') or '').strip().lower()
        return _respond(request, f'/export?dataset={dataset}', data, len(data), 0, len(data), source, note,
                        extra={'dataset': dataset, 'export': True}, fmt=wanted or 'ndjson')

    # -- server lifecycle ------------------------------------------------------------------
    def create_app(self):
        app = web.Application(middlewares=[self._guards])
        app[APP_CONFIG] = self.config
        app[APP_AUTH] = bool(self.config['token'])
        app.router.add_get('/', self.root)
        app.router.add_get(API_PATH + '/view', self.view)
        app.router.add_get(API_PATH, self.index)
        app.router.add_get(API_PATH + '/', self.index)
        app.router.add_get(API_PATH + '/announcements', self.announcements)
        app.router.add_get(API_PATH + '/announcements/{message_id}', self.announcement_one)
        app.router.add_get(API_PATH + '/facts', self.fact_rows)
        app.router.add_get(API_PATH + '/ecr', self.ecr)
        app.router.add_get(API_PATH + '/ecr/current', self.ecr_current)
        app.router.add_get(API_PATH + '/ecr/history', self.ecr_history)
        app.router.add_get(API_PATH + '/ecr/rotations', self.ecr_rotations)
        app.router.add_get(API_PATH + '/events', self.events)
        app.router.add_get(API_PATH + '/forum', self.forum)
        app.router.add_get(API_PATH + '/forum/meta', self.forum_meta)
        app.router.add_get(API_PATH + '/stats', self.stats)
        app.router.add_get(API_PATH + '/export', self.export)
        # `/health` for a deployment's health check, and the same route under the API prefix so a consumer
        # that only knows `base_url` can probe it too. Both answer with state and counts, never with a row.
        app.router.add_get('/health', self.health)
        app.router.add_get(API_PATH + '/health', self.health)
        return app

    async def health(self, request):
        return web.json_response({'ok': True, 'service': 'archangle-data-api', 'read_only': True,
                                  'as_of_utc': _iso(int(time.time())), 'revision': await self._revision()})

    @web.middleware
    async def _guards(self, request, handler):
        """Method, auth, CORS, caching and error shape in one place.

        Every failure answers as JSON: a consumer that asked for a dataset and got an HTML exception page
        cannot tell whether the data is missing or the API is broken.
        """
        if request.method == 'OPTIONS':
            return web.Response(status=204, headers=self._cors(request))
        if request.method != 'GET':
            return web.json_response({'error': 'read-only: only GET is served here', 'method': request.method},
                                     status=405, headers={'Allow': 'GET, OPTIONS'})
        token = self.config['token']
        if token and request.path not in EXEMPT_PATHS:
            header = str(request.headers.get('Authorization') or '')
            supplied = header[7:].strip() if header[:7].strip().lower() == 'bearer' else \
                str(request.query.get('token') or '').strip()
            if not hmac.compare_digest(supplied, token):
                return web.json_response({'error': 'this deployment requires a reader token',
                                          'how': 'send `Authorization: Bearer <token>`',
                                          'note': 'the token is only a reader key; nothing here is writable'},
                                         status=401)
        try:
            response = await handler(request)
            etag = None
            if request.path not in EXEMPT_PATHS:
                etag = await self._revision()
        except web.HTTPException as exc:
            response = web.json_response({'error': str(getattr(exc, 'reason', '') or exc.text or exc.status),
                                          'path': getattr(exc, 'path', request.path)}, status=exc.status)
            etag = None
        except Exception as exc:
            response = web.json_response({'error': f'the data API could not answer: {type(exc).__name__}',
                                          'path': request.path}, status=500)
            etag = None
        for key, value in self._cors(request).items():
            response.headers[key] = value
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Boundary'] = 'public-game-data-only'
        response.headers['Cache-Control'] = (f'public, max-age={self.config["cache_seconds"]}'
                                             if self.config['cache_seconds'] else 'no-store')
        if etag:
            fresh = f'W/"{etag}"'
            if str(request.headers.get('If-None-Match') or '').strip() == fresh:
                return web.Response(status=304, headers={'ETag': fresh,
                                                         'Cache-Control': response.headers['Cache-Control']})
            response.headers['ETag'] = fresh
        return response

    def _cors(self, request):
        origin = self.config['allow_origin']
        shared = {'Access-Control-Allow-Headers': 'Authorization, Accept', 'Access-Control-Allow-Methods': 'GET, OPTIONS'}
        if origin == '*':
            return {'Access-Control-Allow-Origin': '*', **shared}
        allowed = [item.strip() for item in origin.split(',') if item.strip()]
        requested = str(request.headers.get('Origin') or '')
        if requested and requested in allowed:
            return {'Access-Control-Allow-Origin': requested, 'Vary': 'Origin', **shared}
        return dict(shared)

    async def start(self):
        self.config = config() if not self.config else self.config
        if not self.config['enabled']:
            return None
        self.started_at = time.time()
        self.runner = web.AppRunner(self.create_app(), access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config['host'], self.config['port'])
        await self.site.start()
        return self._base_url()

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = self.site = None
