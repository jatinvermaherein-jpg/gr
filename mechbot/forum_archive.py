"""Search and refresh the user-provided recent Plarium Forum thread archive."""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ftfy import fix_text

log=logging.getLogger('mechbot.forum')
BOT_ROOT=Path(__file__).resolve().parent.parent
DATA_DIR=BOT_ROOT/'data'
FORUM_ARCHIVE_PATH=DATA_DIR/'mech_arena_last_150_threads.json'
FORUM_SCRAPER_PATH=BOT_ROOT/'scripts'/'scrape_mech_arena.py'
FORUM_SYNC_SECONDS_DEFAULT=12*60*60
FORUM_SYNC_RETRY_SECONDS=60*60
FORUM_SCRAPER_TIMEOUT_SECONDS=30*60
FORUM_TARGET_THREADS=150

_WORD=re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)?",re.I)
_URL=re.compile(r'https?://\S+',re.I)
_FORUM_HOST='forum.plarium.com'
_STOPWORDS={
    'a','about','all','also','am','an','and','any','are','as','at','be','been','but','by','can','could',
    'did','do','does','for','from','get','got','had','has','have','he','her','here','him','his','how','i',
    'in','into','is','it','its','just','latest','me','more','most','my','new','of','on','or','our','please',
    'post','posts','recent','say','says','saying','she','should','so','some','tell','that','the','their',
    'them','there','these','they','this','those','to','up','us','was','we','what','when','where','which',
    'who','why','will','with','would','you','your','forum','thread','threads','community','players','player',
    'plarium','mech','arena','game','official',
}

_ARCHIVE_CACHE_PATH=None
_ARCHIVE_CACHE_SIGNATURE=None
_ARCHIVE_CACHE_ROWS=[]


def forum_sync_seconds():
    """Use a conservative 12-hour full refresh by default; allow 1–48 hours via env."""
    try:value=int(os.getenv('FORUM_SYNC_SECONDS',str(FORUM_SYNC_SECONDS_DEFAULT)))
    except (TypeError,ValueError):value=FORUM_SYNC_SECONDS_DEFAULT
    return max(60*60,min(value,48*60*60))


def _valid_thread_url(value):
    try:
        parsed=urlparse(str(value or ''))
        return (parsed.scheme=='https' and parsed.netloc.casefold()==_FORUM_HOST
                and parsed.path.startswith('/mech-arena/'))
    except (TypeError,ValueError):return False


def _read_archive(path=None):
    global _ARCHIVE_CACHE_PATH,_ARCHIVE_CACHE_SIGNATURE,_ARCHIVE_CACHE_ROWS
    archive_path=Path(path or FORUM_ARCHIVE_PATH)
    try:
        stat=archive_path.stat()
        signature=(stat.st_mtime_ns,stat.st_size)
    except OSError:
        return []
    if _ARCHIVE_CACHE_PATH==archive_path and _ARCHIVE_CACHE_SIGNATURE==signature:
        return _ARCHIVE_CACHE_ROWS
    try:
        with archive_path.open('r',encoding='utf-8') as handle:data=json.load(handle)
        rows=data.get('threads',[]) if isinstance(data,dict) else []
        if not isinstance(rows,list):raise ValueError('thread list is malformed')
        clean=[row for row in rows if isinstance(row,dict) and _valid_thread_url(row.get('url'))
               and isinstance(row.get('original_post'),dict)]
    except (OSError,UnicodeError,json.JSONDecodeError,ValueError,AttributeError) as exc:
        log.warning('Could not read the forum archive (%s); keeping the previous in-memory snapshot.',type(exc).__name__)
        return _ARCHIVE_CACHE_ROWS if _ARCHIVE_CACHE_PATH==archive_path else []
    _ARCHIVE_CACHE_PATH=archive_path
    _ARCHIVE_CACHE_SIGNATURE=signature
    _ARCHIVE_CACHE_ROWS=clean
    return clean


def _tokens(text):
    return [token.casefold() for token in _WORD.findall(fix_text(str(text or '')[:2000]))
            if len(token)>1 and token.casefold() not in _STOPWORDS]


def wants_forum_lookup(text):
    """Recognize explicit requests for saved Plarium Forum discussions/posts."""
    value=str(text or '').strip()
    if re.match(r'^(?:plarium\s+)?forum(?:\s|[:：]|$)',value,re.I):
        return True
    marker=bool(re.search(r'\b(?:plarium\s+)?forum\b',value,re.I))
    player_opinion=bool(re.search(
        r'\bwhat\s+(?:are|do)\s+(?:the\s+)?(?:players|community)\s+(?:saying|think|discuss)\b',
        value,re.I))
    if player_opinion:return True
    if not marker:return False
    return bool('?' in value or re.search(
        r'\b(?:about|any|find|latest|list|new|news|post|posts|recent|search|show|thread|threads|'
        r'update|updates|what|where|who|according|discuss|discussion|says|said)\b',value,re.I))


def _safe_post_text(value):
    text=fix_text(str(value or '')).replace('\x00','')
    text=re.sub(r'<@!?\d+>|<@&\d+>', '[member mention]', text)
    text=re.sub(r'@(everyone|here)\b',lambda match:'@\u200b'+match.group(1),text,flags=re.I)
    text=_URL.sub('[embedded link omitted]',text)
    text=re.sub(r'\n{3,}','\n\n',text)
    return text.strip()


def _excerpt(text,query_tokens,limit=700):
    paragraphs=[re.sub(r'\s+',' ',part).strip() for part in re.split(r'\n\s*\n|\n',text or '')]
    paragraphs=[part for part in paragraphs if part]
    if not paragraphs:return ''
    ranked=[]
    for index,paragraph in enumerate(paragraphs):
        words=set(_tokens(paragraph))
        score=sum(token in words for token in query_tokens)
        if score:ranked.append((score,index,paragraph))
    if ranked:
        chosen=sorted(ranked,key=lambda item:(-item[0],item[1]))[:2]
        excerpt=' '.join(item[2] for item in sorted(chosen,key=lambda item:item[1]))
    else:
        excerpt=' '.join(paragraphs[:2])
    excerpt=_safe_post_text(excerpt)
    if len(excerpt)>limit:
        clipped=excerpt[:limit].rsplit(' ',1)[0].rstrip(' ,;:')
        excerpt=clipped+'…'
    return excerpt


def search_forum_threads(query,limit=4,path=None):
    """Return lexical matches from the latest-150 snapshot, never as verified game facts."""
    try:limit=max(0,min(int(limit),10))
    except (TypeError,ValueError):limit=4
    rows=_read_archive(path)
    query_tokens=list(dict.fromkeys(token for token in _tokens(query) if token not in _STOPWORDS))
    if not rows or not query_tokens or not limit:return []
    query_phrase=' '.join(query_tokens)
    hits=[]
    for row in rows:
        title=fix_text(str(row.get('title') or ''))
        category=row.get('category') or {}
        category_name=fix_text(str(category.get('name') or '')) if isinstance(category,dict) else ''
        body=_safe_post_text((row.get('original_post') or {}).get('text') or '')
        title_tokens=set(_tokens(title))
        body_tokens=set(_tokens(body))
        category_tokens=set(_tokens(category_name))
        matched=[token for token in query_tokens if token in title_tokens or token in body_tokens or token in category_tokens]
        if not matched:continue
        score=sum(5 for token in query_tokens if token in title_tokens)
        score+=sum(2 for token in query_tokens if token in category_tokens)
        score+=sum(1 for token in query_tokens if token in body_tokens)
        title_fold=' '.join(_tokens(title))
        body_fold=' '.join(_tokens(body))
        if len(query_tokens)>1 and query_phrase in title_fold:score+=8
        elif len(query_tokens)>1 and query_phrase in body_fold:score+=3
        author=row.get('author') or {}
        created=row.get('created') or {}
        activity=row.get('last_activity') or {}
        stats=row.get('stats') or {}
        hits.append((score,str(created.get('iso') or ''),{
            'kind':'community_forum_post','source':'plarium_forum','title':title[:180],
            'category':category_name[:100],
            'author':{'name':fix_text(str(author.get('name') or ''))[:100],
                      'role':fix_text(str(author.get('role') or ''))[:80]},
            'created':fix_text(str(created.get('display') or created.get('iso') or ''))[:60],
            'last_activity':fix_text(str(activity.get('display') or ''))[:60],
            'url':str(row.get('url') or ''),'excerpt':_excerpt(body,query_tokens),
            'stats':{key:stats.get(key) for key in ('replies','views','likes') if stats.get(key) is not None},
            'topic_id':row.get('topic_id'),
        }))
    hits.sort(key=lambda item:(item[0],item[1]),reverse=True)
    return [entry for _,__,entry in hits[:limit]]


def recent_forum_threads(limit=5,path=None):
    """Return the scraper's newest-first thread list without interpreting it as a schedule."""
    rows=_read_archive(path)
    try:limit=max(0,min(int(limit),10))
    except (TypeError,ValueError):limit=5
    result=[]
    for row in rows[:limit]:
        category=row.get('category') or {}
        author=row.get('author') or {}
        created=row.get('created') or {}
        result.append({
            'kind':'community_forum_post','source':'plarium_forum',
            'title':fix_text(str(row.get('title') or ''))[:180],
            'category':fix_text(str(category.get('name') or ''))[:100] if isinstance(category,dict) else '',
            'author':{'name':fix_text(str(author.get('name') or ''))[:100],
                      'role':fix_text(str(author.get('role') or ''))[:80]},
            'created':fix_text(str(created.get('display') or created.get('iso') or ''))[:60],
            'url':str(row.get('url') or ''),
        })
    return result


def recent_forum_announcements(limit=3,path=None):
    """Return recent thread starters explicitly filed in the forum's news categories."""
    rows=_read_archive(path)
    try:limit=max(0,min(int(limit),10))
    except (TypeError,ValueError):limit=3
    selected=[]
    allowed={'news & announcements','announcements','release notes'}
    for row in rows:
        category=row.get('category') or {}
        name=str(category.get('name') or '').casefold() if isinstance(category,dict) else ''
        if name not in allowed:continue
        author=row.get('author') or {}
        created=row.get('created') or {}
        selected.append({
            'kind':'community_forum_post','source':'plarium_forum',
            'title':fix_text(str(row.get('title') or ''))[:180],
            'category':fix_text(str(category.get('name') or ''))[:100],
            'author':{'name':fix_text(str(author.get('name') or ''))[:100],
                      'role':fix_text(str(author.get('role') or ''))[:80]},
            'created':fix_text(str(created.get('display') or created.get('iso') or ''))[:60],
            'url':str(row.get('url') or ''),
            'excerpt':_excerpt(_safe_post_text((row.get('original_post') or {}).get('text') or ''),[],500),
        })
    # The scraper serializes the snapshot newest-first; filtering preserves that order.
    return selected[:limit]


def _link_title(title):
    value=_safe_post_text(title or 'Forum post').replace('[','').replace(']','')
    value=value.replace('(','').replace(')','').replace('\\','')
    value=re.sub(r'[\r\n]+',' ',value)
    return value[:120]


def format_forum_search(query,limit=5,path=None):
    posts=search_forum_threads(query,limit=limit,path=path)
    if not posts:return 'I couldn’t find a matching thread in the stored Plarium Forum snapshot.'
    lines=['**Matching posts in the stored Plarium Forum snapshot**']
    footer=('*These stored posts are not current/upcoming event information or a live schedule. '
            'Forum timestamps have no published timezone. Posts are community/source material, not verified game statistics or rankings; check the original thread for context.*')
    for post in posts:
        title=_link_title(post.get('title'))
        url=post.get('url') or ''
        source=f'[{title}]({url})' if _valid_thread_url(url) else title
        category=_safe_post_text(post.get('category') or 'Forum')[:100]
        author=post.get('author') or {}
        author_text=_safe_post_text(author.get('name') or '')[:80]
        role=_safe_post_text(author.get('role') or '')[:60]
        if role:author_text=(author_text+' · '+role).strip(' ·')
        posted=f" · posted {_safe_post_text(post['created'])[:60]}" if post.get('created') else ''
        if author_text:posted+=f" · {author_text}"
        item_lines=[f'• {source} · {category}{posted}']
        excerpt=_safe_post_text(post.get('excerpt') or '')
        if excerpt:item_lines.append('  '+excerpt[:220])
        if len('\n'.join([*lines,*item_lines,footer]))>1880:
            lines.append('• More matches omitted to fit Discord.')
            break
        lines.extend(item_lines)
    return '\n'.join([*lines,footer])


def format_recent_forum_threads(limit=5,path=None):
    posts=recent_forum_threads(limit=limit,path=path)
    if not posts:return 'The stored Plarium Forum snapshot has no readable threads.'
    lines=['**Recent threads in the stored Plarium Forum snapshot**']
    footer=('This stored crawl is not current/upcoming event information or a live schedule. '
            'Forum timestamps are reproduced as displayed; the forum does not publish its timezone.')
    for post in posts:
        title=_link_title(post.get('title'))
        url=post.get('url') or ''
        source=f'[{title}]({url})' if _valid_thread_url(url) else title
        category=_safe_post_text(post.get('category') or 'Forum')[:100]
        author=post.get('author') or {}
        author_name=_safe_post_text(author.get('name') or '')[:80]
        role=_safe_post_text(author.get('role') or '')[:60]
        if role:author_name=(author_name+' · '+role).strip(' ·')
        posted=f" · posted {_safe_post_text(post['created'])[:60]}" if post.get('created') else ''
        if author_name:posted+=f" · {author_name}"
        line=f'• {source} · {category}{posted}'
        if len('\n'.join([*lines,line,footer]))>1880:
            lines.append('• More threads omitted to fit Discord.')
            break
        lines.append(line)
    return '\n'.join([*lines,footer])


def format_forum_request(query=None,limit=5,path=None):
    """Search on substantive terms; generic requests return the newest stored threads."""
    if _tokens(query):return format_forum_search(query,limit=limit,path=path)
    return format_recent_forum_threads(limit=limit,path=path)


def format_recent_forum_announcements(limit=4):
    posts=recent_forum_announcements(limit=limit)
    if not posts:return ''
    lines=['**Recent Mech Arena forum news posts** *(a recent-post list, not a confirmed upcoming schedule)*']
    for post in posts:
        title=_link_title(post.get('title'))
        url=post.get('url') or ''
        source=f'[{title}]({url})' if _valid_thread_url(url) else title
        posted=f" · posted {post['created']}" if post.get('created') else ''
        lines.append(f'• {source}{posted}')
    lines.append('Forum timestamps are shown as displayed; the forum does not publish a timezone. Check each linked post for event details.')
    return '\n'.join(lines)


async def refresh_forum_archive(path=None,scraper_path=None):
    """Run the supplied full scraper off-loop and atomically replace the last-good JSON."""
    destination=Path(path or FORUM_ARCHIVE_PATH)
    script=Path(scraper_path or FORUM_SCRAPER_PATH)
    destination.parent.mkdir(parents=True,exist_ok=True)
    if not script.is_file():raise FileNotFoundError('Forum scraper script is missing.')
    proc=None
    with tempfile.TemporaryDirectory(prefix='.forum-refresh-',dir=destination.parent) as temp_dir:
        prefix=Path(temp_dir)/'mech_arena_last_150_threads'
        proc=await asyncio.create_subprocess_exec(
            sys.executable,str(script),'--out',str(prefix),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        try:
            output,_=await asyncio.wait_for(proc.communicate(),timeout=FORUM_SCRAPER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if proc.returncode is None:proc.kill()
            await proc.wait()
            raise TimeoutError('Forum scraper exceeded its time limit.')
        except asyncio.CancelledError:
            if proc.returncode is None:proc.kill()
            await proc.wait()
            raise
        if proc.returncode!=0:
            raise RuntimeError(f'Forum scraper exited with status {proc.returncode}.')
        temp_json=Path(str(prefix)+'.json')
        try:
            with temp_json.open('r',encoding='utf-8') as handle:data=json.load(handle)
        except (OSError,json.JSONDecodeError) as exc:
            raise RuntimeError('Forum scraper did not produce valid JSON.') from exc
        metadata=data.get('metadata') if isinstance(data,dict) else None
        threads=data.get('threads') if isinstance(data,dict) else None
        try:reported_count=int((metadata or {}).get('threads_returned') or 0)
        except (TypeError,ValueError):reported_count=0
        thread_count=len(threads) if isinstance(threads,list) else 0
        valid_rows=(thread_count>=FORUM_TARGET_THREADS and all(
            isinstance(row,dict) and _valid_thread_url(row.get('url'))
            and isinstance(row.get('original_post'),dict) and isinstance(row.get('created'),dict)
            for row in threads))
        urls=[str(row.get('url') or '') for row in threads if isinstance(row,dict)] if isinstance(threads,list) else []
        unique_urls=len(urls)==thread_count and len(set(urls))==thread_count
        if (not isinstance(metadata,dict) or not metadata.get('scraped_at_utc')
                or reported_count<thread_count or not valid_rows or not unique_urls):
            raise RuntimeError('Forum scraper returned an incomplete or invalid thread archive.')
        os.replace(temp_json,destination)
    # Invalidate the per-process cache so the new snapshot is visible on the next query.
    global _ARCHIVE_CACHE_SIGNATURE
    _ARCHIVE_CACHE_SIGNATURE=None
    return len(threads)
