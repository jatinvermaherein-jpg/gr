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


def answer(text, facts=None, data=None, limit=MAX_ANSWER):
    """One prose question in, one channel-ready answer out, with the envelope that justifies it.

    Returns a dict with `reply`, `capability`, `envelope` and a `style` note recording what the
    humanizing pass changed, so a reviewer can audit the wording without guessing.
    """
    data = data or advisor.load()
    body = str(text or '').strip()
    if not body:
        return {'capability': 'empty', 'reply': 'Ask me something about a buy, a crate or a build.',
                'envelope': {}, 'status': 'empty'}
    capability = route(body, data)
    if capability in ('ask_loadouts', 'clarify_item'):
        engine = {'kind': 'ask', 'verdict': 'ASK', 'numbers': set(), 'caveats': [], 'assumptions': [],
                  'data_gaps': [], 'status': 'ask', 'missing_slots': ['item'],
                  'followups': (['Which mechs on each side? Name them and I can model the fight, and say '
                                 'whether you expect to meet close or long.'] if capability == 'ask_loadouts'
                                else ['What is "it"? Name the item and I can price it.'])}
        return _package(engine, capability, body, data, limit)
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
            return _package(engine, capability, body, data, limit)
        engine = dict(engine, kind=(engine.get('kind') or 'explain'),
                      refusal=issues[0], numbers=set(engine.get('numbers') or set()))
        for issue in issues:
            engine['numbers'] |= {str(value) for value in
                                  (advisor.normalise(issue['note']).split() + issue['candidates'])}
    if capability == 'explain' and SPEND_WORDS.search(body) and not ctx_role(body, data):
        # Q20: a one-item buy question is answered, then asked the one thing that would reorder it
        engine = dict(engine, followups=list(engine.get('followups') or []) + [advisor.FOLLOWUP_TEXT['role']],
                      provisional=True)
    return _package(engine, capability, body, data, limit)


def _package(engine, capability, body, data, limit=MAX_ANSWER):
    """Render, guard, humanize, clip. One place so every capability gets the same treatment."""
    reply = _render(engine, body)
    reply, guard = _guard(reply, engine)
    styled, tells = polish(reply)
    if _digits_ok(styled, engine):
        reply = styled
    return {'capability': capability, 'status': engine.get('status') or 'ok', 'envelope': engine,
            'reply': _clip(reply, limit), 'guard': guard, 'style': {'tells': tells, 'audit': audit(styled)}}


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
            'ecr': _say_ecr, 'explain': _say_explain, 'note': _say_note, 'ask': _say_ask}.get(kind,
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


def _say_compare(engine, question=''):
    picks = engine.get('picks') or []
    scores = engine.get('scores') or {}
    verdict = engine.get('verdict')
    if engine.get('status') in ('ambiguous', 'refused', 'missing', 'unknown'):
        return _say_failed(engine)
    left, right = (picks + [None, None])[:2]
    a, b = scores.get('A') or {}, scores.get('B') or {}
    winner = left if verdict == 'A' else right if verdict == 'B' else None
    loser = right if winner is left else left
    head = []
    tiers = _row(engine, 'tier')
    if tiers and tiers[0] == tiers[1]:
        head.append(f'both {tiers[0]} in the community list, so the label is not what decides this')
    elif tiers:
        head.append(f'the community list has {left} at {tiers[0]} and {right} at {tiers[1]}')
    if verdict == 'TIE':
        opener = f'{left} and {right} land in the same place on these weights'
    else:
        opener = f'{winner} is the pick'
        cost = _row(engine, 'store price')
        if cost and len(set(cost)) > 1:
            opener += f' and it is the one that costs {_short(cost[0] if winner is left else cost[1])}'
    parts = [opener + ((' - ' + '; '.join(head) + '.') if head else '.')]
    detail = f'On the {engine.get("profile")} weights {left or "A"} is {_num(a.get("total"))} and ' \
             f'{right or "B"} is {_num(b.get("total"))}'
    decisive = engine.get('decisive_factors') or []
    if decisive:
        top = decisive[0]
        name = top.get('component') or top.get('label')
        words = str(top.get('label') or name).replace('_', ' ')
        detail += f', and {words} is where the gap is ({_num((a.get("parts") or {}).get(name))} against ' \
                  f'{_num((b.get("parts") or {}).get(name))})'
        if top.get('role'):
            detail += f' for a {top["role"]} build'
    parts.append(detail + '.')
    why = (engine.get('why') or '').strip()
    if why:
        parts.append(_upcase_first(_closed(why)))
    counter = (engine.get('counter_argument') or '').strip()
    if counter:
        parts.append('The other side: ' + _closed(counter))
    for note in [row for row in (engine.get('notes') or []) if 'energy' in row or 'not in the catalog' in row][:2]:
        parts.append(_upcase_first(_closed(note)))
    gaps = engine.get('data_gaps') or []
    if gaps:
        parts.append('What I could not check: ' + _closed(gaps[0]))
    change = engine.get('what_would_change')
    if change:
        parts.append(f'What would change it: {change}.')
    if loser and verdict != 'TIE':
        parts.append(f'{loser} is not a bad item, it is the weaker fit here.')
    return ' '.join(parts)


def _say_invest(engine, question=''):
    ranked = engine.get('ranked') or []
    if engine.get('verdict') == 'ASK' or not ranked:
        ask = [line for line in (engine.get('followups') or []) if line][:2]
        if ask:
            return ('Two things before I rank anything: ' + ' '.join(ask)
                    + ' With those I can order the store list against your build.')
        return _say_failed(engine)
    top = ranked[0]
    lines = []
    budget = engine.get('budget') or {}
    if budget:
        money = ', '.join(advisor.amount_text(key, value) for key, value in budget.items() if value)
        opener = f'With {money}, {top["name"]}'
    else:
        opener = f'{top["name"]}'
    price = _money(top.get('buy'))
    verb = 'buy it' if top.get('action') == 'buy' else 'rank it'
    tier = f' (tier {top["tier"]} in the community list)' if top.get('tier') else ''
    lines.append(f'{opener} is where I would put it{tier}: {verb} at {price or "no store price"}, '
                 f'scoring {_num(top.get("score"))} on the {engine.get("profile")} weights.')
    if top.get('power_gain') is not None:
        lines.append(f'The confirmed rows say it gains {_num(top["power_gain"])}% over the ladder '
                     f'({top.get("power_gain_basis")}).')
    also = [row for row in ranked[1:3]]
    if also:
        lines.append('After that: ' + '; '.join(f'{row["name"]} at {_money(row.get("buy")) or "the same price"}'
                                                for row in also) + '.')
    skip = engine.get('do_not_invest') or []
    if skip:
        worst = skip[0]
        lines.append(f'Leave {worst["name"]} alone - {worst["reason"]}.')
    stretch = engine.get('stretch') or []
    if stretch:
        lines.append(f'{stretch[0]["name"]} is better on the numbers and costs {_money(stretch[0].get("buy"))}, '
                     'which your budget does not cover.')
    guide = (top.get('guide') or {})
    if guide.get('verdict'):
        lines.append(f'The announcement guide\'s own line on it: {advisor.ADVICE_PHRASE.get(guide["verdict"], guide["verdict"])}.')
    return ' '.join(lines)


def _say_plan(engine, question=''):
    plan = engine.get('plan') or []
    if not plan:
        return _say_failed(engine)
    bits = []
    for row in plan:
        cost = ', '.join(advisor.amount_text(key, value) for key, value in (row.get('cost') or {}).items() if value)
        bits.append(f'{row["action"]} {row["name"]} ({cost})' if cost else f'{row["action"]} {row["name"]}')
    head = 'Spend it in this order: ' + '; '.join(bits) + '.'
    leftover = {key: value for key, value in (engine.get('leftover') or {}).items() if value}
    if leftover:
        head += ' That leaves ' + ', '.join(advisor.amount_text(key, value) for key, value in leftover.items()) + '.'
    else:
        head += ' That uses the lot.'
    binding = engine.get('binding')
    if binding and binding != 'none':
        head += f' {_upcase_first(binding)} is what you run out of first.'
    next_one = engine.get('next_milestone')
    if next_one:
        need = ', '.join(advisor.amount_text(key, value) for key, value in (next_one.get('needs') or {}).items()
                         if value)
        head += f' The next thing you could reach is {next_one["name"]} ({next_one.get("action")}), needing {need}.'
    skip = engine.get('do_not_buy') or []
    if skip:
        head += f' Not worth it at any price here: {skip[0]["name"]} - {skip[0]["reason"]}.'
    return head


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
    if engine.get('status') in ('ambiguous', 'refused', 'missing', 'unknown'):
        return _say_failed(engine)
    name = engine.get('item') or 'It'
    review = engine.get('review') or {}
    bits = []
    if engine.get('tier'):
        bits.append(f'tier {engine["tier"]} in the community list')
    if review.get('category') or review.get('role'):
        kind_word = str(review.get('category') or review.get('role'))
        article = 'an' if kind_word[:1].lower() in 'aeiou' else 'a'
        bits.append(f'{article} {kind_word}')
    if engine.get('energy'):
        bits.append(f'energy {int(engine["energy"])}')
    opener = f'{name}: ' + ', '.join(bits) if bits else name
    verdict = (engine.get('verdict') or '').strip()
    if verdict:
        verdict = verdict.rstrip('.') + '.'
        verdict = verdict[0].upper() + verdict[1:]
    text = opener + ('. ' + verdict if verdict else '.')
    strong = (review.get('strengths') or '').strip()
    weak = (review.get('weaknesses') or '').strip()
    if strong:
        text += ' ' + _closed(strong)
    if weak:
        text += ' Where it loses: ' + _closed(weak)
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
        # Capitalise the lead: an item with no shelf price opens on the lower-case "the ladder from rank 1
        # to max is ...", which reads like a paste rather than a sentence.
        text += ' ' + _upcase_first(money[0] if len(money) == 1 else money[0] + ', and ' + money[1]) + '.'
    value = engine.get('value') or {}
    if value.get('discount_pct'):
        text += f' It is {_num(value["discount_pct"])}% cheaper out of a crate than on the shelf.'
    guide = engine.get('guide') or {}
    if guide.get('verdict'):
        phrase = ADVICE_PHRASE.get(guide['verdict'], guide['verdict'])
        mention = (guide.get('mentions') or [{}])[0]
        text += f' The weekly guide\'s own line on it: {phrase}'
        if mention.get('note'):
            text += ', in its words ' + _quote(mention['note'])
        text += '.'
    for note in (engine.get('notes') or [])[:1]:
        if note and 'with the rest of the sentence ignored' not in note:
            text += ' ' + _upcase_first(_closed(note))
    rival = engine.get('alternative') or {}
    if rival.get('name'):
        text += f' Better for the same money: {rival["name"]}' \
                + (f' (tier {rival["tier"]}, {rival["cost"]})' if rival.get('tier') else '') + '.'
    gaps = engine.get('data_gaps') or []
    if gaps:
        text += ' What is missing: ' + _closed(gaps[0])
    change = engine.get('what_would_change')
    if change:
        text += f' What would change this: {change}.'
    return text


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
    if caveats:
        bits.append(_upcase_first(_closed(caveats[0])))
    assumptions = [row for row in (engine.get('assumptions') or []) if 'assuming' not in row][:1]
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


def _row(engine, label):
    for row in engine.get('head_to_head') or []:
        if row.get('label') == label:
            return [row.get('A'), row.get('B')]
    return None


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
