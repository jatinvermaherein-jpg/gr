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


def fact_source_text(fact):
    """The fullest text build_facts kept for a post.

    The stored quote is clamped to 400 characters, which cuts off the strategy and the
    spin maths in a long guide. The bullets are not clamped, so they are rejoined here and
    the quote is used only when there are no bullets.
    """
    details = fact.get('details') or {}
    bullets = [b for entry in (details.get('entries') or ()) for b in (entry.get('bullets') or [])]
    if bullets:
        return ' '.join(bullets)
    return str(details.get('quote') or fact.get('summary') or '')


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
        if _ADVICE_LABEL.match(label) or _VERDICT_HEAD.match(label):
            # "Buy or skip" is the post's verdict on the rotation, not a third item.
            advice = (str(entry.get('note') or '').strip() or label)
            continue
        if _CONDITION_HEAD.match(label):
            # "If you already own Ingrid" is strategy, not an item slot.
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
    # A rotation that has not started yet is not running. Testing only the end date put a
    # rotation scheduled for 24 December under "Running now" in November, which is the one
    # thing a reader would act on.
    live = [r for r in rotations
            if r['end_date'] and r['end_date'] >= today and not (r['start_date'] and r['start_date'] > today)]
    soon = [r for r in rotations if r['start_date'] and r['start_date'] > today]
    named = [r for r in rotations if r['named']]
    unnamed = [r for r in rotations if not r['named']]
    live_ids = {id(r) for r in live}
    soon_ids = {id(r) for r in soon}

    out = []
    if title:
        out.append(title)
    if live:
        out.append('**Running now**')
        for rotation in live[:2]:
            out.append(_rotation_block(rotation, today))
    if soon:
        out.append('**Scheduled next**')
        for rotation in soon[:2]:
            out.append(_rotation_block(rotation, today))
    shown = [r for r in named if id(r) not in live_ids and id(r) not in soon_ids][:max(1, int(limit))]
    if shown:
        if any(r['start_date'] or r['end_date'] for r in shown):
            heading = '**Most recent completed rotations**'
        else:
            heading = '**Latest on record**'
        out.append(heading)
        for rotation in shown:
            out.append(_rotation_block(rotation, today))
    # A rotation already shown above is not part of the "further" count, and one rotation takes
    # "was", not "were".
    unnamed = [r for r in unnamed if id(r) not in live_ids and id(r) not in soon_ids]
    if unnamed:
        kinds = sorted({r.get('kind') for r in unnamed for r in r['rows'] if r.get('kind')})
        span = f'{_fmt_date(unnamed[-1]["start"])} – {_fmt_date(unnamed[0]["end"] or unnamed[0]["start"])}'
        out.append(f'*{len(unnamed)} further rotation{"" if len(unnamed) == 1 else "s"} '
                   f'({span}) {"was" if len(unnamed) == 1 else "were"} logged without item names'
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


# =====================================================================================
# Guide-post support
#
# Real announcements are not a list of two items with a price. The two samples below show
# the two shapes that actually occur, and neither was readable by the first renderer:
#
#   "## VORTEX (R4)" + "• Max Credits - 800K" + "• Get or Skip, it's your choice!"
#   "## INGRID MEGA ECR" + "## If you already own Ingrid:" + a bulleted strategy
#
# The block below adds: hero-item detection (the item is named in the heading, not the
# heading alone), structured extraction of the handful of facts these posts actually carry
# (max credits, whether it is in the Gear Hub, the verdict word, the spin maths), and
# conditional sections that are rendered as guidance rather than as an item.
# =====================================================================================

# Headings that introduce advice for a condition rather than an item.
_CONDITION_HEAD = re.compile(
    r'^(?:if\s+you|if\s+i|when\s+you|for\s+(?:new|returning|free|paying)|'
    r'already\s+own|don.?t\s+own|do\s+not\s+own|already\s+have|without\b)', re.I)

# Headings that are the post's verdict rather than an entry.
_VERDICT_HEAD = re.compile(
    r'^(?:buy\s*(?:or|\/)\s*skip|get\s+or\s+skip|skip\s+it|verdict|worth\s+it|is\s+it\s+worth|'
    r'my\s+recommendation|recommendation|tl;?dr|summary|should\s+you)\b', re.I)

_ITEM_TYPE_WORD = re.compile(r'\b(mech|weapon|pilot|skin|implant|mod)\b', re.I)

# "800K" / "800,000" / "1.2M"
_K_AMOUNT = re.compile(r'\b(\d[\d,]*(?:\.\d+)?)\s*([KkMm])\b')

_GEAR_HUB = re.compile(r'\bgear\s*hub\b', re.I)
_TOKEN_WORD = re.compile(r'\bfree\s+token|token\b', re.I)
_ACOIN_WORD = re.compile(r'\ba-?coins?\b', re.I)
_SPIN_MATH = re.compile(r'\b(\d+)\s*spins?\s*[=x\u00d7]\s*([\d,]+)\s*(?:playoff\s+)?points?\b', re.I)
_POINTS_PER_SPIN = re.compile(r'\beach\s+(?:ecr\s+|mega\s+)?spin\s+(?:gives?|is\s+worth)\s+([\d,]+)\s*(?:playoff\s+)?points?\b', re.I)
# The figure usually precedes the word: "you receive 2,500 A-Coins as duplicate compensation".
_DUPLICATE_COMP = re.compile(r'([\d,]{3,})\s*a-?coins?[^.]{0,60}?duplicate|duplicate[^.]{0,60}?([\d,]{3,})\s*a-?coins?', re.I)
_LOGIN_DAY = re.compile(r'\b(\d+)(?:st|nd|rd|th)[- ]day\s+(?:daily\s+)?login', re.I)

_VERDICT_GOOD = re.compile(r'\bworth\s+(?:the\s+)?(?:purchase|credits|picking|it|getting|buying)\b|'
                           r'\bgood\s+(?:mech|weapon|pick|buy)\b|\bget\s+it\b|\bpick\s+it\s+up\b', re.I)
_VERDICT_BAD = re.compile(r"\bdon'?t\s+spend\b|\bdon'?t\s+buy\b|\bskip\b|\bnot\s+worth\b|"
                          r'\bavoid\b|\bwaste\b', re.I)
_VERDICT_MIXED = re.compile(r'\byour\s+choice\b|\bget\s+or\s+skip\b|\bdepends\b|\bup\s+to\s+you\b', re.I)
_BEYOND_META = re.compile(r'\bfar\s+from\s+(?:the\s+)?meta\b|\bnot\s+meta\b|\boff[- ]meta\b', re.I)


def _expand_amounts(text):
    """'800K' -> '800,000', '1.2M' -> '1,200,000'. Presentation only; nothing is inferred."""
    def swap(match):
        try:
            value = float(match[1].replace(',', ''))
        except ValueError:
            return match[0]
        value *= 1000 if match[2].lower() == 'k' else 1_000_000
        return f'{int(round(value)):,}'
    return _K_AMOUNT.sub(swap, text or '')


def _rank_in_label(label):
    match = re.search(r'\(\s*R\s*(\d{1,2})\s*\)|\bR\s*(\d{1,2})\b|\brank\s*(\d{1,2})\b', label or '', re.I)
    if not match:
        return None
    return int(next(g for g in match.groups() if g))


def _strip_rank_from_name(label):
    """'VORTEX (R4)' -> 'VORTEX'; the rank is reported separately."""
    return re.sub(r'\s*\(\s*R\s*\d{1,2}\s*\)\s*$', '', str(label or '')).strip()


def _type_for(label, note=''):
    match = _ITEM_TYPE_WORD.search(label or '')
    if match:
        return match[1].lower()
    match = _ITEM_TYPE_WORD.search(note or '')
    return match[1].lower() if match else ''


def _verdict_of(bullets, note=''):
    """Read the post's own verdict. Returns (word, quote) or ('', '').

    Judged bullet by bullet rather than on the flattened note: the bullets are separate
    sentences, and joining them first made the quoted verdict the entire note.
    """
    candidates = list(bullets or ()) or [str(note or '')]
    for clean in (str(b).strip(' •-—') for b in candidates):
        if not clean:
            continue
        if _VERDICT_MIXED.search(clean):
            return 'mixed', clean
        if _VERDICT_BAD.search(clean) and not _VERDICT_GOOD.search(clean):
            return 'avoid', clean
        if _VERDICT_GOOD.search(clean):
            return 'worth', clean
    return '', ''


def _credit_figures(bullets):
    """Credit figures the post states, expanded.

    The bullet may put the word first ("Max Credits - 800K") or the figure first
    ("worth 800,000 Credits"), so both orders are read. Anything else is left alone: a
    bare number with no currency word next to it is not assumed to be credits.
    """
    found = []
    for bullet in bullets or ():
        expanded = _expand_amounts(bullet)
        for match in re.finditer(r'(\d[\d,]{2,})\s*(?:credits?|cr)\b', expanded, re.I):
            value = _num(match[1])
            if value and value not in found:
                found.append(value)
        for match in re.finditer(r'\bcredits?\b[^\d]{0,20}(\d[\d,]{2,})', expanded, re.I):
            value = _num(match[1])
            if value and value not in found:
                found.append(value)
    return found


def guide_sections(entries):
    """Split parsed entries into (hero item, other items, conditional sections, verdict).

    A conditional section is a ``##`` block whose heading is advice for a situation
    ("If you already own Ingrid") rather than an item. Treating those as items, which the
    first renderer did, produced three fake mechs in one rotation.
    """
    hero, items, conditions, verdict = None, [], [], ''
    for entry in entries or ():
        label = str(entry.get('label') or '').strip()
        if not label:
            continue
        bullets = list(entry.get('bullets') or [])
        if _VERDICT_HEAD.match(label) or _ADVICE_LABEL.match(label):
            verdict = ' '.join(bullets) or str(entry.get('note') or '')
            continue
        if _CONDITION_HEAD.match(label):
            conditions.append({'heading': label, 'bullets': bullets})
            continue
        record = {'name': _strip_rank_from_name(label), 'label': label,
                  'rank': _rank_in_label(label), 'kind': _type_for(label, entry.get('note')),
                  'bullets': bullets, 'note': str(entry.get('note') or ''),
                  'credits': _credit_figures(bullets),
                  'gear_hub': bool(_GEAR_HUB.search(entry.get('note') or '')),
                  'token': bool(_TOKEN_WORD.search(entry.get('note') or '')),
                  'acoins': bool(_ACOIN_WORD.search(entry.get('note') or '')),
                  'beyond_meta': bool(_BEYOND_META.search(entry.get('note') or '')),
                  'verdict': _verdict_of(bullets, entry.get('note') or '')}
        if hero is None and _ITEM_TYPE_WORD.search(label):
            hero = record
        else:
            items.append(record)
    if hero is None and items:
        hero = items.pop(0)
    return hero, items, conditions, verdict


def _verdict_line(record):
    word, quote = record.get('verdict') or ('', '')
    if not word:
        return ''
    label = {'worth': 'worth it', 'avoid': 'skip', 'mixed': 'your call'}[word]
    return f'Verdict: **{label}** — "{quote[:160]}"'


def render_guide(entries, *, heading='', now=None, verbose=True, limit_bullets=8):
    """Render one guide post: the item(s) it names, then any conditional strategy."""
    hero, items, conditions, verdict = guide_sections(entries)
    if not hero and not items and not conditions:
        return None
    out = []
    if heading:
        out.append(heading)

    for record in ([hero] if hero else []) + items:
        head = [f'**{record["name"] or record["label"]}**']
        if record.get('kind'):
            head.append(record['kind'])
        if record.get('rank'):
            head.append(f'rank {record["rank"]}')
        if record.get('credits'):
            head.append(' · '.join(f'{v:,} Credits' for v in record['credits'][:2]))
        lines = [' · '.join(head)]
        # The hook line (### under the item) is the post's own subject sentence.
        hook = next((b for b in record['bullets'] if b.endswith('?')), '')
        if verbose and hook:
            lines.append(f'_{hook[:180]}_')
        shown = [b for b in record['bullets'] if b != hook][:limit_bullets]
        for bullet in shown:
            lines.append(f'• {_expand_amounts(bullet)[:240]}')
        flags = []
        if record.get('gear_hub'):
            flags.append('in the Gear Hub')
        if record.get('token'):
            flags.append('free token available')
        if record.get('beyond_meta'):
            flags.append('far from the meta')
        if flags:
            lines.append(f'• *{" · ".join(flags)}*')
        verdict_line = _verdict_line(record)
        if verdict_line:
            lines.append(f'• {verdict_line}')
        out.append('\n'.join(lines))

    for section in conditions[:3]:
        lines = [f'**{section["heading"]}**']
        for bullet in section['bullets'][:limit_bullets]:
            lines.append(f'• {_expand_amounts(bullet)[:260]}')
        out.append('\n'.join(lines))

    if verdict:
        out.append(f'**Buy or skip:** {_expand_amounts(re.sub(chr(10), " ", verdict))[:400]}')
    return '\n\n'.join(out)


def render_post(entries, *, title='', now=None, verbose=True):
    """Render one announcement post, choosing the shape its parser output implies."""
    if not entries:
        return None
    kind = classify_entry_set(entries)
    if kind == 'item':
        return render_guide(entries, heading=title, now=now, verbose=verbose)
    return render_guide(entries, heading=title, now=now, verbose=verbose)


def classify_entry_set(entries):
    """'item' when the post names a typed item, else 'guide'."""
    for entry in entries or ():
        if _ITEM_TYPE_WORD.search(str(entry.get('label') or '')):
            return 'item'
    return 'guide'


def post_facts(text_or_entries):
    """The handful of real figures a guide post states, pulled out and labelled.

    Announcements are written for humans, so the numbers that matter are buried in prose:
    "70 spins = 3,500 Playoff Points", "Each ECR spin gives 50 Playoff Points",
    "2,500 A-Coins as duplicate compensation", "120th-day daily login reward". Each is
    reported with the phrase it came from so the reader can check it, and nothing is
    calculated — 70 x 50 is left as the two stated numbers, not as a new one.
    """
    if isinstance(text_or_entries, str):
        text = text_or_entries
    else:
        text = ' '.join(b for entry in (text_or_entries or ())
                        for b in (entry.get('bullets') or []) + [entry.get('label') or ''])
        text = _expand_amounts(text)
    facts = []
    for match in _POINTS_PER_SPIN.finditer(text):
        facts.append(('Per spin', f'{_num(match[1]):,} Playoff Points'))
    for match in _SPIN_MATH.finditer(text):
        facts.append(('Spin maths', f'{match[1]} spins = {_num(match[2]):,} Playoff Points'))
    for match in _DUPLICATE_COMP.finditer(text):
        facts.append(('Duplicate', f'{_num(match[1]):,} A-Coins if already owned'))
    for match in _LOGIN_DAY.finditer(text):
        facts.append(('Daily login', f'also a day-{match[1]} login reward'))
    for match in _K_AMOUNT.finditer(text or ''):
        context = text[max(0, match.start() - 40):match.end() + 40]
        if re.search(r'\bcredits?\b', context, re.I):
            value = _expand_amounts(match[0])
            entry = ('Max credits', f'{value} Credits')
            if entry not in facts:
                facts.append(entry)
    # de-duplicate while keeping the order they appear in the post
    seen, unique = set(), []
    for label, value in facts:
        if (label, value) in seen:
            continue
        seen.add((label, value))
        unique.append((label, value))
    return unique[:8]


def render_announcement(entry_list, *, title='', text='', now=None, verbose=True):
    """Full render for one ingested post: heading, items or strategy, then stated figures."""
    body = render_post(entry_list, title=title, now=now, verbose=verbose)
    if not body:
        return None
    figures = post_facts(text or entry_list)
    if figures:
        body += '\n\n**Figures the post states**\n' + '\n'.join(
            f'• {label}: {value}' for label, value in figures)
    return body
