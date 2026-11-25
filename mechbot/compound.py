"""Answer a message that asks two or more separate questions instead of one.

The ladder in ``MechBot.on_message`` is first-match-wins: the first handler that
fires returns, and everything below it never runs. That is correct for a single
question and wrong for a compound one, and the failure has three shapes:

* "what ecr is live and what are the promo codes" matched ``wants_promo_codes``
  only, gave the promo answer, and silently dropped the ECR half.
* "promo codes and the ecr this week" matched neither gate — ``wants_promo_codes``
  needs a question marker and the trailing words defeated it — so it fell all the
  way through to "I don't have reviewed information on that".
* "what is the ecr and what does the battle pass cost" matched only the ECR gate.

This module does the minimum needed to stop that: split a message on an explicit
conjunction, run each clause through the same gates, and if two or more clauses
resolve to different topics, answer each one under its own heading.

What it deliberately does not do
--------------------------------
* It never splits on "and" alone when the two sides belong to one question. "storm
  racks and charge rockets", "oracle and redox", "compare A and B" are single
  questions about two items, which the advisor already handles correctly; splitting
  them would break a feature that works.
* It never routes a clause to a handler with a side effect. Reminders, notes,
  timezone and loop commands are excluded from both the split and the gate set, so
  a compound message can never create two reminders.
* It never invents a topic. Only the five read-only announcement gates plus the
  catalogue lookups are eligible, and a clause that matches nothing is reported as
  unanswered rather than guessed at.
"""

from __future__ import annotations

import re

# A clause boundary needs an explicit conjunction joining two question-shaped halves.
# "and also", "and what", "plus", "also", "and the" are the shapes that actually occur.
# Splits are lookaheads, not consumes: the clause must keep its own question word or
# determiner, or the gates below cannot recognise it. Consuming "any" out of
# "and any new promo?" left "new promo?", which no gate matches.
_BOUNDARY = re.compile(
    r'\s*(?:'
    r';\s*'
    r'|\?\s*(?:and|also|plus)\s+'
    r'|\s+(?:and\s+also|also|plus)\s+'
    r'|\s+and\s+(?=(?:what|when|which|who|where|how|why|is|are|was|were|do|does|did|can|'
    r'could|should|will|would|the|any|there|new|next|current|tell|show|list|give|'
    r'whats|whens|hows|wheres)\b)'
    r')', re.I)

# Side effects stay out of compound handling entirely: one message must never perform two.
_SIDE_EFFECT = re.compile(
    r'\bremind\s+me\b|\bremind(?:er)?\b|\bremember\s+that\b|\bdelete\s+note\b|'
    r'\bcancel\s+reminder\b|\bset\s+(?:my\s+)?timezone\b|\bstop\s+(?:the\s+)?loops?\b|'
    r'^\s*note\s', re.I)

MAX_CLAUSES = 4


def split(question):
    """Split a message into its separate questions, or return [] when it is really one.

    Returns the ordered clauses only when at least two survive shaping; a single clause
    means the caller should use the ordinary ladder.
    """
    text = str(question or '').strip()
    if not text:
        return []
    # Drop the interrogative a split can leave stranded at the start of a clause.
    # "what ecr is live and what are the promo codes" splits to "are the promo codes",
    # and the promo gate needs a question word or "?" to recognise it as a request.
    parts = [re.sub(r'^(?:and|also|plus)\s+', '', part.strip(' .,;'), flags=re.I) for part in _BOUNDARY.split(text)]
    parts = [re.sub(r'^(?:is|are|was|were|does|do|did|can|could|should|will|would)\s+(?:there\s+)?',
                    'what ', part, count=1, flags=re.I) if not part.endswith('?') else part
             for part in parts]
    parts = [part.strip() for part in parts if part.strip()]
    if len(parts) < 2:
        return []
    # No question-shape filter. "promo codes" is a complete request with no verb in it,
    # and requiring one silently rejected the very clause that needed splitting. Whether a
    # clause means anything is decided by the topic detectors, not by its grammar.
    shaped = [part for part in parts if len(part) >= 3][:MAX_CLAUSES]
    return shaped if len(shaped) >= 2 else []


def is_compound(question):
    """True when the message asks more than one separable question."""
    if _SIDE_EFFECT.search(str(question or '')):
        return False
    return bool(split(question))


# Topic detectors, deliberately broader than the gates in announcements.py and
# source_summaries.py. Those gates answer "is this message THE ECR question?" and are
# tuned to reject near-misses. For splitting I only need "does this clause concern the
# ECR?", because the actual answer is still produced by the real handler, which receives
# the whole original message and applies its own strict gate to it.
TOPIC_PATTERNS = (
    ('ecr', re.compile(r'\bcrate\s*rush(?:es)?\b|\becrs?\b', re.I)),
    ('promo', re.compile(r'\bpromo(?:\s*-?\s*codes?)?\b|\bredeem\b|\bcode(s)?\b', re.I)),
    ('upcoming', re.compile(r'\bupcoming\b|\bevents?\b|\bwhat\s+is\s+coming\b|\bcoming\s+up\b|'
                            r'\bthis\s+week\b|\bbattle\s*pass\b|\bseason\b', re.I)),
    ('patch', re.compile(r'\bpatch(?:\s*notes?)?\b|\bbug(?:s|\s*reports?)?\b|\brelease\s*notes\b|'
                         r'\brebalance\b|\bnerf(?:s|ed)?\b|\bbuff(?:s|ed)?\b', re.I)),
    ('forum', re.compile(r'\bforum\b|\bcommunity\s+(?:saying|think|say)\b|\bplayers?\s+(?:saying|think)\b', re.I)),
)

TOPIC_LABELS = {'ecr': 'Crate Rush / ECR', 'promo': 'Promo codes', 'upcoming': 'Upcoming events',
                'patch': 'Patches and bugs', 'forum': 'What the forum says'}


def topics(question):
    """Map each clause to the read-only topics it concerns, in first-mention order."""
    resolved = []
    for clause in split(question):
        hits = [topic for topic, pattern in TOPIC_PATTERNS if pattern.search(clause)]
        if hits:
            resolved.append((clause, hits))
    return resolved


def distinct_topics(resolved):
    """The flat, de-duplicated, order-preserving topic list from :func:`topics` output."""
    seen = []
    for _, hits in resolved:
        for topic in hits:
            if topic not in seen:
                seen.append(topic)
    return seen


def should_fan_out(question, minimum=2):
    """Decide whether the caller should answer several topics instead of one.

    Requires at least ``minimum`` distinct topics across the clauses, so a message that
    is compound in form but single in topic ("the ecr and what the ecr costs") keeps
    using the ordinary single-answer path.
    """
    if not is_compound(question):
        return []
    found = distinct_topics(topics(question))
    return found if len(found) >= minimum else []


def join(sections, *, heading=True):
    """Assemble per-topic answers into one reply, labelled and de-duplicated.

    An empty section is dropped rather than shown as a blank heading. If only one topic
    actually produced text the headings are removed, so the reader does not see a
    "1 topic" wrapper around a normal answer.
    """
    usable = [(label, text.strip()) for label, text in sections if text and text.strip()]
    if not usable:
        return None
    if len(usable) == 1 or not heading:
        return usable[0][1]
    blocks = []
    for index, (label, text) in enumerate(usable, 1):
        blocks.append(f'**{index}. {label}**\n{text}')
    return '\n\n'.join(blocks)


def unanswered_note(clauses, answered=()):
    """Name the parts of a compound message that no handler could answer.

    Being explicit here is the point: the old behaviour dropped half a question without
    saying so, which reads as though the bot answered everything it was asked.
    """
    missing = [clause for clause in clauses if clause not in set(answered)]
    if not missing:
        return ''
    quoted = '; '.join(f'"{clause}"' for clause in missing[:3])
    return (f'*I could not answer {quoted} from the saved data. '
            f'I would rather say that than guess.*')
