"""The voice the advisor wears in the channel.

`mechbot/advisor.py` decides what is true. This module decides what gets said, in what order, and it
is the only layer a player ever sees. Three rules hold the whole thing together:

* Every figure in a reply has to be in the engine's envelope. The renderer adds no arithmetic of its
  own, so a number that is not in `envelope['numbers']` never reaches the channel - if the guard sees
  one, the reply is rebuilt from a numberless template instead of being quietly wrong.
* The prose carries its own provenance. A community tier is named as a community tier and a screenshot
  row as a screenshot row, because a player spending A-coins needs to know how much to trust the number
  before they spend, not after.
* Questions are limited. Two follow-ups at most, one is usual, and an answer that can be given with a
  documented assumption is given rather than asked.

It runs with no LLM configured: routing and wording are deterministic, which is also what makes the
golden tests below meaningful.
"""
from __future__ import annotations

import re
import time

from . import advisor
from .humanize import audit, polish
from .pipeline import correct_names

MAX_ANSWER = 1800
DISCORD_LIMIT = 1900

# Routing is prose only. Players @-mention the bot and type a sentence; nobody opens a slash menu in
# the middle of a raid, and nothing here is reachable only as a command.
ECR_WORDS = re.compile(r'\b(?:ecr|crates?|crate rush|pull(?:ing|ed)?|this week|weekly)\b', re.I)
COMPARE_WORDS = re.compile(r'\b(?:vs\.?|versus|compare|comparison|better|worse|instead of|rather than|'
                           r'or the|which one|difference)\b', re.I)
FIGHT_WORDS = re.compile(r"\b(?:who wins|who'd win|1v1|one v one|duel|beat|kill me|take the fight|"
                         r"fights? (?:me|you))\b", re.I)
SPEND_WORDS = re.compile(r'\b(?:invest|spend|buy|worth|budget|bang for (?:the )?buck|upgrade|rank up|'
                         r'ac(?:oins)?|credits?|blueprints?)\b', re.I)
ASK_WORDS = re.compile(r'\b(?:should i|is it worth|worth (?:my|the)|should we|do i)\b', re.I)


ADVISOR_INTENT = re.compile(r'\b(?:worth|invest|spend|buy|budget|ecr|crates?|pull|bang for (?:the )?buck|'
                            r'upgrade|rank up|rank|max(?:ed|ing)?|vs\.?|versus|better|worse|who wins|should i|'
                            r'which (?:one|mech|weapon|gun)|a?coins?|ac|credits?|blueprints?)\b', re.I)
COST_LOOKUP = re.compile(r'\b(?:how much|what.{0,12}cost|cost (?:to|of)|price (?:to|of))\b', re.I)
SPEND_INTENT = re.compile(r'\b(?:worth|should i|buy|invest|spend|pull|budget|ecr|crate|max(?:ed|ing)?)\b', re.I)

CHOICE_WORDS = re.compile(r'\b(?:or|vs\.?|versus|instead of|rather than|better|difference)\b', re.I)


RANKABLE = re.compile(r'\b(?:mechs?|weapons?|guns?|loadouts?|gear|store|shop|market|hub|budget|save|'
                      r'savings|a?coins?|ac|credits?|blueprints?|bps?|ecr|crates?|rank|level|max|upgrade|'
                      r'pull|invest|spend|buy|worth|best|anything|something|everything)\b', re.I)
QUALITY_WORDS = re.compile(r'\b(?:good|great|awful|terrible|bad|op|meta|broken|viable|strong|weak|'
                            r'schlump?y|worth|max|use|run|play|buff|nerf)\b', re.I)

# How to *get* a currency is not a spend decision: that is what the announcement brain and the AI answer,
# and a store ranking in its place reads like the bot dodged the question. Only the acquiring verbs are
# listed, so "what should I spend my credits on" and "should I save for the hub" still reach the advisor.
EARNING_WORDS = re.compile(r'\b(?:how (?:do|can|could|would|to)\s+i?\s*(?:get|make|earn|farm|grind|obtain|'
                            r'save up|collect)|free (?:a-?coins?|credits?|gears?|crates?)|'
                            r'(?:earn|farm|grind|obtain) (?:free |more |extra )?(?:a-?coins?|credits?|'
                            r'blueprints?|bps?|gears?))\b', re.I)


def _names_a_choice(body, data, mentions=None):
    """"storm racks or charge rockets" carries no spend word at all, yet it is exactly the question.

    The wording gate has to accept a two-item choice on its own shape, otherwise the advisor only ever
    hears from players who happen to say "worth" or "invest" in the same sentence.
    """
    if not CHOICE_WORDS.search(body):
        return False
    if COST_LOOKUP.search(body):
        return False
    mentions = advisor.gear_mentions(body, data) if mentions is None else mentions
    return len(mentions) >= 2


# The engine may ask up to two questions before it answers. Those questions are only useful if a
# four-word reply is understood as an answer rather than as a fresh, contextless question, so the last
# unanswered question is held in memory per player and folded back in.
FOLLOWUP_TTL_SECONDS = 15 * 60
FOLLOWUP_LIMIT = 2
ANSWER_WORDS = 12
_PENDING: dict = {}


def _scope_key(scope):
    return tuple(str(part or '') for part in (scope or ()))


# A pending question is only worth carrying into the next message if that message actually answers it.
# Without this check the bot reads "hello there" as the answer to "what can you spend?", which is how a
# helpful feature becomes a hijack of somebody else's sentence.
SLOT_ANSWER = {
    'budget': re.compile(r'\d|\bk\b|grand|\ba?c\b|\ba?[- ]?coins?\b|credits?|blueprints?|\bbps?\b', re.I),
    'role': re.compile(r'scout|tank|support|assault|sniper|bruiser|close|long|mid\b|range|cq|comp|'
                       r'tryhard|casual|flexible|any', re.I),
    'engagement': re.compile(r'close|long|mid\b|range|cq|open ground|corridor|vertical', re.I),
    'goal': re.compile(r'comp|trophy|ladder|fun|catch ?up|free to play|f2p|max|finish|save', re.I),
    'owned': re.compile(r"\bi (?:have|own|got|run|main|use)\b|don'?t have|\bnothing\b|\bmy \w+", re.I),
}
NAME_SLOTS = ('items', 'item', 'loadouts')   # these ask for a name, so a name is what fills them

# The follow-up the player actually reads is the thing they are answering, so the slots a reply has to
# fill are read out of those questions as well as out of the engine's own `missing_slots`: the two can
# disagree, and when they do the player's sentence wins.
FOLLOWUP_SLOTS = (('budget', r'spend|credits?|a-?coins?|blueprints?|\bbps?\b'),
                  ('role', r'\brole\b|close quarters|long range|support|playstyle|what (?:mech|weapon).*main'),
                  ('engagement', r'range band|close, mid or long|where do you expect'),
                  ('goal', r'optimis|optimiz|competing|catching up|maxing|for what'),
                  ('owned', r'already have|what do you (?:own|have)|what.*run'),
                  ('items', r'which two|name (?:them|the two)'),
                  ('item', r'which item|what is "it"'))


def _slots_needed(envelope, followups):
    slots = [str(slot) for slot in (envelope or {}).get('missing_slots') or []]
    for row in followups:
        text = str(row or '').casefold()
        for name, pattern in FOLLOWUP_SLOTS:
            if name not in slots and re.search(pattern, text):
                slots.append(name)
    return slots


def _fills_a_slot(record, body, data):
    """Whether this sentence answers something the advisor said it still needed."""
    slots = [str(slot) for slot in (record.get('slots') or [])]
    if not slots:
        return False
    for slot in slots:
        if slot in NAME_SLOTS:
            if advisor.gear_mentions(body, data):
                return True
            continue
        pattern = SLOT_ANSWER.get(slot)
        if pattern is None:
            if len(body.split()) <= 4:
                return True
        elif pattern.search(body):
            return True
    return False


def forget(scope):
    _PENDING.pop(_scope_key(scope), None)


def pending_question(scope):
    """What the advisor asked this player and is still waiting for, if that thread is still live."""
    key = _scope_key(scope)
    record = _PENDING.get(key)
    if not record:
        return None
    if record['expires'] < time.monotonic():
        _PENDING.pop(key, None)
        return None
    return record


def carried_question(scope, text, data=None):
    """The question to answer now: this sentence, or the pending one with this sentence attached.

    "500 ac" is not a question by itself, but it is exactly what the advisor asked for. A reply that
    names a different item is a new question and wins outright, and a long sentence is treated as one
    too, because nobody answers "what role do you play" in three clauses.
    """
    record = pending_question(scope)
    if not record:
        return None
    body = re.sub(r'\s+', ' ', str(text or '').strip())
    if not body or len(body.split()) > ANSWER_WORDS:
        return None
    data = data or advisor.load()
    asked = set(advisor.gear_mentions(record['question'], data))
    said = set(advisor.gear_mentions(body, data))
    if said - asked:
        return None
    if not said and len(body.split()) > 8:
        return None
    if not _fills_a_slot(record, body, data):
        return None
    return f"{record['question']} {body}"


# A comparison is a decision, and players keep talking after it. "which is better X or Y" followed by
# "i have rosa and gemini implant, what do i do now" used to be two unrelated sentences: the second one
# named gear, so `route` called it a store ranking and the answer ranked Charge Rocket 6 to a player who
# had just been told about two missiles. The thread below keeps the last decision for a few minutes so a
# follow-up can be answered against it.
THREAD_TTL_SECONDS = 20 * 60
_THREAD: dict = {}


def remember_thread(scope, question, result):
    """Hold the decision just made, so the next sentence can be read as a follow-up to it.

    It also holds the one thing the answer said it could not settle. A comparison that comes out even
    asks which role the player plays; "what should i buy" a minute later is that question being
    answered, and the store ranking has no way to know it.
    """
    key = _scope_key(scope)
    if not key:
        return
    envelope = (result or {}).get('envelope') or {}
    picks = [str(pick) for pick in (envelope.get('picks') or []) if pick]
    if not picks and envelope.get('item'):
        picks = [str(envelope['item'])]      # one-item answers have a subject, not a pair
    verdict = str(envelope.get('verdict') or '')
    if not picks:
        _THREAD.pop(key, None)
        return
    if len(_THREAD) > 400:
        now = time.monotonic()
        for stale in [other for other, row in _THREAD.items() if row['expires'] < now]:
            _THREAD.pop(stale, None)
    _THREAD[key] = {'question': str(question or '').strip(), 'picks': picks,
                    'winner': (picks[0] if verdict == 'A' else picks[1] if verdict == 'B'
                               else picks[0] if len(picks) == 1 and verdict in ('BUY', 'YES', 'GOOD') else None),
                    'capability': (result or {}).get('capability'),
                    'verdict': verdict,
                    'owned': [str(name) for name in (envelope.get('owned') or []) if name],
                    'expires': time.monotonic() + THREAD_TTL_SECONDS}


def last_decision(scope):
    """The decision this player was just reading, if that thread is still live."""
    key = _scope_key(scope)
    row = _THREAD.get(key)
    if not row:
        return None
    if row['expires'] < time.monotonic():
        _THREAD.pop(key, None)
        return None
    return row


FOLLOW_UP_SHAPE = re.compile(r'\b(?:so|then|ok(?:ay)?|now|what|which|and|but|should|do|next|about|instead|'
                             r'for me|my|with|use|using|build|fit|fits|worth|better|still|have|own|got|run)\b',
                             re.I)

# A sentence that asks for one item's own numbers is a lookup, never a follow-up: a thread must not
# swallow "what are Acantha rank 3 pilot bonuses" just because it names a pilot the thread mentioned.
LOOKUP_SHAPE = re.compile(r'\b(?:rank|stars?|level|lvl|bonus(?:es)?|stats?|hp|speed|power|energy|'
                         r'costs?|price|upgrade|ladder|how much|worth it)\b', re.I)

# Words for gear the catalog does not hold a row for. Saying so is the honest answer; quietly reading
# "gemini implant" as the Gemini weapon and then talking about implants would be an invention.
UNMODELLED_WORDS = re.compile(r'\b(?:implants?|mods?|chips?|skins?|avatars?)\b', re.I)


def thread_follow_up(scope, text, data=None):
    """The earlier question this sentence is a follow-up to, or None when it stands on its own.

    A follow-up does not open a new comparison: it adds something to the decision the player is still
    reading - a role, a pilot, or what they already own. Anything that names two fresh things to weigh
    is a new question and falls through to the normal router.
    """
    thread = last_decision(scope)
    if not thread:
        return None
    body = re.sub(r'\s+', ' ', str(text or '').strip().strip('?!.'))
    if not body or len(body.split()) > 24:
        return None
    if LOOKUP_SHAPE.search(body):
        # "what are Acantha rank 3 pilot bonuses" is a catalog lookup for one named thing, whatever
        # thread is open. It needs the stat row, not a continuation of a decision.
        return None
    data = data or advisor.load()
    mentions = advisor.gear_mentions(body, data)
    picked = {str(name).casefold() for name in thread['picks']}
    fresh = {str(name).casefold() for name in mentions} - picked
    if len(fresh) >= 2 and re.search(r'\b(?:or|vs\.?|versus|better|instead of|which)\b', body, re.I):
        return None                       # "storm racks or charge rockets" is its own question
    if not (FOLLOW_UP_SHAPE.search(body) or fresh or advisor._ctx(body, data)['role']
            or advisor._ctx(body, data)['owned']):
        return None
    return f"{thread['question']} {body}".strip()


def _owned_phrases(text, data):
    """What the player says they have, with their own wording kept: ('gemini implant' -> Gemini).

    The canonical name alone is not enough to answer with. "gemini implant" is not a Gemini 10, and a
    reply that says "you already own Gemini 10" is inventing a purchase the player never mentioned.
    """
    clause = re.search(r'\bi\s+(?:own|have|got|run|play|use|main)(?:s|ed|ing)?\b([^?!.]*)', str(text or ''),
                       re.I)
    if not clause:
        return []
    phrases = []
    for piece in re.split(r'\band\b|,|;', clause.group(1)):
        piece = re.sub(r'\s*\([^)]*\)', '', piece).strip(' .,')
        if not piece:
            continue
        names = advisor._mention_names(piece, data)
        if len(names) == 1:
            phrases.append((names[0], piece))
    return phrases


BUY_WORDS = re.compile(r'\b(?:buy|purchase|spend|invest|worth it|pick up|get)\b', re.I)


def _say_thread(engine, question=''):
    """The follow-up answer: the earlier decision, applied to what the player just said.

    The opening line names the decision being continued. A player who types one sentence after a
    comparison should be able to see which comparison it was answered against.
    """
    picks = [str(name) for name in (engine.get('picks') or []) if name]
    winner = engine.get('winner') or engine.get('item')
    if not picks:
        return _say_failed(engine)
    heading = (f'Following up on **{picks[0]} vs {picks[1]}**.' if len(picks) > 1
               else f'Following up on **{picks[0]}**.')
    blocks = [heading]
    data = advisor.load()
    verdict = str(engine.get('verdict') or '')
    if len(picks) > 1 and BUY_WORDS.search(str(question or '')):
        # "so what should I buy" after a comparison is asking which of the two. Ranking the whole store
        # was the answer it used to get, which is a different question with a longer answer.
        blocks.append(_buy_from_the_pair(picks, winner, engine.get('owned') or [], data, question))
        return '\n'.join(block for block in blocks if block)
    if len(picks) == 1 and not winner:
        winner = picks[0]
    role_line = _role_answer(winner, picks, question, data)
    if role_line:
        blocks.append(role_line)
    owned_line = _owned_answer(winner, picks, question, data, verdict)
    if owned_line:
        blocks.append(owned_line)
    next_line = _next_step(winner, picks, question, data)
    if next_line:
        blocks.append(f'**What I would do:** {next_line}')
    return '\n'.join(blocks)


def _buy_from_the_pair(picks, winner, owned, data, question):
    """The answer to "so what should I buy" when the last thing the player saw was two items."""
    left, right = picks[0], picks[1]
    mine = [str(name) for name in owned]
    if mine:
        rest = [name for name in picks if name not in mine]
        if rest:
            return (f'**Buy {rest[0]}.** You already have {mine[0]}, so this is an addition rather than a '
                    f'replacement, and it is the one of the two you do not own.')
        return f'**Buy neither.** You already have both of them.'
    prices = {name: advisor.store_price(advisor.resolve(name, data).get('item') or {}, data)
              for name in picks}
    costs = {name: (advisor.amount_text(row['currency'], row['amount'])
                    if row.get('status') == 'ok' else None) for name, row in prices.items()}
    if winner:
        return (f'**Buy {winner}.** It is the one this comparison put ahead'
                + (f', at {costs.get(winner)}' if costs.get(winner) else '') + '.')
    if costs.get(left) and costs.get(right) and costs[left] != costs[right]:
        cheap = left if (advisor.store_price(advisor.resolve(left, data).get('item') or {},
                                             data).get('amount') or 0) < \
            (advisor.store_price(advisor.resolve(right, data).get('item') or {},
                                 data).get('amount') or 0) else right
        return (f'**Buy neither yet.** They are the same tier, the catalog holds nothing separating them, '
                f'and only the price differs: {cheap} is the cheaper of the two at {costs[cheap]}.')
    return (f'**Buy whichever you will actually run.** The two are the same tier and the catalog holds no '
            f'figure that separates them, so the decision is which of the two suits your hangar.')


def _role_answer(winner, picks, question, data):
    """A pilot or a role in the follow-up, weighed against the pick already given."""
    pilot = _pilot_named(question, data)
    role = (advisor._ctx(question, data) or {}).get('role')
    if not winner or not (pilot or role):
        return None
    if pilot:
        skill = str(pilot.get('skill') or '').strip()
        fits = [name for name in picks if skill and _category_matches(skill, name, data)]
        if skill and fits:
            if winner in fits:
                return (f'**{pilot["name"]} is a {skill.lower()} pilot,** and {winner} is the {skill.lower()} '
                        f'weapon of the two - the pilot backs the pick you already have.')
            return (f'**{pilot["name"]} is a {skill.lower()} pilot,** which points at {fits[0]} rather than '
                    f'{winner}: a pilot boosts the weapon it is built for. It does not change the numbers, '
                    f'and {winner} still wins those.')
        kind = str((advisor.resolve(winner, data).get('item') or {}).get('kind') or '')
        if kind == 'mech':
            # A pilot's specialty is a weapon word. Against a mech question the honest line is that it
            # boosts a different thing, not that the catalog knows nothing about the pilot.
            return (f'**{pilot["name"]} is {_article(skill)} {skill.lower()} pilot,** so it boosts '
                    f'{skill.lower()} weapons - a different question from the one about {winner}. '
                    f'It does not change the answer on the mech.')
        return (f'**{pilot["name"]} does not decide this one.** The catalog records no skill for it that '
                f'maps to either weapon, so {winner} stays the pick on price and damage per energy.')
    return (f'**Reading that as {role} play:** {winner} stays the pick. Price and damage per energy decide '
            f'it, and role does not move either number.')


def _owned_answer(winner, picks, question, data, verdict=''):
    """What the player says they already have, and what that means for buying the other one."""
    phrases = _owned_phrases(question, data)
    if not phrases:
        return None
    if not winner:
        # A one-item answer has no winner field; the subject of it is the thing to talk about.
        winner = picks[0] if picks else None
    if not winner:
        return None
    if len(picks) == 1:
        return _owned_answer_one(winner, phrases, verdict, data, question)
    pilot = _pilot_named(question, data)
    pilot_name = str((pilot or {}).get('name') or '').casefold()
    blocks = []
    for canonical, phrase in phrases:
        if pilot_name and canonical.casefold() == pilot_name:
            continue                       # the pilot line above already answered this one
        named = [name for name in picks if name.casefold().startswith(canonical.casefold())
                 or canonical.casefold() in name.casefold()]
        if named:
            rest = [name for name in picks if name not in named]
            blocks.append(f'**You have {phrase}, so {named[0]} is not a purchase.**'
                          + (f' The only question is whether {rest[0]} adds enough over it, and on the '
                             f'figures above it does.' if rest else ''))
        elif UNMODELLED_WORDS.search(phrase):
            blocks.append(f'**{_upcase_first(phrase)}:** the catalog holds no implant rows, so I read that '
                          f'as the {canonical} weapon line and I cannot price the implant itself.')
        else:
            blocks.append(f'**You have {phrase}.** It is not one of the two above, so it does not change '
                          f'the pick.')
    return ' '.join(blocks)


def _owned_answer_one(subject, phrases, verdict, data, question=''):
    """What the player owns, when the thread is about a single item rather than a pair."""
    pilot = _pilot_named(question, data)
    pilot_name = str((pilot or {}).get('name') or '').casefold()
    lines = []
    for canonical, phrase in phrases:
        if pilot_name and canonical.casefold() == pilot_name:
            continue
        if canonical.casefold() in subject.casefold() or subject.casefold().startswith(canonical.casefold()):
            lines.append(f'**You have {phrase}, so {subject} is not a purchase** for you. The answer above '
                         f'is about the upgrade ladder, not the shelf price.')
        elif UNMODELLED_WORDS.search(phrase):
            lines.append(f'**{_upcase_first(phrase)}:** the catalog holds no implant rows, so I read that as '
                         f'the {canonical} weapon line and cannot price the implant itself.')
        else:
            lines.append(f'**You have {phrase}.** It is not {subject}, so it does not change the answer on it.')
    return ' '.join(lines) or None


def _next_step(winner, picks, question, data):
    """One instruction, so a follow-up never ends on a description of the problem."""
    if not winner:
        return None
    other = next((name for name in picks if name != winner), None)
    if not other:
        return (f'spend on {winner} itself - the upgrade ladder, not a second copy of anything you '
                f'already run.')
    price = advisor.store_price(advisor.resolve(winner, data).get('item') or {}, data)
    cheaper = other and advisor.store_price(advisor.resolve(other, data).get('item') or {}, data)
    if price.get('status') == 'ok' and cheaper and cheaper.get('status') == 'ok' \
            and cheaper.get('amount', 0) < price.get('amount', 0):
        return (f'put the {advisor.amount_text(cheaper["currency"], cheaper["amount"])} toward {winner} '
                f'rather than {other}, and spend nothing on a second copy of what you own.')
    return f'work on {winner} first, and leave the other one until it shows up cheaper.'


def _pilot_named(body, data):
    """A pilot in the sentence, read from the pilot list so no name is ever invented."""
    names = getattr(data, 'pilots', None) or []
    found = []
    lowered = str(body or '').casefold()
    for row in names:
        name = str((row or {}).get('name') or '').strip()
        if name and re.search(r'\b' + re.escape(name.casefold()) + r'\b', lowered):
            found.append(row)
    return found[0] if len(found) == 1 else None


def _category_matches(skill, name, data):
    """Whether a pilot's skill word describes a weapon's category, using the catalog's own label."""
    item = advisor.resolve(name, data).get('item') or {}
    category = str(item.get('category') or '').strip().casefold()
    want = str(skill or '').strip().casefold()
    return bool(want and category and (want in category or category in want))


def remember_question(scope, question, result):
    """Hold the question the advisor still needs answered, and drop it the moment it is not pending.

    The cap is what keeps an interview from becoming an interrogation: after two answers the engine
    says what it assumed and commits to one, which is the rule the advisor was built to.
    """
    key = _scope_key(scope)
    if not key:
        return
    record = _PENDING.get(key)
    asked_before = record['asks'] if record else 0
    envelope = (result or {}).get('envelope') or {}
    followups = [str(row) for row in envelope.get('followups') or []]
    capability = (result or {}).get('capability')
    if not followups or capability in (None, 'unknown', 'empty') or asked_before >= FOLLOWUP_LIMIT:
        _PENDING.pop(key, None)
        return
    if len(_PENDING) > 400:
        now = time.monotonic()
        for stale in [other for other, row in _PENDING.items() if row['expires'] < now]:
            _PENDING.pop(stale, None)
    _PENDING[key] = {'question': str(question or '').strip(), 'followups': followups,
                      'slots': _slots_needed(envelope, followups),
                      'asks': asked_before + 1, 'expires': time.monotonic() + FOLLOWUP_TTL_SECONDS}


def advisor_subject(result):
    """The short name of whatever an answer is about, for acknowledging a carried-over reply.

    Nothing is invented here: it reads the item the engine resolved, and falls back to the generic
    phrase when a capability spans the whole store rather than one named thing.
    """
    envelope = (result or {}).get('envelope') or {}
    item = envelope.get('item')
    if isinstance(item, dict):
        item = item.get('name')
    if isinstance(item, str) and item.strip():
        return item.strip()
    names = [row.get('name') for row in (envelope.get('picks') or []) if isinstance(row, dict) and row.get('name')]
    if not names:
        names = [row.get('name') for row in (envelope.get('plan') or envelope.get('actions') or [])
                 if isinstance(row, dict) and row.get('name')]
    if names:
        return ' and '.join(str(name) for name in names[:2])
    return ''


def wants_advisor(text, data=None):
    """Is this a spend, crate or loadout question for the advisor, and not one of the bot's other paths?

    Two shapes qualify. A sentence with decision wording has to name gear the advisor models, or talk
    about a crate or a budget. A short sentence that is nothing but an item name qualifies on its own:
    "@bot voidghost" is a player asking what we know about the Voidghost, and the catalog answers that
    better than a request for a screenshot. The mention scan runs only once the wording gate passes,
    because this sits on the hot path for every @-mention in the channel.
    """
    body = str(text or '').strip()
    if len(body) > 900:
        return False
    words = body.split()
    if not words:
        return False
    data = data or advisor.load()
    if len(words) < 3:
        return bool(advisor.gear_mentions(body, data))
    mentions = advisor.gear_mentions(body, data)
    # A decision word is enough on its own; "is the voidghost any good" only counts because it names a
    # mech, and "what should i invest in" counts because ranking the store is the answer to it.
    decided = bool(ADVISOR_INTENT.search(body) or SPEND_INTENT.search(body)
                   or (mentions and QUALITY_WORDS.search(body)) or _names_a_choice(body, data, mentions))
    if not decided:
        return False
    if COST_LOOKUP.search(body) and not SPEND_INTENT.search(body):
        return False
    if ECR_WORDS.search(body) or advisor.parse_budget(body) or mentions:
        return True
    # Nothing named, but a question about the store, a currency or a rank is still the advisor's job:
    # "what should I invest in?" is answered by ranking the catalog, and if the sentence turns out to be
    # nothing it can rank, `route` says so and the normal answer goes back to the old path.
    if EARNING_WORDS.search(body):
        return False
    return bool(RANKABLE.search(body))


def _repair_names(body, data):
    """Rewrite misspelled catalog names in the question, and say which ones were rewritten."""
    # The engine's own name index, not the bot catalog: this module runs with no bot and no database,
    # and the advisor pack is the thing it resolves names against everywhere else.
    pool = tuple(name for name, (kind, _row) in advisor._candidate_names(data).items()
                 if kind in ('mech', 'weapon', 'pilot', 'mod'))
    if not pool:
        return body, {}
    corrected, corrections = correct_names(body, pool)
    return (corrected, corrections) if corrections else (body, {})


def route(text, data=None):
    """Which capability a sentence is asking for. The shape of the question decides, not a keyword list alone."""
    data = data or advisor.load()
    body = str(text or '')
    mentions = advisor.gear_mentions(body, data)
    budget = advisor.parse_budget(body)
    if len(mentions) >= 2 and (FIGHT_WORDS.search(body) or re.search(r'\+|\bwith\b', body)):
        return 'simulate_1v1'
    if FIGHT_WORDS.search(body):
        return 'ask_loadouts'
    if ECR_WORDS.search(body) and not budget:
        return 'ecr_advice'
    if len(mentions) >= 2 and (COMPARE_WORDS.search(body) or re.search(r'\bor\b|\bvs\b', body, re.I)):
        return 'compare'
    if budget and SPEND_WORDS.search(body):
        return 'plan_budget'
    if not mentions and ASK_WORDS.search(body) and re.search(r'\b(?:it|that|this|they)\b', body, re.I) \
            and not advisor.parse_budget(body):
        return 'clarify_item'          # "is it worth it" is a question about a pronoun, not an item
    if len(mentions) == 1:
        # "is the tailsting worth my a-coins" is about the Tailsting, not about the whole store
        return 'explain'
    if SPEND_WORDS.search(body) or ASK_WORDS.search(body):
        return 'rank_investments'
    if len(mentions) == 1:
        return 'explain'
    return 'unknown'


def answer(text, facts=None, data=None, limit=MAX_ANSWER, scope=None, thread_only=False):
    """One prose question in, one channel-ready answer out, with the envelope that justifies it.

    Returns a dict with `reply`, `capability`, `envelope` and a `style` note recording what the
    humanizing pass changed, so a reviewer can audit the wording without guessing.

    `scope` is the channel-and-author key the bot already uses for short-lived questions. With it, a
    sentence that follows a comparison is answered against that comparison instead of being routed
    from scratch - "i have rosa and gemini, what now" is a follow-up, not a store ranking.
    """
    data = data or advisor.load()
    if thread_only:
        # Only the continuation, or nothing: used by the caller that has already decided this
        # sentence is a follow-up and must not be re-routed as a fresh store question.
        result = _answer_follow_up(scope, text, None, facts, data, limit)
        return result or {'capability': 'unknown', 'status': 'no_match', 'envelope': {}, 'guard': 'n/a',
                          'style': {'tells': [], 'audit': []}, 'corrections': {}, 'reply': ''}
    follow = thread_follow_up(scope, text, data) if scope else None
    if follow:
        result = _answer_follow_up(scope, text, follow, facts, data, limit)
        if result:
            return result
    body = str(text or '').strip()
    if not body:
        return {'capability': 'empty', 'reply': 'Ask me something about a buy, a crate or a build.',
                'envelope': {}, 'status': 'empty', 'corrections': {}}
    # A misspelled item name used to delete half the question. "which is better outlow or deathwalker"
    # found Deathwalker and dropped "outlow", and the comparison came out as Deathwalker against
    # nothing. The repair is the same one the bot's own ladder uses, and the reply owns up to it.
    body, corrections = _repair_names(body, data)
    capability = route(body, data)
    repaired = dict(corrections or {})
    if capability in ('ask_loadouts', 'clarify_item'):
        engine = {'kind': 'ask', 'verdict': 'ASK', 'numbers': set(), 'caveats': [], 'assumptions': [],
                  'data_gaps': [], 'status': 'ask', 'missing_slots': ['item'],
                  'followups': (['Which mechs on each side? Name them and I can model the fight, and say '
                                 'whether you expect to meet close or long.'] if capability == 'ask_loadouts'
                                else ['What is "it"? Name the item and I can price it.'])}
        return _package(engine, capability, body, data, limit, None, repaired)
    facts = advisor._facts(facts)
    if capability == 'simulate_1v1':
        left, right = _split_versus(body)
        engine = advisor.simulate_1v1(left, right, _band(body), data)
    elif capability == 'compare':
        names = advisor._mention_names(body, data)
        left, right = (names[0], names[1]) if len(names) >= 2 else _split_versus(body)
        engine = advisor.compare(left, right, body, facts, data)
    elif capability == 'plan_budget':
        engine = advisor.plan_budget(body, facts, data)
    elif capability == 'ecr_advice':
        named = advisor._mention_names(body, data)
        engine = advisor.ecr_advice(name=(named[0] if named else None), facts=facts, question=body, data=data)
    elif capability in ('rank_investments', 'rank_investments_one'):
        engine = advisor.rank_investments(body, facts, data)
    elif capability == 'explain':
        named = advisor._mention_names(body, data)
        # The prose is what carries the player's rank, role and range band, so it has to travel with the
        # name: without it "i am rank 4" priced the whole ladder from zero and the answer was wrong.
        engine = advisor.explain(named[0] if named else body, facts, data, ctx=advisor._ctx(body, data))
    else:
        return {'capability': 'unknown', 'status': 'no_match', 'envelope': {}, 'guard': 'n/a',
                'style': {'tells': [], 'audit': []}, 'reply': 'I read gear, crates and builds. Name an item, a crate or two things you are '
                         'choosing between and I can price it.'}
    engine = advisor.apply_defaults(engine)
    issues = advisor.mention_issues(body, data)
    if issues:
        # a variant that does not exist is the answer; comparing its neighbours instead would be polite
        # and useless, so the refusal stands on its own with the variants offered
        refused = {issue['family'] for issue in issues}
        named_families = set()
        for name in advisor._mention_names(body, data):
            found = advisor.resolve(name, data)
            if found['item']:
                named_families.add(found['item']['family'])
        if capability == 'compare' and refused and refused <= named_families:
            engine = {'kind': 'note', 'verdict': 'REFUSED', 'status': 'refused', 'numbers': set(),
                      'caveats': [], 'assumptions': [], 'data_gaps': [], 'followups': [],
                      'refusal': issues[0], 'notes': [issue['note'] for issue in issues]}
            for issue in issues:
                engine['numbers'] |= {str(value) for value in
                                      (advisor.normalise(issue['note']).split() + issue['candidates'])}
            return _package(engine, capability, body, data, limit, scope, repaired)
        engine = dict(engine, kind=(engine.get('kind') or 'explain'),
                      refusal=issues[0], numbers=set(engine.get('numbers') or set()))
        for issue in issues:
            engine['numbers'] |= {str(value) for value in
                                  (advisor.normalise(issue['note']).split() + issue['candidates'])}
    if capability == 'explain' and SPEND_WORDS.search(body) and not ctx_role(body, data):
        # Q20: a one-item buy question is answered, then asked the one thing that would reorder it
        engine = dict(engine, followups=list(engine.get('followups') or []) + [advisor.FOLLOWUP_TEXT['role']],
                      provisional=True)
    return _package(engine, capability, body, data, limit, scope, repaired)


def _answer_follow_up(scope, text, merged, facts, data, limit):
    """Answer a sentence in the light of the decision the player is still reading.

    Two shapes, and both keep the earlier pick instead of starting over. A follow-up that adds a role,
    a pilot or what the player owns is answered from the thread alone; anything that still names two
    new items falls through to the normal router, because that is a new question wearing the same words.
    """
    thread = last_decision(scope)
    if not thread:
        return None
    winner = thread.get('winner')
    verdict = str(thread.get('verdict') or '')
    picks = list(thread.get('picks') or [])
    # A "what should I buy" after two items were compared is answered about those two, not from the
    # store ranking - it is the same decision carrying on.
    from_pair = len(picks) > 1 and BUY_WORDS.search(text)
    if not (from_pair or _role_answer(winner, picks, text, data)
            or _owned_answer(winner, picks, text, data, verdict)):
        return None
    engine = {'kind': 'thread', 'verdict': (thread.get('winner') or 'ASK'), 'status': 'ok',
              'picks': list(thread.get('picks') or []), 'item': thread.get('winner'),
              'owned': list(thread.get('owned') or []),
              'numbers': set(), 'caveats': [], 'assumptions': [], 'data_gaps': [], 'followups': [],
              'thread_question': thread.get('question')}
    # Any figure the follow-up quotes has to survive the digit guard, so it is gathered from the catalog
    # facts the thread already resolved rather than re-typed here.
    # The pick names themselves carry digits ("Gemini 10"), so they have to be allowed or the guard
    # reads the item's own name as an invented figure and throws the answer away.
    for name in engine['picks']:
        engine['numbers'] |= {part for part in re.findall(r'\d[\d,.]*', str(name))}
        item = advisor.resolve(name, data).get('item') or {}
        price = advisor.store_price(item, data)
        if price.get('status') == 'ok':
            engine['numbers'] |= {str(price.get('amount')), f"{price.get('amount'):,}"}
        for key in ('dmgPerEnergy', 'optimalRange'):
            value = str((item.get('review') or {}).get(key) or '')
            engine['numbers'] |= {part for part in re.findall(r'\d[\d,.]*', value)}
        engine['numbers'] |= {str(value) for value in (item.get('energy'),) if value is not None}
    pilot = _pilot_named(text, data)
    if pilot:
        engine['numbers'] |= {str(pilot.get('cost', {}).get('amount'))} if pilot.get('cost') else set()
    reply = _render(engine, text)
    reply, guard = _guard(reply, engine)
    styled, tells = polish(reply)
    if _digits_ok(styled, engine):
        reply = styled
    return {'capability': 'thread', 'status': 'ok', 'envelope': engine, 'reply': _clip(reply, limit),
            'guard': guard, 'style': {'tells': tells, 'audit': audit(styled)}, 'corrections': {}}


def _package(engine, capability, body, data, limit=MAX_ANSWER, scope=None, corrections=None):
    """Render, guard, humanize, clip. One place so every capability gets the same treatment."""
    reply = _render(engine, body)
    reply, guard = _guard(reply, engine)
    styled, tells = polish(reply)
    if _digits_ok(styled, engine):
        reply = styled
    result = {'capability': capability, 'status': engine.get('status') or 'ok', 'envelope': engine,
              'reply': _clip(reply, limit), 'guard': guard, 'style': {'tells': tells, 'audit': audit(styled)},
              'corrections': dict(corrections or {})}
    if capability in ('compare', 'explain') and scope:
        remember_thread(scope, body, result)
    return result


def ctx_role(text, data=None):
    return (advisor._ctx(str(text or ''), data) or {}).get('role')


# ---------------------------------------------------------------------------------------------
# wording
# ---------------------------------------------------------------------------------------------

def _say_note(engine, question=''):
    return _refusal_line(engine) or _say_failed(engine)


def _say_ask(engine, question=''):
    follow = engine.get('followups') or []
    return ('I can answer that, but not yet: ' + (follow[0] if follow else 'which item do you mean?'))


def _render(engine, question=''):
    kind = engine.get('kind')
    body = {'compare': _say_compare, 'invest': _say_invest, 'plan': _say_plan, '1v1': _say_1v1,
            'ecr': _say_ecr, 'explain': _say_explain, 'note': _say_note, 'ask': _say_ask,
            'thread': _say_thread}.get(kind,
                                                                                                _say_unknown)(engine,
                                                                                                  question)
    lead = _refusal_line(engine)
    if lead:
        body = f'{lead} {body}' if body and not body.startswith(lead) else (body or lead)
    tail = _tail(engine, body)
    return (body + '\n\n' + tail) if tail else body


def _upcase_first(text):
    """Capitalise the first letter and nothing else.

    `str.capitalize` lowercases the whole rest of the string, which is how a reply ends up saying
    "charge rocket" when the catalog spells it "Charge Rocket". The names are the part a player reads.
    """
    text = str(text or '')
    return text[:1].upper() + text[1:]


def _money(value):
    """A price exactly as the engine labelled it, no rounding, no re-derivation."""
    if not value or value.get('status') != 'ok':
        return None
    return advisor.amount_text(value.get('currency'), value.get('amount'))


MECH_STATS = (('hp', 'HP'), ('speed', 'speed'), ('power', 'power'), ('energy', 'energy'))


def _panel(item):
    """The community stat row for one item, as (label, value) pairs, skipping what is missing."""
    review = (item or {}).get('review') or {}
    out = []
    for key, label in MECH_STATS:
        value = review.get(key)
        if value in (None, '', 0, '0'):
            continue
        out.append((label, str(value)))
    return out


def _panel_line(item):
    """One item's figures on one line: "**Outlaw:** 213,100 HP · 24 speed · 931 power · 32 energy"."""
    rows = _panel(item)
    if not rows:
        return ''
    return f'**{item.get("name") or "It"}:** ' + ' · '.join(f'{value} {label}' if label != 'HP' else
                                                          f'{value} HP' for label, value in rows)


def _item(engine, name):
    """The catalog row the comparison resolved, so figures come from data rather than from a table cell."""
    return ((engine.get('resolved') or {}).get(_side(engine, name)) or {})


def _side(engine, name):
    """'A' or 'B' for a pick name, so a cell lookup never guesses."""
    picks = engine.get('picks') or []
    return 'A' if picks[:1] == [name] else 'B'


def _cell(engine, label):
    """Both sides of one head-to-head row, or (None, None)."""
    for row in engine.get('head_to_head') or []:
        if row.get('label') == label:
            left, right = row.get('A'), row.get('B')
            if left in (None, '', 'not in data'):
                left = None
            if right in (None, '', 'not in data'):
                right = None
            return left, right
    return None, None


def _number(text):
    """The digits inside a formatted figure, so two prices can be compared without printing anything."""
    try:
        return float(re.sub(r'[^0-9.]', '', str(text or '')))
    except ValueError:
        return None


def _first_clause(text):
    """The first real clause of a review paragraph, so one advantage does not become five lines.

    The catalog's strengths are written as "X; Y; Z" prose. Dropping the rest of the list keeps the
    line a reason rather than a second answer.
    """
    body = str(text or '').strip().strip('.')
    if not body:
        return ''
    parts = re.split(r'[;.]|, (?=[A-Z])', body)
    return parts[0].strip().strip('.')


# Field names the engine uses internally. They are the engine's vocabulary, not a player's, and
# "Oracle 16 still leads on dmgPerEnergy" is how one reached the channel.
PLAIN_NAMES = {'dmgPerEnergy': 'damage per energy', 'role_fit': 'role fit', 'meta_relevance': 'meta',
               'cost_efficiency': 'cost efficiency', 'optimalRange': 'range'}


def _player_words(text):
    """Swap internal field names for the words a player uses, and fix a/an before a vowel."""
    body = str(text or '')
    for find, replace in PLAIN_NAMES.items():
        body = body.replace(find, replace)
    body = re.sub(r'\b([Aa])\s+(?=(?:assault|energy|implant|oracle|attacker|epic)\b)', r'\1n ', body)
    return body


def _article(word):
    return 'an' if str(word or '')[:1].lower() in 'aeiou' else 'a'


def _grade_line(engine, left, right):
    """Which side leads on each stat the catalog holds for both, as one line rather than a score."""
    a, b = _item(engine, left), _item(engine, right)
    if not a or not b:
        return ''
    leads = {left: [], right: []}
    for label, _ in MECH_STATS:
        first, second = (a.get('review') or {}).get(label), (b.get('review') or {}).get(label)
        one, two = _number(str(first)), _number(str(second))
        if one is None or two is None or one == two:
            continue
        leads[left if one > two else right].append(label)
    lines = [f'{name} leads on {", ".join(stats)}' for name, stats in leads.items() if stats]
    if not lines:
        return ''
    return '**On the figures:** ' + '; '.join(lines) + '.'


def labels_for(key):
    """Which word a stat is compared by, so the loser's case names the right one."""
    return {'hp': 'survivability', 'speed': 'speed', 'power': 'damage output',
            'energy': 'energy budget'}.get(key, key)


def _loser_edge(engine, left, right, loser, counter):
    """The losing side's real advantage, in place of "the gap is small enough that ...".

    A comparison that says only "the other one is still reasonable" has told the player nothing.
    The range row, the category and the damage-per-energy rating are all in the head-to-head, so the
    loser gets named with the figure it actually wins on.
    """
    # The engine's own counter-argument can be a component gap - "Voidghost wins stats (0.37 against
    # 0.292)" - which is a number about our weights, not about the two items. Anything carrying a
    # two-decimal figure is treated as internal and replaced with a figure the player can use.
    generic = ('gap is small enough' in str(counter or '')
               or bool(re.search(r'\b\d\.\d{2,}\b', str(counter or ''))))
    spread = _cell(engine, 'optimal range')
    worth = _cell(engine, 'dmg per energy')
    sizes = _cell(engine, 'energy')
    if not generic:
        return re.sub(rf'^{re.escape(str(loser))} (?:still )?leads on', 'it still leads on', str(counter))
    other = right if loser is left else left
    cells = {left: (spread[0], worth[0], sizes[0]), right: (spread[1], worth[1], sizes[1])}
    mine, theirs = cells.get(loser), cells.get(other)
    for index, label in ((1, 'damage per energy'), (0, 'reach'), (2, 'energy efficiency')):
        if index == 2:
            continue
        a, b = (mine or (None, None, None))[index], (theirs or (None, None, None))[index]
        if not (a and b) or a == b:
            continue
        if index == 1 and (_number(a) or 0) > (_number(b) or 0):
            return f'it rates {a} damage per energy against {b}'
        if index == 0:
            a_hi = _number(str(a).split('\u2013')[-1])
            b_hi = _number(str(b).split('\u2013')[-1])
            if a_hi and b_hi and a_hi != b_hi:
                if a_hi > b_hi:
                    return f'it reaches {a} where {other} stops at {b}'
                return f'it is the tighter weapon: {a} against {b}, which suits close fights'
    gap = next((str(row) for row in (engine.get('data_gaps') or []) if str(row).startswith(loser)), '')
    if gap:
        return f'the catalog holds no stat row for it, so the tier list is most of what separates them'
    mine, theirs = _item(engine, loser), _item(engine, other)
    for key, label in MECH_STATS:
        one = _number(str((mine.get('review') or {}).get(key)))
        two = _number(str((theirs.get('review') or {}).get(key)))
        if one and two and one > two:
            raw_mine = str((mine.get('review') or {}).get(key))
            raw_theirs = str((theirs.get('review') or {}).get(key))
            return (f'it carries {raw_mine} {label} where {other} carries {raw_theirs}, which is the '
                    f'figure a {labels_for(key)} player would feel')
    if not (mine.get('tier') or (mine.get('review') or {}).get('hp')):
        return (f'the catalog holds no tier and no stat row for it, so it is unrated rather than rated '
                f'worse - {other} wins because it has figures to be rated on')
    return (f'nothing the catalog holds separates them, so {other} wins only on the ratio it is rated '
            f'on rather than on anything a player would feel')


def _review_of(engine, name):
    row = ((engine.get('resolved') or {}).get(_side(engine, name)) or {})
    return row.get('review') or {}


def _bullets(rows):
    """A bullet list, one row per line. Discord keeps the newlines; a single paragraph does not."""
    lines = []
    for row in rows:
        body = _upcase_first(_closed(str(row or '').strip())) if row else ''
        if body:
            lines.append('• ' + body)
    return '\n'.join(lines)


def _say_compare(engine, question=''):
    """The two-item answer: the pick, the figures that decide it, then what the data cannot settle.

    The weighted score is deliberately absent. "0.82 against 0.71" is a fact about our arithmetic,
    and a player reading it in the channel learns nothing about the two items they are choosing
    between - it was most of the reply a player called unreadable.
    """
    picks = [pick for pick in (engine.get('picks') or []) if pick]
    if len(picks) < 2 or engine.get('status') in ('ambiguous', 'refused', 'missing', 'unknown'):
        return _say_failed(engine)
    left, right = picks[0], picks[1]
    verdict = engine.get('verdict')
    winner = left if verdict == 'A' else right if verdict == 'B' else None
    loser = right if winner is left else left
    tiers = _cell(engine, 'tier')
    prices = _cell(engine, 'store price')
    worth = _cell(engine, 'dmg per energy')
    spread = _cell(engine, 'optimal range')
    categories = _cell(engine, 'category')
    sizes = _cell(engine, 'energy')
    category_of = {left: categories[0], right: categories[1]}

    def price_of(name):
        return prices[0] if name is left else prices[1]

    def worth_of(name):
        return worth[0] if name is left else worth[1]

    # The headline: the pick and the measurable reason it is the pick, in the catalog's own units.
    def price_of(name):
        return prices[0] if name is left else prices[1]

    def worth_of(name):
        return worth[0] if name is left else worth[1]

    def tier_phrase():
        if tiers[0] and tiers[0] == tiers[1]:
            return f'both tier {tiers[0]} in the community list'
        if tiers[0] and tiers[1]:
            return f'tier {tiers[0]} against tier {tiers[1]}'
        return ''

    def winner_price_leads(name):
        if not (prices[0] and prices[1]) or 'not priced' in prices:
            return False
        first, second = _number(prices[0]) or 0, _number(prices[1]) or 0
        if not (first and second) or first == second:
            return False
        cheaper_is_left = first < second
        return cheaper_is_left == (name is left)

    def price_phrase():
        if not (prices[0] and prices[1]) or 'not priced' in prices or prices[0] == prices[1]:
            return ''
        cheap, dear = (left, right) if (_number(prices[0]) or 0) < (_number(prices[1]) or 0) else (right, left)
        return f'{_short(price_of(cheap))} for {cheap} against {_short(price_of(dear))} for {dear}'

    def worth_phrase():
        if not (worth[0] and worth[1]) or worth[0] == worth[1]:
            return ''
        better, worse = (left, right) if (_number(worth[0]) or 0) > (_number(worth[1]) or 0) else (right, left)
        return f'{worth_of(better)} damage per energy for {better} against {worth_of(worse)} for {worse}'

    panels = [_panel_line(_item(engine, name)) for name in (left, right)]
    panels = [line for line in panels if line]
    # The stat panels are the catalog's own figures, but the reply guard only lets through numbers the
    # envelope already carries, so they are declared here rather than assumed to be there. Both spellings
    # go in, because the guard compares "213,100" and "213100" as the same figure.
    declared = [str(value) for name in (left, right) for _label, value in _panel(_item(engine, name))]
    engine['numbers'] = sorted(set(engine.get('numbers') or []) |
                               {form for value in declared for form in (value, value.replace(',', ''))})
    if winner:
        # What decided it, in a figure the player can check, or an honest statement that nothing did.
        reason = ''
        # The price only decides it when the cheaper side is the pick; otherwise it is the pick's
        # drawback and belongs in the loser's case, not in the reason line.
        if price_phrase() and winner_price_leads(winner):
            reason = price_phrase()
        elif worth_phrase():
            better_left = (_number(worth[0]) or 0) > (_number(worth[1]) or 0)
            if (winner is left) == better_left:
                reason = worth_phrase()
        elif spread[0] and spread[1] and spread[0] != spread[1]:
            reason = f'{spread[0]} reach against {spread[1]}'
        elif tiers[0] and tiers[1] and tiers[0] != tiers[1]:
            reason = f'tier {tiers[0]} against tier {tiers[1]}'
        if reason and tiers[0] and tiers[0] == tiers[1]:
            reason += f', both tier {tiers[0]} in the community list'
        elif not reason and tiers[0] and tiers[0] == tiers[1]:
            reason = f'both tier {tiers[0]} in the community list'
        head = f'**{winner}** over **{loser}**' + (': ' + reason if reason else '') + '.'
    elif prices[0] and prices[1] and prices[0] == prices[1] and tiers[0] == tiers[1]:
        head = (f'**{left}** and **{right}** are the same price and the same tier, so the catalog does '
                f'not split them.')
    else:
        head = f'**{left}** and **{right}** come out level on what the catalog holds.'
        if price_phrase():
            head = head[:-1] + f': {price_phrase()}.'

    # The bullets hold the separations the headline did not use, so no figure is printed twice.
    figures = []
    if worth_phrase() and worth_phrase() not in head:
        figures.append(worth_phrase())
    if spread[0] and spread[1] and spread[0] != spread[1]:
        figures.append(f'{left} works at {spread[0]}, {right} at {spread[1]}')
    if categories[0] and categories[1] and categories[0] != categories[1]:
        # A mech has a role (attacker, tank) and a weapon has a class (guided, missile). "Outlaw is an
        # attacker weapon" said the wrong thing; a mech's sentence needs the role word, a weapon's
        # sentence needs its class word to mean anything at all.
        def role_phrase(name, category):
            item = _item(engine, name) or {}
            if str(item.get('kind') or '') == 'mech':
                return f'{name} plays {_article(category)} {category} role'
            return f'{name} is {_article(category)} {category} {item.get("kind") or "weapon"}'
        figures.append(role_phrase(left, categories[0]) + ', ' + role_phrase(right, categories[1]))
    if sizes[0] and sizes[1] and sizes[0] != sizes[1] and not panels:
        # Already in the stat panels when both items have one; only worth a line when it is the sole
        # figure the catalog holds for them.
        figures.append(f'it takes {_num(sizes[0])} energy against {_num(sizes[1])}')
    figures = figures[:3]

    # Who leads on which figure. "Voidghost wins stats (0.37 against 0.292)" was our own weighting
    # printed as a decimal; "Outlaw leads on speed" is a fact about two mechs.
    grades = _grade_line(engine, left, right)

    blocks = [head]
    if panels:
        blocks.append('\n'.join(panels))
    if grades:
        blocks.append(grades)
    if figures:
        blocks.append(_bullets(figures))

    # What the player already told us. Their own pilot and their own pick outrank a generic argument.
    owned = engine.get('owned') or []
    if owned:
        mine = [name for name in (left, right) if any(name.casefold().startswith(item.casefold())
                                                       or item.casefold() in name.casefold() for item in owned)]
        if mine:
            blocks.append(f'**You already have {" and ".join(mine)}:** '
                          + _closed(f'the question is what the second one adds, not whether to buy '
                                    f'the one you own again'))
    role = (engine.get('role') or '').strip()
    if role and winner:
        kind = (category_of.get(winner) or '').strip()
        blocks.append(f'**For {role} play:** {winner} is the {_closed(kind) if kind else "one"} that fits, '
                      f'and it is the pick above on the figures as well.')

    strengths = _first_clause((_review_of(engine, winner).get('strengths') or '') if winner else '')
    counter = (engine.get('counter_argument') or '').strip()
    if strengths:
        blocks.append(f'**The case for {winner}:** {_closed(strengths)}')
    counter = _player_words(_loser_edge(engine, left, right, loser, counter))
    if counter:
        blocks.append(f'**The case for {loser}:** {_closed(counter)}')

    # One closing line for the open question, not three.
    gaps = [gap for gap in (engine.get('data_gaps') or []) if gap]
    change = [part.strip() for part in (engine.get('what_would_change') or '').split(';')
              if part.strip() and 'which role you play' not in part]
    closing = []
    if change:
        closing.append('whether ' + change[0] if not change[0].lower().startswith(('whether', 'your', 'the'))
                       else _closed(change[0]))
    if gaps:
        closing.append(_closed(gaps[0]))
    if closing:
        clauses = [clause.strip().rstrip('.;') for clause in closing if clause.strip()]
        blocks.append('**Not settled by the data:** ' + '; '.join(clauses) + '.')
    return '\n'.join(block for block in blocks if block)


def _say_invest(engine, question=''):
    """The store ranking: one item a line, with the price and the reason it is first.

    "Charge Rocket 6 is where I would put it (tier S in the community list): buy it at 600,000
    Credits. After that: ..." was one paragraph of clauses. What a player acts on is the order, the
    price and the one skip worth naming.
    """
    ranked = engine.get('ranked') or []
    if engine.get('verdict') == 'ASK' or not ranked:
        ask = [line for line in (engine.get('followups') or []) if line][:2]
        if ask:
            return 'Two things before I rank anything: ' + ' '.join(ask) \
                   + ' With those I can order the store list against your build.'
        return _say_failed(engine)
    top = ranked[0]
    budget = engine.get('budget') or {}
    money = ', '.join(advisor.amount_text(key, value) for key, value in budget.items() if value)
    price = _money(top.get('buy'))
    verb = 'Buy' if top.get('action') == 'buy' else 'Rank up'
    tier = f' - tier {top["tier"]} in the community list' if top.get('tier') else ''
    money = f'With {money}: ' if money else ''
    lines = [f'**{money}{verb} {top["name"]}**' + (f' at {price}' if price else '') + tier + '.']
    gain = top.get('power_gain')
    if gain:
        lines.append(f'**Why this one:** the confirmed rows put it {_num(round(gain))}% stronger on the '
                     f'ladder, which is the largest gain on the list.')
    also = [row for row in ranked[1:3] if row.get('name')]
    for row in also:
        row_price = _money(row.get('buy'))
        lines.append(f'• {row["name"]}' + (f' - {row_price}' if row_price else ' - no store price'))
    skip = engine.get('do_not_invest') or []
    if skip:
        lines.append(f'**Leave alone:** {skip[0]["name"]} - {_closed(skip[0]["reason"])}')
    stretch = engine.get('stretch') or []
    if stretch:
        cost = _money(stretch[0].get('buy'))
        lines.append(f'**Better but out of reach:** {stretch[0]["name"]}' + (f' at {cost}' if cost else '')
                     + ', which your budget does not cover.')
    guide = (top.get('guide') or {})
    if guide.get('verdict'):
        lines.append(f'**The guide\'s own line:** '
                     f'{advisor.ADVICE_PHRASE.get(guide["verdict"], guide["verdict"])}.')
    return '\n'.join(lines)


def _say_plan(engine, question=''):
    """The budget answer: what to buy, in order, one line each.

    "buy X (66,550 Credits); buy Y (95,000 Credits); buy Z" is one sentence with five purchases
    inside it. A numbered list is the same information in the shape a player can act on.
    """
    plan = engine.get('plan') or []
    if not plan:
        return _say_failed(engine)
    steps = []
    for row in plan:
        cost = ', '.join(advisor.amount_text(key, value) for key, value in (row.get('cost') or {}).items()
                         if value)
        steps.append(f'{row["action"]} {row["name"]}' + (f' - {cost}' if cost else ''))
    leftover = {key: value for key, value in (engine.get('leftover') or {}).items() if value}
    spare = (', '.join(advisor.amount_text(key, value) for key, value in leftover.items()) if leftover
             else 'nothing')
    head = (f'**Spend it in this order** ({spare} left over afterwards):')
    # Bullets, not "1. 2. 3.": a list marker is a digit the engine never produced, and the reply guard
    # refuses any figure that is not in the envelope. Order is carried by the order of the lines.
    lines = [head] + [f'• {step}' for step in steps]
    binding = engine.get('binding')
    if binding and binding != 'none':
        lines.append(f'**What runs out first:** {binding}.')
    milestone = engine.get('next_milestone') or {}
    if milestone:
        need = ', '.join(advisor.amount_text(key, value) for key, value in
                         (milestone.get('needs') or {}).items() if value)
        if need:
            lines.append(f'**Next step up:** {milestone.get("name")} needs another {need}.')
    skip = engine.get('do_not_buy') or []
    if skip:
        lines.append(f'**Not worth it here:** {skip[0]["name"]} - {_lower_first(skip[0]["reason"])}.')
    return '\n'.join(lines)


def _lower_first(reason):
    """The engine's recorded reason as a clause that reads after a dash."""
    body = str(reason or '').strip().rstrip('.')
    return (body[0].lower() + body[1:]) if body else 'no reason recorded'


def _say_1v1(engine, question=''):
    if not engine.get('win_chance'):
        return _say_failed(engine)
    loads = engine.get('loadouts') or {}
    left = (loads.get('A') or {}).get('mech') or 'A'
    right = (loads.get('B') or {}).get('mech') or 'B'
    chance = engine.get('win_chance')
    ttk = engine.get('ttk') or {}
    band = engine.get('engagement') or 'mid'
    distance = engine.get('band_distance_m')
    share = engine.get('win_pct')
    if engine.get('ttk'):
        head = (f'{left} wins it on paper at {_num(distance)} m: {_num(ttk.get("A"))} s to kill, '
                f'{_short(str(ttk.get("B")))} s the other way, so about {_num(share)}% of fights.')
    else:
        head = (f'{left} comes out ahead on listed power and HP, about {_num(share)}%, but neither side named a '
                'weapon, so there is no time to kill to work with.')
    basis = ((engine.get('damage_basis') or {}).get('A') or {}).get('basis') or []
    if basis:
        head += f' That is {basis[0]}, not a measured rate.'
    hp = (engine.get('hp_basis') or {}).get('A') or {}
    if hp.get('shown'):
        head += f' {hp["shown"]} comes from the screenshot rows, which are user-confirmed.'
    swing = engine.get('sensitivity') or {}
    shares = engine.get('win_pct_by_band') or {}
    other = {key: value for key, value in shares.items() if key != band}
    if other:
        key, value = sorted(other.items(), key=lambda row: -abs(row[1] - (share or 50)))[0]
        head += (f' Move it to {key} range and the model puts it at {_num(value)}%, so distance is what decides '
                 'this.' if (value > 50) != ((share or 50) > 50) else f' It holds at {key} range too ({_num(value)}%).')
    head += ' None of this is the game engine: positioning and aim are not in any file I have.'
    return head


def _say_ecr(engine, question=''):
    picks = [pick for pick in (engine.get('picks') or []) if pick.get('announced')]
    if not picks:
        return _say_failed(engine)
    freshness = engine.get('freshness')
    if freshness == 'finished':
        head = 'There is no ECR running right now, so this is the last one for context, not an offer.'
    else:
        verdict = engine.get('verdict')
        head = {'skip': 'The guide says skip it.', 'consider': 'The guide says it is worth a look.'}.get(
            verdict, f'The guide\'s own line: {verdict}.')
    lines = [head]
    for pick in picks[:3]:
        bits = [f'{pick["announced"]}']
        if pick.get('resolved') and pick['resolved'] != pick['announced']:
            bits[0] = pick['resolved']
        if pick.get('tier'):
            bits.append(f'tier {pick["tier"]}')
        store, crate = _money(pick.get('store_price')), None
        ecr = pick.get('ecr_value') or {}
        if (ecr.get('ecr') or {}).get('amount'):
            crate = advisor.amount_text(ecr['ecr'].get('currency'), ecr['ecr'].get('amount'))
        if store and crate:
            bits.append(f'{crate} in the crate against {store} in the Gear Hub')
        elif store:
            bits.append(f'{store} in the Gear Hub')
        elif pick.get('resolved') is None:
            bits.append('not in the catalog, so I cannot price it')
        phrase = advisor.ADVICE_PHRASE.get(pick.get('advice'))
        if phrase:
            bits.append(f'the guide says {phrase}')
        lines.append(', '.join(bits) + '.')
        note = (pick.get('note') or '').strip()
        if note:
            lines.append('Its words: ' + _quote(note))
    if len(picks) > 3:
        lines.append(f'{len(picks) - 3} more item(s) in the post, same reading if you want one named.')
    lines.append('The buy or skip line is the guide\'s, priced against the store list - I did not decide it.')
    return ' '.join(lines)


def _say_explain(engine, question=''):
    """One item, described: what it is, the figures, what it is good at, and what it costs."""
    if engine.get('status') in ('ambiguous', 'refused', 'missing', 'unknown'):
        return _say_failed(engine)
    name = engine.get('item') or 'It'
    review = engine.get('review') or {}
    sheet = engine.get('stats') or []
    bits = []
    if engine.get('tier'):
        bits.append(f'tier {engine["tier"]} in the community list')
    if review.get('category') or review.get('role'):
        kind_word = str(review.get('category') or review.get('role'))
        bits.append(f'{_article(kind_word)} {kind_word}')
    # Only mention energy here when it is not already in the stat line, or the answer says 32 twice.
    if engine.get('energy') and not any(row.get('label') == 'Energy' for row in sheet):
        bits.append(f'energy {int(engine["energy"])}')
    first = f'{name}: ' + ', '.join(bits) if bits else name
    # The figures, not a score. "Scores 0.405 on the balanced weights" is a number about our own
    # weights; HP, speed and power are numbers about the mech. When the sheet is missing, say so
    # plainly instead of substituting the score, and drop the standalone "no community tier" line
    # because the verdict already carries the tier.
    was_rank = (engine.get('stats_rank') or {})
    asked = was_rank.get('asked')
    if sheet:
        # The figures carry no rank of their own: the review row is a single value per stat. When
        # the player named a rank, say which rank these are from and whether the asked-for rank is
        # among the ranks the catalog holds per-rank cells for, rather than letting rank-1 numbers
        # sit under a question about rank 5.
        where = 'the community values, which are not split by rank'
        if asked:
            if was_rank.get('from_confirmed_cells'):
                have = ', '.join(str(rank) for rank in was_rank.get('available') or [])
                where = (f'rank {asked} cells' if asked in (was_rank.get('available') or [])
                         else f'the community values; per-rank cells exist only for rank {have}')
            else:
                where = (f'the community values; there is no per-rank stat row, so these are the only '
                         f'figures on record and they carry no rank. You asked about rank {asked}')
        blocks = [f'{first}. Stats from {where}: ' + ' · '.join(
            f'{row["label"]} {row["value"]}{row.get("unit") or ""}'.strip() for row in sheet) + '.']
    else:
        line = f'{first}. I have no HP, speed or power figures for it in the catalog.'
        if asked:
            line += f' Not for rank {asked} or for any other rank.'
        blocks = [line]
    verdict = '' if sheet else (engine.get('verdict') or '').strip()
    if verdict:
        verdict = verdict.rstrip('.') + '.'
        blocks.append(verdict[0].upper() + verdict[1:])
    strong = [part.strip() for part in (review.get('strengths') or '').split(';') if part.strip()]
    weak = [part.strip() for part in (review.get('weaknesses') or '').split(';') if part.strip()]
    # The review text is a list of clauses separated by semicolons; printed inline it turns one answer
    # into a paragraph. One line for what it is good at, one for where it loses.
    if strong:
        blocks.append('**Good at:**\n' + _bullets(strong[:4]))
    if weak:
        blocks.append('**Where it loses:**\n' + _bullets(weak[:4]))
    price = _money((engine.get('price') or {}))
    ladder = engine.get('cost_to_max') or {}
    money = []
    if price:
        money.append(f'Shelf price {price}')
    if ladder.get('status') == 'ok':
        # Naming the starting rank is the difference between a number the player can use and a number
        # that happens to match their account: rank 1 to max is not what a rank 4 mech costs to finish.
        money.append(f'the ladder from rank {ladder.get("from_rank") or 1} to max is '
                     + ', '.join(advisor.amount_text(key, ladder[key])
                                 for key in ('credits', 'acoins', 'blueprints') if ladder.get(key)))
    if money:
        blocks.append(_upcase_first(money[0] if len(money) == 1 else money[0] + ', and ' + money[1]) + '.')
    value = engine.get('value') or {}
    if value.get('discount_pct'):
        blocks.append(f'It is {_num(value["discount_pct"])}% cheaper out of a crate than on the shelf.')
    guide = engine.get('guide') or {}
    if guide.get('verdict'):
        phrase = ADVICE_PHRASE.get(guide['verdict'], guide['verdict'])
        mention = (guide.get('mentions') or [{}])[0]
        line = f'The weekly guide\'s own line on it: {phrase}'
        if mention.get('note'):
            line += ', in its words ' + _quote(mention['note'])
        blocks.append(line + '.')
    for note in (engine.get('notes') or [])[:1]:
        if note and 'with the rest of the sentence ignored' not in note:
            # The tier verdict already says there is no community tier; printing the flag as well
            # made the same absence the first and last line of the answer.
            if 'no community tier' in str(note).casefold() and 'tier' in ' '.join(blocks).casefold():
                continue
            blocks.append(_upcase_first(_closed(note)))
    rival = engine.get('alternative') or {}
    if rival.get('name'):
        blocks.append(f'Better for the same money: {rival["name"]}'
                      + (f' (tier {rival["tier"]}, {rival["cost"]})' if rival.get('tier') else '') + '.')
    gaps = engine.get('data_gaps') or []
    if gaps:
        blocks.append('What is missing: ' + _closed(gaps[0]))
    # "What would change this: the mech you pilot and whether you fight at its range band" reads as
    # advice and carries nothing: it was on every single item, including ones with no verdict, and
    # it is the same sentence whether or not the player told us anything. Dropped rather than
    # reworded, because there is no version of it that says something about this mech.
    return '\n'.join(block for block in blocks if block)


ADVICE_PHRASE = {'skip': 'skip it', 'worth_it': 'worth the spend', 'depends': 'only if it fits your build'}


def _say_unknown(engine, question=''):
    return _say_failed(engine) if engine else ('Ask me about an item, a crate or two things you are choosing '
                                               'between.')


def _say_failed(engine):
    """Ambiguous, refused or empty: say which, offer the candidates, and ask one question."""
    status = engine.get('status') or 'no_data'
    notes = [note for note in (engine.get('notes') or []) if note]
    candidates = (engine.get('candidates') or [])[:4]
    if status == 'refused':
        head = (notes[0] if notes else 'That item is not in the catalog.')
    elif status == 'ambiguous':
        head = 'I could not tell which one you meant.'
    elif status == 'missing':
        head = 'Name the item and I can price it.'
    else:
        head = (engine.get('data_gaps') or ['The saved announcements do not cover that yet.'])[0]
    if candidates:
        head += ' The catalog has ' + ', '.join(str(value) for value in candidates) + '.'
    ask = (engine.get('followups') or [])[:1]
    if ask:
        head += ' ' + ask[0]
    return head


def _tail(engine, body=''):
    """Provenance and caveats, in the same voice as the answer rather than a footer of tags."""
    bits = []
    caveats = engine.get('caveats') or []
    for caveat in caveats[:1]:
        line = str(caveat)
        if 'community catalog' in line:
            # The answer already says the tier is the community list. Repeating the long form of the
            # same provenance under every comparison is what left a paragraph of disclaimers at the
            # end of a two-item answer - and "damage-per-energy" has nothing to do with two mechs.
            has_damage = any('damage per energy' in str(row.get('label'))
                             for row in engine.get('head_to_head') or [])
            line = ('Tier and damage-per-energy labels come from the community catalog, not the '
                    'developers.' if has_damage else
                    'Tier and stat labels come from the community catalog, not the developers.')
        bits.append(_upcase_first(_closed(line)))
    answered = engine.get('verdict') not in ('ASK', None) and engine.get('status') != 'ask'
    # "Ordered on the budget weights" names a weighting set the player never chose. Say the ordering
    # rule in words instead: what each purchase buys for the currency they named.
    assumptions = [re.sub(r'^ordered on the .* weights$', 'ordered by what each purchase buys for the '
                          'currency you named', str(row))
                   for row in (engine.get('assumptions') or []) if 'assuming' not in row
                   and not (answered and re.search(r'^(?:role not given|no budget given|you did not name|'
                                                   r'no budget, no role and no item named)', str(row)))][:1]
    # Tiers and damage-per-energy labels are the community's numbers in every comparison, so the
    # compact line above already says so; the engine's longer wording is a paragraph with no fact in it.
    for line in assumptions:
        # the engine writes these as complete clauses, so they are capitalised and left alone
        bits.append(_upcase_first(_closed(line)))
    asked = str(body or '')
    follow = [row for row in (engine.get('followups') or [])
              if row and row.strip() not in asked]
    if engine.get('provisional') or engine.get('verdict') == 'ASK':
        follow = follow[:1]
    if follow:
        bits.append(_upcase_first(follow[0]))
    return ' '.join(bits)


# ---------------------------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------------------------

def _refusal_line(engine):
    """The sentence that tells a player the thing they named does not exist, and what does."""
    issue = engine.get('refusal') or {}
    line = str(issue.get('note') or '').strip()
    if line and not line.endswith('.'):
        line += '.'
    return line


def _split_versus(text):
    parts = re.split(r'\b(?:vs\.?|versus|or|against|compared to)\b', str(text or ''), maxsplit=1, flags=re.I)
    left = re.split(r'[?]', parts[0])[0]
    left = re.sub(r'^(?:who wins|which (?:is|wins|should)|should i|is|are|do i|compare|tell me)\b', '',
                  left, flags=re.I).strip(' ,:.')
    right = (parts[1] if len(parts) > 1 else '').strip(' ?.,:!')
    return left or str(text or ''), right or str(text or '')


def _band(text):
    body = str(text or '').casefold()
    if re.search(r'\bclose[- ]?(?:range|quarters?)\b|\bcq\b', body):
        return 'close'
    if re.search(r'\blong[- ]?range\b|\bsniping\b', body):
        return 'long'
    return 'mid'


def _short(value):
    return re.sub(r'\s*A-?coins', ' A-coins', str(value or '')).strip()


def _num(value):
    if value is None:
        return 'nothing'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f'{value:g}' if isinstance(value, (int, float)) else str(value)


def _closed(value):
    """A clause from the data, ending in the punctuation it actually deserves.

    Sentence-joining prose from the pack with a bare `+ '.'` turns a question in the source into "?." in
    the reply, which reads like a machine pasted it.
    """
    text = _sentence(value)
    return text + ('.' if text and not text.endswith(('.', '!', '?')) else '')


def _sentence(value):
    """One clause from the data's own prose, trimmed to a sentence so it does not read like a transcript."""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    text = re.split(r'(?<=[.!?])\s+', text)[0].strip(' .;,-')
    return text[:220]


def _quote(value, limit=150):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    clipped = text[:limit]
    if len(clipped) < len(text):
        clipped = clipped.rsplit(' ', 1)[0]
    return '"' + clipped.strip(' .;:,') + '"'


def _digits_ok(text, engine):
    allowed = set((engine or {}).get('numbers') or [])
    for raw in re.findall(r'\d[\d,]*(?:\.\d+)?%?', str(text or '')):
        if raw in allowed:
            continue
        plain = raw.replace(',', '').rstrip('.').rstrip('%')
        if plain in allowed or raw.replace(',', '') in allowed or f'{plain}%' in allowed:
            continue
        return False
    return True


def _guard(reply, engine):
    """Refuse to ship a number the engine did not produce."""
    if _digits_ok(reply, engine):
        return reply, 'pass'
    safe = _say_failed({**engine, 'status': engine.get('status') or 'no_data'})
    return safe + ' (the numbers in that draft did not all come from the data, so they are left out.)', 'blocked'


def _clip(text, limit=DISCORD_LIMIT):
    text = str(text or '')
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit('.', 1)[0]
    return (cut or text[:limit - 1]) + '.'
