"""Curated fact layer for saved announcements.

Announcements are the *source*; the facts table is what the bot answers from. Every message is
read — attachment/mention-only stubs are merged into the post they belong to instead of being
shown as separate items — and each substantive post becomes one typed fact with its source, date
and the rule that classified it, so a reply can always be traced back to the message it came from.

ECR rules encoded here (provided by the data owner, used to pick the current event, never narrated):
  • weekly rotations alternate Mon→Thu; a new one starts as soon as the previous one expires;
  • weekly rotations cover a weapon plus a mech or pilot and carry a single grand prize;
  • credit / A-coin / mod ECRs last one day (24 hours from the post);
  • seasonal ECRs carry multiple grand prizes;
  • only the latest ECR is reported unless the user explicitly asks for older ones.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import datetime, timedelta, timezone

CATEGORIES = ('ecr', 'promo_code', 'store_offer', 'gear_hub', 'balance_change', 'event', 'community', 'other')
ECR_KINDS = ('weekly', 'one_day', 'seasonal', 'special', 'unknown')
ONE_DAY_SECONDS = 86400
ONE_DAY_GRACE_SECONDS = 4 * 3600
STUB_MERGE_WINDOW_SECONDS = 240
MAX_SUMMARY = 300
MAX_NOTE = 220
# An ECR guide post carries a full strategy; 220 characters cut the useful half off.
ECR_NOTE_LIMIT = 1200

_MEMBER = re.compile(r'\[member\]', re.I)
_MENTION = re.compile(r'@(?:\\u200b)?(?:everyone|here)|<[@!][#&]?\d{15,20}>')
_CUSTOM_EMOJI = re.compile(r'<a?:[^:>]{1,60}:\d+>')
_EMOJI_TOKEN = re.compile(r'</?(?::)?[A-Za-z_][A-Za-z0-9_]{0,60}(?::\d+)?>')
_LINK = re.compile(r'https?://\S+')
_DECOR = re.compile(r'^[\s#*_`>~•·|\-–—=:.✨🎁🔥⚡🏆🚀🧩👾😹💀🔫💥🕶️❄️🤖⛑️🚨❤️‍🔥🎃👻🧛⏎]+$')
_STUB = re.compile(r'^(?:image|images|gif|video|attachment|attachments|media|file|embed)\s*(?:[-–—:.]?\s*\d+)?$', re.I)
_PROMO_HEADLINE = re.compile(r'\bpromo\b|\bpromocode\b|\bredeem\b|\bcodes?\b', re.I)
# "Crate Rush" is the in-game name for the ECR, and current announcements use it without
# ever writing "ECR". Matching only \becr\b filed those posts as category 'other', which
# is why a brand-new rotation never reached the ECR summary.
_ECR_MENTION = re.compile(r'\becrs?\b|\bcrate\s*rush(?:es)?\b', re.I)
_ONSLAUGHT = re.compile(r'\bonslaught\b', re.I)
_GEAR_HUB = re.compile(r'\bin\s+gear\s*hub\b|gear\s*hub\s*(?:update|updates|stock|price|prices)'
                       r'|new\s+items?\s+in\s+gear|available\s+in\s+(?:game\s+)?currency', re.I)
_STORE_OFFER = re.compile(r'plarium\s+store|new\s+offer|free\s+gift|claim\s+the\s+free|store\s*!?\)', re.I)
_BALANCE = re.compile(r'patch\s+notes|rebalance|nerf(?:ed|f)?\b|buff(?:ed)?\b|revert\s+the\s+fix|version\s+3\.\d+', re.I)
_SURVEY = re.compile(r'\bsurvey\b', re.I)
_EVENT = re.compile(r'new\s+(?:event|feature|mech|skin)|is\s+now\s+live|has\s+arrived|update\s+has\s+arrived|event\s+kicks\s+off', re.I)
_WEEKLY_HEAD = re.compile(r'(?:current\s+)?(?:weekly\s+)?ecr\s+guide|\becr\s+guide\b', re.I)
_ONE_DAY = re.compile(r'\b(?:a[-\s]?coins?|credits?|credit|mods?)\s+(?:rush\s+)?ecr\b|\becr\b[^.]{0,40}?\b(?:rush)\b|'
                      r'\b(?:surprise|crazy|flash)\s+ecr\b|\b(?:only\s+)?\d{1,2}\s*hours?\s*(?:more|left)?\b|\b24\s*h(?:ou?rs?)?\b', re.I)
_SEASONAL = re.compile(r'\bseason(?:al)?\s+ecr\b|prize\s+pool\s+overview|\bmultiple\s+grand\s+prizes?\b', re.I)
_SPECIAL = re.compile(r'\bspecial\s+ecr\b', re.I)
_GRAND_PRIZE = re.compile(r'\bgrand\s+prizes?\b', re.I)
_CODE_ASSIGN = re.compile(r'\bcode\b\s*[-–—:=]?\s*\n?\s*#*\s*([A-Z0-9][A-Z0-9_-]{3,31})\b', re.I)
_PROMOCODE_DASH = re.compile(r'\bpromo-?code\b\s*[-–—:=]?\s*([A-Z0-9][A-Z0-9_-]{3,31})\b', re.I)
_RANK_NOTE = re.compile(r'\(\s*(R\d{1,2})\s*\)|\b(R\d{1,2})\b')
_MONTHS = 'January|February|March|April|May|June|July|August|September|October|November|December'
_STATED_DATE = re.compile(rf'\b(?:(?:{_MONTHS})\.?\s+\d{{1,2}}(?:,?\s+20\d{{2}})?'
                          r'|(?:\d{{1,2}}\.?\s+(?:{_MONTHS})(?:,?\s+20\d{{2}})?)'
                          r'|20\d{{2}}-\d{{2}}-\d{{2}})\b', re.I)
# Banners inside a guide post (prize-pool overview, tips): they inform details, never the title.
_SECTION_LABEL = re.compile(r'prize pool|overview|official tip|rewards? worth|worth priorit|how to\b|'
                            r'chances|advice|\btips?\b|stay (?:updated|ahead)|current(?:ly)? (?:running )?(?:ecr|events)|'
                            r'ecr\s+guide|(?:weekly|seasonal|special|one[- ]day) ecr|^notes?\s*[:;]?\s*$', re.I)
# How long a "no newer post of this kind" reading may still be called current before it is history.
# The channel's own rhythm is the only honest clock, so it is *measured* from the saved posts
# (median gap between posts of that kind) and these numbers are only the fallback for a thin archive:
# a weekly guide every week, an A-coins/currency rush that may not return for a month.
CADENCE_FALLBACK_DAYS = {'weekly': 8, 'seasonal': 30, 'special': 14, 'unknown': 8}
# Twice the slowest ordinary gap, not the average one: a rotation is only called history once the
# channel has gone noticeably quieter than it usually is, never while a normal rotation is running.
CADENCE_STALE_FACTOR = 2.0
CADENCE_STALE_QUANTILE = 0.75
CADENCE_MIN_STALE_DAYS = 3
CADENCE_MAX_STALE_DAYS = 90    # nothing stays "current" past a quarter year of silence
CADENCE_MAX_GAP_DAYS = 120     # longer than this is a dead channel, not a cadence


def normalise(text):
    """Strip mentions, emoji codes, links and markup so classification and matching stay stable."""
    value = str(text or '').replace('\\n', '\n')
    value = _CUSTOM_EMOJI.sub(' ', value)
    value = _EMOJI_TOKEN.sub(' ', value)
    value = _MEMBER.sub(' ', value)
    value = _MENTION.sub(' ', value)
    value = _LINK.sub(' ', value)
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r'\n\s*\n+', '\n', value)
    return value.strip()


# The bot's own replies and bare greetings sometimes land in an announcement channel. They are read
# and counted, but they never become a fact the bot answers from.
_SELF_ECHO = re.compile(r'how can i help you today|for a quick overview|i couldn.t find|please try again later', re.I)


def is_noise(text):
    value = _clean_line(text)
    if not value or _SELF_ECHO.search(value):
        return True
    return len(value.split()) < 12 and not re.search(r'\d', value)


def _sort_key(row):
    """Snowflake ids are numeric, but test rows need not be."""
    raw = str(row.get('message_id') or '')
    digits = int(raw) if raw.isdigit() else 0
    return (_posted(row), digits)


def is_stub(text):
    body = _DECOR.sub('', normalise(text)).strip()
    body = re.sub(r'[^A-Za-z0-9]', '', body)
    if len(body) < 4:
        return True
    return bool(_STUB.match(body))


def _posted(row):
    try:
        return int(row.get('posted_at') or 0)
    except (TypeError, ValueError):
        return 0


def group_posts(rows):
    """Pair stub messages with the nearest substantive message in time (stubs may lead or follow)."""
    ordered = sorted(rows or [], key=_sort_key)
    out, orphans = [], []
    for row in ordered:
        text = normalise(row.get('content'))
        stub_id = str(row.get('message_id') or '')
        if is_stub(text):
            orphans.append({'message_id': stub_id, 'posted_at': _posted(row),
                            'text': str(row.get('content') or '').strip()[:40]})
            continue
        item = dict(row)
        item['_text'] = text
        item['_stubs'] = []
        out.append(item)
    for stub in list(orphans):
        # Stub rows carry their own posted_at, so match them to the nearest timestamp.
        best = None
        best_gap = None
        for item in out:
            gap = abs(_posted(item) - int(stub.get('posted_at') or 0))
            if best_gap is None or gap < best_gap:
                best, best_gap = item, gap
        if best is not None and best_gap <= STUB_MERGE_WINDOW_SECONDS:
            best['_stubs'].append(stub['message_id'])
            orphans.remove(stub)
    return out, orphans


def classify(text):
    """Return (category, ecr_kind, basis) for one normalised post body."""
    low = str(text or '')
    if _PROMO_HEADLINE.search(low) and codes_in(low):
        return 'promo_code', 'unknown', 'promo-code wording with a code token in the post'
    if _ONSLAUGHT.search(low) and not _WEEKLY_HEAD.search(low):
        return 'event', 'unknown', 'onslaught/challenge post rather than an ECR rotation'
    if _ECR_MENTION.search(low):
        kind = 'weekly' if _WEEKLY_HEAD.search(low) else 'unknown'
        if _SEASONAL.search(low) or len(_GRAND_PRIZE.findall(low)) > 1:
            kind = 'seasonal'
        elif _SPECIAL.search(low):
            kind = 'special'
        elif _ONE_DAY.search(low):
            kind = 'one_day'
        elif kind != 'weekly':
            kind = 'one_day' if re.search(r'\becr\b.*\b(?:is here|come in|coming)\b', low, re.I) else 'weekly'
        basis = 'ECR wording; kind matched ' + kind + ' rule'
        return 'ecr', kind, basis
    # An ECR guide always mentions the Gear Hub for comparison, so the rotation reading wins first.
    if _GEAR_HUB.search(low):
        return 'gear_hub', 'unknown', 'gear hub price or stock listing'
    if _STORE_OFFER.search(low):
        return 'store_offer', 'unknown', 'store offer or free-gift link'
    if _EVENT.search(low) and not re.search(r'patch notes', low, re.I):
        return 'event', 'unknown', 'event/feature release wording'
    if _BALANCE.search(low):
        return 'balance_change', 'unknown', 'patch/balance wording'
    if _SURVEY.search(low):
        return 'community', 'unknown', 'survey or feedback post'
    return 'other', 'unknown', 'no typed pattern matched; kept so nothing is silently dropped'


KIND_TITLES = {'weekly': 'Weekly ECR', 'one_day': 'One-day ECR', 'seasonal': 'Seasonal ECR',
               'special': 'Special ECR', 'unknown': 'ECR'}


def _clean_line(line):
    """Strip Discord markup, attachment placeholders and decorative glyphs from one line."""
    value = str(line or '')
    # Mentions and custom emoji first: a role ping on its own bullet otherwise ended up
    # appended to the line above it and inside the item label.
    value = re.sub(r'<a?:[A-Za-z0-9_]+:\d+>', ' ', value)
    value = re.sub(r'<@[!&]?\d+>|<#\d+>', ' ', value)
    value = re.sub(r'\[[A-Za-z]+\s*[-–]\s*\d+\]', ' ', value, flags=re.I)
    value = re.sub(r'[#*`>~]+', ' ', value)
    value = re.sub(r'[・·|│┃═─━]+', ' ', value)
    value = re.sub(r'[✨🎁🔥⚡🏆🚀🧩👾💀🔫💥❄️🤖⛑️🚨🎃👻🧛🥀😹💰✅🔨🎯📅📌🐯🦂🕷️🪱🥊⚔️🛡️🎟️🧪🔷🟡🟣⏎❤️‍🔥🕶️]', ' ', value)
    value = re.sub(r'\s*[•·]\s*', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' -—:=,.')
    return value


def entry_lines(text):
    """One entry per line; single-line (flattened) posts are split on their block headings."""
    body = str(text or '')
    lines = body.splitlines()
    headings = len([line for line in lines if re.match(r'^\s*#{2,3}\s', line)])
    if headings < 2 and len(lines) < 3 and body.count('#') >= 2:
        lines = re.split(r'(?=\s#{2,3}\s)', body.replace('\n', ' '))
    return [line.strip() for line in lines if str(line or '').strip()]


# Headings that are advice for a situation, not a featured item.
_CONDITION_HEAD = re.compile(
    r'^(?:if\s+you|if\s+i|when\s+you|for\s+(?:new|returning|free|paying)|'
    r'already\s+own|don.?t\s+own|do\s+not\s+own|already\s+have)\b', re.I)
_VERDICT_HEAD = re.compile(
    r'^(?:buy\s*(?:or|\/)\s*skip|get\s+or\s+skip|skip\s+it|verdict|worth\s+it|is\s+it\s+worth|'
    r'my\s+recommendation|recommendation|tl;?dr|summary|should\s+you)\b', re.I)


def title_from(text, category='other', ecr_kind='unknown', blocks=None, limit=110):
    """Short, markup-free label; for ECR posts the featured item names lead the title."""
    items = [block for block in (blocks or [])
             if not block.get('section')
             and not _CONDITION_HEAD.match(str(block.get('label') or ''))
             and not _VERDICT_HEAD.match(str(block.get('label') or ''))]
    names = ', '.join(str(block.get('label') or '')[:40] for block in items[:2]).strip(', ')
    if not names:
        for line in entry_lines(text):
            candidate = _clean_line(line)
            if category == 'ecr' and _SECTION_LABEL.search(candidate):
                continue
            if len(re.sub(r'[^A-Za-z0-9]', '', candidate)) >= 4:
                names = _fit(candidate, 60)
                break
    return _fit(f"{KIND_TITLES.get(ecr_kind, 'ECR')} · {names}".strip(' ·') if category == 'ecr'
                else (names or 'Announcement'), limit)


def _fit(value, limit):
    """Trim to a whole word instead of slicing a sentence in half."""
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(value) <= limit:
        return value or 'Announcement'
    head = value[:limit].rstrip()
    if ' ' in head:
        head = head[:head.rfind(' ')].rstrip(' ,;:')
    return (head or value[:limit]) + '…'


def summary_from(text, blocks=None, category='other', limit=MAX_SUMMARY):
    """Quote the post's own sentences, skipping headings and decoration."""
    headings = {_clean_line(block.get('label')).casefold() for block in (blocks or [])}
    kept = []
    for line in entry_lines(text):
        cleaned = _clean_line(line)
        if len(re.sub(r'[^A-Za-z0-9]', '', cleaned)) < 8:
            continue
        if re.fullmatch(r'(?:MECH ARENA|Stay updated!? ?(?:Stay ahead!?)?|Stay ahead!?)', cleaned, re.I):
            continue
        if cleaned.casefold() in headings:
            continue
        if category == 'ecr' and re.match(r'^\s*#{1,6}\s', line) and _SECTION_LABEL.search(cleaned):
            continue
        kept.append(cleaned)
    body = re.sub(r'\s+', ' ', ' '.join(kept)).strip()
    if not body:
        body = _clean_line(text)[:limit]
    out = ''
    for part in re.split(r'(?<=[.!?])\s+', body):
        if out and len(out) + len(part) > limit:
            break
        out = (out + ' ' + part).strip()
    return (out or body[:limit]).strip()[:limit]


def entries(text, note_limit=MAX_NOTE):
    """Pull the featured-item blocks out of an ECR guide post.

    A `##` line names an item; the `###` hook and bullets under it become that item's note. A block
    whose heading is only decoration (prize-pool banner, "Stay updated!") keeps its note so nothing
    is lost, but is flagged `section` so titles and pick-lists ignore it.

    `bullets` keeps the lines separately. The flattened `note` is fine for a one-clause summary
    but destroys a guide post, where each bullet is a different instruction and the useful
    figure ("70 spins = 3,500 Playoff Points") sits at the end, past the clamp.
    """
    found, current = [], None
    for line in entry_lines(text):
        heading = re.match(r'^(#{2,6})\s*(?!#)(.+)$', line)
        if heading:
            label = _clean_line(heading[2])
            if heading[1] != '##':
                if current is not None and label:
                    current['note'] = (current['note'] + ' ' + label).strip()[:MAX_NOTE]
                continue
            if len(re.sub(r'[^A-Za-z0-9]', '', label)) < 3 or _STUB.match(label):
                current = None
                continue
            if re.search(r'mech arena|stay updated|stay ahead', label, re.I):
                current = None
                continue
            current = {'label': label[:90], 'note': '', 'bullets': [],
                       'section': bool(_SECTION_LABEL.search(label))}
            found.append(current)
            continue
        piece = _clean_line(line)
        if not piece or current is None:
            continue
        if re.search(r'stay updated|stay ahead|mech arena', piece, re.I):
            continue
        current['note'] = (current['note'] + ' ' + piece).strip()[:note_limit]
        if len(current['bullets']) < 40:
            current['bullets'].append(piece[:400])
    for item in found:
        item['note'] = re.sub(r'\s+', ' ', item['note']).strip()
        verdict = advice_for(item['note'])
        if verdict:
            item['advice'], item['advice_quote'] = verdict[0], verdict[1][:150]
    return found


# Phrases that announce an event rather than name a reward; never treated as an item.
_ITEM_NOISE = re.compile(r'\bis\s+here\b|\b(?:hours?|mins?)\s+(?:more|left)\b|starts\s+now|\bcoming\b|\bcome\s+in\b|'
                         r'\bschedule\b|\bnew\s+event\b|\bonslaught\b|\bredeem\b|\bavailable\s+for\b', re.I)


def item_like(label):
    value = _clean_line(label)
    if not value or len(value) < 3 or _ITEM_NOISE.search(value):
        return False
    return len(value.split()) <= 5


def bold_entities(text):
    """Reward names the post itself marks bold (the guides use bold for the real prizes)."""
    out = []
    for span in re.findall(r'\*\*([^*\n]{3,60})\*\*', str(text or '')):
        value = _clean_line(span)
        if not value or len(value.split()) > 5 or len(re.sub(r'[^A-Za-z0-9]', '', value)) < 4:
            continue
        if _ITEM_NOISE.search(value):
            continue
        if re.search(r'main weapon|primary reward|actual|gear hub|grand prize', value, re.I) and not re.search(r'\d', value):
            continue
        if value.casefold() not in [item.casefold() for item in out]:
            out.append(value)
    return out[:6]


ADVICE_WORDS = {'skip': 'skip it', 'worth_it': 'worth the spend', 'depends': 'only if it fits your build'}

# The guides are formulaic about money, so the verdict is only ever their own wording, never ours.
_ADVICE_SKIP = re.compile(r"\bskip\b|\btrash\b|\bnot worth\b|\bdon['\u2019]?t (?:buy|get|spend)\b|\bavoid\b|\bpass on\b|"
                          r"\bwait for\b|\bsave your\b|\bdon\u2019?t spend\b", re.I)
_ADVICE_GO = re.compile(r"\bgo for it\b|\bmust (?:buy|get)\b|\bworth (?:it|buying|the (?:spend|credits))\b|"
                        r"\bsuggested to go\b|\bbest value\b|\bget it\b|\bgrab (?:it|the)\b|"
                        r"\bcheaper than (?:in |the )?gear hub\b|\brecommended\b", re.I)
_ADVICE_CONDITION = re.compile(r"\bif you (?:have|need|run|play)\b|\bunless you need\b|\bdepends\b|"
                               r"\bsituational\b|\bcasual\b|\b\d+ ?energy\b|\bmid in\b", re.I)


def advice_for(text):
    """(verdict, quote) from the post's own buy/skip wording, or None when it says nothing either way."""
    body = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not body:
        return None
    skip, go = bool(_ADVICE_SKIP.search(body)), bool(_ADVICE_GO.search(body))
    if not skip and not go:
        return None
    ask = re.search(r'\bworth\b[^.?!]{0,40}\?', body, re.I)
    skipped = _ADVICE_SKIP.search(body)
    if ask and go and not skip:
        verdict = 'depends'                      # "worth the purchase?" asks; it does not advise
    elif skip and go and skipped and ask and skipped.start() > ask.start():
        verdict = 'depends'                       # the later wording answers the question
    elif skip and go and re.search(r'skip\b[^.!?]{0,60}\bunless\b', body, re.I):
        verdict = 'skip'                                   # "skip unless you need it" is still a skip
    elif skip and go:
        verdict = 'depends'
    elif skip and ask and skipped and skipped.start() < ask.start():
        verdict = 'skip'                                   # "worth it?" asked first, answered "don't spend"
    elif skip:
        verdict = 'skip'
    elif _ADVICE_CONDITION.search(body):
        verdict = 'depends'                                 # "if you have the A-coins, go for it"
    else:
        verdict = 'worth_it'
    pattern = _ADVICE_GO if verdict == 'worth_it' else _ADVICE_SKIP
    match = pattern.search(body)
    if verdict == 'depends':
        match = (_ADVICE_CONDITION.search(body) or _ADVICE_SKIP.search(body) or _ADVICE_GO.search(body))
    if match is None:
        return verdict, _fit(body, 130)
    # The clause the verdict came from, trimmed to whole words at both ends. The old window
    # started 14 characters *before* the match and ran to a fixed width, which is how a reply
    # ended up quoting a bare "Credits" or "Don't".
    start = max(0, body.rfind(' ', 0, match.start()) + 1)
    clause = body[start:match.end() + 70].strip(' •*-')
    clause = re.sub(r'\s+', ' ', clause)
    if len(clause) > 110:
        clause = _fit(clause, 110)
    return verdict, ('…' if start else '') + clause


def _plain(value, limit):
    """Post text with the author's emoji removed and spacing normalised.

    The channel writes with emoji as punctuation; carrying them into a reply produced lines
    like "…the purchase? \U0001F615 Max Credits". Dropping them is what makes the rest readable.
    """
    text = re.sub(r'[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+', ' ', str(value or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+([.,;:!?])', r'\1', text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    # A post is a run of clauses; cutting at the last one that fits reads as a sentence,
    # where a hard cut leaves "...via free token Don't".
    for separator in ('! ', '? ', '. '):
        if separator in head:
            return head[:head.rfind(separator) + 1].strip(' ,;:')
    for separator in (' — ', ' - ', '; ', ', '):
        if separator in head:
            return head[:head.rfind(separator)].strip(' ,;:') + '…'
    return _fit(text, limit)


_DANGLING = re.compile(r"\s+(?:the|a|an|of|and|for|from|to|on|in|with|its|it|but|very|than|at|by)$", re.I)


def _trim_quote(quote):
    """A quote that ends on a whole word: "...Don't spend A-Coins on it", not "...on the"."""
    text = str(quote or '').strip(' ….,;:')
    for _ in range(3):
        trimmed = _DANGLING.sub('', text).strip(' ….,;:')
        if trimmed == text:
            break
        text = trimmed
    return text


def spend_check(fact, allow_silent=True):
    """One answer to "should I spend on this?", per featured item, in the post's own words.

    One short line per item: the verdict plus the clause that earned it. Quoting whole notes
    made a single reply read like a transcript, and the verdict got lost inside it.
    """
    details = fact.get('details') or {}
    items = [entry for entry in (details.get('entries') or []) if not entry.get('section')]
    lines = []
    for entry in items[:3]:
        found = advice_for(entry.get('note'))
        if not found:
            continue
        verdict, quote = found
        quote = _trim_quote(_plain(quote, 70))
        line = f"\u2022 {_plain(entry.get('label'), 40)}: **{ADVICE_WORDS[verdict]}**"
        lines.append(line + (f" \u2014 \u201c{quote}\u201d" if quote else ''))
    if not lines:
        found = advice_for(details.get('quote') or fact.get('summary'))
        if not found:
            if not allow_silent:
                return None
            return '\u2022 No buy or skip advice in the post.'
        verdict, quote = found
        quote = _trim_quote(_plain(quote, 70))
        lines.append(f"\u2022 The post says **{ADVICE_WORDS[verdict]}**" + (f" \u2014 \u201c{quote}\u201d" if quote else ''))
    return '\n'.join(lines)


def codes_in(text):
    body = str(text or '')
    values = []
    for pattern in (_PROMOCODE_DASH, _CODE_ASSIGN):
        for match in pattern.finditer(body):
            value = match[1].strip(' `*#-—')
            if len(value) >= 4 and value.upper() not in {'CODE', 'HERE', 'BELOW', 'PROMO'} and value not in values:
                values.append(value)
    return values[:6]


def stated_dates(text):
    return list(dict.fromkeys(match.group(0).strip(' .,') for match in _STATED_DATE.finditer(str(text or ''))))[:6]


def rank_note(text):
    match = _RANK_NOTE.search(str(text or ''))
    return match.group(1) or match.group(2) if match else None


def _now_epoch(now):
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _date_label(epoch):
    try:
        posted = datetime.fromtimestamp(int(epoch), timezone.utc) + timedelta(hours=5, minutes=30)
    except (TypeError, ValueError, OverflowError, OSError):
        return 'date not recorded'
    return f'{posted.strftime("%b")} {posted.day}, {posted.year}'


def age_label(epoch, now):
    days = int((now - int(epoch)) // 86400) if epoch else 0
    if days <= 0:
        return 'today'
    if days == 1:
        return 'yesterday'
    if days < 14:
        return f'{days} days ago'
    weeks = days // 7
    return f'{weeks} week' + ('s' if weeks > 1 else '') + ' ago'


def ecr_status(kind, posted_at, now, superseded):
    """Which ECR is current, using the rotation rules; never claims a time the post did not state."""
    if kind == 'one_day':
        if now >= int(posted_at) + ONE_DAY_SECONDS + ONE_DAY_GRACE_SECONDS:
            return 'ended', 'one-day ECR: 24 hours after the post (plus a grace margin)'
        return 'current', 'one-day ECR window'
    if superseded:
        return 'superseded', 'a newer ECR post was published in the same channel'
    return 'current', 'newest saved ECR post of this kind; nothing newer has been announced'


def build_facts(rows, now=None):
    """Turn every saved announcement into a typed fact. Returns (facts, stats)."""
    now = _now_epoch(now)
    posts, orphans = group_posts(rows)
    facts = []
    noise = 0
    for item in posts:
        text = item['_text']
        category, kind, basis = classify(text)
        if category == 'other' and is_noise(text):
            noise += 1
            continue
        posted = _posted(item)
        blocks = entries(text, note_limit=ECR_NOTE_LIMIT) if category == 'ecr' else []
        found_codes = codes_in(text) if category == 'promo_code' else []
        dates = stated_dates(text)
        # A one-day notice has no reward list; sentences like "Only 23 Hours More!!" are not items.
        if kind in ('one_day', 'unknown'):
            blocks = [block for block in blocks if item_like(block['label'])]
        items = ([block['label'] for block in blocks if not block.get('section')]
                 or [block['label'] for block in blocks])
        items = [label for label in items if item_like(label)]
        entities = (items + [value for value in bold_entities(text)
                             if item_like(value)
                             and value.casefold() not in {item.casefold() for item in items}])[:6] or found_codes[:6]
        message_id = str(item.get('message_id') or '')
        fact_id = hashlib.sha256(f'{message_id}:{category}'.encode('utf-8')).hexdigest()[:20]
        summary = summary_from(text, blocks, category)
        details = {
            'entries': blocks[:6],
            'codes': found_codes,
            'stated_dates': dates,
            'rank_note': rank_note(text) if category == 'ecr' else None,
            'merged_attachment_messages': item['_stubs'],
            'orphan_attachment_messages': [],   # filled per run in stats, not guessed onto a fact
            'prize_mentions': len(_GRAND_PRIZE.findall(text)),
            'classification_basis': basis,
            'quote': text[:400],
        }
        fact = {
            'fact_id': fact_id,
            'message_id': message_id,
            'channel_id': str(item.get('channel_id') or ''),
            'channel_name': str(item.get('channel_name') or '')[:100],
            'guild_name': str(item.get('guild_name') or '')[:100],
            'message_url': str(item.get('message_url') or '')[:500],
            'posted_at': posted,
            'category': category,
            'ecr_kind': kind,
            'status': ecr_status(kind, posted, now, False)[0] if category == 'ecr' else 'noted',
            'title': title_from(text, category, kind, blocks),
            'summary': summary[:MAX_SUMMARY],
            'entities': entities,
            'event_date_label': dates[0] if dates else '',
            'date_basis': 'stated in post' if dates else 'post date only',
            'details': details,
            'provenance': f'saved announcement {_date_label(posted)} · message {message_id}',
            'extractor': 'deterministic',
            'model': None,
        }
        facts.append(fact)
    # Mark superseded rotations and attach orphaned stub ids for completeness.
    for fact in facts:
        if fact['category'] != 'ecr':
            continue
        newer = [other for other in facts if other['category'] == 'ecr'
                 and other['ecr_kind'] in (fact['ecr_kind'], 'unknown') and other['posted_at'] > fact['posted_at']]
        status, why = ecr_status(fact['ecr_kind'], fact['posted_at'], now, bool(newer))
        fact['status'] = status
        fact['details']['status_basis'] = why
    # A stub with no post nearby is listed here for transparency and never attached to an unrelated fact.
    orphan_ids = [stub['message_id'] for stub in orphans[:40]]
    stats = {'messages_read': len(rows or []), 'facts_written': len(facts),
             'stub_messages': len(rows or []) - len(posts), 'orphans': len(orphan_ids),
             'orphan_message_ids': orphan_ids, 'noise_messages': noise}
    return facts, stats


def refresh_ecr_status(facts, now=None):
    """Re-derive ECR status for *now*; a dump freezes it, and a reader must not inherit that.

    Supersession still comes from the stored value, because that judgement needs the whole source
    set. What has to be live is the clock: a one-day rush saved last week must stop being reported
    as the current one the moment its window closes, even if nothing has re-dumped the table since.
    """
    now = _now_epoch(now)
    out = []
    for fact in facts or []:
        if fact.get('category') != 'ecr':
            out.append(fact)
            continue
        status, why = ecr_status(fact.get('ecr_kind') or 'unknown', fact.get('posted_at') or 0, now,
                                 fact.get('status') == 'superseded')
        if status != fact.get('status'):
            fact = dict(fact, status=status)
            fact['details'] = {**dict(fact.get('details') or {}), 'status_basis': why}
        out.append(fact)
    return out


def ecr_cadence_days(facts, kind):
    """Median gap, in days, between saved posts of one ECR kind; the fallback if history is thin.

    Returns (days, measured). This never reaches a reply as a schedule: it only decides how long a
    post may be called current. A rotation the channel announces weekly goes stale in days, one it
    announces monthly stays a live answer for weeks, and neither claim is invented here.
    """
    posted = sorted(int(fact.get('posted_at') or 0) for fact in facts or []
                    if fact.get('category') == 'ecr' and fact.get('ecr_kind') == kind)
    posted = [value for value in posted if value]
    gaps = sorted((later - earlier) / 86400 for earlier, later in zip(posted, posted[1:])
                  if 0 < (later - earlier) / 86400 <= CADENCE_MAX_GAP_DAYS)
    if len(gaps) >= 3:
        slowest = statistics.quantiles(gaps, n=4)[int(4 * CADENCE_STALE_QUANTILE) - 1] if len(gaps) >= 4 else gaps[-1]
        days = max(gaps[-1] * 1.05, slowest) * CADENCE_STALE_FACTOR
    else:
        days = CADENCE_FALLBACK_DAYS.get(kind, 8) * CADENCE_STALE_FACTOR
    return min(CADENCE_MAX_STALE_DAYS, max(CADENCE_MIN_STALE_DAYS, days)), bool(len(gaps) >= 3)


def current_ecrs(facts, now=None, kinds=('weekly', 'seasonal', 'special', 'one_day', 'unknown')):
    """The newest saved ECR of each kind — the rotation state the bot should describe."""
    now = _now_epoch(now)
    ecrs = [fact for fact in facts or [] if fact.get('category') == 'ecr']
    out = []
    for kind in kinds:
        pool = sorted([fact for fact in ecrs if fact.get('ecr_kind') == kind], key=lambda f: -int(f['posted_at']))
        if not pool:
            continue
        current = dict(pool[0])
        current['age'] = age_label(current['posted_at'], now)
        out.append(current)
    return sorted(out, key=lambda f: -int(f['posted_at']))


def older_ecrs(facts, limit=3, now=None):
    now = _now_epoch(now)
    ecrs = sorted([fact for fact in facts or [] if fact.get('category') == 'ecr'], key=lambda f: -int(f['posted_at']))
    current = {fact['fact_id'] for fact in current_ecrs(facts, now=now)}
    return [fact for fact in ecrs if fact['fact_id'] not in current][:max(0, int(limit))]


def wants_older_ecr(text):
    return bool(re.search(r'\b(?:older|previous|earlier|past|last|history|archive|recent)\s+(?:\w+\s+)?ecrs?\b|'
                          r'ecrs?\s+(?:before|prior)|which\s+ecrs?\b|previous\s+(?:weekly\s+)?(?:ecr|crate\s+rush)',
                          str(text or ''), re.I))


def _label(value, limit=90):
    """One clean line for a reply, cut at a word boundary so nothing reads as a fragment."""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) <= limit:
        return text
    return _fit(text, limit)


def _entry_lines(fact, limit=3):
    """One line per featured item: what it is, then the post's own note, kept to one clause."""
    details = fact.get('details') or {}
    out = []
    for entry in [e for e in (details.get('entries') or []) if not e.get('section')][:limit]:
        label = _plain(entry.get('label'), 60)
        if not label:
            continue
        note = _plain(entry.get('note'), 90).strip(' …')
        note = re.split(r'(?<=[.!?])\s+(?=[A-Z])', note)[0].strip(' …')
        out.append(f'\u2022 **{label}**' + (f' \u2014 {note}' if note else ''))
    return out


def format_ecr_reply(facts, now=None, include_older=False):
    """Report the current ECR state; older rotations only when the user asks for them."""
    now = _now_epoch(now)
    facts = dedupe_facts(refresh_ecr_status(facts, now=now))
    current = current_ecrs(facts, now=now)
    if not current:
        return None
    lines = []
    for fact in current[:2]:
        details = fact.get('details') or {}
        kind = fact.get('ecr_kind') or 'unknown'
        kind_label = {'weekly': 'weekly rotation', 'one_day': 'one-day ECR', 'seasonal': 'seasonal ECR',
                      'special': 'special ECR'}.get(kind, 'ECR')
        ended = fact.get('status') == 'ended'
        horizon = ecr_cadence_days(facts, kind)[0] * 86400
        stale = ended or (now - int(fact['posted_at'])) > horizon
        head_label = ('Last ECR announcement' if stale and fact is current[0]
                      else f'Earlier {kind_label}' if stale else
                      'Current ECR' if fact is current[0] else 'Also running')
        block = [f"**{head_label}** \u00b7 {kind_label} \u00b7 {_date_label(fact['posted_at'])} ({fact.get('age', '')})"]
        entries = _entry_lines(fact)
        if entries:
            block.extend(entries)
            if details.get('rank_note'):
                block.append(f"\u2022 Labelled \u201c{_plain(details['rank_note'], 20)}\u201d in the post, unexplained there.")
        else:
            summary = _plain(fact.get('summary'), 200).strip(' …')
            block.append(f'\u2022 {summary}' if summary else '\u2022 No reward list in the saved post.')
        check = spend_check(fact)
        if check:
            block.extend(check.split('\n'))
        note = ''
        if ended:
            note = 'That one-day window has passed; a newer post may replace it.'
        elif kind == 'one_day' and now - int(fact['posted_at']) < ONE_DAY_SECONDS + ONE_DAY_GRACE_SECONDS:
            note = 'A one-day event: treat it as ending within 24 hours of the announcement.'
        elif stale:
            note = 'Nothing newer of this kind is saved and the post states no end date, so it may already be over.'
        if note:
            block.append(f'\u2022 {note}')
        lines.append('\n'.join(block))

    # Rotations that are still current but not shown, plus superseded ones, both wait for an explicit ask.
    extras = [dict(fact, superseded=False) for fact in current[2:]]
    extras += [dict(fact, superseded=True) for fact in older_ecrs(facts, now=now)]
    if include_older:
        if extras:
            block = ['**Other ECR posts in the saved announcements**']
            for fact in extras:
                details = fact.get('details') or {}
                labels = ', '.join(_label(entry.get('label'), 40)
                                   for entry in [e for e in (details.get('entries') or []) if not e.get('section')][:2])
                tag = 'superseded' if fact['superseded'] else 'also current'
                block.append(f"• {_date_label(fact['posted_at'])} · {fact.get('ecr_kind') or 'ecr'} · {tag}"
                             + (f" · {labels}" if labels else f" · {_label(fact.get('title'), 50)}"))
            lines.append('\n'.join(block))
        else:
            lines.append('*No other ECR posts are saved, so there is nothing earlier to show.*')
    elif extras:
        lines.append(f'*{len(extras)} other ECR post(s) are saved but hidden — ask for “previous ECRs” to see them.*')
    text = '\n\n'.join(lines)
    return text[:1900] + '…' if len(text) > 1900 else text


def format_announcement_reply(facts, now=None, limit=10):
    """Group every saved fact by type, newest first, so mixed announcements read cleanly."""
    now = _now_epoch(now)
    facts = dedupe_facts(refresh_ecr_status(facts, now=now))
    if not facts:
        return None
    groups = [
        ('ecr', '🎁 ECR rotations'),
        ('promo_code', '🧾 Promo codes'),
        ('gear_hub', '⚙️ Gear hub'),
        ('store_offer', '🛒 Store offers'),
        ('balance_change', '🔧 Patches & balance'),
        ('event', '🚀 Events & new content'),
        ('community', '💬 Community'),
        ('other', '📌 Other saved posts'),
    ]
    blocks = []
    for category, heading in groups:
        pool = sorted([fact for fact in facts if fact.get('category') == category], key=lambda f: -int(f['posted_at']))
        if not pool:
            continue
        block = [f'**{heading}**']
        for fact in pool[:limit if category != 'ecr' else 3]:
            line = f"• {_label(fact.get('title'), 70)} · {_date_label(fact['posted_at'])}"
            summary = _label(fact.get('summary'), 160)
            if summary:
                line += f'\n  {summary}'
            if fact.get('category') == 'promo_code':
                found = (fact.get('details') or {}).get('codes') or []
                if found:
                    line += '\n  ' + ', '.join(f'`{code}`' for code in found[:3]) + ' *(redemption not verified)*'
            blocks_line = None
            if fact.get('category') == 'ecr':
                blocks_line = ', '.join(_label(entry.get('label'), 45)
                                        for entry in [e for e in ((fact.get('details') or {}).get('entries') or [])
                                                      if not e.get('section')][:2])
                if blocks_line:
                    line += f'\n  Featured: {blocks_line}'
            block.append(line)
        blocks.append('\n'.join(block))
    if not blocks:
        return None
    text = '\n\n'.join(blocks)
    return text[:1900] + '…' if len(text) > 1900 else text


_STOPWORDS = {'the', 'and', 'for', 'with', 'what', 'when', 'which', 'is', 'are', 'was', 'were', 'any',
              'new', 'now', 'tell', 'me', 'you', 'your', 'about', 'there', 'have', 'has', 'please', 'does'}


def search_facts(facts, query=None, categories=()):
    """Filter saved facts by the words the user actually used, newest first.

    Empty query means "everything in these categories", which is what the slash commands want.
    """
    pool = [fact for fact in (facts or []) if fact.get('category')]
    if categories:
        wanted = {str(value) for value in categories}
        pool = [fact for fact in pool if fact.get('category') in wanted]
    tokens = [token for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9_-]{2,}', str(query or '').casefold())
              if token not in _STOPWORDS]
    if tokens:
        def matches(fact):
            haystack = ' '.join([str(fact.get('title') or ''), str(fact.get('summary') or ''),
                                 ' '.join(str(value) for value in (fact.get('entities') or [])),
                                 ' '.join(str(value) for value in ((fact.get('details') or {}).get('codes') or []))]).casefold()
            return any(token in haystack for token in tokens)
        pool = [fact for fact in pool if matches(fact)]
    return sorted(pool, key=lambda fact: -int(fact.get('posted_at') or 0))


def dedupe_facts(facts):
    """Collapse re-posted duplicates of the same announcement, keeping the newest and noting the rest.

    The channel posts the same code or one-day notice twice sometimes; listing it twice reads like a
    bug, while keeping the extra ids preserves the fact that it really was announced twice.
    """
    kept, seen = [], {}
    for fact in sorted(facts or [], key=lambda item: -int(item.get('posted_at') or 0)):
        details = fact.setdefault('details', {})
        codes = tuple(sorted({str(code).casefold() for code in (details.get('codes') or [])}))
        slug = re.sub(r'[^a-z0-9]', '', str(fact.get('title') or '').casefold())
        key = codes or (fact.get('category'), fact.get('ecr_kind'), slug)
        previous = seen.get(key)
        if previous is not None:
            previous.setdefault('details', {}).setdefault('also_announced_in', []).append(
                {'message_id': fact.get('message_id'), 'posted_at': fact.get('posted_at')})
            continue
        seen[key] = fact
        kept.append(fact)
    return kept


def format_fact_list(facts, heading, limit=6, show_codes=False, note=None, now=None):
    """Plain answer block for a fact-backed lookup: headline, what the post said, source date."""
    shown = dedupe_facts(refresh_ecr_status(facts, now=now))[:max(1, int(limit))]
    if not shown:
        return None
    lines = [f'**{heading}**']
    for fact in shown:
        details = fact.get('details') or {}
        title = _label(fact.get('title'), 90) or 'Announcement'
        when = _date_label(int(fact.get('posted_at') or 0))
        age = age_label(int(fact.get('posted_at') or 0), _now_epoch(None))
        line = f"• {title} · {when} ({age})"
        if fact.get('status') == 'superseded':
            line += ' *(a newer post replaces it)*'
        elif fact.get('status') == 'ended':
            line += ' *(the one-day window it announced has passed)*'
        summary = _label(fact.get('summary'), 240)
        if summary and summary.casefold() != title.casefold():
            line += f'\n  {summary}'
        if fact.get('category') == 'ecr':
            verdict_line = spend_check(fact, allow_silent=False)
            if verdict_line:
                line += '\n  ' + verdict_line.lstrip('• ')
        codes = [str(value) for value in (details.get('codes') or [])]
        entities = [str(value) for value in (fact.get('entities') or [])
                    if str(value).strip() and str(value).casefold() not in {code.casefold() for code in codes}]
        if entities:
            line += "\n  Items: " + ', '.join(_label(value, 45) for value in entities[:4])
        if show_codes:
            codes = details.get('codes') or []
            if codes:
                line += '\n  ' + ', '.join(f'`{code}`' for code in codes[:3]) + ' *(redemption not verified)*'
        if details.get('stated_dates'):
            line += f"\n  Date stated in the post: {', '.join(details['stated_dates'][:2])}"
            if fact.get('date_basis'):
                line += f" ({fact['date_basis']})"
        url = str(fact.get('message_url') or '')
        if url.startswith('http'):
            line += f'\n  Source: <{url[:120]}>'
        lines.append(line)
    if note:
        lines.append(note)
    text = '\n\n'.join(lines)
    return text[:1900] + '…' if len(text) > 1900 else text


def facts_prompt_bundle(facts, budget=5200, now=None):
    """Compact all-facts digest for the AI model: every saved fact, newest first, within budget."""
    now = _now_epoch(now)
    ordered = sorted(refresh_ecr_status(facts or [], now=now), key=lambda f: -int(f['posted_at']))
    bundle, used = [], 0
    for fact in ordered:
        details = fact.get('details') or {}
        entry = {
            'date': _date_label(fact['posted_at']),
            'age': age_label(fact['posted_at'], now),
            'category': fact.get('category'),
            'ecr_kind': fact.get('ecr_kind') if fact.get('category') == 'ecr' else None,
            'status': fact.get('status'),
            'title': _label(fact.get('title'), 90),
            'summary': _label(fact.get('summary'), 200),
            'codes': details.get('codes') or None,
            'stated_dates': details.get('stated_dates') or None,
            'spend_advice': [{'item': _label(item.get('label'), 40), 'advice': item.get('advice')}
                             for item in (details.get('entries') or [])[:3] if item.get('advice')] or None,
            'entries': [{'label': _label(item.get('label'), 60), 'note': _label(item.get('note'), 160)}
                        for item in (details.get('entries') or [])[:3]] or None,
        }
        entry = {key: value for key, value in entry.items() if value not in (None, [], '')}
        blob = json.dumps(entry, ensure_ascii=False, separators=(',', ':'))
        if used + len(blob) > budget:
            bundle.append({'omitted_for_space': len(ordered) - len(bundle)})
            break
        used += len(blob)
        bundle.append(entry)
    return bundle


EXTRACTION_SYSTEM_PROMPT = ('You extract facts for a game announcement database. You never invent, extend or reword '
 'a claim: every fact must be backed by a verbatim quote from the message it describes, and anything '
 'the post does not state (dates, prices, durations, rewards) stays absent rather than guessed.')


EXTRACTION_RULES = (
    'Return ONLY a JSON array, one object per message, same order as the input. Each object: '
    '{"message_id":string,"category":"ecr|promo_code|store_offer|gear_hub|balance_change|event|community|other",'
    '"ecr_kind":"weekly|one_day|seasonal|special|unknown","title":string,"summary":string,'
    '"evidence":string,"entities":[string],"stated_dates":[string]}. '
    'Titles and summaries must contain only what the post states. "evidence" must be a verbatim substring of '
    'that message (10-160 characters) or the fact is dropped. Never add dates, prices, durations or rewards '
    'that are not written in the message. Promo codes are copied exactly, character for character.'
)


def extract_prompt(rows):
    posts = [{'message_id': str(row.get('message_id') or ''), 'posted': _date_label(_posted(row)),
              'text': normalise(row.get('content'))[:1500]} for row in rows]
    return ('You are turning game announcement posts into a typed fact table for a game bot. ' + EXTRACTION_RULES
            + ' ECR types: weekly rotations cover a weapon plus a mech or pilot and have one grand prize; '
              'credit/A-coin/mod ECRs last one day; seasonal ECRs have multiple grand prizes. '
              'Use that to choose ecr_kind, but never write the schedule into a summary. '
              'Messages:\n' + json.dumps(posts, ensure_ascii=False, separators=(',', ':')))


def parse_json_array(text):
    """Best-effort JSON array extraction from a model reply (fences, prose, trailing commas)."""
    value = str(text or '').strip()
    value = re.sub(r'^```(?:json)?|```$', '', value, flags=re.I | re.M).strip()
    start, end = value.find('['), value.rfind(']')
    if start < 0 or end <= start:
        return []
    blob = value[start:end + 1]
    try:
        parsed = json.loads(blob)
    except Exception:
        try:
            parsed = json.loads(re.sub(r',\s*([}\]])', r'\1', blob))
        except Exception:
            return []
    return parsed if isinstance(parsed, list) else []


def _evidence_key(text):
    """Markup-free comparison form, so a quote is matched on what it says rather than its styling."""
    return re.sub(r'[^A-Za-z0-9]+', '', str(text or '')).casefold()


def validate_ai_facts(payload, rows):
    """Keep only AI facts that cite the source verbatim; drop the rest instead of trusting them."""
    sources = {str(row.get('message_id') or ''): _evidence_key(normalise(row.get('content'))) for row in rows}
    allowed = set(CATEGORIES)
    kinds = set(ECR_KINDS)
    accepted, rejected = [], []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get('message_id') or '')
        category = str(item.get('category') or '').casefold()
        evidence = re.sub(r'\s+', ' ', str(item.get('evidence') or '')).strip()
        key = _evidence_key(evidence)
        if message_id not in sources or category not in allowed or len(key) < 12:
            rejected.append({'message_id': message_id, 'reason': 'unknown message, category or short evidence'})
            continue
        if key not in sources[message_id]:
            rejected.append({'message_id': message_id, 'reason': 'evidence is not a verbatim quote from the post'})
            continue
        accepted.append({
            'message_id': message_id, 'category': category,
            'ecr_kind': str(item.get('ecr_kind') or 'unknown').casefold() if str(item.get('ecr_kind') or '').casefold() in kinds else 'unknown',
            'title': str(item.get('title') or '')[:110], 'summary': str(item.get('summary') or '')[:MAX_SUMMARY],
            'entities': [str(value)[:60] for value in (item.get('entities') or []) if str(value).strip()][:6],
            'stated_dates': [str(value)[:40] for value in (item.get('stated_dates') or [])][:4],
            'evidence': evidence[:200], 'model': None,
        })
    return accepted, rejected


def merge_ai_facts(facts, ai_facts, now=None):
    """Overlay validated AI extractions on the deterministic facts; disagreements keep the source quote."""
    now = _now_epoch(now)
    by_message = {fact['message_id']: fact for fact in facts}
    for item in ai_facts:
        fact = by_message.get(item['message_id'])
        if fact is None:
            continue
        details = dict(fact.get('details') or {})
        details['ai'] = {'category': item['category'], 'ecr_kind': item['ecr_kind'], 'evidence': item['evidence'],
                         'summary': item['summary'], 'title': item['title']}
        agrees = item['category'] == fact['category']
        if agrees and item['summary']:
            fact['summary'] = item['summary'][:MAX_SUMMARY]
            fact['title'] = item['title'] or fact['title']
        if item['category'] == 'ecr' and item['ecr_kind'] != fact['ecr_kind']:
            details['ai_kind_conflict'] = item['ecr_kind']
        details['ai_agrees'] = agrees
        fact['details'] = details
        if item.get('model'):
            fact['model'] = str(item['model'])[:120]
        fact['extractor'] = 'model+source-checked' if agrees else 'deterministic'
    ecrs = [fact for fact in facts if fact['category'] == 'ecr']
    for fact in ecrs:
        newer = [other for other in ecrs if other['ecr_kind'] in (fact['ecr_kind'], 'unknown')
                 and other['posted_at'] > fact['posted_at']]
        status, why = ecr_status(fact['ecr_kind'], fact['posted_at'], now, bool(newer))
        fact['status'] = status
        fact['details'] = {**dict(fact.get('details') or {}), 'status_basis': why}
    return facts


async def ai_extract_facts(rows, *, base, key, model, session=None, batch_size=4, max_batches=30,
                           timeout_seconds=45, system_prompt=None):
    """Ask the configured model to type every merged announcement post into facts.

    Batches are validated against the exact text that was sent: a reply that cannot quote the post
    verbatim is dropped rather than trusted, so a model hallucination never becomes a stored fact.
    Returns {'batches','accepted','rejected','error','facts','messages'}.
    """
    result = {'batches': 0, 'accepted': 0, 'rejected': 0, 'error': None, 'facts': [], 'messages': 0}
    if not key or not model:
        result['error'] = 'ai_not_configured'
        return result
    posts, _ = group_posts(rows)
    posts = [post for post in sorted(posts, key=lambda row: -_posted(row)) if post.get('_text')]
    result['messages'] = len(posts)
    if not posts:
        return result
    import aiohttp
    headers = {'Authorization': 'Bearer ' + str(key), 'Content-Type': 'application/json'}
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=int(timeout_seconds), connect=5,
                                                                       sock_read=int(timeout_seconds)))
    limit = min(len(posts), int(batch_size) * max(1, int(max_batches)))
    try:
        for offset in range(0, limit, int(batch_size)):
            chunk = [{'message_id': str(row.get('message_id') or ''), 'posted_at': _posted(row),
                      'content': row.get('_text') or ''} for row in posts[offset:offset + int(batch_size)]]
            chunk = [item for item in chunk if item['content']]
            if not chunk:
                continue
            payload = {'model': model,
                       'messages': [{'role': 'system', 'content': system_prompt or EXTRACTION_SYSTEM_PROMPT},
                                    {'role': 'user', 'content': extract_prompt(chunk)}],
                       'temperature': 0.1, 'max_tokens': 1600}
            try:
                async with session.post(str(base).rstrip('/') + '/chat/completions', headers=headers,
                                        json=payload, allow_redirects=False) as response:
                    if response.status != 200:
                        raise RuntimeError('AI provider returned HTTP ' + str(response.status))
                    obj = await response.json(content_type=None)
                text = obj['choices'][0]['message']['content']
            except Exception as exc:
                # A failed pass must not lose the deterministic facts; a later run retries these posts.
                result['error'] = type(exc).__name__
                break
            accepted, rejected = validate_ai_facts(parse_json_array(text if isinstance(text, str) else ''), chunk)
            for fact in accepted:
                fact['model'] = model
            result['facts'].extend(accepted)
            result['accepted'] += len(accepted)
            result['rejected'] += len(rejected)
            result['batches'] += 1
    finally:
        if own_session:
            await session.close()
    return result
