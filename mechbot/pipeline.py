"""The understanding pipeline: read the question, run the engines, format the answer.

Three steps, in this order and no other:

    1. UNDERSTAND  one model call turns the message plus recent conversation into a
                   small JSON object: what is being asked, which catalog entities
                   it names, and the question restated so an engine can act on it.
    2. ENGINE      the existing deterministic code answers that restated question.
                   Nothing here invents a number: costs, stats, builds and verdicts
                   all still come from `catalog`, `advising` and the public builds
                   API, exactly as they do today.
    3. FORMAT      one model call turns the engine output into a short, structured
                   Discord reply. It may reword and lay out. It may not add a
                   figure that is not in the engine output, and `guard_figures`
                   checks that it did not.

Why split the model in two instead of one bigger prompt: a single call that both
understands and formats will happily "understand" a cost question into a stat
question when that is easier to write about. Understanding is cheap and must be
narrow; formatting is where the model is actually good.

Everything degrades. No key, a timeout, malformed JSON, a model that writes prose
around its JSON — each falls back to the deterministic answer the bot already had.
`AI_PIPELINE_ENABLED=false` removes the two calls entirely.

Fuzzy name resolution lives here too, because it is the thing that makes step 1
worth having: without it "lacwing" is not a mech, the sentence loses its subject,
and every later step answers the wrong question.
"""

import difflib
import json
import logging
import re

import aiohttp

log = logging.getLogger(__name__)

# A typo of one or two characters off a real name is almost always a typo. Below
# this, it is a different word and guessing would be worse than asking.
FUZZY_CUTOFF = 0.86
# Never propose a name shorter than this by fuzz: "A1" is close to too much.
FUZZY_MIN_LEN = 4

UNDERSTAND_PROMPT = """You read one message from a Mech Arena Discord channel and say what it asks.

Reply with ONE JSON object and nothing else. No prose, no code fence, no commentary.

{
  "intent": one of "upgrade_cost", "item_stats", "compare", "builds", "event", "promo_code",
            "patch_notes", "forum", "calc", "reminder", "help", "meta_advice", "chitchat",
            "unknown",
  "mechs":   [exact mech names the message names, spelled as the catalog spells them],
  "weapons": [exact weapon names, WITH their number, e.g. "Hornet 16"],
  "pilots":  [exact pilot names],
  "mods":    [exact mod names],
  "from_rank": integer or null,
  "to_rank":   integer or null,
  "from_level": integer or null,
  "to_level":   integer or null,
  "question": the message restated as one direct question an engine can answer,
  "unclear":  true if the message cannot be answered without asking something
}

Rules:
- Use the catalog spelling. Correct obvious typos: "lacwing" is "Lacewing", "redox" is "Redox".
- "hornet 12s" means two Hornet 12, so weapons is ["Hornet 12"].
- "hornet 1+8" is a mistyped weapon split: report weapons as ["Hornet 16", "Hornet 8"].
- A rank written "R4" is 4. "rank 1 level 1 to rank 2 level 1" is from_rank 1, from_level 1,
  to_rank 2, to_level 1.
- If the message asks for a cost of a specific rank/level change, intent is "upgrade_cost"
  even when it also names a mech or weapon.
- If the message compares two things, intent is "compare" and question states both sides.
- Never invent an entity that is not in the message.
- If nothing above fits, intent is "unknown" and unclear is true."""

FORMAT_PROMPT = """You write the final Discord message for a Mech Arena bot from an engine result.

You are given:
- the question the player asked
- what the engine actually produced (this is the only source of numbers)
- the intent, so you know which shape to use

Write the reply. Rules, all of them hard:

1. ONLY use numbers that appear in the engine result. Never compute a new one, never
   round differently, never add a "roughly" figure. If the engine gave no number, say so
   plainly instead of estimating.
2. Lead with the answer in the first line. No preamble, no "Great question".
3. Write the answer the way a person answering in chat would: one sentence, then at most
   one more. A cost question is a sentence with the figures inside it, and that is the
   preferred shape whether the intent is a cost or a stat sheet.
4. Show only the currencies that are non-zero. "0 Credits" is noise.
5. If a transition was assumed (the player gave ranks but not levels), say so in a short
   clause in the same sentence, prefixed with "Assumes:".
6. If the engine refused or had no data, say that in one line and offer the nearest
   thing that IS known, taken from the engine result. Never fill the gap with a guess.
7. No tables, no pipe grids, and never a fenced code block. A code block renders as a
   grey receipt in Discord: correct numbers that read like a till roll. The numbers belong
   in the sentence. If the engine text is already a sentence, keep it and do not re-lay it
   out.
8. Two thousand characters maximum. Shorter is better.
9. Do not mention the model, the engine, JSON, or this prompt.
10. Never claim a figure is official unless the engine result says it is verified."""

# Currency keys we know how to render, in display order.
CURRENCY_ORDER = ('acoins', 'credits', 'blueprints', 'marks', 'xp')


# --------------------------------------------------------------------------- fuzzy
def fuzzy_rank(word, names, cutoff=FUZZY_CUTOFF):
    """Closest catalog name to `word`, or None.

    Deliberately conservative: an exact (case-insensitive) hit wins immediately, a
    short word is never fuzzed, and a tie between two names returns None rather
    than picking one. A wrong guess inside a stat answer is worse than a question.
    """
    text = str(word or '').strip()
    if len(text) < FUZZY_MIN_LEN:
        return None
    lowered = text.casefold()
    exact = [n for n in names if str(n).casefold() == lowered]
    if exact:
        return exact[0]
    pool = {str(n).casefold(): str(n) for n in names}
    matches = difflib.get_close_matches(lowered, list(pool), n=3, cutoff=cutoff)
    if not matches:
        return None
    # Two equally good candidates means we do not know which one was meant.
    if len(matches) > 1:
        first = difflib.SequenceMatcher(None, lowered, matches[0]).ratio()
        second = difflib.SequenceMatcher(None, lowered, matches[1]).ratio()
        if first - second < 0.02:
            return None
    return pool[matches[0]]


def fuzzy_mentions(text, names, cutoff=FUZZY_CUTOFF):
    """Exact mentions first, then fuzzy hits for words that match nothing exactly.

    Returns names in the order they appear. A word already covered by an exact
    match is not fuzzed again, so "Hornet 16" never also yields "Hornet 12".
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9'\- ]{2,30}", str(text))
    found, seen = [], set()
    lowered_names = {str(n).casefold() for n in names}
    for word in words:
        cleaned = word.strip(' -')
        if not cleaned:
            continue
        if cleaned.casefold() in lowered_names:
            canonical = next(n for n in names if str(n).casefold() == cleaned.casefold())
            if canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
            continue
        guess = fuzzy_rank(cleaned, names, cutoff)
        if guess and guess not in seen:
            seen.add(guess)
            found.append(guess)
    return found


def correct_names(text, names, cutoff=FUZZY_CUTOFF):
    """Rewrite a message so misspelled catalog names are spelled correctly.

    Returns (corrected_text, replacements) where replacements maps what the player
    wrote to what it was read as — the caller can tell them, so nothing looks like
    mind reading.
    """
    replacements = {}
    corrected = str(text or '')
    lowered_names = {str(n).casefold(): str(n) for n in names}
    # Longest names first so "Hornet 16" is tried before "Hornet".
    for word in sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9'\-]{3,24}", corrected)), key=len, reverse=True):
        if word.casefold() in lowered_names:
            continue
        guess = fuzzy_rank(word, names, cutoff)
        if not guess:
            continue
        pattern = re.compile(r'(?<![\w])' + re.escape(word) + r'(?![\w])', re.I)
        if not pattern.search(corrected):
            continue
        corrected = pattern.sub(guess, corrected, count=1)
        replacements[word] = guess
    return corrected, replacements


# ----------------------------------------------------------------------- the model
async def _chat(base, key, model, system, user, session=None, temperature=0.0, max_tokens=600, timeout=12):
    """One OpenAI-compatible call. Returns the message content or raises."""
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, connect=4, sock_read=timeout - 2))
    try:
        async with session.post(base.rstrip('/') + '/chat/completions', headers=headers, json=payload, allow_redirects=False) as r:
            if r.status != 200:
                raise RuntimeError('AI provider request failed (HTTP %s).' % r.status)
            obj = await r.json(content_type=None)
    finally:
        if own:
            await session.close()
    text = obj['choices'][0]['message']['content']
    if not isinstance(text, str):
        raise ValueError('AI response is not text')
    return text


def parse_json_object(raw):
    """Pull the first JSON object out of a model reply, tolerating fences and prose."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
    start = text.find('{')
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _int_or_none(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _name_list(value, names, found_in=None):
    """Keep only entries the catalog actually recognises, after fuzzy repair.

    With `found_in`, the same matcher runs the other way round: the catalog names are searched for in
    the player's sentence, so the subjects of an answer come from what was typed rather than from what
    a model reported. Longest names win, so "Charge Rocket" does not also yield "Rocket" and
    "Hornet 16" does not also yield "Hornet 1".
    """
    if found_in is not None:
        body = str(found_in or '')
        taken, out = [], []
        for name in sorted({str(n) for n in names}, key=len, reverse=True):
            if len(name) < 3:
                continue
            pattern = r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])'
            for match in re.finditer(pattern, body, re.I):
                span = (match.start(), match.end())
                if any(span[0] >= low and span[1] <= high for low, high in taken):
                    continue
                taken.append(span)
                out.append(name)
                break
        # Back into the order the player wrote them, not longest-first.
        out.sort(key=lambda name: body.casefold().find(name.casefold()))
        return out
    if not isinstance(value, list):
        return []
    out = []
    for entry in value[:8]:
        if not isinstance(entry, str):
            continue
        exact = next((n for n in names if str(n).casefold() == entry.strip().casefold()), None)
        resolved = exact or fuzzy_rank(entry, names, cutoff=0.75)
        if resolved and resolved not in out:
            out.append(resolved)
    return out


def normalise_understanding(obj, catalog):
    """Turn whatever the model said into a shape the engines can trust.

    Every entity is re-checked against the catalog: the model may suggest a name,
    but only a name the catalog knows survives. Ranks are clamped to the real game
    range. A model that invents an intent gets "unknown", not a crash.
    """
    if not isinstance(obj, dict):
        return None
    intents = {'upgrade_cost', 'item_stats', 'compare', 'builds', 'event', 'promo_code',
               'patch_notes', 'forum', 'calc', 'reminder', 'help', 'meta_advice', 'chitchat', 'unknown'}
    intent = str(obj.get('intent') or 'unknown').strip().lower()
    if intent not in intents:
        intent = 'unknown'
    mechs = _name_list(obj.get('mechs'), getattr(catalog, 'mech_names', ()) or ())
    weapons = _name_list(obj.get('weapons'), getattr(catalog, 'weapon_names', ()) or ())
    pilots = _name_list(obj.get('pilots'), getattr(catalog, 'pilot_names', ()) or ())
    mods = _name_list(obj.get('mods'), getattr(catalog, 'mod_names', ()) or ())
    question = obj.get('question')
    if not isinstance(question, str) or not question.strip():
        question = ''
    return {
        'intent': intent,
        'mechs': mechs,
        'weapons': weapons,
        'pilots': pilots,
        'mods': mods,
        'from_rank': _int_or_none(obj.get('from_rank'), 1, 6),
        'to_rank': _int_or_none(obj.get('to_rank'), 1, 6),
        'from_level': _int_or_none(obj.get('from_level'), 1, 7),
        'to_level': _int_or_none(obj.get('to_level'), 1, 7),
        'question': question.strip()[:400],
        'unclear': bool(obj.get('unclear')),
    }


RANK_WORDS = re.compile(r'\b(?:rank|ranks|stars?|level|levels|lvl|lv|r)\s*\d|\b\d\s*(?:to|->|\u2192|-)\s*'
                        r'(?:rank|level|lvl|l)?\s*\d|\bmax(?:ed|ing)?\b', re.I)
COST_WORDS = re.compile(r'\b(?:upgrade|upgrading|cost|costs|price|priced|rank ?up|to rank|how much|'
                        r'cheaper|expensive)\b', re.I)


def reconcile_entities(understanding, text, catalog):
    """What the player typed outranks what the model heard.

    The catalog check on the model's names only asks whether each name exists, never whether it is
    the name in the message. "redox" - one word, no rank, no cost wording - came back from the model
    as an upgrade-cost question about Charge Rocket 10. Every name in that answer is a real catalog
    row, so it survived validation, and the bot priced a weapon the player never mentioned. Here the
    message itself is scanned for catalog names and the two are compared:

    * a name the player typed that the model missed is added,
    * a name the model reported that the player did not type is dropped,
    * an upgrade-cost intent with no rank and no cost wording in the message is not a cost question.
    """
    body = str(text or '')
    # Mechs and weapons first: a mech or a weapon is what these questions are about, and no mech name
    # is an ordinary English word. Pilots and mods are only read out of the sentence when the model
    # already heard one - "in case of a bug" must not resolve to the pilot called Case.
    gear = {
        'mechs': _name_list(None, getattr(catalog, 'mech_names', ()) or (), found_in=body),
        'weapons': _name_list(None, getattr(catalog, 'weapon_names', ()) or (), found_in=body),
    }
    if any(gear.values()):
        # The message names catalog gear, so those are the subjects. Anything else the model added is
        # not in the sentence and has no business being priced or described.
        understanding = dict(understanding, **gear)
    for key in ('pilots', 'mods'):
        reported = understanding.get(key) or []
        if reported:
            lowered = body.casefold()
            understanding = dict(understanding,
                                 **{key: [name for name in reported if name.casefold() in lowered]})
    if understanding.get('intent') == 'upgrade_cost' and not (RANK_WORDS.search(body)
                                                              or COST_WORDS.search(body)):
        # One word, no rank, no cost word: that is "tell me about this", not "price this transition".
        understanding = dict(understanding, intent='item_stats', from_rank=None, to_rank=None,
                             from_level=None, to_level=None)
    return understanding


async def understand(base, key, model, text, history=None, catalog=None, session=None):
    """Step 1. Returns a normalised understanding, or None if the model failed."""
    if not key or not model:
        return None
    recent = []
    for item in (history or [])[-6:]:
        if isinstance(item, dict) and item.get('role') in ('user', 'assistant') and item.get('content'):
            recent.append({'role': item['role'], 'content': str(item['content'])[:400]})
    user_payload = {'message': str(text)[:1200]}
    if recent:
        user_payload['recent_conversation'] = recent
    try:
        raw = await _chat(base, key, model, UNDERSTAND_PROMPT, json.dumps(user_payload, ensure_ascii=False),
                          session=session, temperature=0.0, max_tokens=400)
    except Exception as exc:
        log.warning('Understand step failed (%s); falling back to the deterministic ladder.', type(exc).__name__)
        return None
    if catalog is None:
        return parse_json_object(raw)
    understanding = normalise_understanding(parse_json_object(raw), catalog)
    if understanding is None:
        return None
    return reconcile_entities(understanding, text, catalog)


# -------------------------------------------------------------------- formatting
def guard_figures(reply, engine_text):
    """Refuse a formatted reply that introduced a number the engine never gave.

    This is the one safety property the format step must not lose: the model is
    allowed to lay numbers out, never to produce them. A number in the reply that
    does not appear in the engine output means the answer is no longer traceable to
    verified data, so we drop the formatting and keep the engine's own text.
    """
    def numbers(text):
        # Any number of two or more digits. A single digit is a rank, a level or a
        # weapon size ("rank 1", "Hornet 8"), and policing those would reject good
        # formatting; a two-digit figure, though, is a cost or a stat and must be
        # traceable to the engine.
        found = re.findall(r'\b\d[\d,]*\b', str(text or ''))
        return {value for value in found if len(value.replace(',', '')) >= 2}

    engine_numbers = numbers(engine_text)
    reply_numbers = numbers(reply)
    # A number the engine gave may be re-punctuated ("1,525" vs "1525"). Compare
    # stripped forms so formatting choices are not mistaken for invention.
    strip = lambda values: {v.replace(',', '') for v in values}
    invented = strip(reply_numbers) - strip(engine_numbers)
    return (not invented), invented


def guard_substance(reply, engine_text):
    """Refuse a formatted reply that kept the figures and threw away the answer.

    The second half of the same problem: a card of correct numbers is not a study of the mech.
    Asked to lay out a full review, a model returned "Redox - 227,300 HP - 22 speed - 948 power -
    32 energy (9,375 A-Coins)." - every figure present, so guard_completeness passed it, and the
    review, the ability and the caveat were gone. Judged on content words, not length, and only
    when the engine produced something substantial to lose.
    """
    def content(text):
        return {word.casefold() for word in re.findall(r'[A-Za-z]{4,}', str(text or ''))}
    engine = content(engine_text)
    if len(engine) < 12:
        return True, 'engine text too short to judge'
    kept = len(engine & content(reply)) / len(engine)
    if kept < 0.4:
        return False, f'kept {kept:.0%} of the answer'
    return True, ''


def guard_completeness(reply, engine_text):
    """Refuse a formatted reply that dropped figures the engine gave.

    The mirror of guard_figures, and it was missing. A model asked to "write the reply" will
    happily compress a stat sheet into "Redox is a solid attacker." - no invented numbers, so
    the invention guard passed it, and a player got one opinion instead of HP, speed, power and
    the price. Layout is the model's job; deciding which verified figures the player needs is
    not. Returns (complete, dropped_numbers).
    """
    def numbers(text):
        found = re.findall(r'\b\d[\d,]*\b', str(text or ''))
        return {value.replace(',', '') for value in found if len(value.replace(',', '')) >= 2}

    dropped = numbers(engine_text) - numbers(reply)
    return (not dropped), dropped


async def format_reply(base, key, model, understanding, engine_text, history=None, session=None):
    """Step 3. Returns a laid-out Discord reply, or None to keep the engine's own."""
    if not key or not model or not engine_text:
        return None
    recent = []
    for item in (history or [])[-4:]:
        if isinstance(item, dict) and item.get('role') in ('user', 'assistant') and item.get('content'):
            recent.append({'role': item['role'], 'content': str(item['content'])[:300]})
    payload = {
        'question': understanding.get('question') or '',
        'intent': understanding.get('intent'),
        'engine_result': str(engine_text)[:2500],
    }
    if recent:
        payload['recent_conversation'] = recent
    try:
        raw = await _chat(base, key, model, FORMAT_PROMPT, json.dumps(payload, ensure_ascii=False),
                          session=session, temperature=0.3, max_tokens=700)
    except Exception as exc:
        log.warning('Format step failed (%s); sending the engine reply unchanged.', type(exc).__name__)
        return None
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
    if not text or len(text) < 8:
        return None
    if len(text) > 1990:
        text = text[:1988].rsplit('\n', 1)[0] + ' …'
    ok, invented = guard_figures(text, engine_text)
    if not ok:
        log.warning('Formatted reply introduced %s figure(s); keeping the engine text.', len(invented))
        return None
    ok, dropped = guard_completeness(text, engine_text)
    if not ok:
        log.warning('Formatted reply dropped %s verified figure(s); keeping the engine text.', len(dropped))
        return None
    ok, reason = guard_substance(text, engine_text)
    if not ok:
        log.warning('Formatted reply replaced the answer with a thinner one (%s); keeping the engine text.',
                    reason)
        return None
    return text


# ------------------------------------------------------------------- the engine
def engine_answer(understanding, catalog, cost_answer, weapon_answer, mech_answer,
                  advisor_answer=None, builds_answer=None):
    """Step 2. Run the existing deterministic engines for the understood intent.

    Every argument is a callable the caller supplies, so this module owns no game
    logic and no data. Whatever the intent, the answer text comes from code that
    already existed and is already tested; the pipeline only decides which one to
    run and with what.
    """
    intent = understanding['intent']
    text, source = '', 'none'

    if intent == 'upgrade_cost':
        text = cost_answer(understanding) or ''
        source = 'cost'
    elif intent == 'compare':
        # "X or Y?" is a choice, so route it to the comparison engine, never to a pair of
        # catalog cards stapled together: the cards do not say which one wins.
        text, source = '', 'compare'
        if advisor_answer:
            text = advisor_answer(understanding.get('question') or '') or ''
    elif intent in ('item_stats', 'meta_advice'):
        # A single subject: describe it. Only fall back to the advisor if the catalog
        # had nothing, and never treat "describe this" as a preference question.
        parts = []
        for name in understanding['mechs'][:3]:
            piece = mech_answer(name) or ''
            if piece:
                parts.append(piece)
        for name in understanding['weapons'][:3]:
            piece = weapon_answer(name) or ''
            if piece:
                parts.append(piece)
        if not parts and advisor_answer:
            piece = advisor_answer(understanding.get('question') or '') or ''
            if piece:
                parts.append(piece)
        text, source = '\n\n'.join(parts), 'catalog'
    elif intent == 'builds':
        text = (builds_answer(understanding) if builds_answer else '') or ''
        source = 'builds'
    else:
        # Event, promo, patch, forum, calc, reminder, help and chitchat keep their own
        # deterministic handlers; the pipeline does not answer them.
        return None

    text = (text or '').strip()
    if not text:
        return None
    return {'text': text, 'source': source, 'intent': intent}
