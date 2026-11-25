"""Worker assignment and reliable-ish daily / one-off reminder state."""
import json
from pathlib import Path
import re
import time
import uuid
from .timeparse import clock_time, next_daily, tzinfo

GUILD_ID=1509560571563868160
CHANNEL_ID=1526008373009715313
REFERENCE_URL='https://discord.com/channels/1509560571563868160/1526008373009715313'
ROLE_NAME='bot database workers'
WORKERS={x['user_id']:x for x in json.loads((Path(__file__).resolve().parent.parent/'data'/'workers.json').read_text(encoding='utf-8'))}


def get_worker(user_id):return WORKERS.get(str(user_id))


def assignment_text(user_id):
    w=get_worker(user_id)
    if not w:return None
    return f"Group {w['group']}: "+', '.join(x['name']+' ('+x['tier']+')' for x in w['mechs'])


def parse_worker_time(text):
    """Recognize a clearly stated worker time. Return (clock, optional zone)."""
    s=text.strip()
    patterns=[
        r'(?:(?:change|set)\s+(?:(?:my|the|that)\s+)?(?:daily\s+)?(?:work\s+|task\s+)?(?:reminder\s+)?time\s+(?:to\s+)?|(?:my\s+)?(?:work\s+|task\s+)?(?:reminder\s+)?time\s+(?:is|to)\s+|(?:change|set)\s+(?:(?:my|the|that)\s+)?(?:daily\s+)?(?:work\s+)?reminder\s+(?:time\s+)?to\s+)(.+)',
        r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+[A-Za-z_+\-/]+)?)'
    ]
    rest=None
    for pattern in patterns:
        m=re.fullmatch(pattern,s,re.I)
        if m:rest=m[1].strip();break
    if rest is None:return None
    m=re.fullmatch(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)(?:\s+([A-Za-z_+\-/]+))?',rest,re.I)
    if not m:return None
    clock_time(m[1])
    return m[1].strip(),m[2]


def parse_worker_snooze(text):
    m=re.fullmatch(r'(?:please\s+)?(?:remind\s+me\s+again|snooze)(?:\s+me)?\s+(?:in|after)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)',text.strip(),re.I)
    if not m:return None
    n=int(m[1]);sec=n*(3600 if m[2].lower().startswith(('hour','hr')) else 60)
    if not 60<=sec<=7*86400:raise ValueError('Snooze must be between 1 minute and 7 days.')
    return sec


class WorkerStore:
    def __init__(self,store):self.store=store

    async def set_config(self,key,value):
        await self.store.execute('INSERT INTO worker_campaign(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,str(value))

    async def get_config(self,key):
        rows=await self.store.fetch('SELECT value FROM worker_campaign WHERE key=$1',key)
        return rows[0]['value'] if rows else None

    async def daily(self,user_id):
        rows=await self.store.fetch('SELECT * FROM worker_reminders WHERE schedule_key=$1','daily:'+str(user_id))
        return rows[0] if rows else None

    async def set_daily(self,user_id,clock,zone,now=None):
        if not get_worker(user_id):raise ValueError('This worker task is not assigned to your Discord account.')
        tzinfo(zone);hour,minute=clock_time(clock);now=int(time.time() if now is None else now)
        due=next_daily(hour,minute,zone,now)
        # Update only the daily row. Existing one-off snoozes remain untouched.
        row=await self.store.fetch('''INSERT INTO worker_reminders(schedule_key,user_id,kind,due_at,zone,hour,minute,status,updated_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT(schedule_key) DO UPDATE SET due_at=excluded.due_at,zone=excluded.zone,
             hour=excluded.hour,minute=excluded.minute,status='pending',lease_until=NULL,lease_token=NULL,
             dm_state='pending',channel_state='pending',last_error=NULL,updated_at=excluded.updated_at
            RETURNING id''','daily:'+str(user_id),str(user_id),'daily',due,zone,hour,minute,'pending',now)
        return row[0]['id'],due

    async def snooze(self,user_id,seconds,now=None):
        if not get_worker(user_id):raise ValueError('This worker task is not assigned to your Discord account.')
        if not 60<=seconds<=7*86400:raise ValueError('Snooze must be between 1 minute and 7 days.')
        now=int(time.time() if now is None else now);due=now+seconds
        current=await self.daily(user_id)
        if not current or current['status']=='paused':raise ValueError('Set your daily worker time and timezone first.')
        rows=await self.store.fetch('''INSERT INTO worker_reminders(schedule_key,user_id,kind,due_at,zone,status,updated_at)
          VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''','snooze:'+uuid.uuid4().hex,str(user_id),'snooze',due,current['zone'],'pending',now)
        return rows[0]['id'],due

    async def pause(self,user_id):
        rows=await self.store.fetch("UPDATE worker_reminders SET status='paused',lease_until=NULL,lease_token=NULL WHERE schedule_key=$1 AND status<>'paused' RETURNING id",'daily:'+str(user_id))
        return bool(rows)

    async def claim(self,now,limit=5):
        s=self.store;token=uuid.uuid4().hex
        if s.pool:
            sql='''WITH due AS (SELECT id FROM mech_bot_app.worker_reminders
                WHERE (status='pending' AND due_at<=$1) OR (status='delivering' AND lease_until<=$1)
                ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT $2)
                UPDATE mech_bot_app.worker_reminders r SET status='delivering',lease_until=$3,lease_token=$4
                FROM due WHERE r.id=due.id RETURNING r.*'''
            async with s.pool.acquire() as conn:return [dict(x) for x in await conn.fetch(sql,now,limit,now+120,token)]
        async with s.lock:
            s.db.execute('BEGIN IMMEDIATE')
            try:
                ids=[r[0] for r in s.db.execute("SELECT id FROM worker_reminders WHERE (status='pending' AND due_at<=?) OR (status='delivering' AND lease_until<=?) ORDER BY due_at LIMIT ?",(now,now,limit))]
                rows=[]
                for rid in ids:
                    s.db.execute("UPDATE worker_reminders SET status='delivering',lease_until=?,lease_token=? WHERE id=?",(now+120,token,rid))
                    rows.append(dict(s.db.execute('SELECT * FROM worker_reminders WHERE id=?',(rid,)).fetchone()))
                s.db.commit();return rows
            except Exception:s.db.rollback();raise

    async def active(self,row):
        rows=await self.store.fetch("SELECT id FROM worker_reminders WHERE id=$1 AND status='delivering' AND lease_token=$2",row['id'],row['lease_token'])
        return bool(rows)

    async def mark(self,row,destination,state):
        if destination not in ('dm','channel') or state not in ('sent','failed'):raise ValueError('Invalid delivery state')
        column=destination+'_state'
        await self.store.execute(f"UPDATE worker_reminders SET {column}=$1 WHERE id=$2 AND status='delivering' AND lease_token=$3",state,row['id'],row['lease_token'])
        row[column]=state

    async def finish(self,row,now,error):
        if row['kind']=='daily':
            due=next_daily(row['hour'],row['minute'],row['zone'],now)
            await self.store.execute("UPDATE worker_reminders SET status='pending',due_at=$1,dm_state='pending',channel_state='pending',lease_until=NULL,lease_token=NULL,last_error=$2,updated_at=$3 WHERE id=$4 AND status='delivering' AND lease_token=$5",due,error,now,row['id'],row['lease_token'])
        else:
            await self.store.execute("UPDATE worker_reminders SET status='completed',lease_until=NULL,lease_token=NULL,last_error=$1,updated_at=$2 WHERE id=$3 AND status='delivering' AND lease_token=$4",error,now,row['id'],row['lease_token'])

    async def abandon_removed(self,row):
        await self.store.execute("UPDATE worker_reminders SET status='paused',lease_until=NULL,lease_token=NULL,last_error=$1 WHERE id=$2 AND status='delivering' AND lease_token=$3",'Worker not found in the server; paused.',row['id'],row['lease_token'])
