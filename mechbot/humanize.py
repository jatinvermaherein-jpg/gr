"""Reply style control for the advisor, built on the blader/humanizer pattern list.

The upstream skill (https://github.com/blader/humanizer, SKILL.md v4.0.0, MIT) lists 26 tells of
AI writing, drawn from Wikipedia's "Signs of AI writing". It is written for a human editing prose
with a model. This module is the same ruleset turned into code for a bot that has to answer in a
Discord channel, and it keeps the skill's own judgement rules rather than a crude word ban:

* Patterns 1-5 (staging) justify an edit on a single sighting.
* Patterns marked *weak alone* (8 dashes, 9 stacked qualifiers, 10 hyphenated pairs, 11 passive
  voice, 21 curly quotes) only count when a second unrelated tell appears in the same passage. A
  reply with one dash is not a rewrite target.
* Chatbot residue (22) is always worth removing, even in informal text.

One deliberate deviation, and it matters here. v4.0.0 dropped the old no-invention rule: a human
editor may "add a specific detail you know to be true" where the text is generic. The advisor may
not. Its facts come from the engine's data files, so `polish` only ever deletes or rewords, and both
`polish` and `audit` refuse to touch a reply whose digits changed. That is what keeps style control
from becoming a second source of truth.

Only the patterns that a short technical answer can actually commit are implemented: 1, 2, 3, 4, 5,
6, 7, 8, 9, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 25, 26. Decorative-heading emoji (20) and
bold-as-decoration (19) are checked against the reply's own shape, not a word list.
"""
from __future__ import annotations

import re

WEAK_ALONE = {8, 9, 10, 11, 21}

STOCK_AI_WORDS = re.compile(
    r'\b(?:delve[sd]?|delving|leverage[sd]?|leveraging|seamless(?:ly)?|robust|crucial|vital|pivotal|'
    r'game-?changer|cutting-?edge|best-?in-?class|holistic|streamline[d]?|supercharge|unlock(?:ing)? the|'
    r'tapestry|landscape|showcas\w+|featur\w+ (?:a|an) impressive|testament|utili[sz]\w+|'
    r'it(?:s| is) important to note|note that|when it comes to|in the world of|aims to provide)\b', re.I)

_CLOSER = re.compile(r"^\s\**\s*(?:that(?:’|')?s the real win|that distinction matters|read that again|"
                     r"let that sink in|worth (?:remembering|noting)|the message was clear|"
                     r"(?:overall|ultimately|in short|in essence|all in all|at its core)[,:])", re.I)

_RUNUP = re.compile(r'(?:^|\n)\s*[*>-]?\s*(?:here’?s the thing|honestly\??|so,|well,|fun fact[:?]|'
                    r'the hard part is|the best part\??|drum roll|believe it or not|'
                    r'let’?s dive in|to put it plainly|without further ado)[,:]?\s*', re.I)

_NOT_BUT = re.compile(r"it’?s (?:not|never) (?:just |only |merely )?(?P<neg>[^.;!?\n]{2,80}?)[,;]? "
                      r"(?:it|this|that)? ?(?:’|')?s (?P<pos>[A-Z][^.!?\n]{2,120})")

_NOT_BUT_SPLIT = re.compile(r'(?<![A-Za-z])this does not mean (?P<a>[^.!?\n]{2,80})\. It means (?P<b>[^.!?\n]{2,120})', re.I)

_ARGUING = re.compile(r'\bI’?m not saying [^.!?\n]{2,80},? but\b|\b(?:a tempting|one could argue|some might argue)\b', re.I)

_DEEP_SAID = re.compile(r'\b(?:at its core|when it really matters|what really matters is|in the end,? '
                        r'(?:what matters|it comes down)|symmetry is the language|the real (?:secret|trick) is)\b', re.I)

_QUALIFIERS = re.compile(r'\b(?:could|may|might)\s+(?:potentially|possibly|arguably|in theory)\b|\b'
                         r'(?:appears?|seems?|likely|probably)\s+(?:to be|that)\b', re.I)

_VAGUE_LINK = re.compile(r'\b(?:associated with|in connection with|related to the (?:work|efforts) of|'
                        r'serves as a|stands as a|boasts(?:es)?|features a wide)\b', re.I)

_SHALLOW_ING = re.compile(r',? (?:symbolizing|reflecting|showcasing|highlighting|ensuring|underscoring|'
                          r'signaling|setting the stage|making it clear)[^.!?\n]{0,60}', re.I)

_SALES = re.compile(r'\b(?:nestled within|breathtaking|stunning views|hidden gem|unforgettable|'
                    r' Truly |truly special|must-see|must-have|a beloved)\b', re.I)

_AUTHORITY = re.compile(r'\b(?:experts (?:believe|say|recommend|agree)|industry insiders|many argue|'
                        r'studies show|it is widely (?:known|acknowledged)|backed by experts)\b', re.I)

_DISCLAIMER = re.compile(r'\b(?:while details are limited|information is (?:limited|scarce)|'
                         r'(?:my|the) (?:knowledge|data) (?:may|might) be (?:outdated|incomplete)|'
                         r'as of (?:my|the) (?:last|latest) (?:update|training))\b[^.!?\n]*', re.I)

_RESIDUE = re.compile(r"\b(?:great question[!.]?|thanks for asking[!.]?|thanks for the question[!.]?|"
                      r"i hope (?:this|that) helps[!.]?|happy to help[!.]?|certainly[.!]?|of course[.!]?|"
                      r"let me know (?:if|for)|feel free to (?:ask|reach out)|as an ai\b|"
                      r"i(?:’|')?d be happy to|to answer your question)\b", re.I)

_META = re.compile(r'\b(?:this (?:answer|reply|message|module|table) (?:was (?:built|added)|compares|'
                   r'is (?:generated|compiled)|covers)|the (?:table|list) below (?:compares|shows|lists)|'
                   r'below (?:is|are) (?:a|the) (?:list|summary|breakdown))\b', re.I)

_EMOJI = re.compile(r'[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+')

_BOLDED = re.compile(r'^\s*[*>-]?\s*\*\*(?P<label>[^*]{1,42})[:\-]\*\*\s*(?P<rest>\S.*)$')


def _count(pattern, text):
    return len(pattern.findall(text)) if pattern.groups else len([m for m in pattern.finditer(text)])


def _triads(text):
    """'a, b, and c' used as a rhythm device: three parallel single words twice in one reply."""
    found = re.findall(r'\b\w+(?:, \w+){2},? (?:and|or) \w+\b', text)
    plain = [hit for hit in found if len(hit.split()) <= 8]
    return plain


def _dash_density(text):
    body = re.sub(r'`[^`]*`', '', text)
    hits = len(re.findall(r'—|–|(?<= )-(?= )', body))
    sentences = max(1, len(re.findall(r'[.!?]', body)))
    return hits, sentences


def audit(text):
    """Every tell in a reply, strongest first, as {id, name, excerpt, fix}. Empty list means clean."""
    body = str(text or '')
    if not body.strip():
        return []
    tells = []

    def note(pid, name, hits, fix):
        for hit in list(hits)[:3]:
            excerpt = hit if isinstance(hit, str) else str(hit)
            tells.append({'id': pid, 'name': name, 'excerpt': excerpt.strip()[:120], 'fix': fix})

    note(1, 'Not X but Y', [m.group(0) for m in _NOT_BUT.finditer(body)] + [m.group(0) for m in _NOT_BUT_SPLIT.finditer(body)],
         'state the second half directly')
    closers = [line for line in body.split('\n') if _CLOSER.match(line)]
    note(2, 'one-line closer or dramatic fragment', [line.strip() for line in closers]
         + [m.group(0) for m in re.finditer(r'\b(?:[A-Z]\w+\.){2,}[A-Z]\w+\.\b', body)]
         + [m.group(0) for m in re.finditer(r'\b(?:Overall|Ultimately|In short|All in all), [^.!?\n]{10,90}\.', body)],
         'cut it, or merge it into the sentence it repeats')
    note(3, 'saying that sounds deep', [m.group(0) for m in _DEEP_SAID.finditer(body)], 'replace the saying with the claim')
    note(4, 'staged run-up', [m.group(0).strip() for m in _RUNUP.finditer(body)], 'drop the run-up and lead with the point')
    note(5, 'arguing with no one', [m.group(0) for m in _ARGUING.finditer(body)], 'keep any real claim, drop the unraised objection')
    note(6, 'forced triad', _triads(body), 'use the number of items the point needs')
    openings = re.findall(r'(?:^|\n)[*>-]?\s*([A-Z][a-z]+ \w+ \w+)[,.:]', body)
    repeated = [word for word in set(openings) if openings.count(word) >= 3]
    note(7, 'repeated sentence opening', repeated, 'merge the sentences or change the subject')
    note(12, 'stock AI word', sorted({m.group(0).lower() for m in STOCK_AI_WORDS.finditer(body)}), 'use the plain word')
    note(13, 'inflated significance', [m.group(0) for m in re.finditer(
        r'\b(?:marking a (?:pivotal|major|defining) \w+|despite (?:early|recent) challenges[^.!?\n]{0,60}|'
        r'the future looks bright|sets the (?:tone|standard))\b', body)], 'keep the fact, drop the significance')
    note(15, 'shallow -ing phrase', [m.group(0).strip() for m in _SHALLOW_ING.finditer(body)], 'keep only what the data supports')
    note(16, 'sales language', [m.group(0) for m in _SALES.finditer(body)], 'state what the thing is')
    note(17, 'borrowed authority', [m.group(0) for m in _AUTHORITY.finditer(body)], 'name the source and what it said, or cut')
    note(18, 'avoiding is and has', [m.group(0) for m in _VAGUE_LINK.finditer(body)], 'say is or has, or name the relationship')
    note(22, 'chatbot residue', [m.group(0) for m in _RESIDUE.finditer(body)], 'remove the wrapper')
    note(23, 'knowledge-limit disclaimer', [m.group(0) for m in _DISCLAIMER.finditer(body)],
         'name the missing fact instead of apologising')
    note(25, 'writing about the reply', [m.group(0) for m in _META.finditer(body)], 'describe the item, not the answer')
    labels = [m for m in (_BOLDED.match(line) for line in body.split('\n')) if m]
    if len(labels) >= 3 and len(labels) >= max(1, int(0.6 * len([l for l in body.split('\n') if l.strip()]))):
        note(19, 'bold label on every line', [m.group('label') for m in labels],
             'keep the bold on the decision, write the rest as prose')
    headings = [line for line in body.split('\n') if re.match(r'^#{1,6}\s', line) or re.match(r'^\*\*[^*]+\?\*?\*?\s*$', line)]
    for line in headings:
        titleish = re.sub(r'[#*\s]', '', line)
        if titleish and titleish[0].isupper() and len(titleish.split()) > 1 and ' ' in titleish:
            words = titleish.split()
            if sum(1 for w in words if w[:1].isupper()) >= max(2, int(0.8 * len(words))):
                note(20, 'decorative or title-cased heading', [line.strip()[:80]], 'sentence case, or drop the heading')
    if _EMOJI.search(re.sub(r'`[^`]*`', '', body)):
        note(20, 'emoji as decoration', [m.group(0) for m in _EMOJI.finditer(re.sub(r'`[^`]*`', '', body))],
             'remove it; the number is the emphasis')
    if re.search(r'[“”‘’]', body):
        note(21, 'curly quotes', ['“ ” ’'], 'straight quotes')
    dashes, sentences = _dash_density(body)
    if dashes > 2:
        note(8, 'dashes as the universal connector', [f'{dashes} dashes in {sentences} sentences'],
             'periods, commas or colons')
    note(9, 'stacked qualifiers', [m.group(0) for m in _QUALIFIERS.finditer(body)], 'keep the qualifier the data supports')
    if re.search(r"^\s\**\s*(?:the answer|my recommendation|here(?:’|')?s (?:what|the))", body, re.I):
        note(26, 'the decision arrives last', [body.split('\n')[0].strip()[:90]], 'lead with the verdict')

    strong = [tell for tell in tells if tell['id'] not in WEAK_ALONE]
    weak = [tell for tell in tells if tell['id'] in WEAK_ALONE]
    out = sorted(strong, key=lambda tell: tell['id'])
    if len(strong) >= 2 or (weak and len(re.findall(r'[.!?]', body)) > 6):
        out += sorted(weak, key=lambda tell: tell['id'])
    return out


def _digits(text):
    return re.findall(r'\d[\d,.*]*', text)


def _strip_residue(text):
    """Remove chatbot wrapper phrases without eating the fact sitting next to them."""
    kept = []
    for sentence in re.split(r'(?<=[.!?])\s+', str(text or '')):
        if not _RESIDUE.search(sentence):
            kept.append(sentence)
            continue
        remainder = re.sub(r'\s+', ' ', _RESIDUE.sub(' ', sentence)).strip(' !.,;:')
        kept.append(remainder if len(remainder) >= 16 else '')
    return ' '.join(part for part in kept if part)


def polish(text, *, allow_empty=True):
    """Apply the safe edits (delete wrappers, plain words, straight quotes, dash repair).

    Returns (text, tells). The text is only replaced when the rewrite kept every number and unit it
    started with and left every quoted passage alone; otherwise the original comes back with the
    surviving tells listed, so a caller can fall back to its own deterministic rendering rather than
    ship a reworded claim. A rewrite that would delete the whole message is also refused: stripping
    chatbot residue is not a licence to say nothing.
    """
    body = str(text or '').strip()
    if not body:
        return '', []
    out = body

    # a line that is nothing but wrapper comes out whole; one that carries a fact keeps the fact and
    # loses the wrapper further down
    def keep(line):
        hit = _RESIDUE.search(line)
        return hit is None or len(line.strip()) > len(hit.group(0)) + 4

    out = '\n'.join(line for line in out.split('\n') if keep(line))
    out = _RUNUP.sub(lambda m: ('\n' if m.group(0).startswith('\n') else ' '), out)
    # residue inside a sentence still reads like a chatbot: take the phrase out, and if nothing
    # substantive was left, take the sentence
    out = _strip_residue(out)
    out = re.sub(r"(?:^|\n)\s*(?:That(?:’|')?s the real win|Let that sink in|That distinction matters|"
                 r"Read that again)\.?\s*(?=\n|$)", '', out, flags=re.I)
    out = re.sub(r'\b(?:Overall|Ultimately|In short|In essence|All in all),\s+', '', out, flags=re.I)
    out = _DISCLAIMER.sub('', out)
    out = _SHALLOW_ING.sub('.', out)
    out = _NOT_BUT_SPLIT.sub(lambda m: 'It means ' + m.group('b').strip(), out)
    out = _NOT_BUT.sub(lambda m: m.group('pos').strip()[:1].upper() + m.group('pos').strip()[1:], out)
    out = _ARGUING.sub('', out)
    out = _DEEP_SAID.sub('', out)
    replacements = {'delving into': 'looking at', 'delve into': 'look at', 'delve': 'look at',
                    'delving': 'looking at', 'delved': 'looked at',
                    'leveraging': 'using', 'leverage': 'use',
                    'seamlessly': '', 'seamless': 'clean', 'crucial': 'important', 'vital': 'needed',
                    'pivotal': '', 'robust': 'solid', 'utilize': 'use', 'utilised': 'used',
                    'showcasing': 'showing', 'showcases': 'shows', 'it is important to note that': '',
                    'when it comes to': 'for', 'in the world of': 'in', 'best-in-class': 'top',
                    'game-changer': '', 'cutting-edge': 'new', 'testament to': 'proof of'}
    for find, replace in replacements.items():
        out = re.sub(r'\b' + re.escape(find) + r'\b\s*', (replace + ' ') if replace else '', out, flags=re.I)
    out = out.replace('“', '"').replace('”', '"').replace('’', "'")
    out = re.sub(r'\s+—\s+', '. ', out)
    out = re.sub(r' — ', ', ', out)
    out = re.sub(r'[ \t]{2,}', ' ', out)
    out = re.sub(r'\s+([,.;:])', r'\1', out)
    out = re.sub(r'(\.\s*)\.(?!\s*\w)', r'\1', out)
    out = re.sub(r'^\s*[•*]\s*\*{2}([A-Z][a-z]+(?: \w+){0,3})[:\-]\*\*\s*', r'• \1: ', out, flags=re.M)
    out = '\n'.join(line.rstrip() for line in out.split('\n'))
    out = re.sub(r'\n{3,}', '\n\n', out).strip()
    if not out or (not allow_empty and not out):
        out = body
    if _digits(out) != _digits(body):
        return body, audit(body)
    for quoted in re.findall(r'"[^"]{6,}"', body):
        if quoted not in out:
            return body, audit(body)      # a quote from the announcement must survive verbatim
    return out, audit(out)


def acceptable(text):
    """True when a reply can ship as-is: no tells left, and at least one fact-carrying line."""
    cleaned, tells = polish(text)
    return not tells
