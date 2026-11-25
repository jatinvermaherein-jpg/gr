"""Curated ECR and promo-code digests built from data the bot already ships with.

Why this module exists
----------------------
The ECR and promo-code answers were the weakest replies in the bot, and for a
structural reason: both were reading the *announcement* store, which is empty in
every deployment that has not run a live ingest. So "what is the current ECR"
found nothing and fell through to "no ECR guide post in the saved announcements",
even though the repository ships 261 rows of ECR history and two complete promo
code posts.

Nothing here scrapes, invents or re-times anything. Every figure comes from one of
two pinned, hash-verified sources:

* ``data/advisor/ecr_history_261.json`` — 139 rotations over 261 item slots, trust
  tier ``community``. Real observed per-spin prices (``350 ac``, ``30000 cr``) and
  observed pool sizes, not estimates.
* ``archive_documents`` rows 64 and 81 — two forum posts written by players that
  list the actual promotion codes. After the boot promotion their trust is
  ``user_confirmed``.

The honesty rule is the same one the rest of the bot follows: where the data runs
out, say so. 35 of the rotations are logged as ``"name": "?"`` — the tracker kept
the dates and the item *types* but not the names. They are reported as unnamed,
never guessed. The newest *named* rotation is usually months old, so the reply says
that in the first line rather than implying anything is live.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, OrderedDict
from datetime import date

# Field-level trust, shown to the user exactly as the pack declares it.
ECR_TRUST = 'community'
PROMO_TRUST = 'player-posted, user-confirmed'

_ITEM_TYPES = {'mech': 'mech', 'weapon': 'weapon', 'pilot': 'pilot', 'resource': 'resource'}

# "code - rewards", "**CODE** - rewards", "CODE: rewards". The dash is what the old
# extractor missed: it only accepted an explicit "promo code" or "code" keyword
# immediately before the token, so a post written as one bare code per line yielded
# nothing at all and the bot reported that no code was "clearly labeled".
_CODE_LINE = re.compile(r'^[\s\-•*_#>]*(?:\*\*)?([A-Z0-9][A-Z0-9_\-]{3,31})(?:\*\*)?\s*[-–—:=]\s*(.+)$')
_AUDIENCE = re.compile(r'^\s*#*\s*(?:for\s+)?(new players?|returning players?|everyone|all players?)\s*:?\s*$', re.I)
_AVAILABILITY = re.compile(r'\(\s*(?:available\s+)?(?:till|until|through)\s+([^)]{3,40})\)', re.I)

# Words that look like a code at the start of a line ("SOURCES: see the thread") but are
# prose. Real promotion codes are never any of these.
_NOT_A_CODE = {'NOTE', 'NOTES', 'DISCLAIMER', 'SOURCE', 'SOURCES', 'EDIT', 'EDITS', 'UPDATE',
               'UPDATED', 'SORTED', 'THESE', 'THIS', 'CODE', 'CODES', 'PROMO', 'PROMOS', 'HERE',
               'BELOW', 'EXAMPLE', 'IMPORTANT', 'CAUTION', 'WARNING', 'NOTICE', 'TIP', 'TIPS',
               'STEPS', 'LINK', 'LINKS', 'THREAD', 'LIST', 'GUIDE', 'RULES', 'READ', 'REFER',
               'SEE', 'MORE', 'ALSO', 'THANKS', 'CREDITS:', 'PLEASE'}


# --------------------------------------------------------------------------- helpers
def _as_date(value):
    """Parse the pack's own date shape (``2026-12-7``, no zero padding) without guessing."""
    text = str(value or '').strip()
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _span_days(start, end):
    if not start or not end:
        return None
    days = (end - start).days
    return days + 1 if days >= 0 else None


def _price_text(row):
    """Render the observed per-spin price, separating the three kinds the tracker mixes.

    ``cps`` means "cost per spin". ``350 ac`` and ``30000 cr`` are real observed game
    prices, ``$1`` is a real-money offer, and ``inf`` marks an unlimited-quantity pool.
    They are not interchangeable and are never summed.
    """
    raw = str(row.get('cps') or '').strip()
    if not raw:
        return ''
    match = re.fullmatch(r'(\d[\d,]*)\s*(ac|cr)', raw, re.I)
    if match:
        amount = int(match[1].replace(',', ''))
        unit = 'A-Coins' if match[2].lower() == 'ac' else 'Credits'
        return f'{amount:,} {unit}/spin'
    if raw.startswith('$'):
        return f'{raw} cash offer'
    return ''


def _pool_text(row):
    """The observed pool size, when the tracker recorded one."""
    items = row.get('items')
    if items == 'inf':
        return 'unlimited pool'
    if items in (None, ''):
        return ''
    try:
        count = int(float(items))
    except (TypeError, ValueError):
        return ''
    return f'{count}-item pool' if count else ''


def _rank_text(row):
    rank = row.get('rank')
    if rank in (None, '', 'N/A'):
        return ''
    return f'rank {rank}'


def _chance_text(row):
    """Only 21 rows carry a chance figure; keep the source's own spacing rather than reformat it."""
    value = str(row.get('chances') or '').strip()
    if not value:
        return ''
    return f'{re.sub(r"\s+", "", value)} drop chance'


def _detail_bits(row):
    return ' · '.join(x for x in (_rank_text(row), _price_text(row), _pool_text(row), _chance_text(row)) if x)


def _type_breakdown(rows):
    """'1 mech + 1 weapon' — what the pool was made of, from the tracker's type field."""
    counts = Counter(_ITEM_TYPES.get(str(r.get('type') or '').strip().lower(),
                                     str(r.get('type') or '').strip()) for r in rows)
    parts = [f'{n} {kind}{"" if n == 1 else ("es" if kind.endswith("s") else "s")}'
             for kind, n in counts.most_common() if kind]
    return ' + '.join(parts)


def _item_label(row):
    name = str(row.get('name') or '').strip()
    kind = _ITEM_TYPES.get(str(row.get('type') or '').strip().lower(),
                           str(row.get('type') or '').strip())
    if not name or name == '?':
        return f'an unnamed {kind or "item"} slot'
    return f'{name} ({kind})' if kind else name


# --------------------------------------------------------------------------- rotations
def rotations(rows):
    """Group the flat history into rotations, newest first.

    A rotation is one (start, end) pair. Rows that disagree with themselves are marked
    rather than fixed: silently re-timing a date would be inventing a schedule.
    """
    grouped = OrderedDict()
    for row in rows or ():
        key = (row.get('start') or '', row.get('end') or '')
        grouped.setdefault(key, {'start_text': key[0], 'end_text': key[1],
                                 'start': _as_date(key[0]), 'end': _as_date(key[1]), 'rows': []})
        grouped[key]['rows'].append(row)
    out = list(grouped.values())
    for rotation in out:
        rotation['days'] = _span_days(rotation['start'], rotation['end'])
        rotation['inconsistent'] = (rotation['days'] is None
                                    and bool(rotation['start']) and bool(rotation['end']))
        rotation['named'] = [r for r in rotation['rows'] if str(r.get('name') or '?') != '?']
        rotation['types'] = _type_breakdown(rotation['rows'])
    out.sort(key=lambda r: ((r['end'] or date.min), (r['start'] or date.min)), reverse=True)
    return out


def cadence(rows):
    """Median gap between consecutive rotation start dates, reported as observed rhythm.

    Gaps over 60 days are dropped: they are recording gaps in the tracker, not real
    pauses in the schedule, and including them would invent a length nobody observed.
    """
    starts = sorted({d for d in (_as_date(r.get('start')) for r in rows or ()) if d})
    gaps = [(b - a).days for a, b in zip(starts, starts[1:]) if 0 < (b - a).days <= 60]
    if not gaps:
        return None
    gaps.sort()
    middle = len(gaps) // 2
    median = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
    return {'median_days': median, 'samples': len(gaps),
            'shortest': gaps[0], 'longest': gaps[-1]}


def rotation_block(rotation, include_header=True):
    """One readable paragraph per rotation: when it ran, what was in it, what it cost."""
    lines = []
    if include_header:
        start, end = rotation['start_text'], rotation['end_text']
        if rotation['inconsistent']:
            lines.append(f'**{start} → {end}** *(the tracker\'s own dates disagree; shown as recorded)*')
        elif rotation['days']:
            plural = 's' if rotation['days'] != 1 else ''
            lines.append(f'**{start} → {end}** · {rotation["days"]} day{plural}')
        else:
            lines.append(f'**{start}** *(no end date recorded)*')
    rows = rotation['rows']
    if rotation['named']:
        for row in rows:
            detail = _detail_bits(row)
            lines.append(f'• {_item_label(row)}' + (f' — {detail}' if detail else ''))
    else:
        lines.append(f'• Nothing named. The tracker logged {rotation["types"]} in this window '
                     f'and did not record what they were.')
    return '\n'.join(lines)


def rotation_digest(rows, *, limit=3, now=None):
    """The 'what has the Crate Rush actually been' answer, newest named rotations first."""
    everything = rotations(rows)
    if not everything:
        return None
    today = now or date.today()
    named = [r for r in everything if r['named']]
    unnamed = [r for r in everything if not r['named']]
    rhythm = cadence(rows)
    total_slots = sum(len(r['rows']) for r in everything)

    out = [f'**Crate Rush (ECR) history on record** · {len(everything)} rotations · '
           f'{total_slots} item slots · trust: {ECR_TRUST}']

    if named:
        newest_named = named[0]
        end = newest_named['end'] or newest_named['start']
        stale_days = (today - end).days if end else None
        line = (f'The most recent rotation the tracker recorded **by name** ran '
                f'**{newest_named["start_text"]} → {newest_named["end_text"]}**')
        if stale_days and stale_days > 0:
            line += (f' — {stale_days} days ago. Items and prices below are what that rotation '
                     f'actually cost; I cannot tell you what is live right now.')
        out.append(line)

    shown = named[:max(1, int(limit))]
    if shown:
        out.append(f'**The {len(shown)} most recent named rotation'
                   f'{"" if len(shown) == 1 else "s"}**')
        for rotation in shown:
            out.append(rotation_block(rotation))

    if unnamed:
        span = f'{min(r["start_text"] for r in unnamed)} → {max(r["start_text"] for r in unnamed)}'
        out.append(f'**After that the names stop.** {len(unnamed)} further rotation'
                   f'{"" if len(unnamed) == 1 else "s"} ({span}) are on record with the dates and '
                   f'the item types only — the tracker did not record which items they contained, '
                   f'and I will not invent them. That is why "what is the current ECR" has no '
                   f'named answer: the newest named entry is '
                   f'{named[0]["start_text"] if named else "earlier"}.')

    if rhythm:
        median = f'{rhythm["median_days"]:g}'
        out.append(f'**How often it turns over:** a new rotation started every **{median} days** '
                   f'at the middle of the range ({rhythm["samples"]} gaps observed, '
                   f'{rhythm["shortest"]}–{rhythm["longest"]} days). A typical rotation lists two '
                   f'items, usually one vehicle or pilot and one weapon.')
    return '\n\n'.join(out)


# --------------------------------------------------------------------------- one item
def item_history(name, rows, *, limit=6):
    """Every recorded appearance of one item, with its observed price each time."""
    wanted = str(name or '').strip().casefold()
    if not wanted:
        return None
    hits = [r for r in rows or () if str(r.get('name') or '').strip().casefold() == wanted]
    if not hits:
        return None
    hits.sort(key=lambda r: _as_date(r.get('start')) or date.min, reverse=True)
    priced = [r for r in hits if _price_text(r)]

    out = [f'**{hits[0].get("name")}** has appeared in **{len(hits)} recorded Crate Rush'
           f'{"" if len(hits) == 1 else "es"}** · trust: {ECR_TRUST}']
    for row in hits[:max(1, int(limit))]:
        detail = _detail_bits(row)
        skew = ' · flagged as skewed pricing' if row.get('skewed') else ''
        out.append(f'• {row.get("start") or "?"} → {row.get("end") or "?"}'
                   + (f' — {detail}' if detail else '') + skew)
    if len(hits) > int(limit):
        out.append(f'*{len(hits) - int(limit)} older appearance(s) not shown.*')
    if len(priced) > 1 and _price_text(priced[-1]) != _price_text(priced[0]):
        out.append(f'**Price trend:** {_price_text(priced[-1])} in {priced[-1].get("start")} → '
                   f'{_price_text(priced[0])} in {priced[0].get("start")}.')
    skewed = sum(1 for r in hits if r.get('skewed'))
    if skewed:
        out.append(f'*{skewed} of {len(hits)} appearances are flagged as skewed pricing, so the '
                   f'Crate Rush was not good value against the Gear Hub in those weeks.*')
    out.append('*The data does not announce future appearances, so I cannot say when it comes back.*')
    return '\n'.join(out)


def resolve_item(question, rows):
    """Find the item a question is about, using the tracker's own names.

    Longest name first so "Oracle 6" wins over "Oracle", and the name has to appear as a
    whole token so "art" cannot match "Parasite".
    """
    text = f' {str(question or "").casefold()} '
    names = sorted({str(r.get('name') or '') for r in rows or () if str(r.get('name') or '?') != '?'},
                   key=len, reverse=True)
    for name in names:
        if re.search(r'(?<![\w-])' + re.escape(name.casefold()) + r'(?![\w-])', text):
            return name
    return None


# --------------------------------------------------------------------------- promo codes
def _promo_documents(catalog=None, db_path=None):
    """Read the player-written code posts out of the confirmed archive, read-only.

    Only rows that already promoted to ``review_status='verified'`` are eligible, which
    is the same gate the rest of the bot applies to archive content.
    """
    sql = """SELECT title,url,content,trust,review_status,category,source
             FROM archive_documents
             WHERE review_status='verified'
               AND source='plarium_forum'
               AND COALESCE(category,'') NOT IN ('script','client_source','sitemap','rules')
               AND (LOWER(title) LIKE '%promo%' OR LOWER(title) LIKE '%code%')
             ORDER BY id"""
    if catalog is not None and getattr(catalog, 'db', None) is not None:
        try:
            return [dict(r) for r in catalog.db.execute(sql).fetchall()]
        except sqlite3.Error:
            return []
    if db_path:
        try:
            with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                return [dict(r) for r in db.execute(sql)]
        except sqlite3.Error:
            return []
    return []


_BARE_CODE = re.compile(r'^[\s*_#>]*(?:\*\*)?([A-Z0-9][A-Z0-9_\-]{3,31})(?:\*\*)?[\s*_]*$')
_LEADING_AVAILABILITY = re.compile(r'^\s*(\([^)]{3,60}\))\s*[-–—:]\s*(.+)$')


def _rejoin_wrapped_codes(lines):
    """Put a code back on the same line as its rewards when the post wrapped it.

    The archived post has ``MIDGAMECHANNEL4ALL`` alone on one line and
    ``(available till 1st Dec) - Grants you ...`` on the next, which is why the
    availability window was being lost.
    """
    out, index = [], 0
    while index < len(lines):
        line, nxt = lines[index], lines[index + 1] if index + 1 < len(lines) else ''
        bare = _BARE_CODE.match(line)
        joined = _LEADING_AVAILABILITY.match(nxt)
        if bare and joined:
            out.append(f'{bare[1]} - {joined[1]} {joined[2]}')
            index += 2
            continue
        if bare and nxt.strip().startswith(('-', '–', '—', ':')):
            out.append(f'{bare[1]} {nxt.strip()}')
            index += 2
            continue
        out.append(line)
        index += 1
    return out


def extract_codes(text):
    """Pull ``CODE — rewards`` pairs out of a forum post, grouped by audience."""
    codes, audience = [], ''
    for raw_line in _rejoin_wrapped_codes(str(text or '').splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        heading = _AUDIENCE.match(line)
        if heading:
            audience = heading[1].strip().title()
            continue
        match = _CODE_LINE.match(line)
        if not match:
            continue
        token = match[1].strip('*-_ ')
        if len(token) < 4 or token in _NOT_A_CODE:
            continue
        if any(ch.islower() for ch in token):
            continue
        reward = re.sub(r'\s+', ' ', match[2]).strip(' .*')
        reward = re.sub(r'\brank(\d)', r'Rank \1', reward, flags=re.I)
        availability = ''
        found = _AVAILABILITY.search(line)
        if found:
            availability = found[1].strip()
            reward = _AVAILABILITY.sub('', reward).strip(' .,')
        if not reward:
            continue
        entry = {'code': token, 'rewards': reward, 'audience': audience or 'Unspecified',
                 'availability': availability}
        if not any(c['code'] == token and c['audience'] == entry['audience'] for c in codes):
            codes.append(entry)
    return codes


def promo_digest(catalog=None, db_path=None, *, per_audience=8):
    """What each code gives you, exactly as the player post words it, grouped by audience."""
    documents = _promo_documents(catalog=catalog, db_path=db_path)
    merged = OrderedDict()
    used_documents = []
    for document in documents:
        found = extract_codes(document.get('content') or '')
        if found and document not in used_documents:
            used_documents.append(document)
        for entry in found:
            entry['source_title'] = document.get('title') or 'saved forum post'
            entry['url'] = document.get('url') or ''
            key = (entry['code'], entry['audience'])
            previous = merged.get(key)
            # Keep whichever version carries the availability window: that detail is the
            # only part of the code's status the posts ever state.
            if previous and previous.get('availability') and not entry['availability']:
                continue
            merged[key] = entry
    if not merged:
        return None

    # A post that repeats a code under both a real heading and no heading is the same
    # code twice; the specific audience wins and the bare repeat is dropped.
    specific = {entry['code'] for entry in merged.values() if entry['audience'] != 'Unspecified'}
    ordered = [entry for entry in merged.values()
               if not (entry['audience'] == 'Unspecified' and entry['code'] in specific)]
    groups = OrderedDict()
    for entry in ordered:
        groups.setdefault(entry['audience'], []).append(entry)

    out = [f'**Promo codes from the saved forum posts** · trust: {PROMO_TRUST}',
           '*A player wrote each of these up on the Plarium forum. Plarium has not confirmed them '
           'and any of them may have expired since the post was written.*']
    for audience in ('New Players', 'Returning Players', 'Everyone', 'Unspecified'):
        entries = groups.get(audience) or []
        if not entries:
            continue
        out.append(f'**{audience}**')
        for entry in entries[:max(1, int(per_audience))]:
            line = f'`{entry["code"]}` — {entry["rewards"]}'
            if entry['availability']:
                line += f' *(post says available until {entry["availability"]})*'
            out.append(f'• {line}')
        if len(entries) > int(per_audience):
            out.append(f'*+{len(entries) - int(per_audience)} more in this group.*')
    out.append('**The catch the post stresses:** a new or returning account has 24 hours from '
               'account creation to claim, and can redeem only **one** of the new-player codes — '
               'claiming one blocks the others. The post lists them in value order.')
    sources = []
    for document in used_documents:
        title = str(document.get('title') or '').strip()
        if title and title not in sources:
            sources.append(title)
    if sources:
        out.append('*Source: ' + '; '.join(sources[:3]) + ' (saved Plarium forum posts).*')
    return '\n'.join(out)


# --------------------------------------------------------------------------- combined
def ecr_answer(question, ecr_rows):
    """Route an ECR question to the item view when an item is named, else the rotation view."""
    name = resolve_item(question, ecr_rows)
    if name:
        block = item_history(name, ecr_rows)
        if block:
            return block
    return rotation_digest(ecr_rows)
