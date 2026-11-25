"""The public page served alongside the data API: the same rows, read with your eyes instead of curl.

Pure rendering — it takes the blocks `DataApi.view()` assembled and returns one HTML string. No judgements
live here on purpose: every number, date and standing it prints was derived by `mechbot.facts` and is the
same value the JSON route returns, so the page cannot tell a different story from the API. The Discord post
text and the forum posts are escaped and printed verbatim inside `<details>` blocks, because a page that
re-typed the game's announcement would be a worse source than the announcement.

No JavaScript, no external fonts, no build step: it is one response from the same process that answers the
JSON, and it still renders when a network is unavailable.
"""

import html

VERBATIM_NOTE = ('Everything in a folded block is the original text, byte for byte, as it was stored. '
                 'Everything outside one is the bot’s own reading of it, labelled as such.')

_STATE_CLASS = {'live': 'live', 'outdated': 'stale', 'unknown': 'unknown'}
_ADVICE_LABEL = {'worth_it': 'the guide says it is worth it', 'skip': 'the guide says skip',
                 'depends': 'the guide makes it conditional', None: 'the guide says nothing either way'}


def e(value):
    """Escape, and turn nothing into an empty string rather than the word “None”."""
    if value is None:
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    return html.escape(str(value), quote=True)


def _pill(value, kind=''):
    return f'<span class="pill {kind}">{e(value)}</span>' if value else ''


def _counts(blocks):
    counts = blocks.get('counts') or {}
    order = (('announcements', 'saved posts'), ('facts', 'curated facts'), ('ecr', 'ECR posts'),
             ('events', 'dated events'), ('forum_threads', 'forum threads'), ('ecr_history', 'tracker rows'),
             ('rotations', 'rotations'))
    tiles = ''.join(f'<div class="k"><b>{e(counts.get(key))}</b><span>{e(label)}</span></div>'
                    for key, label in order if key in counts)
    return f'<div class="cards">{tiles}</div>' if tiles else ''


def _ecr(blocks):
    rows = blocks.get('ecr') or []
    if not rows:
        return ('<section><h2>Current ECR</h2><p class="empty">Nothing stored yet. This page fills itself '
                'as the bot reads the announcement channels; it is never seeded with a guess.</p></section>')
    out = []
    for row in rows:
        items = []
        for item in (row.get('items') or []):
            bullets = ''.join(f'<li>{e(bullet)}</li>' for bullet in (item.get('bullets') or []))
            quote = item.get('advice_quote')
            verdict = _pill(_ADVICE_LABEL.get(item.get('advice')),
                            'go' if item.get('advice') == 'worth_it' else
                            'no' if item.get('advice') == 'skip' else 'mid')
            items.append(f'''<div class="item">
<div class="ih"><b>{e(item.get('label'))}</b>{verdict}</div>
{f'<p class="note">{e(item.get("note"))}</p>' if item.get('note') else ''}
{f'<ul>{bullets}</ul>' if bullets else ''}
{f'<details><summary>the guide’s own words behind that</summary><blockquote>{e(quote)}</blockquote></details>' if quote else ''}
</div>''')
        codes = ''.join(f'<code>{e(code)}</code>' for code in (row.get('codes') or []))
        merged = row.get('merged_attachment_messages') or []
        out.append(f'''<article class="ecr">
<header><h3>{e(row.get('title'))}</h3>
<div class="meta">{_pill(row.get('standing').split(' — ')[0] if row.get('standing') else None,
                         _STATE_CLASS.get(row.get('state'), 'unknown'))}
<span>{e(row.get('ecr_kind'))} · posted {e(row.get('posted_at_date') or row.get('posted_at_utc'))} · {e(row.get('age'))}</span>
{f'<span>status {e(row.get("status"))}: {e(row.get("status_basis"))}</span>' if row.get('status_basis') else ''}
{f'<a href="{e(row.get("message_url"))}" rel="noopener">original post ↗</a>' if row.get('message_url') else ''}
</div></header>
{f'<div class="items">{"".join(items)}</div>' if items else '<p class="empty">No item blocks in this post.</p>'}
{f'<div class="codes"><b>codes in the post</b>{codes}</div>' if codes else ''}
{f'<p class="why">{e(row.get("standing"))}</p>' if row.get('standing') else ''}
{f'<p class="why">{len(merged)} extra image-only message(s) were merged into this post before it was read.</p>' if merged else ''}
</article>''')
    return f'<section><h2>Current ECR <span>one block per featured item, in the guide’s words</span></h2>{"".join(out)}</section>'


def _events(blocks):
    rows = blocks.get('events') or []
    if not rows:
        return ''
    body = ''.join(f'''<tr><td><b>{e(row.get('title'))}</b><div class="mut">{e(row.get('category'))}
{f' · {e(row.get("channel_name"))}' if row.get('channel_name') else ''}
{f' · <a href="{e(row.get("message_url"))}" rel="noopener">post ↗</a>' if row.get('message_url') else ''}</div></td>
<td>{e(row.get('event_date_label')) or '<span class="mut">no date stated</span>'}
{f'<div class="mut">{e(row.get("date_note"))}</div>' if row.get('date_note') else ''}</td>
<td>{e((row.get('window') or {}).get('label')) or '<span class="mut">—</span>'}
{_pill((row.get('window') or {}).get('state'), 'live' if (row.get('window') or {}).get('state') == 'running' else 'mid') if row.get('window') else ''}</td>
<td>{e(row.get('standing'))}</td></tr>''' for row in rows)
    return (f'<section><h2>Events and patches <span>a window appears only when the post stated a year</span>'
            f'</h2><table><thead><tr><th>what</th><th>when the post said</th><th>window</th>'
            f'<th>how current</th></tr></thead><tbody>{body}</tbody></table></section>')


def _announcements(blocks):
    rows = blocks.get('announcements') or []
    if not rows:
        return ''
    out = []
    for row in rows:
        content = str(row.get('content') or '')
        out.append(f'''<div class="post"><details>
<summary><b>{e(row.get('channel_name') or 'announcement')}</b>
<span>{e(row.get('posted_at_date') or row.get('posted_at_utc'))} · {e(row.get('content_characters'))} characters
· message {e(row.get('message_id'))}</span>
{f'<a href="{e(row.get("message_url"))}" rel="noopener" onclick="event.stopPropagation()">on Discord ↗</a>' if row.get('message_url') else ''}
</summary>
<pre>{e(content)}</pre>
</details></div>''')
    return (f'<section><h2>Announcement originals <span>{len(rows)} newest, unfolded they are the post itself'
            f'</span></h2>{"".join(out)}</section>')


def _forum(blocks):
    rows = blocks.get('forum') or []
    if not rows:
        return ''
    out = []
    for row in rows:
        text = str(((row.get('original_post') or {}).get('text')) or '')
        out.append(f'''<tr><td><details><summary><b>{e(row.get('title'))}</b>
<div class="mut">{e(((row.get('category') or {}).get('name')) or '')} · by {e(((row.get('author') or {}).get('name')) or '')}
· {e(((row.get('created') or {}).get('display')) or '')}</div></summary>
<pre>{e(text) or '（no original post text in the crawl）'}</pre>
</details></td>
<td class="n">{e(row.get('replies'))}<span class="mut">replies</span></td>
<td class="n">{e(row.get('views'))}<span class="mut">views</span></td>
<td class="p">{f'<a href="{e(row.get("url"))}" rel="noopener">thread ↗</a>' if row.get('url') else ''}</td></tr>''')
    meta = blocks.get('forum_meta') or {}
    parts = []
    if meta.get('first_created') and meta.get('last_created'):
        parts.append(f"created {meta['first_created']} → {meta['last_created']}")
    if meta.get('scraped_at_utc'):
        parts.append(f"scraped {meta['scraped_at_utc']}")
    if meta.get('threads_in_file'):
        parts.append(f"{meta['threads_in_file']} threads in the snapshot")
    window = f'<p class="why">Crawl window: {e("; ".join(parts))}.</p>' if parts else ''
    return (f'<section><h2>Plarium forum <span>newest threads the bot has, with their original first post</span>'
            f'</h2><table class="forum"><thead><tr><th>thread</th><th>replies</th><th>views</th>'
            f'<th>link</th></tr></thead><tbody>{"".join(out)}</tbody></table>{window}</section>')


def render(blocks):
    """One HTML document. `blocks` keys: counts, ecr, events, announcements, forum, ingestion, caveats, links."""
    counts = blocks.get('counts') or {}
    tiles = _counts(blocks)
    ingest = ''.join(f'<li><b>{e(key)}</b> {e(value)}</li>' for key, value in (blocks.get('ingestion') or {}).items())
    caveats = ''.join(f'<li>{e(caveat)}</li>' for caveat in (blocks.get('caveats') or []))
    links = blocks.get('links') or {}
    nav = ''.join(f'<a href="{e(href)}">{e(label)}</a>' for label, href in links.items() if href)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mech Arena · everything the bot has</title>
<meta name="description" content="Original announcement posts, ECR guide readings, dated events and the Plarium forum crawl, read-only.">
<style>
:root{{color-scheme:dark;--bg:#080918;--tx:#f1f0ff;--mu:#aeacd0;--ln:#ffffff26;--a:#8b7bff;--b:#3ee0ff;--c:#ff7ad9;
--ok:#5ce39a;--no:#ff8fa3}}
*{{box-sizing:border-box}}body{{margin:0;color:var(--tx);min-height:100vh;background:
radial-gradient(120vw 60vw at 12% -10%,#8b7bff2e,transparent 60%),
radial-gradient(90vw 50vw at 90% 6%,#3ee0ff22,transparent 60%),var(--bg);
font:400 15px/1.62 Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.w{{max-width:1080px;margin:0 auto;padding:clamp(26px,5vw,64px) max(18px,4vw) 60px}}
h1{{font:700 clamp(28px,6vw,58px)/1.03 'Space Grotesk',ui-sans-serif,sans-serif;letter-spacing:-.03em;margin:0}}
h1 b{{color:var(--a)}}.sub{{color:var(--mu);max-width:70ch;margin:12px 0 8px}}
.top{{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;color:var(--mu);font-size:12px;
font-family:ui-monospace,'JetBrains Mono',monospace}}
.top a{{color:var(--b);text-decoration:none}}.top a:hover{{text-decoration:underline}}
nav{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 26px;font:500 12px/1 ui-monospace,monospace;
letter-spacing:.6px;text-transform:uppercase}}nav a{{color:var(--tx);text-decoration:none;padding:7px 12px;
border:1px solid var(--ln);border-radius:999px;background:#ffffff0d}}nav a:hover{{border-color:var(--b);color:var(--b)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:9px;margin:0 0 8px}}
.k{{padding:13px 15px;border:1px solid var(--ln);border-radius:15px;background:#ffffff0f}}
.k b{{display:block;font:700 24px/1 'Space Grotesk',ui-sans-serif,sans-serif;color:var(--b)}}
.k span{{color:var(--mu);font-size:11.5px;letter-spacing:.3px}}
section{{margin:44px 0 0}}
h2{{font:600 clamp(19px,3.4vw,26px)/1.2 'Space Grotesk',ui-sans-serif,sans-serif;margin:0 0 4px;letter-spacing:-.015em}}
h2 span{{display:block;font:400 12.5px/1.5 Inter,sans-serif;color:var(--mu);letter-spacing:0;margin-top:5px}}
.ecr{{border:1px solid var(--ln);border-radius:18px;padding:18px 20px;margin:14px 0;background:#ffffff0b}}
.ecr h3{{margin:0 0 8px;font:600 17px/1.3 'Space Grotesk',ui-sans-serif,sans-serif}}
.meta{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;color:var(--mu);font-size:12.5px}}
.meta a{{color:var(--b);text-decoration:none;font-family:ui-monospace,monospace;font-size:12px}}
.pill{{font:600 11px/1 ui-monospace,monospace;letter-spacing:.7px;text-transform:uppercase;padding:5px 9px;
border-radius:999px;border:1px solid var(--ln);color:var(--mu)}}
.pill.live{{color:var(--ok);border-color:#5ce39a55}}.pill.stale{{color:var(--no);border-color:#ff8fa355}}
.pill.go{{color:var(--ok);border-color:#5ce39a55}}.pill.no{{color:var(--no);border-color:#ff8fa355}}
.pill.mid,.pill.unknown{{color:var(--mu)}}
.items{{display:grid;gap:10px;margin:14px 0 0}}
.item{{border-left:2px solid var(--a);padding:2px 0 2px 14px}}
.item .ih{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.item b{{font:600 14px/1.4 'Space Grotesk',ui-sans-serif,sans-serif;letter-spacing:.02em}}
.item ul{{margin:7px 0 0;padding-left:17px;color:var(--tx);font-size:13.5px}}.item li{{margin:2px 0}}
.note{{margin:6px 0 0;color:var(--mu);font-size:13px}}
blockquote{{margin:8px 0 0;padding:10px 12px;border:1px dashed var(--ln);border-radius:12px;color:var(--mu);
font-size:13px;background:#00000033}}
.codes{{margin:12px 0 0;display:flex;gap:7px;align-items:center;flex-wrap:wrap;font-size:11px;color:var(--mu);
text-transform:uppercase;letter-spacing:.7px}}.codes code{{font:600 12.5px/1 ui-monospace,monospace;color:var(--c);
background:#ffffff12;border:1px solid var(--ln);border-radius:8px;padding:5px 8px;letter-spacing:0;text-transform:none}}
.why{{margin:12px 0 0;color:var(--mu);font-size:12.5px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:12px}}
th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--ln);vertical-align:top}}
th{{font:500 10px/1 ui-monospace,monospace;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu)}}
td a{{color:var(--b);text-decoration:none;font-family:ui-monospace,monospace;font-size:12.5px}}
td a:hover{{text-decoration:underline}}.n{{text-align:right;white-space:nowrap;color:var(--a);
font-family:ui-monospace,monospace}}.n span{{display:block;color:var(--mu);font-size:10.5px;letter-spacing:.4px}}
.p{{white-space:nowrap}}.mut{{color:var(--mu);font-size:12px;font-weight:400}}
.post details,.forum details{{border:1px solid var(--ln);border-radius:14px;background:#ffffff08;margin:8px 0}}
.post summary,.forum summary{{cursor:pointer;padding:12px 14px;list-style:none}}
.post summary::-webkit-details-marker,.forum summary::-webkit-details-marker{{display:none}}
.post summary{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.post summary b{{font:600 14px/1.4 'Space Grotesk',ui-sans-serif,sans-serif}}
.post summary span{{color:var(--mu);font-size:12px;font-family:ui-monospace,monospace}}
.post summary a{{color:var(--b);font:12px ui-monospace,monospace;text-decoration:none}}
.forum details summary{{padding:9px 12px}}.forum details[open]{{background:#ffffff0d}}
pre{{margin:0;padding:14px 16px;border-top:1px solid var(--ln);white-space:pre-wrap;word-break:break-word;
font:400 12.5px/1.6 ui-monospace,'JetBrains Mono',monospace;color:#dcdaf5}}
.empty{{color:var(--mu);font-size:13.5px;border:1px dashed var(--ln);border-radius:14px;padding:14px 16px;margin:12px 0 0}}
.note2{{margin:14px 0 0;padding:16px 18px;border:1px solid var(--ln);border-radius:16px;background:#ffffff0d}}
.note2 h4{{margin:0 0 8px;font:600 11px/1 ui-monospace,monospace;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu)}}
.note2 ul{{margin:0;padding-left:17px;color:var(--mu);font-size:12.5px}}.note2 li{{margin:5px 0}}
.foot{{margin-top:34px;padding-top:16px;border-top:1px solid var(--ln);color:var(--mu);font-size:11.5px;
font-family:ui-monospace,'JetBrains Mono',monospace;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
@media (prefers-reduced-motion:no-preference){{.post details{{transition:background .2s}}}}
</style></head><body><div class="w">
<h1>Mech Arena <b>·</b> what the game actually said</h1>
<p class="sub">Every announcement post, ECR guide, dated event and forum thread the bot has collected — the
originals, with the bot’s own reading kept in separate labelled fields. {e(VERBATIM_NOTE)}</p>
<div class="top"><span>as of {e(blocks.get('as_of_utc'))}</span><span>revision {e(blocks.get('revision'))}</span>
<span>read-only</span>{'<span>no player data is served here</span>'}</div>
{tiles}
{f'<nav>{nav}</nav>' if nav else ''}
{_ecr(blocks)}
{_events(blocks)}
{_announcements(blocks)}
{_forum(blocks)}
{f'<div class="note2"><h4>collector state</h4><ul>{ingest}</ul></div>' if ingest else ''}
{f'<div class="note2"><h4>what these numbers do and do not mean</h4><ul>{caveats}</ul></div>' if caveats else ''}
<div class="foot"><span>{e(counts.get('database') or 'sqlite')} backend · GET only</span>
<span>machine-readable: {e(links.get('Data API') or blocks.get('api_path') or '/api/mech-arena/public')}</span></div>
</div></body></html>"""
