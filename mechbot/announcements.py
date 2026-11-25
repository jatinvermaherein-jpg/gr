"""Safe ingestion and date-aware presentation for configured announcement channels.

Post-ingestion answers come from the curated fact table (see facts.py); these helpers remain the fallback
while no facts are stored, and they provide the shared message normalisation and intent patterns.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import re

from .text_summaries import clean_source_text, omit_publication_only_sentences, summarize_source_text

DEFAULT_ANNOUNCEMENT_CHANNELS = (
    1524319795791659080,
    1570083896823906364,
)
ANNOUNCEMENT_SYNC_SECONDS = 600
INITIAL_BACKFILL_LIMIT = 100
INCREMENTAL_LIMIT = 100
EDIT_REFRESH_LIMIT = 25
ANNOUNCEMENT_MAX_CHARS = 12000
ANNOUNCEMENT_LOOKBACK_DAYS = 45
MAX_UPCOMING_ITEMS = 8
MAX_PROMO_CODE_ITEMS = 10

_PROMO_CONTEXT = re.compile(
    r'\b(?:promo(?:tional)?(?:[\s_-]*codes?)?|ecr\s+(?:(?:promo(?:tional)?[\s_-]+)?codes?|promo(?:tional)?)|'
    r'event\s+crate\s+rush\s+(?:(?:promo(?:tional)?[\s_-]+)?codes?|promo(?:tional)?))\b', re.I)
_PROMO_QUERY_START = re.compile(
    r"^\s*(?:any|what(?:['’]s|\s+is|\s+are)?|which|where|can\s+you|could\s+you|"
    r"do\s+we\s+have|do\s+you\s+have|is\s+there|are\s+there|have\s+we|"
    r"i\s+(?:need|want)|please|show|list|find|get|share|give|send|tell\s+me|look\s+up|"
    r"latest|current|recent|new|active|working|valid|expired)\b", re.I)
_PROMO_CODE_LABEL = re.compile(
    r'\b(?:ecr\s+promo(?:tional)?(?:[\s_-]+codes?)?|ecr\s+codes?|'
    r'event\s+crate\s+rush\s+(?:promo(?:tional)?[\s_-]+)?codes?|'
    r'promo(?:tional)?(?:[\s_-]*codes?)?|redeem\s+codes?)\b', re.I)
_ECR_REFERENCE = re.compile(r'\b(?:ecrs?|event\s+crates?\s+rush(?:es)?)\b', re.I)
_PROMO_CODE_TOKEN = re.compile(r'(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9_-]{2,31})(?![A-Za-z0-9])')
_PROMO_CODE_STOPWORDS = {'CODE','CODES','PROMO','PROMOS','ECR','REDEEM','USE','HERE','BELOW','ABOVE',
                         'AVAILABLE','VALID','EXPIRED','INVALID','NONE','NULL','TBA','NOW','PLEASE'}
_PROMO_EXPIRED = re.compile(
    r'\b(?:promo(?:tional)?\s+)?codes?\s+(?:(?:is|are|has|have)(?:\s+now)?\s+)?'
    r'(?:expired|invalid|no\s+longer\s+valid)\b|'
    r'\b(?:expired|invalid)\s+(?:promo(?:tional)?\s+)?codes?\b|'
    r'\b[A-Z0-9][A-Z0-9_-]{3,31}\s+(?:(?:has|is|was)\s+)?(?:expired|invalid|no\s+longer\s+valid)\b', re.I)

_EVENT_WORDS = re.compile(
    r"\b(?:event|tournament|season|pilot\s+challenge|battle\s+pass|event\s+hub|"
    r"limited[- ]time|coming\s+soon|upcoming|anniversary|birthday|special\s+offer|"
    r"update|patch|release|rotation|challenge|reward)\b",
    re.I,
)
_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7,
    'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
_MONTH_PATTERN = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
_MONTH_FIRST = re.compile(
    rf'\b(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'(?:\s*(?:-|–|—|to|through)\s*(?P<endday>\d{{1,2}})(?:st|nd|rd|th)?)?'
    rf',?\s+(?P<year>20\d{{2}})\b', re.I)
_DAY_FIRST = re.compile(
    rf'\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_PATTERN})'
    rf'\s+(?P<year>20\d{{2}})\b', re.I)
_ISO_DATE = re.compile(r'\b(?P<year>20\d{2})-(?P<month>0?[1-9]|1[0-2])-(?P<day>0?[1-9]|[12]\d|3[01])\b')
_PARTIAL_DATE = re.compile(rf'\b(?:{_MONTH_PATTERN}\s+\d{{1,2}}|\d{{1,2}}\s+{_MONTH_PATTERN})\b', re.I)
_ANNOUNCEMENT_LOOKUP = re.compile(
    r"\b(?:upcoming|coming\s+up|what(?:'s|\s+is)\s+next|next\s+(?:event|update|tournament|season)|"
    r"events?\s+(?:soon|next|coming|today|tomorrow|this\s+week|this\s+month)|what\s+is\s+coming\s+up|"
    r"(?:any|current|latest|recent)\s+(?:official\s+)?events?|"
    r"(?:what|which)\s+(?:the\s+)?(?:(?:current|upcoming|recent|latest)\s+)?events?|"
    r"are\s+there\s+(?:any\s+)?(?:(?:current|upcoming|recent|latest)\s+)?events?|"
    r"(?:do\s+we\s+have|have\s+we\s+got)\s+(?:any\s+)?(?:(?:current|upcoming|recent|latest)\s+)?events?|"
    r"(?:show|list|tell\s+me\s+about)\s+(?:the\s+|any\s+)?(?:(?:current|upcoming|recent|latest)\s+)?events?)\b", re.I)


def wants_upcoming(text):
    """Recognize event-information requests to answer from saved announcements, not memory."""
    value=str(text or '').strip()
    if re.search(r'\b(?:remind(?:er)?|remember|note)\b',value,re.I):return False
    return bool(_ANNOUNCEMENT_LOOKUP.search(value))


def wants_promo_codes(text):
    """Recognize explicit code requests; a bare ECR query is event information, not a code lookup."""
    value=str(text or '').strip()
    compact=re.sub(r'\s+',' ',value).strip(' .!?').casefold()
    if not _PROMO_CONTEXT.search(value):return False
    bare={'promo','promo code','promo codes','promocode','promocodes','promo-code','promo-codes',
          'ecr code','ecr codes','ecr promo','ecr promo code','ecr promo codes',
          'event crate rush promo codes'}
    return bool(compact in bare or '?' in value or _PROMO_QUERY_START.match(value)
                or re.search(r'\bplease[.!]*$',value,re.I))


def wants_ecr_info(text):
    """Recognize Event Crate Rush questions separately from explicit promo-code requests."""
    value=str(text or '').strip()
    if not _ECR_REFERENCE.search(value):return False
    if re.search(r'\b(?:promo(?:tional)?|redeem|codes?)\b',value,re.I):return False
    compact=re.sub(r'\s+',' ',value).strip(' .!?').casefold()
    bare={'ecr','any ecr','latest ecr','current ecr','recent ecr','upcoming ecr','event crate rush'}
    if compact in bare:return True
    # "previous ecrs" / "which ecrs ran last month" is a request for the archive, and the
    # archive answer lives behind this same handler. Its wording matched nothing above once the
    # plural was used, so the question fell through to the general model path.
    if re.match(r'^(?:(?:show|list)\s+)?(?:all\s+)?(?:(?:previous|older|earlier|past|last|prior|'
                r'failed|ended|other)\s+)+(?:weekly\s+)?ecrs?\b',compact):
        return True
    if re.search(r'\becrs?\s+(?:before|prior|history|archive|so far)\b',compact):
        return True
    # "which ecrs ran last month", "ecrs that ran in October": the archive again, asked as a
    # question rather than as a noun phrase.
    if re.search(r'\becrs?\b.*\b(?:ran|run|happened|were\s+there|did\s+we\s+have)\b',compact):
        return True
    return bool('?' in value or re.search(
        r'\b(?:what|when|where|why|how|tell|show|latest|current|recent|upcoming|next|event|'
        r'schedule|details?|guides?|updates?|status|rewards?|live|open|active)\b',value,re.I))


def _looks_like_promo_code(value):
    value=str(value or '').strip().strip('`*_~#[](){}')
    upper=value.upper()
    if len(value)<4 or len(value)>32 or upper in _PROMO_CODE_STOPWORDS:return False
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,31}',value):return False
    return value.isupper() or any(ch.isdigit() for ch in value) or '-' in value or '_' in value


def _codes_from_promo_line(line):
    label=_PROMO_CODE_LABEL.search(str(line or ''))
    if not label:return []
    tail=str(line)[label.end():]
    tail=re.sub(r'^\s*(?:(?:is|are|equals)\s+|(?:code|codes)\s*[:=]\s*|[:=–—-]\s*)','',tail,flags=re.I)
    tail=re.split(r'\b(?:expires?|expiry|valid\s+(?:until|through)|ends?\s+(?:on|at)|use\s+by|redeem\s+by)\b',tail,maxsplit=1,flags=re.I)[0]
    if not tail.strip():return []
    quoted=re.findall(r'`([^`]{1,80})`',tail)
    if quoted:
        raw_values=[token for chunk in quoted for token in _PROMO_CODE_TOKEN.findall(chunk)]
    else:
        tail=re.sub(r'^[\s`*_~#\[\]]+','',tail)
        first=re.match(r'([A-Za-z0-9][A-Za-z0-9_-]{2,31})(?![A-Za-z0-9])',tail)
        if not first:return []
        raw_values=[first.group(1)]
        cursor=first.end()
        while True:
            separator=re.match(r'\s*(?:[,/|;&]|\band\b|\bor\b)\s*',tail[cursor:],re.I)
            if not separator:break
            next_start=cursor+separator.end()
            following=re.match(r'([A-Za-z0-9][A-Za-z0-9_-]{2,31})(?![A-Za-z0-9])',tail[next_start:])
            if not following:break
            raw_values.append(following.group(1))
            cursor=next_start+following.end()
    result=[];seen=set()
    for value in raw_values:
        value=str(value).strip().strip('`*_~#[](){}')
        key=value.casefold()
        if _looks_like_promo_code(value) and key not in seen:
            result.append(value);seen.add(key)
    return result


def promo_code_items(rows,limit=MAX_PROMO_CODE_ITEMS):
    """Extract only clearly labeled promo codes and keep each message's provenance."""
    limit=max(0,int(limit))
    if not limit:return []
    items=[];seen=set()
    ordered=sorted(rows or [],key=_posted_timestamp,reverse=True)
    for row in ordered:
        text=str(row.get('content') or '')
        values=[]
        for line in text.splitlines():values.extend(_codes_from_promo_line(line))
        status='marked_expired' if _PROMO_EXPIRED.search(text) else 'validity_unknown'
        for code in values:
            key=code.casefold()
            if key in seen:continue
            seen.add(key)
            items.append({'code':code,'status':status,'posted_at':_posted_timestamp(row),
                          'source_label':_source_label(row),'url':str(row.get('message_url') or '')})
            if len(items)>=limit:return items
    return items


def format_promo_codes(rows,*,enabled,access_states=None,limit=MAX_PROMO_CODE_ITEMS):
    """Show recent explicit promo-code mentions without claiming that an unverified code works."""
    rows=list(rows or [])
    items=promo_code_items(rows,limit=limit)
    if not items:
        if not enabled:
            return ('Promo-code lookup has no saved results yet. To backfill and monitor the configured channels, '
                    'enable announcement ingestion, the Discord Message Content intent, and channel read access.')
        if access_states and any(state!='available' for state in access_states.values()):
            return 'I haven’t saved a clearly labeled promo-code post yet. One or more configured channels are not readable; I’ll retry when access is available.'
        if rows:
            return 'I found no clearly labeled promo codes in the saved announcement posts. I won’t guess from unlabeled text.'
        return 'I haven’t saved announcement posts from the configured channels yet. I’ll keep retrying their initial backfill and updates.'
    lines=['**Promo-code summary**']
    footer='Codes are not verified in-game; validity can change after a post is published.'
    if not enabled:footer+=' Announcement ingestion is off, so these cached mentions may be stale.'
    for item in items:
        status='the post marks it expired' if item['status']=='marked_expired' else 'validity not confirmed'
        posted=post_date_label(item['posted_at'])
        line=f"• `{item['code']}` — {status} · posted {posted}"
        if len(chr(10).join([*lines,line,footer]))>1900:
            lines.append('• More mentions omitted to fit Discord.')
            break
        lines.append(line)
    return chr(10).join([*lines,footer])


def _safe_announcement_text(text):
    value = str(text or '').replace('\x00', '')
    value = re.sub(r'<@!?\d+>|<@&\d+>', '[member]', value)
    value = re.sub(r'@(everyone|here)\b', lambda match: '@\u200b' + match.group(1), value, flags=re.I)
    return value.strip()


def message_text(message):
    """Collect only readable text from a source message and its embeds; never follow embedded URLs."""
    chunks = []
    content = getattr(message, 'content', None)
    if content:
        chunks.append(str(content))
    for embed in getattr(message, 'embeds', ()) or ():
        title = getattr(embed, 'title', None)
        description = getattr(embed, 'description', None)
        if title:
            chunks.append(str(title))
        if description:
            chunks.append(str(description))
        for field in getattr(embed, 'fields', ()) or ():
            name = getattr(field, 'name', None)
            value = getattr(field, 'value', None)
            if name:
                chunks.append(str(name))
            if value:
                chunks.append(str(value))
    text = '\n'.join(x.strip() for x in chunks if x and x.strip())
    return _safe_announcement_text(text)[:ANNOUNCEMENT_MAX_CHARS]


def event_like(text):
    return bool(_EVENT_WORDS.search(str(text or '')))


def _posted_timestamp(row):
    """Return a safe integer publication timestamp, treating malformed values as absent."""
    try:
        return int(row.get('posted_at') or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


def post_date_label(timestamp):
    """Render an announcement's publication date as an absolute Asia/Kolkata date."""
    try:
        stamp=int(timestamp or 0)
        if stamp<=0:
            return 'date not recorded'
        posted=datetime.fromtimestamp(stamp,ZoneInfo('Asia/Kolkata')).date()
        return f'{posted.strftime("%b")} {posted.day}, {posted.year}'
    except (TypeError,ValueError,OverflowError,OSError):
        return 'date not recorded'


def _date_candidates(text):
    """Return dates that include an explicit year; never infer a missing year or timezone."""
    candidates = []
    for match in _ISO_DATE.finditer(str(text or '')):
        try:
            start = date(int(match['year']), int(match['month']), int(match['day']))
        except ValueError:
            continue
        candidates.append((match.start(), start, start, start.isoformat()))
    for match in _MONTH_FIRST.finditer(str(text or '')):
        month_name = match['month'].casefold()
        month = _MONTH_NAMES.get(month_name, _MONTH_NAMES.get(month_name[:3]))
        try:
            start = date(int(match['year']), month, int(match['day']))
            end = date(int(match['year']), month, int(match['endday'])) if match['endday'] else start
        except (TypeError, ValueError):
            continue
        label = f"{match['month']} {int(match['day'])}"
        if match['endday']:
            label += f"–{int(match['endday'])}"
        label += f", {match['year']}"
        candidates.append((match.start(), start, end, label))
    for match in _DAY_FIRST.finditer(str(text or '')):
        month_name = match['month'].casefold()
        month = _MONTH_NAMES.get(month_name, _MONTH_NAMES.get(month_name[:3]))
        try:
            start = date(int(match['year']), month, int(match['day']))
        except (TypeError, ValueError):
            continue
        label = f"{int(match['day'])} {match['month']} {match['year']}"
        candidates.append((match.start(), start, start, label))
    return sorted(candidates, key=lambda item: item[0])


def _local_today(now):
    return datetime.fromtimestamp(float(now), ZoneInfo('Asia/Kolkata')).date()


def _title_from_content(text, limit=150):
    """Extract a compact label without treating the whole first body sentence as a title."""
    for raw_line in str(text or '').splitlines():
        raw_line=str(raw_line or '').strip()
        if not raw_line:
            continue
        is_heading=bool(re.match(r'^#{1,6}\s+',raw_line))
        cleaned_line=omit_publication_only_sentences(raw_line)
        if not cleaned_line:
            continue
        line=clean_source_text(cleaned_line,limit=500).strip(' :—–-#')
        if not line or re.fullmatch(r'@(?:everyone|here)',line,flags=re.I):
            continue
        if is_heading:
            return line[:limit].rstrip()
        # A short prefix before a colon is a common explicit announcement heading.
        colon=re.search(r'\s*[:：]\s*',line)
        if colon and 2<=colon.start()<=85 and colon.end()<len(line):
            heading=line[:colon.start()].strip()
            if heading:
                return heading[:limit].rstrip()
        # Separate a compact lead-in from body text in one-line announcements.
        semicolon=re.search(r'\s*[;]\s*',line)
        if semicolon and 6<=semicolon.start()<=90:
            heading=line[:semicolon.start()].strip()
            if heading:
                return heading[:limit].rstrip()
        action=re.search(r'\b(?:starts?|begins?|launch(?:es)?|opens?|runs?|ends?|held|occurs?|takes?\s+place)\b',line,flags=re.I)
        if action and action.start()>0:
            subject=line[:action.start()]
            subject=re.split(r'[.!?;\n]',subject)[-1].strip(' :—–-')
            subject=re.sub(r'\b(?:will|is|are|was|were)\s*$','',subject,flags=re.I).strip()
            if 2<=len(subject)<=85:
                return subject[:limit].rstrip()
        if len(line)<=100 and not re.search(r'[.!?]',line):
            return line[:limit].rstrip()
        return 'Event update' if event_like(line) else 'Announcement'
    cleaned=clean_source_text(omit_publication_only_sentences(text),limit=500)
    return (cleaned[:limit].rstrip() or ('Event update' if event_like(text) else 'Announcement'))


def _post_summary(text,title='',limit=250):
    cleaned=omit_publication_only_sentences(text)
    if title and cleaned.casefold().startswith(title.casefold()):
        cleaned=cleaned[len(title):].lstrip(' :—–-')
    return summarize_source_text(cleaned,query_terms=('event','reward','starts','available','price','free','pilot','weapon'),
                                 max_chars=limit,max_sentences=2)


_EVENT_DATE_SUBJECT=(r'(?:event\s+crate\s+rush|ecr|battle\s+pass|event|tournament|season|challenge|'
                     r'playoff|playoffs|onslaught|festival|celebration|crate\s+rush)')
_EVENT_DATE_MARKER=r'(?:date|schedule|scheduled|on|from|until|through|starts?|begins?|starting|launch(?:es)?|opens?|runs?|ends?|held|occurred)'
_EVENT_DATE_CUE=re.compile(
    r'\b(?:date|schedule|scheduled|starts?|begins?|starting|launch(?:es)?|opens?|runs?|ends?|'
    r'held|occurred|occurs?|takes?\s+place|from|until|through)\b',re.I)
_POSTING_CUE=re.compile(
    r'\b(?:post(?:s|ed)?|publish(?:es|ed|ing|ation)?|creat(?:es|ed|ion)|shar(?:es|ed|ing)|'
    r'upload(?:s|ed|ing)?|announc(?:es|ed|ement|ements)|releas(?:es|ed)|updat(?:es|ed))\b',re.I)


def _has_event_date_context(text,position,length=0):
    """Accept explicit event timing, not dates that describe the announcement itself."""
    value=str(text or '')
    before=value[max(0,position-140):position]
    after=value[position+length:position+length+100]
    # Bound the context to one clause so a publication date in one sentence cannot
    # become the event date for the next sentence (or vice versa).
    clause_before=re.split(r'[.!?;\n]',before)[-1]
    clause_after=re.split(r'[.!?;,\n]',after)[0]
    subject=rf'\b{_EVENT_DATE_SUBJECT}\b'
    subject_matches=list(re.finditer(subject,clause_before,flags=re.I))
    if subject_matches:
        tail=clause_before[subject_matches[-1].end():]
        cues=list(_EVENT_DATE_CUE.finditer(tail))
        posting=list(_POSTING_CUE.finditer(tail))
        last_post=posting[-1].end() if posting else -1
        if cues and cues[-1].start()>=last_post:
            return True
        # “The event is on November 15” is explicit, but “ECR announcement on
        # November 9” and “post published on November 9” are publication dates.
        if re.search(r'\bon\b',tail,flags=re.I) and not posting:
            return True
    # Date first, then an event-start statement: “Nov 15 — the tournament begins”.
    if re.search(rf'{subject}.{{0,45}}{_EVENT_DATE_MARKER}\b',clause_after,flags=re.I):
        return True
    return False


_NO_YEAR_DATE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?!\s*[,]?\s*20\d{2})", re.I)


def _no_year_date_label(text, today):
    """The first stated month-and-day the post gives without a year, as a label; '' if none.

    Only a date the post actually printed is used, and only where the sentence is about when
    the event runs - the same clause gate the dated case uses. The year it belongs to is read
    from the post's own clock: this year, or the next one when that day has clearly gone by,
    which is how a post written in December reads "Jan 5".
    """
    for match in _NO_YEAR_DATE.finditer(str(text or '')):
        if not _has_event_date_context(text, match.start(), len(match.group(0))):
            continue
        month_name = match['month']
        month = _MONTH_NAMES.get(month_name.casefold(), _MONTH_NAMES.get(month_name.casefold()[:3]))
        if not month:
            continue
        for year in (today.year + 1, today.year):
            try:
                parsed = date(year, month, int(match['day']))
            except ValueError:
                continue
            if (parsed - today).days >= -31:
                return f'{month_name} {int(match["day"])}'
    return ''


def _explicit_event_dates(text):
    return [entry for entry in _date_candidates(text)
            if _has_event_date_context(text,entry[0],len(entry[3]))]


def _explicit_partial_event_date(text):
    for match in _PARTIAL_DATE.finditer(str(text or '')):
        if _has_event_date_context(text,match.start(),len(match.group(0))):
            return match.group(0)
    return ''


def upcoming_items(rows, now=None, lookback_days=ANNOUNCEMENT_LOOKBACK_DAYS, limit=MAX_UPCOMING_ITEMS):
    """Extract dated future events and recent posts without an identifiable event date."""
    now=datetime.now(timezone.utc).timestamp() if now is None else float(now)
    today=_local_today(now)
    result=[]
    for row in rows:
        text=str(row.get('content') or '')
        if not text or not event_like(text):
            continue
        posted=_posted_timestamp(row)
        title=_title_from_content(text)
        summary=_post_summary(text,title)
        dates=_explicit_event_dates(text)
        future=next((entry for entry in dates if entry[2]>=today),None)
        partial=_explicit_partial_event_date(text)
        if future:
            _,start,__,label=future
            result.append({
                'dated':True,'event_date_status':'dated','date_label':label,
                'sort_date':start,'title':title,'summary':summary,
                'source_label':_source_label(row),'url':str(row.get('message_url') or ''),
                'posted_at':posted,
            })
        elif not dates and posted>=now-int(lookback_days)*86400:
            result.append({
                'dated':False,'event_date_status':'partial' if partial else 'missing',
                'date_label':partial or 'Date not stated','sort_date':None,
                'title':title,'summary':summary,'source_label':_source_label(row),
                'url':str(row.get('message_url') or ''),'posted_at':posted,
            })
    result.sort(key=lambda item:(not item['dated'],item['sort_date'] or date.max,-item['posted_at']))
    return result[:max(0,int(limit))]


def _source_label(row):
    channel = str(row.get('channel_name') or 'announcements').replace('@', '@\u200b')
    guild = str(row.get('guild_name') or '').replace('@', '@\u200b')
    return f'#{channel} · {guild}' if guild else f'#{channel}'


def format_upcoming(rows, *, enabled, access_states=None, now=None):
    items=upcoming_items(rows,now=now)
    if items:
        lines=['**Event update summary**']
        for item in items:
            title=clean_source_text(item.get('title') or 'Event announcement',limit=150)
            summary=clean_source_text(item.get('summary') or '',limit=260)
            if summary:
                lines.append(f"• **{title}** — {summary}")
            else:
                lines.append(f"• **{title}**")
            posted_label=post_date_label(item.get('posted_at'))
            status=item.get('event_date_status')
            if status=='dated':
                lines.append(f"  **Event date:** {item['date_label']} · **Post date:** {posted_label}")
            elif status=='partial':
                lines.append(f"  **Event date mentioned:** {item['date_label']} (year not stated) · **Post date:** {posted_label}")
            else:
                lines.append(f"  **Event date not stated · Post date:** {posted_label} *(publication date, not the event date)*")
        text='\n'.join(lines)
        if len(text)>1900:
            text=text[:1880].rsplit('\n',1)[0]+'\n… More event summaries omitted.'
        return text
    if not rows:
        if not enabled:
            return ('Announcement reading is not enabled yet. It needs the Discord Message Content intent, '
                    'channel View Channel/Read Message History access, and `ANNOUNCEMENT_INGESTION_ENABLED=true`.')
        if access_states and any(state!='available' for state in access_states.values()):
            return 'I haven’t saved announcements yet. One or more configured channels are not readable right now; I’ll retry automatically when access is available.'
        return 'I haven’t saved announcements yet. I’ll keep retrying the configured channels as soon as the bot can read them.'
    return 'I found no future-dated event posts in the saved announcements. A recent post without an explicit event date may still be worth checking.'


def _explicit_event_date(text):
    dates=_explicit_event_dates(text)
    return dates[0][3] if dates else ''


def ecr_event_items(rows,limit=5):
    """Keep saved ECR event posts separate from explicitly labeled promo-code posts."""
    limit=max(0,int(limit))
    if not limit:return []
    ordered=sorted(rows or [],key=_posted_timestamp,reverse=True)
    result=[]
    for row in ordered:
        text=str(row.get('content') or '').strip()
        if not text or not _ECR_REFERENCE.search(text) or _PROMO_CODE_LABEL.search(text):continue
        title=_title_from_content(text)
        summary=_post_summary(text,title,limit=360)
        posted=_posted_timestamp(row)
        event_date=_explicit_event_date(text)
        partial_event_date=_explicit_partial_event_date(text) if not event_date else ''
        result.append({'title':title,'summary':summary,'excerpt':summary,
                       'event_date_label':event_date or partial_event_date,
                       'event_date_status':'dated' if event_date else ('partial' if partial_event_date else 'missing'),
                       'posted_at':posted,'source_label':_source_label(row),
                       'url':str(row.get('message_url') or '')})
        if len(result)>=limit:break
    return result


def _announcement_access_description(state):
    """Explain the last per-channel ingestion result without guessing at permissions."""
    value=str(state or 'not_checked')
    exact={
        'available':'history read and storage succeeded',
        'not_checked':'no ingestion attempt recorded yet',
        'message_content_intent_disabled':'the bot did not request the Discord Message Content intent',
        'announcement_ingestion_disabled':'ANNOUNCEMENT_INGESTION_ENABLED is false',
        'private_table_not_installed':'the private announcement-storage table is missing',
        'view_channel_missing':'the bot’s effective Discord permissions lack View Channel',
        'read_message_history_missing':'the bot’s effective Discord permissions lack Read Message History',
        'view_channel_or_read_history_missing':'the bot’s effective permissions lack View Channel or Read Message History',
        'permission_denied':'Discord returned 403 Forbidden during channel access/history read',
        'permission_denied_channel_lookup':'Discord returned 403 Forbidden while resolving the channel',
        'permission_denied_history_read':'Discord returned 403 Forbidden while reading message history',
        'not_found_or_not_a_member':'Discord returned 404; the ID may be wrong/deleted or the bot may not be in that server',
        'not_found_channel_lookup':'Discord returned 404 while resolving the channel; verify its ID and server membership',
        'not_found_history_read':'Discord returned 404 while reading history; the channel may have been deleted',
        'not_a_server_message_channel':'the resolved ID is not a server text/announcement channel with history',
    }
    if value in exact:return exact[value]
    if value.startswith('permission_denied_'):
        stage=value[len('permission_denied_'):].replace('_',' ')
        return f'Discord returned 403 Forbidden during {stage}'
    if value.startswith('not_found_'):
        stage=value[len('not_found_'):].replace('_',' ')
        return f'Discord returned 404 during {stage}; verify the channel ID and server membership'
    if value.startswith('discord_http_'):
        parts=value[len('discord_http_'):].rsplit('_',2)
        if len(parts)==3:
            stage,error_type,status=parts
            return f'Discord returned HTTP {status} ({error_type}) during {stage.replace("_"," ")}'
        detail=value[len('discord_http_'):].replace('_',' ')
        return f'Discord API returned {detail} during ingestion'
    if value.startswith('ingestion_error_'):
        detail=value[len('ingestion_error_'):].replace('_',' ')
        return f'bot ingestion/storage failed with {detail}; check the bot runtime log for the stack trace'
    return f'unmapped last status `{value}`; check the bot runtime log'


def _announcement_access_report(access_states=None,channel_ids=()):
    states=access_states or {}
    ids=list(channel_ids or states.keys())
    lines=[]
    failures=[]
    for channel_id in ids:
        state=states.get(channel_id,states.get(str(channel_id),'not_checked'))
        line=f'• `{channel_id}` — {_announcement_access_description(state)}.'
        lines.append(line)
        if state!='available':failures.append(line)
    return lines,failures


def format_ecr_events(rows,*,enabled,access_states=None,limit=5,configured_channel_ids=(),disabled_reasons=(),excluded_channel_ids=()):
    """Present ECR event updates only; include exact configured-channel read diagnostics."""
    rows=list(rows or [])
    channel_ids=list(configured_channel_ids or ())
    items=ecr_event_items(rows,limit=limit)
    if not items:
        status_lines,failures=_announcement_access_report(access_states,channel_ids)
        channel_text=', '.join(f'`{channel_id}`' for channel_id in channel_ids) or 'none configured'
        excluded_text=', '.join(f'`{channel_id}`' for channel_id in excluded_channel_ids)
        exclusion_note=f' Explicitly excluded channel IDs: {excluded_text}.' if excluded_text else ''
        if not enabled:
            reasons='; '.join(str(item) for item in disabled_reasons) or 'announcement ingestion is disabled'
            return (f'ECR means Event Crate Rush; it is separate from promo codes. Announcement lookup is disabled: '
                    f'{reasons}. Configured channel IDs: {channel_text}.{exclusion_note} Check bot configuration and restart after changes.')
        if failures:
            return ('I don’t have a saved ECR event update. Last result for each configured source channel:\n'
                    +'\n'.join(status_lines)+'\n'
                    +f'Configured channel IDs: {channel_text}.{exclusion_note} I’ll retry automatically. '
                    'If the ECR post is in another channel, add that channel ID to `ANNOUNCEMENT_CHANNEL_IDS`.')
        status_summary=('\n'.join(status_lines) if status_lines else 'No per-channel read result has been recorded yet.')
        if rows:
            return ('ECR means Event Crate Rush; I read the saved announcements but found no ECR event update '
                    f'(promo-code posts are separate). Configured channel IDs: {channel_text}.{exclusion_note}\n{status_summary}')
        return ('ECR means Event Crate Rush; no saved ECR event update is available yet. '
                f'Configured channel IDs: {channel_text}.{exclusion_note} Last channel-read results:\n{status_summary}\n'
                'Promo codes are looked up separately; I’ll retry the configured channels.')
    lines=['**Event Crate Rush summary** *(event information, not promo codes)*']
    for item in items:
        title=clean_source_text(item.get('title') or 'ECR update',limit=140)
        summary=clean_source_text(item.get('summary') or item.get('excerpt') or '',limit=360)
        line=f"• **{title}** — {summary}" if summary else f"• **{title}**"
        posted_label=post_date_label(item.get('posted_at'))
        status=item.get('event_date_status')
        if status=='dated' and item.get('event_date_label'):
            line+=f"\n  **Event date:** {item['event_date_label']} · **Post date:** {posted_label}"
        elif status=='partial' and item.get('event_date_label'):
            line+=f"\n  **Event date mentioned:** {item['event_date_label']} (year not stated) · **Post date:** {posted_label}"
        else:
            line+=f"\n  **Event date not stated · Post date:** {posted_label} *(publication date, not the event date)*"
        if len('\n'.join([*lines,line]))>1750:
            lines.append('• More ECR summaries omitted to fit Discord.')
            break
        lines.append(line)
    if not enabled:lines.append('*Announcement ingestion is disabled; these are cached summaries.*')
    return '\n'.join(lines)
