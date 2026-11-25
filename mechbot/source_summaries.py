"""Search announcements and forum snapshots as internal material for concise answers."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from .announcements import (
    _PROMO_CODE_LABEL, _PROMO_EXPIRED, _codes_from_promo_line,
    _explicit_event_dates, _explicit_partial_event_date, _no_year_date_label, _posted_timestamp,
    _title_from_content, event_like, post_date_label,
)
from .forum_archive import search_forum_threads
from .text_summaries import (clean_source_text, omit_publication_only_sentences, search_terms,
                             summarize_source_text)

_ISSUE_QUERY = re.compile(
    r"\b(?:bugs?|glitches?|issues?|problems?|crashes?|crashing|fix(?:es|ed|ing)?|"
    r"patch(?:es)?(?:\s+notes?)?|release\s+notes?|hotfix(?:es)?|balance\s+changes?)\b", re.I)
_ISSUE_CONTENT = re.compile(
    r"\b(?:bug|glitch|issue|problem|crash|fix|patch|release\s+notes?|hotfix|balance\s+change)\w*\b", re.I)
_PROMO_QUERY = re.compile(r"\b(?:promo(?:tional)?\s*[-_ ]?codes?|redeem\s+codes?|ecr\s+codes?)\b", re.I)
_EVENT_QUERY = re.compile(
    r"\b(?:event|tournament|season|battle\s+pass|event\s+hub|challenge|reward|schedule)\w*\b", re.I)
_STOPWORDS = {
    'about','any','are','been','being','can','could','did','does','for','from','get','got','have','help',
    'how','into','latest','list','looking','make','me','more','most','new','please','recent','search','show',
    'tell','that','the','their','them','there','this','those','through','what','when','where','which','who',
    'why','with','would','you','your','is','was','were','and','but','not','our','we','they','of','on','in',
    'to','a','an','it','its','my','i','up','next','current','today','tomorrow','soon','coming','happening',
    'event','events','tournament','tournaments','season','seasons','challenge','challenges','reward','rewards',
    'upcoming','coming','happening','tomorrow','soon','up','next','current','today','recent','latest','any',
    'promo','promos','promotional','code','codes','redeem','bug','bugs','glitch','glitches','issue','issues',
    'problem','problems','crash','crashes','fix','fixes','patched','patch','patches','note','notes','update',
    'updates','release','releases','hotfix','hotfixes','balance','change','changes','report','reports','known',
    'reported','working','broken','active',
}
_EVENT_SEED = 'event tournament season challenge rewards schedule'
_PROMO_SEED = 'promo code redeem gift offer'
_ISSUE_SEED = 'bug issue glitch problem crash fix patch release notes hotfix'


def wants_patch_bug_lookup(text):
    """Recognize questions that should search saved patch notes and bug reports."""
    value = str(text or '').strip()
    if not _ISSUE_QUERY.search(value):
        return False
    return bool('?' in value or re.search(
        r"\b(?:what|when|where|why|how|any|latest|recent|known|reported|fixed|fixes|"
        r"patch|bugs?|issues?|glitches?|problems?|crashes?|release\s+notes?)\b", value, re.I))


def _topic(question):
    if _PROMO_QUERY.search(question):
        return 'promo'
    if _ISSUE_QUERY.search(question):
        return 'issues'
    return 'events'


def _specific_terms(question, topic):
    return search_terms(question, stopwords=_STOPWORDS)


def _matches_query(text, specific_terms):
    if not specific_terms:
        return True
    folded = str(text or '').casefold()
    return any(term in folded for term in specific_terms)


def _forum_date(post):
    """Return the forum's displayed creation date without assigning a timezone."""
    created = post.get('created') or {}
    if isinstance(created, dict):
        raw_iso = str(created.get('iso') or '').strip()
        display = str(created.get('display') or '').strip()
    else:
        raw_iso = ''
        display = str(created or '').strip()
    match = re.match(r'(20\d{2})-(\d{1,2})-(\d{1,2})', raw_iso)
    if not match:
        match = re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b', display)
    if match:
        year, month, day = map(int, match.groups())
        try:
            parsed = datetime(year, month, day).date()
            return parsed.isoformat(), parsed.strftime('%b %-d, %Y')
        except (TypeError, ValueError):
            pass
    month_day = re.search(r'\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b', display)
    if month_day:
        try:
            parsed = datetime.strptime(f'{month_day[1]} {month_day[2]} {month_day[3]}', '%b %d %Y').date()
            return parsed.isoformat(), parsed.strftime('%b %-d, %Y')
        except ValueError:
            try:
                parsed = datetime.strptime(f'{month_day[1]} {month_day[2]} {month_day[3]}', '%B %d %Y').date()
                return parsed.isoformat(), parsed.strftime('%b %-d, %Y')
            except ValueError:
                pass
    return '', display[:60]


def _discord_date_label(timestamp):
    """Render a stable absolute publication date instead of a viewer-dependent timestamp."""
    return post_date_label(timestamp)


def _today(now=None):
    stamp = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    return datetime.fromtimestamp(stamp, ZoneInfo('Asia/Kolkata')).date()


def _event_date(text, today):
    dates = _explicit_event_dates(str(text or ''))
    future = next((entry for entry in dates if entry[2] >= today), None)
    if future:
        return future[3], 'dated', future[2]
    if dates:
        return dates[0][3], 'past', dates[0][2]
    # A stated month and day with no year ("ends Nov 30") is a date the post did print. It is
    # dated in from the post's own clock, not invented: the current year, or the next one when
    # that day has clearly gone by. Without this, a two-week window read as "date not stated".
    stated = _no_year_date_label(str(text or ''), today)
    if stated:
        return stated, 'partial', None
    partial = _explicit_partial_event_date(str(text or ''))
    if partial:
        return partial, 'partial', None
    return '', 'missing', None


def _post_date(record):
    if record.get('source_kind') == 'announcement':
        return _discord_date_label(record.get('posted_at'))
    return record.get('published_label') or 'date not recorded'


def _record_text(record):
    text = clean_source_text(record.get('raw_text') or record.get('text') or '')
    title = clean_source_text(record.get('title') or '')
    if title and text.casefold().startswith(title.casefold()):
        text = text[len(title):].lstrip(' \t\n:—–-')
    return text


def _summarize_record(record, question, topic, limit=285):
    terms = _specific_terms(question, topic)
    text = _record_text(record)
    if topic == 'events':
        text = omit_publication_only_sentences(text)
    if topic == 'promo':
        terms = [*terms, 'code', 'promo']
    elif topic == 'issues':
        terms = [*terms, 'bug', 'issue', 'fix', 'patch', 'crash']
    else:
        terms = [*terms, 'event', 'starts', 'rewards', 'available', 'date']
    return summarize_source_text(text, query_terms=terms, max_chars=limit, max_sentences=3)


def _announcement_records(rows, question, topic, *, now, upcoming):
    today = _today(now)
    terms = _specific_terms(question, topic)
    records = []
    for row in sorted(rows or [], key=_posted_timestamp, reverse=True):
        raw = str(row.get('content') or '').strip()
        if not raw:
            continue
        title = _title_from_content(raw)
        event_source = '\n'.join(part for part in (title, raw) if part)
        searchable = f'{title} {raw}'
        if topic == 'promo':
            if not _PROMO_CODE_LABEL.search(raw):
                continue
        elif topic == 'issues':
            if not _ISSUE_CONTENT.search(searchable):
                continue
        elif not event_like(searchable):
            continue
        if not _matches_query(searchable, terms):
            continue
        posted_at = row.get('posted_at') or 0
        if topic == 'events' and upcoming:
            event_label, event_status, event_end = _event_date(event_source, today)
            if event_status == 'past':
                continue
            try:
                posted = int(posted_at or 0)
            except (TypeError, ValueError):
                posted = 0
            if event_status in ('missing', 'partial') and posted and posted < float(now) - 45 * 86400:
                continue
        records.append({
            'source_kind': 'announcement', 'title': title, 'raw_text': raw,
            'text': clean_source_text(raw), 'posted_at': posted_at,
            'url': str(row.get('message_url') or ''),
            'event_date_label': _event_date(event_source, today)[0] if topic == 'events' else '',
            'event_date_status': _event_date(event_source, today)[1] if topic == 'events' else '',
            'sort_date': _discord_sort_date(posted_at),
        })
    return records


def _discord_sort_date(timestamp):
    try:
        stamp = int(timestamp or 0)
        if stamp > 0:
            return datetime.fromtimestamp(stamp, ZoneInfo('Asia/Kolkata')).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return ''


def _forum_records(question, topic, *, now, upcoming):
    terms = _specific_terms(question, topic)
    seed = {'promo': _PROMO_SEED, 'issues': _ISSUE_SEED, 'events': _EVENT_SEED}[topic]
    queries = [question]
    if seed:
        queries.append(f'{question} {seed}')
    records = []
    seen = set()
    today = _today(now)
    for query in queries:
        if not search_terms(query):
            continue
        for post in search_forum_threads(query, limit=10):
            raw = str(post.get('content') or post.get('excerpt') or '').strip()
            title = str(post.get('title') or '').strip()
            searchable = f'{title} {raw}'
            if topic == 'promo' and not _PROMO_CODE_LABEL.search(raw):
                continue
            if topic == 'issues' and not _ISSUE_CONTENT.search(searchable):
                continue
            if topic == 'events' and not event_like(searchable):
                continue
            if not _matches_query(searchable, terms):
                continue
            event_source='\n'.join(part for part in (title,raw) if part)
            if topic == 'events' and upcoming:
                event_label, event_status, event_end = _event_date(event_source, today)
                if event_status == 'past':
                    continue
                iso, __ = _forum_date(post)
                if event_status in ('missing', 'partial') and iso:
                    try:
                        published = datetime.fromisoformat(iso).date()
                        if (today - published).days > 45:
                            continue
                    except ValueError:
                        pass
            key = str(post.get('url') or title.casefold())
            if key in seen:
                continue
            seen.add(key)
            iso, label = _forum_date(post)
            records.append({
                'source_kind': 'forum', 'title': title or 'Forum discussion',
                'raw_text': raw, 'text': clean_source_text(raw),
                'created_iso': iso, 'published_label': label,
                'url': str(post.get('url') or ''),
                'event_date_label': _event_date(event_source, today)[0] if topic == 'events' else '',
                'event_date_status': _event_date(event_source, today)[1] if topic == 'events' else '',
                'sort_date': iso,
                'author_role': str((post.get('author') or {}).get('role') or ''),
            })
    return records


def _sort_records(records):
    return sorted(records, key=lambda record: (record.get('sort_date') or '',
                                                record.get('source_kind') == 'announcement'), reverse=True)


def _dedupe_records(records):
    selected = []
    seen = set()
    for record in _sort_records(records):
        title = re.sub(r'\W+', ' ', str(record.get('title') or '').casefold()).strip()
        if title and title in seen:
            continue
        if title:
            seen.add(title)
        selected.append(record)
    return selected


def _promo_mentions(records):
    mentions = {}
    for record in records:
        raw = str(record.get('raw_text') or '')
        for line in raw.splitlines() or [raw]:
            for code in _codes_from_promo_line(line):
                key = code.casefold()
                status = 'marked_expired' if _PROMO_EXPIRED.search(raw) else 'validity_unknown'
                item = {
                    'code': code, 'status': status, 'record': record,
                    'sort_date': record.get('sort_date') or '',
                    'source_kind': record.get('source_kind'),
                }
                current = mentions.get(key)
                if current is None or item['sort_date'] > current['sort_date']:
                    mentions[key] = item
    return sorted(mentions.values(), key=lambda item: item['sort_date'], reverse=True)


# A saved post that describes the game as it was is not a description of the game as it is. The
# event answer prints that difference as one line per post, so an old thread cannot be read as a
# live schedule - the same judgement the forum and ECR answers make.
_LIVE_CLAIM = re.compile(r'\b(?:is|are)\s+(?:currently|now)\b|\bcurrently\b|\bright\s+now\b|'
                         r'\b(?:today|this\s+week|this\s+patch|this\s+update)\b', re.I)
_STANDING_WINDOW_DAYS = {'events': 45, 'issues': 120}


def _record_days_old(record, now):
    """Days since the post was made, from whichever field this record type carries it in."""
    def from_iso(value):
        match = re.match(r'(20\d{2})-(\d{1,2})-(\d{1,2})', str(value or ''))
        if not match:
            return None
        try:
            year, month, day = map(int, match.groups())
            return datetime(year, month, day, tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None

    stamp = 0.0
    if record.get('source_kind') == 'announcement':
        try:
            stamp = float(record.get('posted_at') or 0)
        except (TypeError, ValueError):
            stamp = 0.0
    else:
        # The forum record is flattened: the date lives in created_iso, and only sometimes in a
        # nested created dict. Reading the wrong one made every thread look undated.
        stamp = from_iso(record.get('created_iso')) or from_iso(
            (record.get('created') or {}).get('iso') if isinstance(record.get('created'), dict)
            else record.get('created')) or 0.0
    if not stamp:
        return None
    return max(0.0, (float(now) - stamp) / 86400)


def _record_standing(record, topic, now=None):
    """One line: is this saved post a statement about the live game, and why."""
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if topic == 'events':
        status = record.get('event_date_status') or 'missing'
        label = record.get('event_date_label') or ''
        if status == 'dated' and label:
            return f'Live — the post states the event date: {label}.'
        if status == 'past':
            return 'Outdated — the date the post states has passed.'
    age = _record_days_old(record, current)
    window = _STANDING_WINDOW_DAYS.get(topic, 90)
    if age is None:
        return 'Live — the saved post carries no date, so its age cannot be checked.'
    if age < 1:
        return 'Live — posted today, so it describes the game as it stands.'
    if age <= window:
        return (f'Live — posted {int(round(age))} day{"s" if round(age) != 1 else ""} ago, '
                f'inside the window this kind of post stays current.')
    months = int(age // 30)
    age_words = f'{months} month{"s" if months != 1 else ""} ago' if months else 'weeks ago'
    claim = bool(_LIVE_CLAIM.search(str(record.get('raw_text') or record.get('text') or '')))
    if claim:
        return (f'Outdated — posted {age_words}, and it describes the game as it stood then. '
                f'Later patches are not reflected in it.')
    return (f'Outdated — posted {age_words}, so it is an archived post rather than a statement '
            f'about the current build.')


def combined_source_summary(question, announcement_rows=(), *, topic=None, now=None, limit=4):
    """Summarize matching source material from saved announcements and forum posts."""
    value = str(question or '').strip()
    selected_topic = topic or _topic(value)
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    upcoming = selected_topic == 'events'
    records = _dedupe_records([
        *_announcement_records(announcement_rows, value, selected_topic, now=current, upcoming=upcoming),
        *_forum_records(value, selected_topic, now=current, upcoming=upcoming),
    ])
    if selected_topic == 'promo':
        codes = _promo_mentions(records)[:max(0, min(int(limit), 10))]
        if not codes:
            return ('I couldn’t find a clearly labeled promo code in the saved announcement or forum posts. '
                    'I won’t guess from unlabeled mentions; validity must still be checked in-game.')
        lines = ['**Promo-code summary**']
        for item in codes:
            status = 'the post marks it expired' if item['status'] == 'marked_expired' else 'validity not confirmed'
            record = item['record']
            posted = _post_date(record)
            date_note = f' · posted {posted}' if posted != 'date not recorded' else ''
            lines.append(f"• `{item['code']}` — {status}{date_note}")
        lines.append('Codes are not verified in-game, and newer posts can change their status.')
        return '\n'.join(lines)
    if not records:
        label = 'event or schedule details' if selected_topic == 'events' else 'matching patch notes or bug reports'
        return f'I couldn’t find {label} in the saved announcement and forum material. I won’t fill in missing details from memory.'
    heading = '**Event summary**' if selected_topic == 'events' else '**Patch and bug summary**'
    lines = [heading]
    count = max(1, min(int(limit), 6))
    standings = []
    for number, record in enumerate(records[:count], start=1):
        title = clean_source_text(record.get('title') or 'Saved post', limit=120).replace('[', '').replace(']', '')
        # The event date is read off this record's own text, not carried on it: a post that
        # states "starts Nov 24, ends Nov 30" was being printed as "event date not stated".
        source = '\n'.join(part for part in (title, str(record.get('raw_text') or record.get('text') or '')) if part)
        event_label, event_status, _event_end = _event_date(source, _today(current))
        record = dict(record, event_date_label=event_label, event_date_status=event_status)
        detail = _summarize_record(record, value, selected_topic)
        if not detail:
            detail = 'The saved post has no readable details beyond its title.'
        # Numbered blocks, one per post, in the same shape as the ECR and forum answers: the
        # detail, the dates, and then whether the post still speaks for the live game.
        lines.append(f'{number}. **{title}**')
        lines.append(f'\u2022 {detail}')
        if selected_topic == 'events':
            event_label = record.get('event_date_label') or ''
            status = record.get('event_date_status') or 'missing'
            posted = _post_date(record)
            if status == 'dated' and event_label:
                lines.append(f'\u2022 **Event date:** {event_label} · **Post date:** {posted}')
            elif status == 'partial' and event_label:
                lines.append(f'\u2022 **Event date mentioned:** {event_label} (year not stated) · '
                             f'**Post date:** {posted}')
            else:
                lines.append(f'\u2022 **Event date not stated · Post date:** {posted} '
                             f'*(publication date, not the event date)*')
        elif record.get('sort_date'):
            lines.append(f'\u2022 **Post date:** {_post_date(record)}')
        standing = _record_standing(record, selected_topic, now=current)
        lines.append(f'**Status:** {standing}')
        standings.append((title, standing.lower().startswith('live')))
    if len(standings) > 1:
        rollup = ['**LIVE OR OUTDATED**', 'Post | Standing']
        rollup.extend(f'{title} | {"Live" if live else "Outdated"}' for title, live in standings)
        lines.append('\n'.join(rollup))
    return '\n\n'.join(lines)
