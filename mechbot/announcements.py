"""Safe ingestion and date-aware presentation for configured announcement channels."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import re

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
_ECR_REFERENCE = re.compile(r'\b(?:ecr|event\s+crate\s+rush)\b', re.I)
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
    ordered=sorted(rows or [],key=lambda row:int(row.get('posted_at') or 0),reverse=True)
    for row in ordered:
        text=str(row.get('content') or '')
        values=[]
        for line in text.splitlines():values.extend(_codes_from_promo_line(line))
        status='marked_expired' if _PROMO_EXPIRED.search(text) else 'validity_unknown'
        for code in values:
            key=code.casefold()
            if key in seen:continue
            seen.add(key)
            items.append({'code':code,'status':status,'posted_at':int(row.get('posted_at') or 0),
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
    lines=['**Latest saved promo-code mentions from configured channels**']
    footer='Codes are not validated in-game; check the linked post before redeeming. Validity may have changed since posting.'
    if not enabled:footer+=' Announcement ingestion is off; these are cached mentions and may be stale.'
    for item in items:
        status='the source post marks it expired' if item['status']=='marked_expired' else 'validity not confirmed'
        posted=f"<t:{item['posted_at']}:d>" if item['posted_at']>0 else 'date not recorded'
        url=item['url']
        label=str(item['source_label'])
        for marker in '[]()<>`*_~':label=label.replace(marker,'')
        source=f"[Source: {label}]({url})" if url.startswith('https://discord.com/channels/') else f"Source: {label}"
        line=f"• `{item['code']}` — {status}; posted {posted} · {source}"
        if len(chr(10).join([*lines,line,footer]))>1900:
            lines.append('• More promo-code mentions omitted to fit Discord.')
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
    lines = [re.sub(r'\s+', ' ', x).strip() for x in str(text or '').splitlines()]
    for line in lines:
        line = re.sub(r'<[^>]{1,80}>', '', line)
        line = re.sub(r'\*{1,3}|_{1,3}|`|~', '', line).strip(' :—-#')
        if line and not re.fullmatch(r'@(?:everyone|here)', line, flags=re.I):
            return line[:limit].rstrip()
    return 'Announcement'


def upcoming_items(rows, now=None, lookback_days=ANNOUNCEMENT_LOOKBACK_DAYS, limit=MAX_UPCOMING_ITEMS):
    """Extract future-dated event posts and recent undated event posts, preserving their source links."""
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    today = _local_today(now)
    result = []
    for row in rows:
        text = str(row.get('content') or '')
        if not text or not event_like(text):
            continue
        posted = int(row.get('posted_at') or 0)
        dates = _date_candidates(text)
        future = next((entry for entry in dates if entry[2] >= today), None)
        if future:
            _, start, end, label = future
            result.append({
                'dated': True,
                'date_label': label,
                'sort_date': start,
                'title': _title_from_content(text),
                'source_label': _source_label(row),
                'url': str(row.get('message_url') or ''),
                'posted_at': posted,
            })
        elif not dates and posted >= now - int(lookback_days) * 86400:
            partial_date = bool(_PARTIAL_DATE.search(text))
            result.append({
                'dated': False,
                'date_label': 'Year not stated' if partial_date else 'Date not stated',
                'sort_date': None,
                'title': _title_from_content(text),
                'source_label': _source_label(row),
                'url': str(row.get('message_url') or ''),
                'posted_at': posted,
            })
    result.sort(key=lambda item: (not item['dated'], item['sort_date'] or date.max, -item['posted_at']))
    return result[:max(0, int(limit))]


def _source_label(row):
    channel = str(row.get('channel_name') or 'announcements').replace('@', '@\u200b')
    guild = str(row.get('guild_name') or '').replace('@', '@\u200b')
    return f'#{channel} · {guild}' if guild else f'#{channel}'


def format_upcoming(rows, *, enabled, access_states=None, now=None):
    items = upcoming_items(rows, now=now)
    if items:
        lines = ['**Upcoming from the announcement channels**']
        for item in items:
            title = item['title'].replace('[', '').replace(']', '')
            url = item['url']
            if url.startswith('https://discord.com/channels/'):
                source = f"[Source: {item['source_label']}]({url})"
            else:
                source = f"Source: {item['source_label']}"
            lines.append(f"• **{item['date_label']}** — {title} · {source}")
        if any(not item['dated'] for item in items):
            lines.append('Dates are shown only when the post gives an explicit year. Undated items may no longer be current; check the source post.')
        text = '\n'.join(lines)
        if len(text) > 1900:
            text = text[:1880].rsplit('\n', 1)[0] + '\n… More posts omitted.'
        return text
    if not rows:
        if not enabled:
            return ('Announcement reading is not enabled yet. It needs the Discord Message Content intent, '
                    'channel View Channel/Read Message History access, and `ANNOUNCEMENT_INGESTION_ENABLED=true`.')
        if access_states and any(state!='available' for state in access_states.values()):
            return 'I haven’t saved announcements yet. One or more configured channels are not readable right now; I’ll retry automatically when access is available.'
        return 'I haven’t saved announcements yet. I’ll keep retrying the configured channels as soon as the bot can read them.'
    return 'I found no future-dated event posts in the saved announcements. A recent post without an explicit year may still be worth checking in its source channel.'


def ecr_event_items(rows,limit=5):
    """Keep saved ECR event posts separate from explicitly labeled promo-code posts."""
    limit=max(0,int(limit))
    if not limit:return []
    ordered=sorted(rows or [],key=lambda row:int(row.get('posted_at') or 0),reverse=True)
    result=[]
    for row in ordered:
        text=str(row.get('content') or '').strip()
        if not text or not _ECR_REFERENCE.search(text) or _PROMO_CODE_LABEL.search(text):continue
        excerpt=re.sub(r'\s+',' ',_safe_announcement_text(text)).strip()
        if len(excerpt)>480:
            excerpt=excerpt[:479].rsplit(' ',1)[0].rstrip(' ,;:')+'…'
        result.append({'excerpt':excerpt,'posted_at':int(row.get('posted_at') or 0),
                       'source_label':_source_label(row),'url':str(row.get('message_url') or '')})
        if len(result)>=max(0,int(limit)):break
    return result


def format_ecr_events(rows,*,enabled,access_states=None,limit=5):
    """Present ECR event updates only; promo-code posts use their separate lookup."""
    rows=list(rows or [])
    items=ecr_event_items(rows,limit=limit)
    if not items:
        if not enabled:
            return ('ECR means Event Crate Rush; it is separate from promo codes. Current ECR updates need '
                    'announcement reading and channel access enabled.')
        if access_states and any(state!='available' for state in access_states.values()):
            return ('I don’t have a saved ECR event update yet because one or more configured channels are not readable. '
                    'I’ll retry when access is available. ECR event updates and promo codes are separate lookups.')
        if rows:
            return 'I found no saved ECR event updates in the configured channels. Promo codes are looked up separately.'
        return 'I haven’t saved an ECR event update from the configured channels yet. I’ll keep retrying; promo codes are looked up separately.'
    lines=['**Saved Event Crate Rush updates** *(event information, not promo codes)*']
    for item in items:
        label=item['source_label'].replace('[','').replace(']','')
        source=f"[#{label.lstrip('#')}]({item['url']})" if item['url'].startswith('https://discord.com/channels/') else label
        posted=f" · posted <t:{item['posted_at']}:d>" if item['posted_at']>0 else ''
        line=f"• {item['excerpt']}{posted} · {source}"
        if len('\n'.join([*lines,line]))>1800:
            lines.append('• More ECR updates omitted to fit Discord.')
            break
        lines.append(line)
    return '\n'.join(lines)
