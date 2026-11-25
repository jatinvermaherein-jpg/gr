"""One renderer for every Crate Rush rotation, whether it was typed by hand or ingested.

The bot answers ECR questions from three places, and each used to render differently:

* ``data/advisor/ecr_history_261.json`` — the vendored tracker, 139 rotations, shallow
  fields only (name, type, start, end, items, cps, rank, skewed, sometimes chances).
* ``wechbot.facts.build_facts`` — announcements ingested from Discord, with rich
  ``details.entries`` (a note per item, sometimes a buy/skip line) and stated dates.
* The raw announcement rows, when the fact table has not been dumped yet.

A player does not care which of those an answer came from. They care about: is it live,
what was in it, what did a spin cost, was it worth it, and is the data current. So this
module takes *either* shape and produces the same five-part block:

    **<host item> · <kind> · <dates> · <status>**
    • what is in it, one line per item, with the post's own note
    • what a spin costs, and the pool size
    • the value read against the Gear Hub price, when both sides are known
    • one honest line about how stale the entry is

Nothing is computed from a figure the source did not state. Where a value is missing the
line is omitted, not filled with a guess.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

try:                                     # facts.py already owns date parsing; reuse it
    from .facts import _as_date as _facts_as_date  # type: ignore[attr-defined]
except Exception:                        # pragma: no cover - facts always has it
    _facts_as_date = None

MAX_ITEMS = 8


# --------------------------------------------------------------------------- value helpers
def _date(value):
    """Accept a date, an epoch, an ISO string, or the tracker's unpadded ``2026-12-7``."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}', text):
        year, month, day = text.split('-')
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None
    if re.fullmatch(r'\d{9,11}', text):
        return _date(int(text))
    for shape in ('%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(text, shape).date()
        except ValueError:
            continue
    return None


def _num(value):
    if value is None or value == '':
        return None
    try:
        return int(float(str(value).replace(',', '').strip()))
    except (TypeError, ValueError):
        return None


def _fmt_date(value):
    parsed = _date(value)
    return parsed.strftime('%d %b %Y').lstrip('0') if parsed else str(value or '')


def _fmt_span(start, end):
    """Human window: full dates, collapsing the year when both ends share it."""
    a, b = _date(start), _date(end)
    if a and b:
        if a == b:
            return f'{a.strftime("%d %b %Y").lstrip("0")} · one day'
        if a.year == b.year:
            return (f'{a.strftime("%d %b").lstrip("0")} – '
                    f'{b.strftime("%d %b %Y").lstrip("0")}')
        return f'{a.strftime("%d %b %Y").lstrip("0")} – {b.strftime("%d %b %Y").lstrip("0")}'
    if a:
        return f'from {a.strftime("%d %b %Y").lstrip("0")}'
    return ''


def _days(start, end):
    a, b = _date(start), _date(end)
    if not a or not b:
        return None
    span = (b - a).days
    return span + 1 if span >= 0 else None


def _money(amount, currency):
    if amount is None:
        return ''
    unit = {'ac': 'A-Coins', 'acoins': 'A-Coins', 'cr': 'Credits', 'credits': 'Credits'}.get(
        str(currency or '').lower(), str(currency or '').title())
    return f'{amount:,} {unit}'


def _price_from_cps(raw):
    """Split the tracker's per-spin figure into (text, currency) without conflating kinds."""
    text = str(raw or '').strip()
    if not text:
        return '', ''
    match = re.fullmatch(r'(\d[\d,]*)\s*(ac|cr)', text, re.I)
    if match:
        currency = 'ac' if match[2].lower() == 'ac' else 'cr'
        return _money(_num(match[1]), currency), currency
    if text.startswith('$'):
        return f'{text} cash', 'cash'
    return '', ''


# --------------------------------------------------------------------------- normalising
def from_tracker_row(row):
    """Shape one ``ecr_history_261.json`` row."""
    price, currency = _price_from_cps(row.get('cps'))
    items = row.get('items')
    pool = None
    try:
        pool = int(float(items)) if items not in (None, '', 'inf') else None
    except (TypeError, ValueError):
        pool = None
    return {'name': str(row.get('name') or '').strip(),
            'kind': _IMMEDIATE.get(str(row.get('type') or '').strip().lower(),
                                     str(row.get('type') or '').strip()),
            'start': row.get('start'), 'end': row.get('end'),
            'price': price, 'price_currency': currency,
            'pool': pool, 'unlimited': items == 'inf',
            'rank': row.get('rank') if row.get('rank') not in ('N/A', '') else None,
            'chance': re.sub(r'\s+', '', str(row.get('chances') or '')),
            'skewed': bool(row.get('skewed')),
            'note': '', 'advice': '', 'source': 'tracker'}


def from_fact(fact):
    """Shape one ingested announcement fact into the same rotation records.

    ``build_facts`` already parsed the post's ``##`` headings into ``details.entries``, each
    with the label and the note written underneath it. Those notes are the most useful thing
    in the post — they carry rank, price and often the buy/skip line — so they are kept.
    """
    details = fact.get('details') or {}
    posted = fact.get('posted_at')
    entries = [e for e in (details.get('entries') or []) if not e.get('section')]
    stated = details.get('stated_dates') or []
    start = stated[0] if stated else posted
    end = stated[1] if len(stated) > 1 else None
    records, advice = [], ''
    # A post usually features two items; each becomes a record sharing the rotation window.
    for entry in entries[:MAX_ITEMS]:
        label = str(entry.get('label') or '').strip()
        if not label:
            continue
        if _ADVICE_LABEL.match(label):
            # "Buy or skip" is the post's verdict on the rotation, not a third item.
            advice = (str(entry.get('note') or '').strip() or label)
            continue
        note = str(entry.get('note') or '').strip()
        rank = _rank_in(note)
        records.append({'name': label, 'kind': _kind_in(note) or '',
                        'start': start, 'end': end,
                        'price': _price_in(note), 'price_currency': _currency_in(note),
                        'pool': None, 'unlimited': False, 'rank': rank,
                        'chance': '', 'skewed': False,
                        'note': note, 'advice': str(entry.get('advice') or ''),
                        'source': 'announcement',
                        'kind_label': fact.get('ecr_kind') or ''})
    if records:
        if advice:
            for record in records:
                record['rotation_advice'] = advice
        return records
    # A one-day notice names no items. Keep the post itself so it is not lost.
    return [{'name': str(fact.get('title') or 'Crate Rush').strip(),
             'kind': 'one_day', 'start': start, 'end': end, 'price': '', 'price_currency': '',
             'pool': None, 'unlimited': False, 'rank': None, 'chance': '', 'skewed': False,
             'note': str(fact.get('summary') or '').strip()[:300], 'advice': '',
             'source': 'announcement', 'kind_label': fact.get('ecr_kind') or ''}]


# Headings that carry the post's verdict or housekeeping rather than an item.
_ADVICE_LABEL = re.compile(
    r'^(?:buy\s*(?:or|\/)\s*skip|skip\s*(?:it)?|should\s+you|verdict|is\s+it\s+worth|'
    r'worth\s+it|tl;?dr|summary|stay\s+updated|stay\s+ahead|good\s+luck|'
    r'rules?|how\s+it\s+works|details|notes?)\b', re.I)

_IMMEDIATE = {'mech': 'mech', 'weapon': 'weapon', 'pilot': 'pilot', 'resource': 'resource'}


def _and_list(values):
    """'a, b and c' — never 'a and b and c'."""
    items = [str(v) for v in values if v]
    if not items:
        return 'item'
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def _kind_in(text):
    for word, label in (('weapon', 'weapon'), ('mech', 'vehicle'), ('pilot', 'pilot')):
        if re.search(rf'\b{word}\b', text or '', re.I):
            return label
    return ''


def _rank_in(text):
    match = re.search(r'\brank\s*(\d{1,2})\b', text or '', re.I)
    return int(match[1]) if match else None


def _price_in(text):
    match = re.search(r'\b(\d[\d,]{2,})\s*(a-?coins?|credits?|cr)\b', text or '', re.I)
    return _money(_num(match[1]), 'ac' if match[2].lower().startswith('a') else 'cr') if match else ''


def _currency_in(text):
    match = re.search(r'\b\d[\d,]{2,}\s*(a-?coins?|credits?|cr)\b', text or '', re.I)
    return ('ac' if match[1].lower().startswith('a') else 'cr') if match else ''


# --------------------------------------------------------------------------- rendering
def group(records):
    """Collapse records into rotations keyed by window, newest first."""
    buckets = {}
    for record in records or ():
        key = (str(record.get('start') or ''), str(record.get('end') or ''))
        buckets.setdefault(key, []).append(record)
    rotations = []
    for (start, end), rows in buckets.items():
        start_d, end_d = _date(start), _date(end)
        rotations.append({'start': start, 'end': end, 'start_date': start_d, 'end_date': end_d,
                          'rows': rows, 'days': _days(start, end),
                          'named': [r for r in rows if r.get('name') and r['name'] != '?']})
    rotations.sort(key=lambda r: (r['end_date'] or r['start_date'] or date.min), reverse=True)
    return rotations


def _status_line(rotation, today):
    start, end = rotation['start_date'], rotation['end_date']
    if start and start > today:
        return f'scheduled from {_fmt_date(start)}'
    if start and end and end >= today:
        return f'live now · ends {_fmt_date(end)}'
    if end and end < today:
        age = (today - end).days
        return f'ended {_fmt_date(end)}' + (f' · {age} day{"s" if age != 1 else ""} ago' if age else '')
    if start:
        return f'posted {_fmt_date(start)} · no end date stated'
    return 'no dates stated'


def _clean_note(note, record):
    """Drop the parts of a post's note that only restate the figures already shown.

    An ingested post writes "Burrow Beam 8 is the weapon pick. Rank 5. 30,000 Credits per
    spin." The rank and price are already on the detail line, so leaving them in the note
    prints the same numbers twice and makes the block look machine-generated.
    """
    text = re.sub(r'\s+', ' ', str(note or '')).strip()
    if record.get('name'):
        text = re.sub(r'^' + re.escape(record['name']) + r'\s+', '', text, flags=re.I)
    if record.get('price'):
        amount = record['price'].split()[0]
        text = re.sub(rf'\b{re.escape(amount)}\s*(?:a-?coins?|credits?)\b\.?', '', text, flags=re.I)
    if record.get('rank'):
        text = re.sub(rf'\brank\s*{record["rank"]}\b\.?', '', text, flags=re.I)
    text = re.sub(r'\bper\s+spin\b\.?', '', text, flags=re.I)
    text = re.sub(r'\s{2,}', ' ', text).strip(' .;,')
    # Removing the leading name can leave "is the weapon pick" with no subject.
    if re.match(r'^(?:is|are|was|were|returns?|comes?|has|have)\b', text, re.I):
        return ''
    return text


def _rotation_block(rotation, today, *, verbose=True):
    """The five-part block: header, items, price, value, freshness."""
    rows = rotation['named'] or rotation['rows']
    host = next((r for r in rows if r.get('kind') in ('vehicle', 'mech', 'pilot')), None)
    others = [r for r in rows if r is not host]
    head_bits = []
    if host and host['name'] != '?':
        head_bits.append(f'**{host["name"]}**')
        if host.get('kind'):
            head_bits.append(host['kind'])
        if len(others) == 1 and others[0]['name'] != '?':
            head_bits.append(f'with {others[0]["name"]}')
        elif len(others) > 1:
            head_bits.append(f'with {len(others)} weapons')
    else:
        named = [r['name'] for r in rows if r['name'] != '?'][:3]
        head_bits.append('**' + ' + '.join(named) + '**' if named else '**Rotation with no item names recorded**')
    span = _fmt_span(rotation['start'], rotation['end'])
    if span and rotation['end_date']:
        head_bits.append(span)
    head_bits.append(_status_line(rotation, today))
    lines = [' · '.join(head_bits)]

    if not rotation['named']:
        kinds = _and_list(sorted({r.get('kind') for r in rows if r.get('kind')}))
        lines.append(f'• The tracker logged a {kinds} rotation but not which item.')
        return '\n'.join(lines)

    single = len(rows) == 1

    for record in rows:
        detail = ' · '.join(x for x in (
            f'rank {record["rank"]}' if record.get('rank') else '',
            record.get('price') or '',
            f'{record["pool"]}-item pool' if record.get('pool') else '',
            'unlimited pool' if record.get('unlimited') else '',
            f'{record["chance"]} to pull' if record.get('chance') else '') if x)
        if single:
            # The heading already named it; repeating the name here just wastes the line.
            lines.append(f'• {detail}' if detail else '')
        else:
            kind = f' ({record["kind"]})' if record.get('kind') else ''
            lines.append(f'• **{record["name"]}**{kind}' + (f' — {detail}' if detail else ''))
        if verbose:
            note = _clean_note(record.get('note'), record)
            if len(note) > 220:
                note = note[:217].rstrip() + '…'
            if len(note) > 12:
                lines.append(f'   _{note}_')
            if record.get('advice'):
                lines.append(f'   _{re.sub(r"\s+", " ", str(record["advice"])).strip()[:200]}_')
    # The post's own verdict on the rotation, printed once rather than per item.
    verdict = next((str(r.get('rotation_advice') or '').strip() for r in rows
                    if r.get('rotation_advice')), '')
    if verbose and verdict:
        lines.append(f'• **Buy or skip:** {re.sub(r"\s+", " ", verdict)[:240]}')
    return '\n'.join(lines)


def render(records, *, now=None, limit=3, title=None):
    """Render rotations as the summary the bot should actually send."""
    today = (now or datetime.now(timezone.utc)).date() if isinstance(now, datetime) \
        else (now or date.today())
    if isinstance(today, datetime):
        today = today.date()
    rotations = group(records)
    if not rotations:
        return None
    live = [r for r in rotations if r['end_date'] and r['end_date'] >= today]
    named = [r for r in rotations if r['named']]
    unnamed = [r for r in rotations if not r['named']]

    out = []
    if title:
        out.append(title)
    if live:
        out.append('**Running now**')
        for rotation in live[:2]:
            out.append(_rotation_block(rotation, today))
    shown = [r for r in named if r not in live][:max(1, int(limit))]
    if shown:
        if any(r['start_date'] or r['end_date'] for r in shown):
            heading = '**Most recent completed rotations**'
        else:
            heading = '**Latest on record**'
        out.append(heading)
        for rotation in shown:
            out.append(_rotation_block(rotation, today))
    if unnamed:
        kinds = sorted({r.get('kind') for r in unnamed for r in r['rows'] if r.get('kind')})
        span = f'{_fmt_date(unnamed[-1]["start"])} – {_fmt_date(unnamed[0]["end"] or unnamed[0]["start"])}'
        out.append(f'*{len(unnamed)} further rotation{"" if len(unnamed) == 1 else "s"} '
                   f'({span}) were logged without item names'
                   + (f' — {_and_list(kinds)} slots only' if kinds else '')
                   + ', so I cannot say what was in them.*')
    return '\n\n'.join(out)


def render_one(records, name, *, now=None):
    """Everything on record about one item, as appearances rather than rotations."""
    wanted = str(name or '').strip().casefold()
    hits = [r for r in (records or ()) if str(r.get('name') or '').strip().casefold() == wanted]
    if not hits:
        return None
    rotations = group(hits)
    out = [f'**{hits[0]["name"]}** · {len(rotations)} Crate Rush appearance'
           f'{"" if len(rotations) == 1 else "s"} on record']
    today = (now or datetime.now(timezone.utc)).date() if isinstance(now, datetime) else (now or date.today())
    if isinstance(today, datetime):
        today = today.date()
    for rotation in rotations:
        record = rotation['rows'][0]
        bits = [_fmt_span(rotation['start'], rotation['end']), _status_line(rotation, today)]
        if record.get('rank'):
            bits.insert(0, f'rank {record["rank"]}')
        if record.get('price'):
            bits.append(f'{record["price"]}/spin')
        if record.get('pool'):
            bits.append(f'{record["pool"]}-item pool')
        out.append('• ' + ' · '.join(b for b in bits if b))
    priced = [r['rows'][0] for r in rotations if r['rows'][0].get('price')]
    if len(priced) > 1 and priced[0]['price'] != priced[-1]['price']:
        out.append(f'**Price moved:** {priced[-1]["price"]} in {_fmt_date(priced[-1]["start"])} → '
                   f'{priced[0]["price"]} in {_fmt_date(priced[0]["start"])}.')
    if any(r['rows'][0].get('skewed') for r in rotations):
        out.append('*Some of these are flagged as skewed pricing, meaning the Crate Rush was '
                   'poor value against the Gear Hub that week.*')
    return '\n'.join(out)
