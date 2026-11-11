"""Question understanding, reviewed game-data retrieval, safe presentation and AI."""
import json
import logging
import re
import aiohttp

log=logging.getLogger('mechbot.assistant')
GAME_WORDS = re.compile(r'\b(mech(?:s|\s*arena)?|weapon(?:s)?|builds?|loadout|hangar|gear\s*hub|pilot(?:s)?|implant(?:s)?|a[- ]coins|credits?|battle\s*rating|dps)\b', re.I)
_PAYMENT_TERMS=r'(?:cost|price|how\s+(?:much|many)|spend|need|credits?|a[- ]coins?)'
_LEVEL_CHANGE=r'(?:upgrade(?:s|d|ing)?|rank\s*up|rank\s*\d+\s*(?:to|→|-)\s*(?:rank\s*)?\d+|level\s*\d+\s*(?:to|→|-)\s*\d+)'
_UPGRADE_COST=re.compile(r'\b'+_PAYMENT_TERMS+r'\b.{0,120}\b'+_LEVEL_CHANGE+r'\b|\b'+_LEVEL_CHANGE+r'\b.{0,120}\b'+_PAYMENT_TERMS+r'\b',re.I)


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
        return 'I couldn’t find that weapon variant. Try its exact name; energy variants are listed separately.'
    if len(matches)>1:
        return 'Several variants match: '+', '.join(safe_text(x['name'],80) for x in matches)+'. Please choose one.'
    item=matches[0]
    result=[f"**{safe_text(item['name'])}** — {item['variant_number']}-energy variant."]
    stats=data['verified_stats']
    if stats:
        result.append('**Stats**')
        for x in stats[:18]:
            rank_desc=f"rank {x['rank']}" + (f", level {x['level']}" if x['level'] is not None else ', level not recorded')
            result.append(f"• {safe_text(x['stat_name'],90)}: {safe_text(x['value'],80)} ({rank_desc})")
        if len(stats)>18:result.append('There are more entries. Add a rank and level to narrow the results.')
    else:
        result.append('I don’t have confirmed stats for this variant yet.')
    return '\n'.join(result)[:1900]


def mech_response(matches):
    if not matches:return 'I couldn’t find that mech. Check the spelling or choose a name from autocomplete.'
    if len(matches)>1:return 'Several mechs match: '+', '.join(safe_text(x['name'],80) for x in matches)+'. Please choose one.'
    x=matches[0]
    return f"**{safe_text(x['name'])}** — I don’t have confirmed detailed stats for this mech yet."


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
    out=['**Public site builds** — not independently verified:']
    for x in items[:10]:
        if isinstance(x,dict):
            # Field names vary between APIs; show only the fields actually returned.
            values=[]
            for k in ('name','title','mech','weapon','weapons','sp'):
                if k in x and x[k] is not None:values.append(k+': '+safe_text(x[k],150))
            out.append('• '+('; '.join(values) if values else safe_text(str(x),250)))
        else:out.append('• '+safe_text(x,250))
    return '\n'.join(out)[:1900]


# The model interprets each question; the bot supplies only locally reviewed facts.
GENERAL_ASSISTANT_PROMPT=(
    'You are ARCHΛNGLE, a friendly Mech Arena community Discord bot. First identify what the user is asking, then answer directly. '
    'Sound natural, warm and conversational, like a helpful teammate; be concise without sounding abrupt. Match the user’s language and formality '
    'without copying typos. Avoid canned praise/openers, filler, generic sign-offs, marketing language, and unnecessary formatting. '
    'Never pretend to be human or claim personal experiences. Keep private notes and other users’ messages out of your answer. '
    'Bot features include /help, /invite, /weapon, /mech, /builds, /recent, private reminders and notes, /timezone, and /calc; worker and emergency tools are also available to their authorized users. '
    'Explain these features when asked, but never claim to have completed an action unless the bot’s command handler actually did it. '
    'For Mech Arena facts, use only the supplied verified_game_facts. Do not use model memory for stats, costs, upgrades, builds, or game mechanics. '
    'If those facts do not answer the question, say what is missing and do not estimate, infer, or invent a number. If the question asks for a calculation, '
    'calculate only when the required verified inputs and formula are present. Preserve names, units and ranks. '
    'Do not mention internal labels or implementation details such as “identity source”, calculator, database, OCR, filenames, or record IDs unless the user '
    'specifically asks how a fact was verified. Never invent a source. Be clear when a general answer is uncertain. Never reveal or ask for API keys or secrets.'
)


def naturalize_discord_reply(text):
    """Remove a couple of common chat wrappers without rewriting the answer's facts."""
    original=str(text).strip()
    value=re.sub(r'^(?:certainly|of course|absolutely|sure|great question)[,!:\s]+','',original,count=1,flags=re.I).strip()
    value=re.sub(r"\s*(?:hope (?:that )?this helps[.!]?|let me know if you(?:'d| would) like anything else[.!]?)\s*$",'',value,flags=re.I).strip()
    return value or original


def game_knowledge_context(question,catalog):
    """Build a small evidence packet from reviewed catalog rows; omit source metadata."""
    weapon_names=find_mentions(question,getattr(catalog,'weapon_names',[])) if catalog else []
    mech_names=find_mentions(question,getattr(catalog,'mech_names',[])) if catalog else []
    entities=list(dict.fromkeys(weapon_names+mech_names))[:3]
    cost_question=bool(_UPGRADE_COST.search(question))
    related=bool(GAME_WORDS.search(question) or entities or (cost_question and re.search(r'\b(?:rank|level|star)\s*\d+\b',question,re.I)))
    rank_match=re.search(r'\brank\s*(\d+)\s*(?:to|→|-)\s*(?:rank\s*)?(\d+)\b',question,re.I)
    facts=[]
    for name in weapon_names[:3]:
        rank_match_from=re.search(r'\brank\s*(\d+)\b',question,re.I)
        level_match=re.search(r'\blevel\s*(\d+)\b',question,re.I)
        lookup=catalog.weapon(name,int(rank_match_from[1]) if rank_match_from else None,int(level_match[1]) if level_match else None)
        for item in lookup.get('matches',[])[:3]:
            stats=[{'stat':x['stat_name'],'value':x['value'],'rank':x['rank'],'level':x['level']} for x in lookup.get('verified_stats',[])[:60]]
            facts.append({'type':'weapon_stats','name':item['name'],'energy':item['variant_number'],'stats':stats})
    for name in mech_names[:3]:
        facts.append({'type':'mech_identity','name':name,'stats':[]})
    enough=bool(not cost_question and any(x.get('stats') for x in facts))
    missing='No confirmed rank-upgrade cost table is available.' if cost_question else ('No relevant confirmed game stats are available.' if related and not enough else None)
    return {
        'intent':'rank_upgrade_cost' if cost_question else ('game_fact_question' if related else 'general_question'),
        'game_related':related,
        'entities':entities,
        'upgrade_cost_question':cost_question,
        'rank_change':list(rank_match.groups()) if rank_match else None,
        'verified_game_facts':facts,
        'enough_verified_facts':enough,
        'missing_information':missing,
    }


def _unsupported_game_answer(question,context):
    entity=context['entities'][0] if context['entities'] else 'that item'
    if context['upgrade_cost_question']:
        ranks=context['rank_change']
        change=f" from rank {ranks[0]} to {ranks[1]}" if ranks else ''
        return f"I don’t have a confirmed upgrade price for {entity}{change} yet, so I won’t guess. If you send a screenshot of the upgrade screen, I can check it."
    if entity!='that item':
        return f"I don’t have confirmed stats for {entity} yet. If you send the specific in-game screen, I can help check it."
    return 'I don’t have a confirmed answer for that Mech Arena detail yet, so I won’t guess. If you share the relevant screen, I can help check it.'


def _supported_game_reply(text,context):
    if not text or re.search(r'\b(?:identity\s+source|source:|calculator|database|ocr|filename)\b',text,re.I):return False
    facts=context.get('verified_game_facts',[])
    names=[x.get('name','') for x in facts if x.get('name')]
    stripped=text
    for name in names:stripped=re.sub(re.escape(name),'',stripped,flags=re.I)
    stat_values=set();ranks=set();levels=set();energies=set()
    for fact in facts:
        energies.add(str(fact.get('energy'))) if fact.get('energy') is not None else None
        for stat in fact.get('stats',[]):
            stat_values.update(v.replace(',','') for v in re.findall(r'\d[\d,]*(?:\.\d+)?',str(stat.get('value',''))))
            ranks.add(str(stat['rank']))
            if stat.get('level') is not None:levels.add(str(stat['level']))
    for match in re.finditer(r'\d[\d,]*(?:\.\d+)?',stripped):
        value=match.group().replace(',','')
        if value in stat_values:continue
        prefix=stripped[max(0,match.start()-20):match.start()]
        if value in ranks and re.search(r'\brank\s*$',prefix,re.I):continue
        if value in levels and re.search(r'\blevel\s*$',prefix,re.I):continue
        if value in energies and re.search(r'\b(?:energy|variant)\s*$',prefix,re.I):continue
        return False
    return True


async def general_ai(base,key,model,question,context=None):
    """Answer one request without sending chat history, notes, or other users’ data."""
    if not key or not model:return None
    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
    user_payload={'question':str(question)[:1500]}
    if context is not None:user_payload['verified_game_facts']=context
    payload={'model':model,'messages':[{'role':'system','content':GENERAL_ASSISTANT_PROMPT},{'role':'user','content':json.dumps(user_payload,ensure_ascii=False)}], 'temperature':0.45, 'max_tokens':450}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        async with session.post(base.rstrip('/')+'/chat/completions',headers=headers,json=payload,allow_redirects=False) as r:
            if r.status!=200:raise RuntimeError('AI provider request failed (HTTP '+str(r.status)+').')
            obj=await r.json(content_type=None)
    text=obj['choices'][0]['message']['content']
    if not isinstance(text,str):raise ValueError('AI response is not text')
    return safe_text(naturalize_discord_reply(text),1800)


async def answer_question_with_brain(base,key,model,question,catalog):
    """Let AI interpret the question, but block unsupported Mech Arena claims."""
    context=game_knowledge_context(question,catalog)
    answer=None
    ai_failed=False
    if key and model:
        try:answer=await general_ai(base,key,model,question,context)
        except Exception as exc:
            ai_failed=True
            log.warning('AI question request failed (%s); using reviewed-data fallback.',type(exc).__name__)
    if context['game_related']:
        if not context['enough_verified_facts']:
            return _unsupported_game_answer(question,context)
        if _supported_game_reply(answer,context):return answer
        # The ordinary lookup below is deterministic and never relies on model memory.
        return None
    if ai_failed:return 'I’m having trouble reaching the AI right now. Please try again in a moment.'
    return answer
