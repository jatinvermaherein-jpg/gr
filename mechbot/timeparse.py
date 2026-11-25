"""Explicit reminder parsing, timezone conversion and recurrence calculations.

The parser is deliberately generous about *how* a reminder is phrased and strict about what it will
schedule: a half-read sentence is a reminder that fires at the wrong hour. Wording is parsed freely,
while a time is only ever taken from what the player typed - with one stated exception, "tomorrow" and
"tonight" without a clock mean 9am and 9pm locally, and the confirmation always shows the absolute time
so an assumption can be seen and corrected.
"""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNIT_SECONDS = {'seconds': 1, 'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
UNIT_CANONICAL = {
    'second': 'seconds', 'seconds': 'seconds', 'sec': 'seconds', 'secs': 'seconds', 's': 'seconds',
    'minute': 'minutes', 'minutes': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'm': 'minutes',
    'hour': 'hours', 'hours': 'hours', 'hr': 'hours', 'hrs': 'hours', 'h': 'hours',
    'day': 'days', 'days': 'days', 'd': 'days',
    'week': 'weeks', 'weeks': 'weeks', 'w': 'weeks',
}
WEEKDAYS = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5,
            'sunday': 6, 'mon': 0, 'tue': 1, 'tues': 1, 'wed': 2, 'thu': 3, 'thur': 3, 'thurs': 3,
            'fri': 4, 'sat': 5, 'sun': 6}
# Times of day that carry a clock in ordinary speech, and the hour they mean when none is given.
DAYPART_HOUR = {'morning': (9, 0), 'afternoon': (15, 0), 'evening': (19, 0), 'tonight': (21, 0),
                'noon': (12, 0), 'midday': (12, 0), 'midnight': (0, 0)}
NUMBER_WORDS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
                'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
                'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'sixty': 60,
                'half': 0.5, 'couple': 2, 'few': 3, 'several': 5}

_DURATION_ALIAS_RE = re.compile(r'(?<!\w)(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)(?!\w)',re.I)
_WORD_QTY_RE = re.compile(r'(?<![\w])((?:(?:an?|the|this)\s+)?(?:one|two|three|four|five|six|seven|eight|'
                          r'nine|ten|eleven|twelve|fifteen|twenty|thirty|forty[- ]?five|sixty|half|couple|few|'
                          r'several|an?)(?:\s+of)?)\s+(?:(?:of\s+)?(?:an?\s+)?)?'
                          r'(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)(?![\w])', re.I)
_UNIT_TEXT = r'seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w'
_CANON_UNIT = r'seconds?|minutes?|hours?|days?|weeks?'
_QTY_CORE = r'(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|' \
             r'forty[- ]?five|sixty|half|couple|few|several|an?|\d{1,3})'
# "a couple of minutes", "half an hour" and "20 mins" are one shape: an optional article, a quantity,
# an optional "of", and the unit - which is what lets the when-clause be found in the words players use.
_QUANTITY = r'(?:(?:an?|the|this)\s+)?' + _QTY_CORE + r'(?:\s+of)?(?:\s+more)?'
_DURATION = _QUANTITY + r'(?:\s+(?:of\s+)?(?:an?\s+)?)?\s*(?:' + _UNIT_TEXT + r')(?![\w])'
_CLOCK = r'\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}[.:]\d{2}'
_WEEKDAY = '|'.join(WEEKDAYS)


def tzinfo(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Use an IANA timezone, for example Asia/Kolkata or Europe/London.')


def localize(naive, zone, ambiguous='reject'):
    """Roundtrip detect nonexistent and ambiguous local wall times."""
    candidates = []
    for fold in (0, 1):
        x = naive.replace(tzinfo=zone, fold=fold)
        if x.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            stamp = x.timestamp()
            if stamp not in candidates:
                candidates.append(stamp)
    if not candidates:
        raise ValueError('That local time does not exist because of a clock change.')
    if len(candidates) > 1 and ambiguous == 'reject':
        raise ValueError('That local time occurs twice because of a clock change; choose a different time.')
    return min(candidates)


def clock_time(text):
    text = text.strip().lower().replace(' ', '')
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm)?', text)
    if not m:
        raise ValueError('Time must look like 08:30, 8am or 8:30pm.')
    h, minute, suffix = int(m[1]), int(m[2] or 0), m[3]
    if minute > 59 or (suffix and not 1 <= h <= 12) or (not suffix and h > 23):
        raise ValueError('Invalid time of day.')
    if suffix:
        h = h % 12 + (12 if suffix == 'pm' else 0)
    return h, minute


def next_daily(hour, minute, zone_name, after):
    """First occurrence strictly after timestamp; skip nonexistent local DST dates."""
    zone = tzinfo(zone_name)
    today = datetime.fromtimestamp(after, zone).date()
    for day in range(370):
        d = today + timedelta(days=day)
        naive = datetime(d.year, d.month, d.day, hour, minute)
        try:
            ts = localize(naive, zone, ambiguous='earlier')
        except ValueError:
            continue
        if ts > after:
            return int(ts)
    raise ValueError('Unable to schedule a future occurrence.')


def _canonical_duration(quantity, unit):
    """`20 sec` and `a couple of minutes` become `20 seconds` and `2 minutes`: whole units, no rounding."""
    unit = UNIT_CANONICAL.get(re.sub(r'\s+of\s*$', '', str(unit).strip().casefold()))
    if unit is None:
        raise ValueError('Use seconds, minutes, hours, days or weeks.')
    count = re.sub(r'^(?:an?|the|this)\s+|\s+(?:of|more)$', '', str(quantity).strip().casefold()).strip()
    value = int(count) if count.isdigit() else NUMBER_WORDS.get(count)
    if value is None:
        raise ValueError('Say the amount as a number or a word, for example 20 minutes or an hour.')
    seconds = value * UNIT_SECONDS[unit]
    if seconds != int(seconds):
        raise ValueError('Pick a whole number of seconds.')
    seconds = int(seconds)
    # The largest whole unit keeps the player's own words: "2 minutes", not "120 seconds".
    for step, name in ((604800, 'weeks'), (86400, 'days'), (3600, 'hours'), (60, 'minutes'), (1, 'seconds')):
        if seconds and seconds % step == 0:
            words, noun = seconds // step, name
            return f'{words} {noun[:-1] if words == 1 else noun}'
    return f'{seconds} seconds'


def normalize_duration_spec(spec):
    """Fold every spelling of a duration into `<n> <canonical unit>` words."""
    s = re.sub(r'\s+', ' ', str(spec or '').strip().lower())
    # Digits first so `20 secs` becomes `20 seconds`, then the worded quantities, whose canonical form
    # is allowed to read "1 hour" rather than "1 hours".
    s = _DURATION_ALIAS_RE.sub(lambda m: f"{m.group(1)} {UNIT_CANONICAL[m.group(2).lower()]}", s)
    s = _WORD_QTY_RE.sub(lambda m: _canonical_duration(m.group(1), m.group(2)), s)
    s = re.sub(r'(\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?))\s+more\b', r'\1', s)
    if re.fullmatch(r'\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)', s):
        s = 'in ' + s
    return s


def _day_part_clock(part, clock):
    """Resolve `tonight at 9`, `this evening`, and `tomorrow morning` to a wall-clock time."""
    if clock:
        hour, minute = clock_time(clock)
        if part and str(part).strip().casefold() in ('afternoon', 'evening', 'tonight') and hour < 12:
            hour += 12  # a bare "9" said about the evening means 9pm
        return hour, minute
    if part:
        key = str(part).strip().casefold()
        if key in DAYPART_HOUR:
            return DAYPART_HOUR[key]
    raise ValueError('Say the time as well, for example “tonight at 9” or “tomorrow at 8:30”.')


def at_local_time(hour, minute, zone_name, now, days_ahead=0):
    """A local wall time N days out, falling forward a day if that instant has already passed."""
    zone = tzinfo(zone_name)
    day = datetime.fromtimestamp(now, zone).date() + timedelta(days=days_ahead)
    for attempt in range(2):
        naive = datetime(day.year, day.month, day.day, hour, minute)
        try:
            due = localize(naive, zone)
        except ValueError:
            due = None
        if due is not None and due > now:
            return int(due)
        day = day + timedelta(days=1)
    return next_daily(hour, minute, zone_name, now)


def next_weekday(weekday, hour, minute, zone_name, now, every_other=False):
    """First local `hour:minute` on the given weekday strictly after now."""
    zone = tzinfo(zone_name)
    today = datetime.fromtimestamp(now, zone).date()
    day = today + timedelta(days=(weekday - today.weekday()) % 7)
    if every_other:
        day = day + timedelta(days=7)
    for attempt in range(2):
        naive = datetime(day.year, day.month, day.day, hour, minute)
        try:
            due = localize(naive, zone)
        except ValueError:
            due = None
        if due is not None and due > now:
            return int(due)
        day = day + timedelta(days=7)
    raise ValueError('Unable to schedule that local time.')


def parse_spec(spec, zone_name, now=None):
    """Supports one-time delays, recurring intervals, clock times, weekdays, daily times and dates."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    s = normalize_duration_spec(spec)
    m = re.fullmatch(r'(in|within|after)\s+(\d+)\s+(' + _CANON_UNIT + r')', s)
    if m:
        seconds = int(m[2]) * UNIT_SECONDS[UNIT_CANONICAL[m[3]]]
        if seconds < 1 or seconds > 366 * 86400:
            raise ValueError('Choose a delay between one second and 366 days.')
        return int(now + seconds), 'once', seconds, None, None
    m = re.fullmatch(r'every\s+(\d+)\s+(' + _CANON_UNIT + r')', s)
    if m:
        seconds = int(m[1]) * UNIT_SECONDS[UNIT_CANONICAL[m[2]]]
        if seconds < 60:
            raise ValueError('Recurring reminders must be at least one minute apart.')
        if seconds > 366 * 86400:
            raise ValueError('Recurring reminders must repeat inside a year.')
        return int(now + seconds), 'interval', seconds, None, None
    m = re.fullmatch(r'(?:daily|every ?days?|each day|everyday)\s+at\s+(.+)', s)
    if m:
        hour, minute = clock_time(m[1])
        return next_daily(hour, minute, zone_name, now), 'daily', None, hour, minute
    m = re.fullmatch(r'(every|each|alternate)\s+(?:day\s+)?(' + _WEEKDAY + r')(?:\s+(?:at|@)\s*(' + _CLOCK + r'))?', s)
    if m:
        # A weekly reminder is an ordinary 7-day interval anchored on the right weekday, which the
        # existing scheduler can repeat without a new column.
        hour, minute = _day_part_clock(None, m[3]) if m[3] else DAYPART_HOUR['morning']
        due = next_weekday(WEEKDAYS[m[2].lower()], hour, minute, zone_name, now,
                           every_other=(m[1].lower() == 'alternate'))
        return due, 'interval', 604800, hour, minute
    m = re.fullmatch(r'(?:(?:on|next|this|upcoming)\s+)?(' + _WEEKDAY + r')(?:\s+(?:at|@)\s*(' + _CLOCK + r'))?', s)
    if m:
        hour, minute = _day_part_clock(None, m[2]) if m[2] else DAYPART_HOUR['morning']
        return next_weekday(WEEKDAYS[m[1].lower()], hour, minute, zone_name, now), 'once', None, None, None
    m = re.fullmatch(r'tomorrow(?:\s+(morning|afternoon|evening|tonight))?(?:\s+(?:at|@)\s*(' + _CLOCK + r'))?', s)
    if m:
        hour, minute = _day_part_clock(m[1] or 'morning', m[2])
        return at_local_time(hour, minute, zone_name, now, days_ahead=1), 'once', None, None, None
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}(?::\d{2})?\s*(?:[ap]m)?))?', s)
    if m:
        if not m[2]:
            raise ValueError('Give the time as well as the date, for example 2026-11-25 09:00.')
        hour, minute = clock_time(m[2])
        date = datetime.strptime(m[1], '%Y-%m-%d').date()
        naive = datetime(date.year, date.month, date.day, hour, minute)
        due = int(localize(naive, tzinfo(zone_name)))
        if due <= now:
            raise ValueError('Choose a future time.')
        return due, 'once', None, None, None
    m = re.fullmatch(r'(?:(?:this|the)\s+)?(?:(?:at|by|before|in|during)\s+(?:the\s+)?)?'
                     r'(morning|afternoon|evening|tonight|noon|midday|midnight)', s)
    if m:
        hour, minute = DAYPART_HOUR[m[1]]
        return at_local_time(hour, minute, zone_name, now), 'once', None, None, None
    m = re.fullmatch(r'(?:(today|tonight|tmr|tmrw)\s+)?(?:(morning|afternoon|evening|tonight|noon)\s+)?'
                     r'(?:at|@|by|before)?\s*(' + _CLOCK + r')', s)
    if m:
        hour, minute = _day_part_clock(m[1] or m[2], m[3])
        return at_local_time(hour, minute, zone_name, now), 'once', None, None, None
    try:
        naive = datetime.strptime(s, '%Y-%m-%d %H:%M')
    except ValueError:
        raise ValueError('Use “in 20 minutes”, “in an hour”, “at 8pm”, “every 2 hours”, “daily at 8pm”, '
                         '“every friday at 9am”, “tomorrow at 9am”, or “YYYY-MM-DD HH:MM”.')
    due = int(localize(naive, tzinfo(zone_name)))
    if due <= now:
        raise ValueError('Choose a future time.')
    return due, 'once', None, None, None


RELATIVE_SPEC = re.compile(r'^(?:in|within|after|for|every)\s+\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)$')


def needs_wall_clock(spec):
    """Whether a spec pins a time on the player's clock rather than a delay from now.

    `in 20 minutes` is the same moment wherever the player lives; `daily at 8pm` is not, and scheduling
    one against a default zone sends the reminder five hours off. Anything that is not plainly a relative
    delay counts as clock-based, so a missing timezone becomes a question instead of a guess.
    """
    return not RELATIVE_SPEC.match(normalize_duration_spec(spec))


# Anything that reads as a question *about* reminders, or as the player declining one, is not a request
# to create one; those sentences belong to the help, list and cancel paths.
_REMINDER_DOUBT = re.compile(
    r"\b(?:don'?t|do not|never|stop|cancel|delete|remove|pause|skip)\b[^.?!]{0,20}remind|"
    r"remind[^.?!]{0,20}\b(?:did|didn'?t|worked|fires?|fired|sent|cancelled|canceled|deleted)\b|"
    r"^(?:did|why|how come|what happened|is my|are my|was my|were my|do you (?:still )?remind|can i see)\b|"
    r"\b(?:yesterday|last night|last week|already)\b", re.I)

_WHEN_CLAUSE = (
    r'(?:(?:in|within|after)\s+' + _DURATION + r')'
    r'|(?:(?:daily|everyday|each day|every\s+days?)\s+(?:at|@)\s*' + _CLOCK + r')'
    r'|(?:(?:every|each)\s+(?:next\s+)?(?:' + _WEEKDAY + r')(?:\s+(?:at|@)\s*' + _CLOCK + r')?)'
    r'|(?:(?:next|alternate)\s+(?:' + _WEEKDAY + r')(?:\s+(?:at|@)\s*' + _CLOCK + r')?)'
    r'|(?:(?:on\s+)?(?:' + _WEEKDAY + r')\s+(?:at|@)\s*' + _CLOCK + r')'
    r'|(?:every\s+(?:' + _DURATION + r'|days?\b|daily\b))'
    r'|(?:(?:tomorrow|tonight|today)(?:\s+(?:morning|afternoon|evening))?(?:\s+(?:at|@)\s*' + _CLOCK + r')?)'
    r'|(?:(?:at|by|before)\s*(?:(?:this|the)\s+)?(?:morning|afternoon|evening|tonight|noon|midday|midnight'
    r'|' + _CLOCK + r'))'
    r'|(?:(?:this\s+)?(?:morning|afternoon|evening|noon|midday|midnight))'
    r'|(?:\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}(?::\d{2})?)?)'
    # A bare weekday is last: the forms that carry a clock must win when both would match.
    r'|(?:(?:on|next|this|upcoming)\s+(?:' + _WEEKDAY + r')\b|(?:' + _WEEKDAY + r')\b)'
)
WHEN_RE = re.compile(r'(?:' + _WHEN_CLAUSE + r')', re.I)
_POLITE_TAIL = re.compile(r'[,!\s]*(?:thanks|thank you|ty|thx|please|plz|mate|dude|bros?|appreciate it'
                          r'|much appreciated)[.!?]*$', re.I)
_POLITE_HEAD = re.compile(r'^(?:(?:hey|hi|hello|yo|ok|okay|so|right|anyway)[,!\s]+)*'
                          r'(?:please|pls|plz|can you|could you|would you|will you|can u|will u|'
                          r'want you to|need you to)?[,!\s]*', re.I)


def parse_natural_reminder(text):
    """Return (spec, content) for a direct reminder request; do not guess missing information.

    Players put the time before the task, after the task, or wrap both in politeness, and abbreviate the
    units. All of that is the same request, so the when-clause is located anywhere after `remind me`
    rather than required to sit in one slot, and the task is whatever is left over - in the player's own
    capitalisation, since they will read it back later. No when-clause, or nothing left to remind about,
    is not a reminder at all.
    """
    original = re.sub(r'\s+', ' ', str(text or '').strip())
    if not original or len(original) > 900:
        return None
    if _REMINDER_DOUBT.search(original):
        return None
    if not re.search(r'\bremind(?:s|ed)?\s+me\b', original, re.I):
        return None
    # `remind my friend to call me` is about somebody else; only `remind me` is a schedule.
    if re.search(r'\breminds?\s+(?:my|his|her|their|our|the|a|an)\b', original, re.I):
        return None
    match = re.search(r'\bremind(?:s|ed)?\s+me\b', original, re.I)
    head = _POLITE_HEAD.sub('', original[:match.start()], count=1).strip(' ,;:!')
    tail = original[match.end():]
    before = _POLITE_HEAD.sub('', head, count=1).strip(' ,;:!-')
    rest = _POLITE_TAIL.sub('', tail.strip()).strip()
    clause = WHEN_RE.search(rest) or (WHEN_RE.search(before) if before else None)
    if not clause:
        return None
    if clause.string is before:
        content = rest
    else:
        content = (rest[:clause.start()] + ' ' + rest[clause.end():])
    content = re.sub(r'\s+', ' ', content).strip()
    content = re.sub(r'^(?:to|that|about|for|and|then)\s+', '', content, flags=re.I).strip(' .,;:!-')
    content = _POLITE_TAIL.sub('', content).strip()
    # A reminder is a note to yourself, so the question mark that ends the sentence is not part of it.
    content = re.sub(r'[?!]+$', '', content).strip()
    if not content:
        return None
    return normalize_duration_spec(clause.group(0)), content


def next_occurrence(kind, due, interval, hour, minute, zone_name, now):
    if kind == 'once':
        return None
    if kind == 'interval':
        if not interval or interval < 60:
            raise ValueError('Invalid recurrence interval.')
        return due + max(1, int((now - due) // interval) + 1) * interval
    if kind == 'daily':
        return next_daily(hour, minute, zone_name, now)
    raise ValueError('Invalid recurrence kind.')
