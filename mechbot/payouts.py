"""Poll only the public payout aggregate; durable per-user/day high water marks."""
from datetime import date
import re
import time
import uuid
import aiohttp

BASE='https://portico-hq-live.netlify.app'
RECIPIENT=1442058583129722986


def parse_snapshot(body):
    if not isinstance(body,dict) or not isinstance(body.get('pilots'),list):raise ValueError('Invalid payout API shape')
    day=body.get('as_of');rules=body.get('rules')
    if not isinstance(day,str) or not isinstance(rules,dict) or rules.get('timezone')!='Asia/Kolkata':raise ValueError('Invalid payout day/timezone')
    date.fromisoformat(day)
    results={}
    for p in body['pilots']:
        if not isinstance(p,dict):continue
        uid=p.get('discord_id');today=p.get('today')
        if not isinstance(uid,str) or not re.fullmatch(r'[0-9]{15,20}',uid) or not isinstance(today,dict):continue
        n=today.get('builds')
        if today.get('day')!=day or type(n) is not int or n<0:continue
        if n>=10:results[uid]=max(n,results.get(uid,0))
    return day,results


async def fetch_snapshot(base=BASE):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(base.rstrip('/')+'/api/mech-arena/public/payouts',allow_redirects=False) as response:
            if response.status!=200:raise RuntimeError(f'Payout API HTTP {response.status}')
            return parse_snapshot(await response.json(content_type=None))


class PayoutTracker:
    def __init__(self,store):self.store=store
    async def observe(self,uid,day,count):
        await self.store.execute('''INSERT INTO payout_alerts AS existing(user_id,day,observed_count) VALUES($1,$2,$3)
          ON CONFLICT(user_id,day) DO UPDATE SET observed_count=CASE WHEN excluded.observed_count>existing.observed_count
          THEN excluded.observed_count ELSE existing.observed_count END''',uid,day,count)

    async def pending(self):
        return await self.store.fetch('SELECT user_id,day,observed_count FROM payout_alerts WHERE observed_count>sent_count ORDER BY day,user_id LIMIT 200')

    async def claim(self,uid,day,count,now=None):
        now=int(time.time() if now is None else now);token=uuid.uuid4().hex
        rows=await self.store.fetch('''UPDATE payout_alerts SET lease_until=$1,lease_token=$2
           WHERE user_id=$3 AND day=$4 AND sent_count<$5 AND (lease_until IS NULL OR lease_until<=$6)
           RETURNING sent_count''',now+120,token,uid,day,count,now)
        return (token,rows[0]['sent_count']) if rows else None
    async def delivered(self,uid,day,count,token):
        await self.store.execute('''UPDATE payout_alerts SET sent_count=$1,lease_until=NULL,lease_token=NULL,last_error=NULL
          WHERE user_id=$2 AND day=$3 AND lease_token=$4''',count,uid,day,token)
    async def failed(self,uid,day,token):
        # Keep the lease until expiry, so a closed DM does not cause rapid retries.
        await self.store.execute("UPDATE payout_alerts SET last_error='Report DM failed' WHERE user_id=$1 AND day=$2 AND lease_token=$3",uid,day,token)
