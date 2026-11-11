"""Routing, verified lookup, safe presentation and optional general AI."""
import re
import aiohttp

GAME_WORDS = re.compile(r'\b(mech(?:s|\s*arena)?|weapon(?:s)?|loadout|hangar|gear\s*hub|pilot(?:s)?|implant(?:s)?|a-coins|battle\s*rating|damage|dps)\b', re.I)


def safe_text(s, length=500):
    s = str(s).replace('@', '@\u200b').replace('```', "''' ")
    return s[:length]


def wants_invite_link(text):
    """Recognize direct requests for the bot's invite link."""
    value=str(text)
    if re.search(r'\b(?:invite|invitation)\b',value,re.I):return True
    return bool(re.search(r'\badd\b',value,re.I) and re.search(r'\b(?:bot|mech\s*arena|server|discord|you)\b',value,re.I))


def find_mentions(text, names):
    # Match exact names, longest first: Dreadshot 10 never becomes Dreadshot 1.
    found=[];occupied=[]
    for name in sorted(names, key=len, reverse=True):
        pat = r'(?<![\w])'+r'\s+'.join(re.escape(x) for x in name.split())+r'(?![\w])'
        for m in re.finditer(pat,text,re.I):
            if not any(m.start()<end and m.end()>start for start,end in occupied):
                found.append((m.start(),name));occupied.append((m.start(),m.end()))
    return [n for _,n in sorted(found)]


def weapon_response(data, rank=None, level=None):
    matches=data['matches']
    if not matches:
        return 'No matching weapon was found in the staged inventory. Try its exact name and energy variant.'
    if len(matches)>1:
        return 'Several variants match: '+', '.join(safe_text(x['name'],80) for x in matches)+'. Please choose one.'
    item=matches[0]
    result=[f"**{safe_text(item['name'])}** — weapon variant {item['variant_number']}." , 'Identity source: '+safe_text(item['sources'])+'.']
    if item['name'].casefold()=='dreadshot 10':
        result.append('Source discrepancy: the calculator energy field is 8, while its screenshot shows 10; identity confirmed by the user.')
    stats=data['verified_stats']
    if stats:
        result.append('**Reviewed stats** (source screenshot filenames shown):')
        for x in stats[:18]:
            rank_desc=f"rank {x['rank']}" + (f", level {x['level']}" if x['level'] is not None else ', level not recorded')
            result.append(f"• {safe_text(x['stat_name'],90)}: {safe_text(x['value'],80)} ({rank_desc}; source: {safe_text(x['source_path'],110)})")
        if len(stats)>18:result.append('More reviewed stats exist; specify rank and level to narrow the results.')
    else:
        result.append('No verified stats available for this selection; I will not guess damage or other values.')
    if data['candidate_count']:
        result.append(f"{data['candidate_count']} screenshot readings are stored separately for review, not treated as verified facts.")
    return '\n'.join(result)[:1900]


def mech_response(matches):
    if not matches:return 'No matching mech found in the staged inventory.'
    if len(matches)>1:return 'Matches: '+', '.join(safe_text(x['name'],80) for x in matches)+'. Please choose one.'
    x=matches[0]
    return f"**{safe_text(x['name'])}** — listed in {safe_text(x['sources'])}. Detailed mech screenshot stats have not been verified; I won’t invent them."


async def fetch_builds(base, query, session=None):
    # Base is operator configuration, never derived from a chat message.
    own=session is None
    if own:session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    try:
        url=base.rstrip('/')+'/api/mech-arena/public/builds'
        async with session.get(url, params={'limit':10,'mech':query} if query else {'limit':10}, allow_redirects=False) as response:
            if response.status!=200:raise RuntimeError(f'Public builds API returned HTTP {response.status}.')
            body=await response.json(content_type=None)
        return body
    finally:
        if own:await session.close()


def builds_response(payload):
    if isinstance(payload,list):items=payload
    elif isinstance(payload,dict):
        items=payload.get('builds', payload.get('data', []))
        if isinstance(items,dict):items=items.get('builds',[])
    else:raise ValueError('Unexpected public builds response.')
    if not isinstance(items,list):raise ValueError('Unexpected public builds response.')
    if not items:return 'The public site returned no builds. I won’t make any up.'
    out=['**Public site builds** (source-reported, not independently verified):']
    for x in items[:10]:
        if isinstance(x,dict):
            # Field names vary between APIs; show only the fields actually returned.
            values=[]
            for k in ('name','title','mech','weapon','weapons','sp'):
                if k in x and x[k] is not None:values.append(k+': '+safe_text(x[k],150))
            out.append('• '+('; '.join(values) if values else safe_text(str(x),250)))
        else:out.append('• '+safe_text(x,250))
    return '\n'.join(out)[:1900]


# Adapt selected humanizer writing principles for short Discord replies; factual safety takes priority.
GENERAL_ASSISTANT_PROMPT=(
    'You are ARCHΛNGLE, a helpful Mech Arena community Discord bot. Write like a natural, clear teammate, not a formal article. '
    'Answer the exact question first. Keep replies concise and use the context already in the conversation instead of restating it. '
    'Match the user’s language and level of formality without copying typos. Use contractions when they fit; use bullets only when useful. '
    'Avoid stock greetings or praise, staged openers, filler, fake “not X but Y” contrasts, forced lists of three, dramatic one-line closers, '
    'generic sign-offs, marketing language, decorative formatting, and em/en-dash overuse. Do not add detail merely to make an answer vivid. '
    'Preserve names, numbers, and supported facts; be direct about uncertainty. Do not pretend to be human or claim personal experiences. '
    'If asked, say you are a bot. Do not answer Mech Arena game stats, loadouts, builds, or weapon questions from model memory; redirect to '
    '/weapon, /mech, or /builds. Never reveal or ask for API keys or other secrets.'
)


def naturalize_discord_reply(text):
    """Remove a couple of common chat wrappers without rewriting the answer's facts."""
    original=str(text).strip()
    value=re.sub(r'^(?:certainly|of course|absolutely|sure|great question)[,!:\s]+','',original,count=1,flags=re.I).strip()
    value=re.sub(r"\s*(?:hope (?:that )?this helps[.!]?|let me know if you(?:'d| would) like anything else[.!]?)\s*$",'',value,flags=re.I).strip()
    return value or original


async def general_ai(base, key, model, question):
    """General questions only. No history or private notes are sent."""
    if not key or not model:return None
    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
    payload={'model':model,'messages':[{'role':'system','content':GENERAL_ASSISTANT_PROMPT},{'role':'user','content':question[:1500]}], 'temperature':0.55, 'max_tokens':400}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        async with session.post(base.rstrip('/')+'/chat/completions',headers=headers,json=payload,allow_redirects=False) as r:
            if r.status!=200:raise RuntimeError('AI provider request failed (HTTP '+str(r.status)+').')
            obj=await r.json(content_type=None)
    text=obj['choices'][0]['message']['content']
    if not isinstance(text,str):raise ValueError('AI response is not text')
    # Conservative output firewall: this assistant must not generate game facts.
    if GAME_WORDS.search(text):return 'For Mech Arena information, use /weapon or /mech so I can look up the staged sources instead of guessing.'
    return safe_text(naturalize_discord_reply(text),1800)
