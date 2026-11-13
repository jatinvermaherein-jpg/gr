#!/usr/bin/env python3
"""
Scraper for the Mech Arena official forum (https://forum.plarium.com/mech-arena/)

Deliverable: the 150 most recently CREATED threads across ALL English sections,
each with the full original post (OP) and complete thread metadata.

Methodology (why this is accurate):
  1. Category scan  : every EN category listing is fetched page by page (list is
                      sorted newest-activity-first, 20 topics/page, News 10/page).
                      Candidates are collected until a provable stop condition is
                      met: the 150th newest thread's creation date must be NEWER
                      than every unfetched listing boundary (+safety margin), so
                      no thread outside the scanned pages can enter the top 150.
  2. Topic scan     : candidate topic pages are fetched (newest activity first)
                      to read the authoritative OP creation timestamp, author,
                      and full OP content.
  3. Final ranking  : fetched threads are sorted by OP creation time desc;
                      the top 150 are written to XLSX + JSON.

Usage: python3 scrape_mech_arena.py [--dry] [--out PREFIX]
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from ftfy import fix_text
from bs4 import BeautifulSoup, NavigableString, Tag

try:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required: pip install openpyxl")

BASE = "https://forum.plarium.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# EN forum categories (verified against the EN hub https://forum.plarium.com/mech-arena/829_en/)
# Order matters: dedupe keeps the first category a topic is seen in.
CATEGORIES = [
    {"id": 854, "slug": "854_news",               "name": "News & Announcements",       "style": "news"},
    {"id": 702, "slug": "702_game-discussion",    "name": "Game Discussion",            "style": "standard"},
    {"id": 704, "slug": "704_tavern",             "name": "Tavern",                     "style": "standard"},
    {"id": 701, "slug": "701_guides-and-tutorials", "name": "Guides and Tutorials",     "style": "standard"},
    {"id": 703, "slug": "703_feedback-and-suggestions", "name": "Feedback & Suggestions", "style": "standard"},
    {"id": 707, "slug": "707_bug-reports",        "name": "Bug Reports",                "style": "standard"},
    {"id": 709, "slug": "709_violations",         "name": "Forum Rules & Game Policies","style": "standard"},
    {"id": 700, "slug": "700_release-notes",      "name": "Release Notes",              "style": "standard"},
    {"id": 699, "slug": "699_announcements",      "name": "Announcements",              "style": "standard"},
    {"id": 891, "slug": "891_archive",            "name": "Archive",                    "style": "standard"},
]

TARGET = 150
MARGIN_DAYS = 1          # safety margin for listing-boundary stop condition
MARGIN_DAYS_LOOSE = 7    # used if listing sort monotonicity is violated
MAX_ROUNDS = 10

_session_local = threading.local()
_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def sess():
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        _session_local.s = s
    return s


def fetch(url, tries=4):
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = sess().get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                return None
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.2 * attempt)
    log(f"  !! failed {url}: {last}")
    return None


# ---------------------------------------------------------------- date helpers
FULL_FMT = "%b %d, %Y, %H:%M"   # "Nov 11, 2026, 16:59"
SHORT_FMT = "%m/%d/%y"          # "10/17/26"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in (FULL_FMT, SHORT_FMT):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


DATE_FULL_RE = re.compile(r"([A-Z][a-z]{2} \d{1,2}, \d{4}, \d{2}:\d{2})")
DATE_SHORT_RE = re.compile(r"(\d{2}/\d{2}/\d{2})")


def parse_num(s):
    """'1k' -> 1000, '1.5k' -> 1500, '33' -> 33"""
    if s is None:
        return None
    s = s.strip().lower().replace(",", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", s)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(val * mult)


# ---------------------------------------------------------------- html -> text
BLOCK_TAGS = {"p", "div", "section", "article", "table", "thead", "tbody",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "ul", "ol", "hr"}
SKIP_TAGS = {"script", "style", "svg", "button", "noscript"}


def html_to_text(el):
    """Convert a BeautifulSoup element to clean plain text preserving paragraphs."""
    out = []
    state = {"line_start": True}

    def emit(s, q):
        if not s:
            return
        if state["line_start"]:
            if q:
                out.append("> ")
            state["line_start"] = False
        out.append(s)

    def nl(n, q):
        out.append("\n" * n)
        state["line_start"] = True

    def walk(node, q):
        if isinstance(node, NavigableString):
            emit(str(node), q)
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name in SKIP_TAGS:
            return
        if name == "br":
            nl(1, q)
            return
        if name == "img":
            alt = node.get("alt") or node.get("title")
            if alt:
                emit(f"[image: {alt}]", q)
            else:
                emit("[image]", q)
            return
        if name == "blockquote":
            for c in node.children:
                walk(c, q + 1)
            nl(2, q)
            return
        if name == "a":
            href = (node.get("href") or "").strip()
            for c in node.children:
                walk(c, q)
            if href.startswith("http") and node.get_text(strip=True):
                text = node.get_text(" ", strip=True)
                if text != href and not text.startswith(href):
                    emit(f" ({href})", q)
            return
        if name == "li":
            if node.parent and node.parent.name == "ul":
                emit("• ", q)
            for c in node.children:
                walk(c, q)
            nl(1, q)
            return
        for c in node.children:
            walk(c, q)
        if name in BLOCK_TAGS:
            nl(2, q)

    for c in el.children:
        walk(c, 0)

    text = fix_text("".join(out))
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------ listing parsing
def last_page_of(html, slug):
    m = re.search(r'href="/mech-arena/%s/(\d+)"[^>]*title="Last page"' % re.escape(slug), html)
    if m:
        return int(m.group(1))
    m = re.search(r'href="/mech-arena/%s/(\d+)"[^>]*data-qa-entity="pagination' % re.escape(slug), html)
    if m:
        return int(m.group(1))
    # no pagination links -> single page
    if f"/mech-arena/{slug}/1 " not in html and f"/{slug}/1" not in html:
        return 1
    return 1


def _card_stats(card_html):
    st = {}
    # e.g. >Views</span> ... <span class="sc-dwh2h3-1 isYDKA">1k</span>
    m = re.search(r">Views</span>.*?sc-dwh2h3-1 [^\"]*\">([^<]+)<", card_html, re.S)
    st["views_display"] = m.group(1).strip() if m else None
    st["views"] = parse_num(st["views_display"]) if st["views_display"] else None
    m = re.search(r">Comments</span>.*?sc-17s1q3g-1 [^\"]*\">([^<]+)<", card_html, re.S)
    st["comments_display"] = m.group(1).strip() if m else None
    st["comments"] = parse_num(st["comments_display"]) if st["comments_display"] else None
    m = re.search(r">Likes</span>(\d+)<", card_html)
    st["likes"] = int(m.group(1)) if m else None
    m = re.search(r">([A-Z][a-z]{2} \d{1,2}, \d{4}, \d{2}:\d{2})<", card_html)
    if not m:
        m = re.search(r">(\d{2}/\d{2}/\d{2})<", card_html)
    st["last_activity_display"] = m.group(1) if m else None
    st["last_activity"] = parse_date(st["last_activity_display"])
    m = re.search(r"Last modified by this user</span><span class=\"[^\"]*\">([^<]*)</span>", card_html)
    st["last_activity_by"] = m.group(1).strip() if m else None
    return st


def parse_standard_listing(html, cat):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select('[data-qa-entity="topic.preview.card.body"]'):
        main = card.select_one('div[data-qa-entity="topic.preview.card.main"]')
        if not main:
            continue
        title_a = None
        for a in main.select('a[href^="/mech-arena/"]'):
            if a.get_text(strip=True):
                title_a = a
                break
        if not title_a:
            continue
        href = title_a["href"]
        m = re.match(r"^/mech-arena/[^/]+/(\d+)_(.*)/$", href)
        if not m:
            continue
        st = _card_stats(str(card))
        out.append({
            "topic_id": int(m.group(1)),
            "title": fix_text(title_a.get_text(strip=True)),
            "url": BASE + href,
            "category": cat["name"],
            "category_id": cat["id"],
            "category_url": BASE + f"/mech-arena/{cat['slug']}/",
            **st,
        })
    return out


def parse_news_listing(html, cat):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select(f'a[href^="/mech-arena/{cat["slug"]}/"]'):
        href = a.get("href", "")
        m = re.match(r"^/mech-arena/[^/]+/(\d+)_(.*)/$", href)
        if not m or not a.get_text(strip=True):
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        seen.add(tid)
        # climb to the <li> that contains the stats footer
        holder = a
        for _ in range(6):
            if holder is None or holder.name == "li":
                break
            holder = holder.parent
        holder = holder if holder is not None else a.parent
        st = _card_stats(str(holder))
        out.append({
            "topic_id": tid,
            "title": fix_text(a.get_text(strip=True)),
            "url": BASE + href,
            "category": cat["name"],
            "category_id": cat["id"],
            "category_url": BASE + f"/mech-arena/{cat['slug']}/",
            **st,
        })
    return out


def parse_listing(html, cat):
    items = parse_standard_listing(html, cat)
    if items:
        return items
    return parse_news_listing(html, cat)


# ------------------------------------------------------------ topic page parsing
def parse_topic(html, cand):
    """Parse a topic page. Two layouts exist:
      * standard threads : <h2 data-qa-entity="topic.card.title"> + topic.card.content
                           inside the first <article> (header = author + OP time)
      * News & Announces : no topic.card.* markers; h2 + content div
                           (class sc-1q1py7n-0) live in the top section before
                           the first comment article; author link is a profile
                           anchor with name/role spans.
    """
    if 'data-qa-entity="topic.card.title"' in html and \
       'data-qa-entity="topic.card.content"' in html:
        return _parse_topic_standard(html)
    return _parse_topic_news(html)


def _parse_topic_news(html):
    # segment = everything before the first comment (the OP section)
    cut = html.find('data-qa-entity="comment.body"')
    if cut < 0:
        cut = html.find("<article")
    segment = html[:cut] if cut > 0 else html
    seg = BeautifulSoup(segment, "html.parser")

    h2 = seg.find("h2")
    content = None
    if h2 is not None:
        for d in h2.find_all_next("div", class_="sc-1q1py7n-0"):
            content = d
            break
    if content is None:
        content = seg.find("div", class_="sc-1q1py7n-0")
    if h2 is None or content is None:
        return None

    res = {
        "title_on_page": fix_text(h2.get_text(strip=True)),
        "author_name": None,
        "author_profile_url": None,
        "author_avatar_url": None,
        "author_joined": None,
        "author_forum_posts": None,
        "author_role": None,
        "created_display": None,
        "created": None,
        "op_text": "",
        "op_html": "",
    }
    a = seg.find("a", href=re.compile(r"/en/profile/"))
    if a:
        res["author_profile_url"] = BASE + a["href"] if a["href"].startswith("/") else a["href"]
        img = a.find("img")
        if img and img.get("src"):
            res["author_avatar_url"] = img["src"]
        spans = a.find_all("span")
        if spans:
            res["author_name"] = fix_text(spans[0].get_text(strip=True))
        if len(spans) > 1:
            res["author_role"] = fix_text(spans[1].get_text(strip=True))
    seg_text = " ".join(seg.stripped_strings)
    m = DATE_FULL_RE.search(seg_text) or DATE_SHORT_RE.search(seg_text)
    if m:
        res["created_display"] = m.group(1)
        res["created"] = parse_date(m.group(1))
    res["op_html"] = content.decode_contents()
    res["op_text"] = html_to_text(content)
    res["op_word_count"] = len(res["op_text"].split())
    return res


def _parse_topic_standard(html):
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find(attrs={"data-qa-entity": "topic.card.title"})
    content_el = soup.find(attrs={"data-qa-entity": "topic.card.content"})
    if title_el is None or content_el is None:
        return None
    article = title_el.find_parent("article")
    if article is None:
        return None

    res = {
        "title_on_page": fix_text(title_el.get_text(strip=True)),
        "author_name": None,
        "author_profile_url": None,
        "author_avatar_url": None,
        "author_joined": None,
        "author_forum_posts": None,
        "author_role": None,
        "created_display": None,
        "created": None,
        "op_text": "",
        "op_html": "",
    }

    header = article.find("header")
    if header is not None:
        a = header.find("a", href=re.compile(r"/en/profile/"))
        if a:
            res["author_name"] = fix_text(a.get_text(strip=True))
            res["author_profile_url"] = BASE + a["href"] if a["href"].startswith("/") else a["href"]
        else:
            txt = header.get_text(" ", strip=True)
            res["author_name"] = txt.split("|")[0].strip() if txt else None
        htxt = " ".join(header.stripped_strings)
        m = DATE_FULL_RE.search(htxt) or DATE_SHORT_RE.search(htxt)
        if m:
            res["created_display"] = m.group(1)
            res["created"] = parse_date(m.group(1))

    # region between </header> and the title: avatar / joined / post-count
    art_html = str(article)
    tpos = art_html.find('data-qa-entity="topic.card.title"')
    pre = art_html[:tpos] if tpos > 0 else art_html
    hend = pre.find("</header>")
    mid = pre[hend:] if hend > 0 else ""
    m = re.search(r'href="(/en/profile/\d+/)"[^>]*>\s*<img[^>]*src="([^"]+)"', mid, re.S)
    if m and not res["author_profile_url"]:
        res["author_profile_url"] = BASE + m.group(1)
        res["author_avatar_url"] = m.group(2)
    m = re.search(r'src="([^"]+)"', mid)
    if m:
        res["author_avatar_url"] = m.group(1)
    m = re.search(r">(\d{2}/\d{2}/\d{2})<", mid)
    if m:
        res["author_joined"] = m.group(1)
    nums = re.findall(r'sc-1vvctsb-6 [^"]*">([^<]+)</div>', mid)
    for n in nums:
        if n != res.get("author_joined") and n.isdigit():
            res["author_forum_posts"] = int(n)
            break

    res["op_html"] = content_el.decode_contents()
    res["op_text"] = html_to_text(content_el)
    res["op_word_count"] = len(res["op_text"].split())
    return res


# ---------------------------------------------------------------- main scraping
def load_topic(cand):
    html = fetch(cand["url"])
    if html is None:
        return cand["topic_id"], None
    data = parse_topic(html, cand)
    if data is None:
        return cand["topic_id"], None
    thread = {**cand, **data}
    return cand["topic_id"], thread


def to_iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="small test run")
    ap.add_argument("--out", default="mech_arena_last_150_threads")
    args = ap.parse_args()
    target = 150 if not args.dry else 5

    scraped_at_utc = datetime.now(timezone.utc)
    log(f"== Mech Arena forum scraper | target: {target} threads ==")

    # ---------------- Phase A: category listing scan (interleaved rounds)
    cands = {}          # topic_id -> candidate
    cat_state = {}      # slug -> {pages, last_page, last_date, first_date_next..., violations}
    for c in CATEGORIES:
        cat_state[c["slug"]] = {"pages": 0, "last_page": None, "last_date": None,
                                "violations": 0, "count": 0}

    def cat_boundaries():
        """Max displayed date over categories that still have UNFETCHED pages.

        For a fully exhausted category nothing is unfetched, so it must not
        contribute a boundary (otherwise the stop condition could never fire).
        For standard categories the displayed date = last-comment date = list
        sort key; for News (sorted by publication) displayed >= creation, so it
        remains a valid (conservative) upper bound on unfetched creations.
        """
        vals = []
        for c in CATEGORIES:
            st = cat_state[c["slug"]]
            if (st["last_page"] is not None and st["pages"] < st["last_page"]
                    and st["last_date"]):
                vals.append(st["last_date"])
        return max(vals) if vals else datetime.min

    def scan_round(rnd):
        for cat in CATEGORIES:
            st = cat_state[cat["slug"]]
            if st["last_page"] is not None and st["pages"] >= st["last_page"]:
                continue
            url = f"{BASE}/mech-arena/{cat['slug']}/" if rnd == 1 else f"{BASE}/mech-arena/{cat['slug']}/{rnd}"
            html = fetch(url)
            if html is None:
                st["last_page"] = st["pages"]  # cannot fetch -> treat exhausted
                continue
            st["last_page"] = last_page_of(html, cat["slug"])
            if st["pages"] >= st["last_page"]:
                continue
            items = parse_listing(html, cat)
            first_page = st["pages"] == 0
            st["pages"] = rnd
            st["count"] += len(items)
            if items:
                dates = [it["last_activity"] for it in items if it["last_activity"]]
                # Monotonicity check (displayed dates must be newest-first).
                # Only for standard categories: News is sorted by publication
                # date, so its displayed (last-comment) dates legitimately jump.
                if cat["style"] == "standard":
                    if st["last_date"] and dates and dates[0] > st["last_date"]:
                        st["violations"] += 1
                    start = 1
                    # page 1 may open with a pinned thread whose date is older
                    if first_page and len(dates) >= 2 and dates[1] > dates[0]:
                        start = 2
                    for i in range(start, len(dates)):
                        if dates[i] > dates[i - 1]:
                            st["violations"] += 1
                st["last_date"] = dates[-1] if dates else st["last_date"]
            added = 0
            for it in items:
                if it["topic_id"] in cands:
                    cands[it["topic_id"]]["also_in"].append(cat["name"])
                else:
                    it["also_in"] = []
                    cands[it["topic_id"]] = it
                    added += 1
            log(f"  [cat] {cat['name']:<32} page {rnd}/{st['last_page']}: "
                f"{len(items)} items (+{added} new), boundary={st['last_date']}")

    # ---------------- Phase B helpers
    fetched = {}   # topic_id -> thread
    failed = set()  # topic_ids that 404'd or could not be parsed

    def sort_key(c):
        # newest-activity first; candidates with an unparseable listing date are
        # fetched FIRST (datetime.max) so they can never hide below the cutoff.
        return (c.get("last_activity") or datetime.max, c["topic_id"])

    def fetch_op_batch(limit=50):
        queue = [c for tid, c in sorted(cands.items(),
                                        key=lambda kv: sort_key(kv[1]),
                                        reverse=True)
                 if tid not in fetched and tid not in failed]
        batch = queue[:limit]
        if not batch:
            return 0
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(load_topic, c): c for c in batch}
            for f in as_completed(futs):
                tid, thread = f.result()
                if thread:
                    fetched[tid] = thread
                else:
                    failed.add(tid)
        return len(batch)

    def violations_total():
        return sum(st["violations"] for st in cat_state.values())

    def stop_condition_met():
        if len(fetched) < target:
            return False
        by_created = sorted(fetched.values(), key=lambda t: t.get("created") or datetime.min,
                            reverse=True)
        if len(by_created) < target:
            return False
        c150 = by_created[target - 1].get("created")
        if c150 is None:
            return False
        margin = timedelta(days=MARGIN_DAYS_LOOSE if violations_total() else MARGIN_DAYS)
        # activity cutoff among fetched candidates (unfetched cands are older)
        fetched_ids = set(fetched)
        unfetched_dates = [c.get("last_activity") or datetime.max
                           for tid, c in cands.items()
                           if tid not in fetched_ids and tid not in failed]
        f_cut = min(unfetched_dates) if unfetched_dates else datetime.min
        # displayed-date boundary of unfetched listing pages
        b_cut = cat_boundaries()
        upper = max(f_cut, b_cut)
        ok = c150 >= upper + margin
        log(f"  [check] fetched={len(fetched)} cands={len(cands)} "
            f"C{target}={c150} unfetched_upper={upper} margin={margin.days}d -> {'STOP' if ok else 'continue'}")
        return ok

    max_rounds = 1 if args.dry else MAX_ROUNDS

    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        log(f"-- listing round {rounds} --")
        scan_round(rounds)
        if len(cands) >= target:
            # fetch OPs until the provable stop condition holds
            while True:
                if stop_condition_met():
                    break
                if args.dry and len(fetched) >= target + 10:
                    break
                if fetch_op_batch(60) == 0:
                    break
        if stop_condition_met():
            break
        all_done = all(cat_state[c["slug"]]["last_page"] is not None
                       and cat_state[c["slug"]]["pages"] >= cat_state[c["slug"]]["last_page"]
                       for c in CATEGORIES)
        if all_done and len(fetched) + len(failed) >= len(cands):
            log("  all categories exhausted and all candidates fetched")
            break

    # safety: keep fetching remaining candidates until stop condition holds
    if not args.dry and not stop_condition_met():
        log("-- top-up: fetching remaining candidates --")
        while not stop_condition_met():
            if fetch_op_batch(60) == 0:
                break

    # ---------------- Final ranking
    threads = [t for t in fetched.values() if t.get("created")]
    threads_no_date = [t for t in fetched.values() if not t.get("created")]
    threads.sort(key=lambda t: t["created"], reverse=True)
    top = threads[:TARGET]
    log(f"== collected {len(cands)} candidates, fetched {len(fetched)} topic pages, "
        f"ranking {len(top)} threads by OP creation date ==")
    if threads_no_date:
        log(f"  note: {len(threads_no_date)} fetched topics had unparseable creation dates")

    if not top:
        sys.exit("No threads collected — aborting.")

    # sanity output
    log("  newest:", top[0]["created_display"], "|", top[0]["title"][:60])
    log("  oldest in set:", top[-1]["created_display"], "|", top[-1]["title"][:60])

    # ---------------- Write JSON
    meta = {
        "source": "https://forum.plarium.com/mech-arena/",
        "scope": "All English sections (News & Announcements, Game Discussion, Tavern, "
                 "Guides and Tutorials, Feedback & Suggestions, Bug Reports, Forum Rules, "
                 "Release Notes, Announcements, Archive)",
        "definition": "The 150 most recently created threads (by original-post timestamp), "
                      "each with its full original post and complete metadata.",
        "ordering": "newest thread first (rank 1 = most recently created)",
        "timezone_note": "Forum timestamps are reproduced exactly as displayed on the site; "
                         "the forum does not publish its timezone.",
        "scraped_at_utc": scraped_at_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "listing_pages_scanned": {c["name"]: cat_state[c["slug"]]["pages"] for c in CATEGORIES},
        "candidates_from_listings": len(cands),
        "topic_pages_fetched": len(fetched),
        "threads_returned": len(top),
        "stop_condition": "150th newest thread creation date > all unfetched listing boundaries "
                          f"(+{MARGIN_DAYS if not violations_total() else MARGIN_DAYS_LOOSE}-day safety margin)",
        "sort_monotonicity_violations": violations_total(),
        "fields": ["rank", "topic_id", "title", "url", "category", "author", "created",
                   "last_activity", "stats", "original_post"],
    }
    out_threads = []
    for i, t in enumerate(top, 1):
        out_threads.append({
            "rank": i,
            "topic_id": t["topic_id"],
            "title": t["title"],
            "url": t["url"],
            "category": {"name": t["category"], "id": t["category_id"], "url": t["category_url"],
                         "also_in": t.get("also_in") or []},
            "author": {
                "name": t.get("author_name"),
                "profile_url": t.get("author_profile_url"),
                "avatar_url": t.get("author_avatar_url"),
                "joined": t.get("author_joined"),
                "forum_posts": t.get("author_forum_posts"),
                "role": t.get("author_role"),
            },
            "created": {"display": t.get("created_display"), "iso": to_iso(t.get("created"))},
            "last_activity": {
                "display": t.get("last_activity_display"),
                "iso": to_iso(t.get("last_activity")),
                "by": t.get("last_activity_by"),
            },
            "stats": {
                "replies": t.get("comments"),
                "replies_display": t.get("comments_display"),
                "views": t.get("views"),
                "views_display": t.get("views_display"),
                "likes": t.get("likes"),
            },
            "original_post": {
                "text": t.get("op_text", ""),
                "html": t.get("op_html", ""),
                "word_count": t.get("op_word_count", 0),
            },
        })
    json_path = args.out + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"metadata": meta, "threads": out_threads}, f, ensure_ascii=False, indent=2)
    log(f"  wrote {json_path}")

    # ---------------- Write XLSX
    wb = Workbook()
    ws = wb.active
    ws.title = "Last 150 Threads"
    headers = ["#", "Title", "Category", "Author", "Author Joined", "Author Posts",
               "Created (forum time)", "Created (ISO)", "Replies", "Views", "Likes",
               "Last Reply (forum time)", "Last Reply By", "URL", "OP Words",
               "Original Post (full text)"]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="203864")
    head_font = Font(bold=True, color="FFFFFF")
    for ci, _ in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci)
        c.fill = head_fill
        c.font = head_font
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for t in out_threads:
        op_text = ILLEGAL_CHARACTERS_RE.sub("", t["original_post"]["text"])
        if len(op_text) > 32000:
            op_text = op_text[:32000] + "\n…[truncated in Excel — full text in JSON]"
        ws.append([
            t["rank"], t["title"], t["category"]["name"], t["author"]["name"],
            t["author"]["joined"], t["author"]["forum_posts"],
            t["created"]["display"], t["created"]["iso"],
            t["stats"]["replies"], t["stats"]["views"], t["stats"]["likes"],
            t["last_activity"]["display"], t["last_activity"]["by"],
            t["url"], t["original_post"]["word_count"], op_text,
        ])

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(out_threads) + 1}"
    widths = [4, 55, 24, 18, 13, 12, 21, 18, 9, 9, 8, 21, 18, 70, 10, 90]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=len(headers)):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=(c.column in (2, 16)))
    for r in range(2, len(out_threads) + 2):
        ws.row_dimensions[r].height = 80

    # Summary sheet
    sm = wb.create_sheet("Summary & Methodology")
    rows = [
        ("Mech Arena forum — last 150 threads (with full original posts)", ""),
        ("Source", meta["source"]),
        ("Scope", meta["scope"]),
        ("Definition", meta["definition"]),
        ("Ordering", meta["ordering"]),
        ("Timestamps", meta["timezone_note"]),
        ("Scraped at", meta["scraped_at_utc"]),
        ("", ""),
        ("Listing pages scanned", ", ".join(f"{k}: {v}" for k, v in meta["listing_pages_scanned"].items())),
        ("Candidate threads found in listings", len(cands)),
        ("Topic pages fetched", len(fetched)),
        ("Threads returned", len(top)),
        ("Stop condition", meta["stop_condition"]),
        ("Sort monotonicity violations", violations_total()),
        ("", ""),
        ("Rank 1 (newest)", f"{top[0]['created_display']} — {top[0]['title']}"),
        ("Rank 150 (oldest in set)", f"{top[-1]['created_display']} — {top[-1]['title']}"),
        ("", ""),
        ("Full machine-readable data", args.out + ".json (includes OP HTML + metadata)"),
    ]
    for a, b in rows:
        sm.append([a, b])
    sm["A1"].font = Font(bold=True, size=14)
    sm.column_dimensions["A"].width = 38
    sm.column_dimensions["B"].width = 110
    for row in sm.iter_rows(min_row=1, max_row=sm.max_row, max_col=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    xlsx_path = args.out + ".xlsx"
    wb.save(xlsx_path)
    log(f"  wrote {xlsx_path}")
    log("== DONE ==")


if __name__ == "__main__":
    main()
