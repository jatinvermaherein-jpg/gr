"""Explicit reminder parsing, timezone conversion and recurrence calculations."""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNITS = {'second': 1, 'seconds': 1, 'minute': 60, 'minutes': 60, 'hour': 3600, 'hours': 3600, 'day': 86400, 'days': 86400, 'week': 604800, 'weeks': 604800}


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


def parse_spec(spec, zone_name, now=None):
    """Supported forms: in 10 minutes, every 2 hours, daily at 8:30pm,
    2026-12-25 14:00 (local IANA time)."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    s = re.sub(r'\s+', ' ', spec.strip().lower())
    m = re.fullmatch(r'(in|every)\s+(\d+)\s+(seconds?|minutes?|hours?|days?|weeks?)', s)
    if m:
        seconds = int(m[2]) * UNITS[m[3]]
        if seconds < 60 or seconds > 366 * 86400:
            raise ValueError('Use an interval between one minute and 366 days.')
        return int(now + seconds), ('interval' if m[1] == 'every' else 'once'), seconds, None, None
    m = re.fullmatch(r'(?:daily|every day)\s+at\s+(.+)', s)
    if m:
        hour, minute = clock_time(m[1])
        return next_daily(hour, minute, zone_name, now), 'daily', None, hour, minute
    m = re.fullmatch(r'tomorrow\s+at\s+(.+)', s)
    if m:
        hour, minute = clock_time(m[1]);zone = tzinfo(zone_name)
        d = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
        return int(localize(datetime(d.year,d.month,d.day,hour,minute),zone)), 'once', None, None, None
    try:
        naive = datetime.strptime(s, '%Y-%m-%d %H:%M')
    except ValueError:
        raise ValueError('Use “in 10 minutes”, “every 2 hours”, “daily at 8pm”, or “YYYY-MM-DD HH:MM”.')
    due = int(localize(naive, tzinfo(zone_name)))
    if due <= now:
        raise ValueError('Choose a future time.')
    return due, 'once', None, None, None


def parse_natural_reminder(text):
    """Return (spec, content) or None; do not guess missing information."""
    m = re.fullmatch(r'\s*remind\s+me\s+(in\s+\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)|every\s+\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)|(?:every\s+day|daily|tomorrow)\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to\s+|that\s+)?(.+?)\s*', text, re.I)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


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
