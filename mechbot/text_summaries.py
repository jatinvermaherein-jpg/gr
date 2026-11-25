"""Small deterministic text-cleaning and extractive-summarization helpers."""
import html
import re

_URL = re.compile(r"https?://\S+", re.I)
_IMAGE_MARKER = re.compile(r"\[(?:image|attachment|media|embed)(?:\s*[-:#]?\s*[^\]]*)?\]", re.I)
_DISCORD_TIMESTAMP = re.compile(r"<t:\d+(?::[tTdDfFR])?>", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*", re.I)


def clean_source_text(value, *, limit=12000):
    """Remove post chrome/markup/URLs while preserving readable source wording."""
    text = html.unescape(str(value or '')).replace('\x00', '')
    text = re.sub(r'<@!?\d+>|<@&\d+>', '[member]', text)
    text = re.sub(r'@(everyone|here)\b', lambda match: '@\u200b' + match.group(1), text, flags=re.I)
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\((?:https?://)?[^)]*\)', r'\1', text)
    text = _URL.sub(' ', text)
    text = _DISCORD_TIMESTAMP.sub(' ', text)
    text = _IMAGE_MARKER.sub(' ', text)
    text = re.sub(r'<[^>]{1,160}>', ' ', text)
    text = re.sub(r'(?m)^\s*(?:[-*_~]{3,}|[─━═─]{3,})\s*$', ' ', text)
    # Markdown headings/bullets are layout, not content; keep the words after them.
    text = re.sub(r'(?m)^\s{0,4}(?:#{1,6}|>+|[-+*])\s*', '', text)
    text = re.sub(r'(?m)(?:^|\s)#{1,6}(?=\s|$)', ' ', text)
    text = re.sub(r'[✦✧❖◆◇━─═▰]+', ' ', text)
    text = re.sub(r'\*\*|__|~~|```|`', '', text)
    text = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', ' ', text)
    cleaned = []
    for line in text.splitlines():
        line = re.sub(r'[\t\u00a0]+', ' ', line)
        line = re.sub(r'\s+', ' ', line).strip(' \t|•·✦✧❖◆◇#—–-')
        if not line or not _WORD.search(line):
            continue
        # Drop hashtag-only footer lines and decorative banner fragments.
        if re.fullmatch(r'(?:#[\w-]+\s*){1,8}', line):
            continue
        line = re.sub(r'\s+', ' ', line).strip()
        cleaned.append(line)
    result = '\n'.join(cleaned)
    if len(result) > limit:
        result = result[:limit].rsplit(' ', 1)[0].rstrip()
    return result


def omit_publication_only_sentences(value):
    """Remove publication dates from body text; the caller reports them as post dates."""
    text=clean_source_text(value)
    if not text:
        return ''
    month=r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    date=r'(?:'+month+r'\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+20\d{2})?|\d{1,2}\s+'+month+r'(?:\s+20\d{2})?|20\d{2}-\d{2}-\d{2})'
    publication_word=r'(?:post(?:ed|s)?|publish(?:ed|ation)?|publication|creat(?:ed|ion)|shar(?:ed)|upload(?:ed)?|announc(?:ed|ement|ements)|releas(?:ed|e)|updat(?:ed|e))'
    publication_date=re.compile(
        rf'\b(?:{publication_word}(?:\s+\w+){{0,3}}|(?:post|publication|announcement)\s+date)\s*(?:(?:on|at|is)\s*)?[:：]?\s*{date}',re.I)
    event_date=re.compile(
        r'\b(?:event\s+crate\s+rush|ecr|battle\s+pass|event|tournament|season|challenge)\b.{0,75}\b(?:date|schedule|scheduled|starts?|begins?|starting|launch(?:es)?|opens?|runs?|ends?|held|occurs?|takes?\s+place)\b|'
        r'\b(?:starts?|begins?|starting|launch(?:es)?|opens?|runs?|ends?|held|occurs?)\b.{0,45}\b(?:event|tournament|season|challenge)\b',re.I)
    output=[]
    for line in text.splitlines():
        for sentence in re.split(r'(?<=[.!?])\s+|\s*[;\n]+',line):
            sentence=sentence.strip()
            if not sentence:
                continue
            cleaned=publication_date.sub('',sentence)
            cleaned=re.sub(r'\b(?:the\s+)?(?:event\s+(?:dates?|schedule)|schedule)\s+(?:was|were|is|are)\b','',cleaned,flags=re.I)
            cleaned=re.sub(r'^[\s,;]*(?:and|but)\s+','',cleaned,flags=re.I)
            cleaned=re.sub(r'\s{2,}',' ',cleaned).strip(' ,;:—–-')
            if cleaned!=sentence:
                substance=re.sub(r'\b(?:the|a|an|is|was|were|on|at|post(?:ed)?|publish(?:ed)?|publication|date|schedule|event|ecr|tournament|season|challenge|update|announcement|announced|created|shared|uploaded|released)\b',' ',cleaned,flags=re.I)
                substance=re.sub(r'[^A-Za-z0-9]+',' ',substance).strip()
                if not substance:
                    continue
                sentence=cleaned
            output.append(sentence)
    return ' '.join(output)


def summarize_source_text(value, *, query_terms=(), max_chars=360, max_sentences=3):
    """Choose a few useful details from a source without dumping its raw post body."""
    text = clean_source_text(value)
    if not text:
        return ''
    terms = {str(term).casefold() for term in query_terms if len(str(term)) > 2}
    candidates = []
    seen = set()
    for line_index, line in enumerate(text.splitlines()):
        for sentence in re.split(r'(?<=[.!?])\s+|\s+\|\s+', line):
            sentence = re.sub(r'\s+', ' ', sentence).strip(' •-–—|')
            if not sentence or len(sentence) < 12:
                continue
            normalized = sentence.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            # Greetings, navigation, and generic calls to action add little to a summary.
            if re.match(r'^(?:hello|hi|hey)\b', sentence, re.I):
                continue
            if re.search(r'\b(?:click here|watch the video|read more|check the link|follow us|like and subscribe)\b', sentence, re.I):
                continue
            words = {word.casefold() for word in _WORD.findall(sentence)}
            relevance = sum(2 for term in terms if term in words or any(word.startswith(term) for word in words))
            specificity = sum(1 for term in (
                'starts', 'starting', 'ends', 'ending', 'available', 'reward', 'rewards',
                'free', 'cost', 'price', 'credits', 'coins', 'version', 'fixed', 'fix',
                'bug', 'issue', 'changed', 'change', 'damage', 'crash', 'problem', 'event',
                'patch', 'weapon', 'pilot', 'mech') if term in words)
            length_bonus = 1 if 35 <= len(sentence) <= 240 else 0
            candidates.append((relevance + specificity + length_bonus, line_index, sentence))
    if not candidates:
        return text[:max_chars].rstrip()
    # Favor on-topic detail, while retaining original order for a readable summary.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen = sorted(candidates[:max(1, int(max_sentences))], key=lambda item: item[1])
    parts = []
    for _, __, sentence in chosen:
        if len(' '.join(parts)) + len(sentence) + (1 if parts else 0) > max_chars:
            continue
        parts.append(sentence)
    if not parts:
        sentence = chosen[0][2]
        return sentence[:max_chars].rsplit(' ', 1)[0].rstrip(' ,;:') + '…' if len(sentence) > max_chars else sentence
    return ' '.join(parts)


def search_terms(value, *, stopwords=()):
    """Return normalized content terms suitable for lightweight relevance checks."""
    stopped = {str(word).casefold() for word in stopwords}
    return [word.casefold() for word in _WORD.findall(str(value or ''))
            if len(word) > 2 and word.casefold() not in stopped]
