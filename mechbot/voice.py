"""Voice: how the bot talks like a person instead of a database.

The bot used to answer a question it had understood correctly in a voice nobody
speaks in — "For **Redox**, the rank 1 L1 → rank 2 L1 upgrade costs **75 A-Coins ·
15,325 Credits · 6 Blueprints (BP)**" is accurate and reads like a receipt. This
module supplies the small human layer:

* **Emoji** — the bot uploads a small set of Mech Arena flavoured application emojis
  (currency + mechs + ranks + GG) once, caches their ids, and uses them in replies.
  When the upload is unavailable the text still reads fine: every emoji has a plain
  fallback and nothing depends on it being present.
* **Tone** — `humanise_cost` and friends keep every number exactly as the engine
  produced it (the honesty guard in `pipeline.py` still applies) and only change the
  sentences around it.

Nothing here contains game data or maths. It is presentation only, so a wrong
number can never come from this module.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from pathlib import Path

import aiohttp

log = logging.getLogger('mechbot.voice')

BOT_ROOT = Path(__file__).resolve().parent.parent
DATA = BOT_ROOT / 'data'

# name, file, plain-text fallback
# ONLY the three real currencies. No decorative emoji, no GIFs — the bot's replies stay
# readable even if an upload fails, because every entry has a plain fallback.
#
# The filenames here must stay identical to bot.CURRENCY_ASSETS: this module used to ask
# for acoin.png / credits.png / blueprint.png, which were never in data/ (the shipped files
# are emoji_*.png). The missing-file guard below made that silent, so the understanding
# pipeline spent every reply on the plain glyphs while /upgrade_cost used PNG attachments.
EMOJI_SET = (
    ('ma_acoin', 'emoji_a_coins.png', '\U0001F150'),
    ('ma_credits', 'emoji_credits.png', '\U0001F152'),
    ('ma_blueprint', 'emoji_blueprints.png', '\U0001F4D8'),
)

# currency key used by upgrade_cost_response -> emoji name
CURRENCY_EMOJI = {'acoins': 'ma_acoin', 'credits': 'ma_credits', 'blueprints': 'ma_blueprint'}


class EmojiBook:
    """Uploads the emoji set once and hands back `<:name:id>` strings.

    Discord allows 256 KiB per application emoji; every file here is a few KB. If the
    API refuses (bad token, no permission, rate limit) we keep the plain fallbacks and
    retry on the next boot rather than degrading the reply.
    """

    def __init__(self, application_id=None, session_factory=None):
        self.application_id = application_id
        self._session_factory = session_factory
        self.ready = {}
        self.plain = {name: fallback for name, _, fallback in EMOJI_SET}

    def get(self, name):
        return self.ready.get(name) or self.plain.get(name, '')

    def currency_map(self):
        return {key: self.get(emoji_name) for key, emoji_name in CURRENCY_EMOJI.items()}

    async def ensure(self):
        token = os.getenv('DISCORD_TOKEN', '').strip()
        if not token or not self.application_id:
            return {}
        base = f'https://discord.com/api/v10/applications/{int(self.application_id)}/emojis'
        headers = {'Authorization': 'Bot ' + token}
        timeout = aiohttp.ClientTimeout(total=25, connect=5, sock_read=15)
        found = {}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(base, headers=headers) as response:
                    if response.status != 200:
                        log.warning('Emoji list unavailable (HTTP %s); plain fallbacks in use.', response.status)
                        return {}
                    body = await response.json(content_type=None)
                existing = {str(i.get('name')): i for i in body.get('items', []) if i.get('name') and i.get('id')}
                for name, filename, _ in EMOJI_SET:
                    emoji = existing.get(name)
                    if emoji is None:
                        path = DATA / filename
                        if not path.is_file():
                            continue
                        raw = path.read_bytes()
                        if len(raw) > 256 * 1024:
                            log.warning('Emoji %s is over Discord’s 256 KiB limit; skipped.', name)
                            continue
                        payload = {'name': name,
                                   'image': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')}
                        async with session.post(base, headers=headers, json=payload) as response:
                            if response.status not in (200, 201):
                                log.warning('Emoji %s upload failed (HTTP %s).', name, response.status)
                                continue
                            emoji = await response.json(content_type=None)
                    if emoji.get('id'):
                        found[name] = f"<:{name}:{emoji['id']}>"
        except Exception as exc:                      # network, DNS, bad payload
            log.warning('Emoji setup skipped (%s); plain fallbacks in use.', type(exc).__name__)
            return {}
        self.ready = found
        if found:
            log.info('Using %s/%s Mech Arena emojis in replies.', len(found), len(EMOJI_SET))
        return found


# ----------------------------------------------------------------- tone helpers
def _plural(n, one, many):
    return one if abs(int(n)) == 1 else many


def humanise_cost(name, transition, costs, emoji, *, assumed='', no_op=False, purchase=''):
    """Rewrite a verified cost in a voice a person would use.

    Every figure is passed through untouched; only the prose changes. `emoji` maps
    acoins/credits/blueprints to something printable (emoji or fallback glyph).
    """
    a = int(costs.get('acoins', 0) or 0)
    c = int(costs.get('credits', 0) or 0)
    b = int(costs.get('blueprints', 0) or 0)
    label = f'**{name}**' if name else 'that item'

    if no_op:
        return (f'{label} is already there — {transition} is the same position, so nothing '
                f'to spend. That one is free. ' + _spend_line(a, c, b, emoji))

    spent = [f"{emoji.get('acoins','')} {a:,} A-Coins" if a else '',
             f"{emoji.get('credits','')} {c:,} Credits" if c else '',
             f"{emoji.get('blueprints','')} {b:,} Blueprints" if b else '']
    spent = [s for s in spent if s]
    if not spent:
        return f"{label}: I don't have a confirmed price for {transition} yet, so I won't guess one.{assumed}"

    # One shape, on purpose. This was three randomly-chosen openers plus a remark about which
    # currency "gates" or is "the scarce half", and the reply a player actually got was
    # "Redox - rank 4 L1 -> rank 5 L1 costs 2,825 A-Coins ... Blueprints are the part that gates
    # this one." The figures were right and the commentary was ours, not the game's. The target
    # format is the plain sentence: subject, then the costs, and nothing after them.
    body = ' · '.join(spent)
    note = f' {purchase}' if purchase else ''
    return f'For {label}, the {transition} upgrade costs **{body}**.{assumed}{note}'


def _spend_line(a, c, b, emoji):
    bits = [f"{emoji.get('acoins','')} {a:,}" if a else '', f"{emoji.get('credits','')} {c:,}" if c else '',
            f"{emoji.get('blueprints','')} {b:,}" if b else '']
    bits = [x for x in bits if x]
    return ('Cost: **' + ' · '.join(bits) + '**.') if bits else ''


def opener(kind):
    """A short, varied lead-in. Never carries a number, so it cannot lie."""
    return {
        'cost': random.choice(['Straight answer:', 'Here you go:', 'Right —', 'Got it:']),
        'compare': random.choice(['Short version:', 'Honest answer:', 'If it were my account:']),
        'stats': random.choice(['Here is the sheet:', 'What the catalog says:']),
        'builds': random.choice(['These are the builds players actually logged:', 'From the build database:']),
        'unknown': random.choice(['I want to get this right —']),
    }.get(kind, '')


def assume_note(from_rank=None, to_rank=None, from_level=None, to_level=None, guessed=False):
    """Say out loud what was inferred, so the player can correct it."""
    if not guessed:
        return ''
    bits = []
    if from_rank and to_rank:
        bits.append(f'rank {from_rank} → {to_rank}')
    if from_level and to_level and from_level != to_level:
        bits.append(f'level {from_level} → {to_level}')
    if not bits:
        return ''
    return f" I read that as {' and '.join(bits)} — say the word if I picked the wrong one."


def missing_fact(name, transition, nearest=None):
    """The honest gap. Never invents a number to fill it."""
    line = (f"I don't have a confirmed price for **{name}** at {transition} yet, and I would "
            f"rather say that than make one up.")
    if nearest:
        line += f' The table does hold {nearest}.'
    return line


def spell_it_out(name, replacements):
    """Disclose a spelling correction instead of silently answering something else."""
    if not replacements:
        return ''
    pairs = ', '.join(f'"{typed}" → **{fixed}**' for typed, fixed in replacements.items())
    return f'Reading {pairs}. '
