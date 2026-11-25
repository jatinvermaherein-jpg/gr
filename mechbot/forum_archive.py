"""Search and refresh the user-provided recent Plarium Forum thread archive."""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from ftfy import fix_text
from .text_summaries import clean_source_text, summarize_source_text

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
            'created_iso':fix_text(str(created.get('iso') or ''))[:40],
            'last_activity':fix_text(str(activity.get('display') or ''))[:60],
            'url':str(row.get('url') or ''),'excerpt':_excerpt(body,query_tokens),
            'content':body[:6000],
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
            'created_iso':fix_text(str(created.get('iso') or ''))[:40],
            'url':str(row.get('url') or ''),
            'content':_safe_post_text((row.get('original_post') or {}).get('text') or '')[:6000],
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


def _post_date_label(post):
    """Use the forum thread's creation date, not its last-activity date or scrape time."""
    created=str(post.get('created_iso') or post.get('created') or '').strip()
    match=re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b',created)
    if match:
        try:
            parsed=date(*map(int,match.groups()))
        except ValueError:
            parsed=None
    else:
        parsed=None
    if parsed is None:
        match=re.search(r'\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b',created)
        if match:
            for month_format in ('%b %d %Y','%B %d %Y'):
                try:
                    parsed=datetime.strptime(f'{match[1]} {match[2]} {match[3]}',month_format).date()
                    break
                except ValueError:
                    continue
    return f'{parsed.strftime("%b")} {parsed.day}, {parsed.year}' if parsed else 'date not recorded'


def _link_title(title):
    value=clean_source_text(title or 'Forum post',limit=180).replace('[','').replace(']','')
    value=re.sub(r'[\r\n]+',' ',value)
    return value[:120]


def _forum_summary(post,query=''):
    title=_link_title(post.get('title') or 'Forum post')
    content=post.get('content') or post.get('excerpt') or ''
    cleaned=clean_source_text(content)
    if title and cleaned.casefold().startswith(title.casefold()):
        cleaned=cleaned[len(title):].lstrip(' :—–-')
    terms=[token for token in _tokens(query) if len(token)>2]
    return summarize_source_text(cleaned or content,query_terms=terms,max_chars=290,max_sentences=2)


# A thread is about a moment in the game. A "worst mech" list from last year, or a bug report
# against a version four patches old, is not a statement about now - and the reply used to print
# every saved thread with no age on it at all, so an eleven-month-old thread read as today's
# community opinion. The bands are the ones a player would use out loud.
_FRESH_DAYS = 21
_AGING_DAYS = 90


def _thread_age_days(post,now=None):
    """Days since the thread was created, or None when the snapshot has no readable date."""
    created=post.get('created_iso')
    if not created:
        # The raw archive row nests the date; the flattened thread dict does not. Both reach here.
        created=(post.get('created') or {}).get('iso') if isinstance(post.get('created'),dict) else post.get('created')
    created=str(created or '').strip()
    match=re.match(r'^(20\d{2})-(\d{1,2})-(\d{1,2})',created)
    if not match:
        return None
    try:
        stamp=date(*map(int,match.groups()))
    except ValueError:
        return None
    today=(now or datetime.now(timezone.utc)).date() if not isinstance(now,date) else now
    return max(0,(today-stamp).days)


def _is_live(post,now=None):
    """True while the thread still speaks for the game as it is now."""
    age=_thread_age_days(post,now)
    return age is not None and age<=_FRESH_DAYS


def _freshness(post,now=None):
    """(label, is_live) for one thread: how the reply should describe its age."""
    age=_thread_age_days(post,now)
    if age is None:
        return 'date not recorded', False
    if age==0:
        return 'today', True
    if age==1:
        return 'yesterday', True
    if age<=_FRESH_DAYS:
        return f'{age} days ago', True
    if age<=_AGING_DAYS:
        return f'{age} days ago', False
    months=age//30
    if months<24:
        return f'{months} month{"s" if months>1 else ""} ago', False
    years=age//365
    return f'{years} year{"s" if years>1 else ""} ago', False


def _outdated_note(post,now=None):
    """What the reply says about a thread that no longer describes the live game."""
    label,_live=_freshness(post,now)
    if label in ('today','yesterday','date not recorded'):
        return ''
    age=_thread_age_days(post,now)
    if age is None:
        return ''
    if age<=_AGING_DAYS:
        return f'Saved post, {label} — still recent, but a later patch may have changed it.'
    return (f'Saved post, {label} — treat it as an archived discussion, not as current '
            f'state of the game.')


def _author_name(post):
    """The author's name only: the reply never prints roles or links back to the forum."""
    author=post.get('author') or {}
    return fix_text(str(author.get('name') or '')).strip()


def _poster(post):
    author=post.get('author') or {}
    name=fix_text(str(author.get('name') or '')).strip()
    role=fix_text(str(author.get('role') or '')).strip()
    if role and role.casefold() not in ('member',''):
        return f'{name} ({role})' if name else role
    return name or 'unknown author'


def _thread_meta(post):
    """Written by, and what the thread got: the two facts that say how much weight it carries."""
    stats=post.get('stats') or {}
    parts=[f'**Posted by:** {_poster(post)}']
    if isinstance(stats,dict) and stats.get('replies') is not None:
        parts.append(f'{int(stats["replies"])} repl{"y" if int(stats["replies"])==1 else "ies"}')
    return parts


def _thread_block(number,post,query='',now=None):
    """One saved thread as its own numbered block: the post, who wrote it, and whether it is live.

    The forum answer used to be one bullet per thread with the summary, the date and the
    category run together on a single line - the reader had to parse the line to find out
    whether they were looking at last week's discussion or last year's.
    """
    title=_link_title(post.get('title')) or 'Forum discussion'
    summary=_forum_summary(post,query)
    category=fix_text(str(post.get('category') or '')).strip()
    label,live=_freshness(post,now)
    lines=[f'{number}. **{title}**']
    written=' · '.join(part for part in (
        f'Posted by {_author_name(post)}' if _author_name(post) else '',
        category,
        f'**Post date:** {_post_date_label(post)} ({label})',
    ) if part)
    if written:
        lines.append(written)
    if summary:
        lines.append(f'\u2022 {summary}')
    note=_outdated_note(post,now)
    stakes=_stakes(post)
    if stakes:
        lines.append(f'**Status:** {stakes}')
    elif note:
        lines.append(f'**Status:** {note}')
    elif live:
        lines.append('**Status:** Current discussion — posted inside the last '
                     f'{_FRESH_DAYS} days.')
    return '\n'.join(lines), live


# Sentences that make a post a report about the game as it is right now, rather than an opinion.
_LIVE_CLAIM=re.compile(r'\b(?:is|are)\s+(?:currently|now)\b|\bcurrently\b|\bright\s+now\b|'
                       r'\b(?:today|this\s+week|this\s+patch|this\s+update)\b|\bjust\s+(?:got|bought|tried)\b|'
                       r'\b(?:after|since)\s+the\s+(?:latest|new|last)\s+(?:patch|update|rebalance)\b',re.I)
# Sentences that tie a post to a moment in the past, which is what makes it a dated archive.
_DATED_CLAIM=re.compile(r'\b(?:back\s+when|used\s+to|last\s+(?:year|season)|old\s+version|'
                        r'before\s+the\s+(?:nerf|buff|rebalance)|no\s+longer)\b',re.I)


def _states_current_claim(post):
    """Does the thread make a claim about the live game, in its own words?"""
    body=_safe_post_text((post.get('content') or post.get('excerpt') or ''))
    return bool(_LIVE_CLAIM.search(body or '')) and not bool(_DATED_CLAIM.search(body or ''))


def _stakes(post,now=None):
    """A one-line judgement of whether the thread still describes the live game."""
    age=_thread_age_days(post,now)
    if age is None:
        return 'Date not recorded in the saved snapshot, so its age cannot be vouched for.'
    if age<=7:
        return f'Live discussion — posted {_freshness(post,now)[0]}.'
    if age<=_FRESH_DAYS:
        return (f'Live discussion — posted {age} days ago, inside the window a patch usually '
                f'replaces.')
    if _states_current_claim(post):
        return (f'Outdated — posted {_freshness(post,now)[0]}, and it argues about the game as '
                f'it stood then. Later patches are not reflected in it.')
    return (f'Outdated — posted {_freshness(post,now)[0]}, so it is an archived discussion '
            f'rather than a statement about the current build.')


def _snapshot_state(rows,now=None):
    """One line on how old the whole snapshot is, so the answer never implies it is live."""
    newest=rows[0] if rows else {}
    age=_thread_age_days(newest,now)
    if age is None:
        return 'The saved snapshot carries no readable thread dates.'
    if age<=1:
        return f'The snapshot was refreshed with threads from {_freshness(newest,now)[0]}.'
    return (f'The newest saved thread is {_freshness(newest,now)[0]}; anything newer than that '
            f'is not in the snapshot.')


def format_forum_search(query,limit=5,path=None,now=None):
    posts=search_forum_threads(query,limit=limit,path=path)
    if not posts:
        return 'I couldn’t find a matching discussion in the saved forum posts.'
    return _forum_reply(posts,f'**Saved forum discussions matching “{_link_title(query)}”**',
                        query=query,now=now,path=path)


def format_recent_forum_threads(limit=5,path=None,now=None):
    posts=recent_forum_threads(limit=limit,path=path)
    if not posts:
        return 'The saved forum snapshot has no readable threads.'
    return _forum_reply(posts,'**Recent saved forum highlights**',query='',now=now,path=path)


def _forum_reply(posts,heading,query='',now=None,path=None):
    """Numbered per-thread blocks, then one roll-up line that separates live from outdated."""
    blocks,flags=[],[]
    for index,post in enumerate(posts,start=1):
        text,live=_thread_block(index,post,query=query,now=now)
        candidate='\n'.join([*blocks,text])
        if len(candidate)>3400:
            blocks.append('• More matching discussions omitted.')
            break
        blocks.append(text)
        flags.append((_link_title(post.get('title')) or f'Thread {index}',live))
    lines=[heading,'\n\n'.join(blocks)]
    if flags:
        live=[title for title,live in flags if live]
        stale=[title for title,live in flags if not live]
        rollup=['**LIVE OR OUTDATED**','Thread | Standing']
        rollup.extend(f'{title} | {"Live" if live else "Outdated"}' for title,live in flags)
        lines.append('\n'.join(rollup))
        if live and stale:
            lines.append(f'{len(live)} of {len(flags)} threads '
                         f'{"still speaks" if len(live)==1 else "still speak"} for the current '
                         f'build; the rest are archive.')
        elif not live:
            lines.append('None of these threads is recent enough to describe the current build.')
    rows=_read_archive(path)
    if rows:
        lines.append(_snapshot_state(rows,now=now))
    lines.append('These are community discussions, not confirmed game guidance or a live event '
                 'schedule.')
    return '\n\n'.join(lines)


def format_forum_request(query=None,limit=5,path=None,now=None):
    """Search on substantive terms; generic requests return concise summaries of recent threads."""
    if _tokens(query):return format_forum_search(query,limit=limit,path=path,now=now)
    return format_recent_forum_threads(limit=limit,path=path,now=now)


def format_recent_forum_announcements(limit=4):
    posts=recent_forum_announcements(limit=limit)
    if not posts:return ''
    lines=['**Recent forum news highlights** *(not a confirmed upcoming schedule)*']
    for post in posts:
        title=_link_title(post.get('title')) or 'News post'
        summary=_forum_summary(post)
        line=f'• **{title}** — {summary or "No additional readable details were saved."}'
        line+=f' · **Post date:** {_post_date_label(post)}'
        if len('\n'.join(lines+[line]))>1880:
            lines.append('• More news posts omitted.')
            break
        lines.append(line)
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
        metadata_valid=isinstance(metadata,dict)
        metadata_fields=metadata if metadata_valid else {}
        try:
            reported_count=int(metadata_fields.get('threads_returned') or 0)
            target_count=int(metadata_fields.get('target_threads') or 0)
            listing_failures=int(metadata_fields.get('listing_pages_failed') or 0)
        except (TypeError,ValueError):
            reported_count=target_count=listing_failures=0
        thread_count=len(threads) if isinstance(threads,list) else 0
        valid_rows=(isinstance(threads,list) and thread_count==FORUM_TARGET_THREADS and all(
            isinstance(row,dict) and _valid_thread_url(row.get('url'))
            and isinstance(row.get('original_post'),dict)
            and isinstance(row.get('created'),dict)
            and bool(row.get('created',{}).get('iso'))
            for row in threads))
        urls=[str(row.get('url') or '') for row in threads if isinstance(row,dict)] if isinstance(threads,list) else []
        topic_ids=[str(row.get('topic_id') or '') for row in threads if isinstance(row,dict)] if isinstance(threads,list) else []
        unique_urls=len(urls)==thread_count and len(set(urls))==thread_count
        unique_topics=(all(topic_ids) and len(topic_ids)==thread_count and len(set(topic_ids))==thread_count)
        if (not isinstance(metadata,dict) or not metadata.get('scraped_at_utc')
                or metadata.get('crawl_complete') is not True
                or target_count!=FORUM_TARGET_THREADS or reported_count!=thread_count
                or listing_failures!=0 or not valid_rows or not unique_urls or not unique_topics):
            raise RuntimeError('Forum scraper returned an incomplete or invalid thread archive.')
        os.replace(temp_json,destination)
    # Invalidate the per-process cache so the new snapshot is visible on the next query.
    global _ARCHIVE_CACHE_SIGNATURE
    _ARCHIVE_CACHE_SIGNATURE=None
    return len(threads)
