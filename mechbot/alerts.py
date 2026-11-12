"""Bounded channel and direct-message ping loops; stop on failure or deadline."""
import asyncio
import logging
import time
import uuid
import discord

log=logging.getLogger('mechbot.alerts')
OPERATORS={1444891876376186901,1442058583129722986}
MIN_INTERVAL=5
MAX_DURATION=1800
MAX_REPEATS=300


def validate(interval,duration):
    if not MIN_INTERVAL<=interval<=MAX_DURATION:raise ValueError(f'Interval must be at least {MIN_INTERVAL} seconds and at most 30 minutes.')
    if not interval<=duration<=MAX_DURATION:raise ValueError('Duration must be at least the interval and at most 30 minutes.')
    total_messages=1+(duration-1)//interval  # one immediate ping, then no more than one per interval
    if total_messages>MAX_REPEATS:raise ValueError(f'This would exceed {MAX_REPEATS} ping messages. Increase the interval or reduce the duration.')


class AlertManager:
    def __init__(self):self.alerts={}

    def available(self,target_id,mode):
        return not any(x['target']==target_id and x['mode']==mode for x in self.alerts.values()) and len(self.alerts)<5

    def start(self,target,mode,destination,interval,duration,creator,reason):
        validate(interval,duration)
        if not self.available(target.id,mode):raise ValueError('A loop is already active for that target and destination, or the five-loop limit has been reached.')
        key=uuid.uuid4().hex[:8]
        record={'id':key,'target':target.id,'mode':mode,'creator':creator,'stop':asyncio.Event()}
        self.alerts[key]=record
        record['task']=asyncio.create_task(self._run(record,target,destination,interval,duration,reason))
        return key

    async def _run(self,record,target,destination,interval,duration,reason):
        deadline=time.monotonic()+duration
        repeats=(duration-1)//interval  # the first channel/DM message is sent before this loop starts
        try:
            for index in range(1,repeats+1):
                delay=min(interval,max(0,deadline-time.monotonic()))
                try:await asyncio.wait_for(record['stop'].wait(),timeout=delay);break
                except asyncio.TimeoutError:pass
                if record['stop'].is_set() or time.monotonic()>deadline+1:break
                if record['mode']=='channel':
                    content=f'{target.mention} 🔔 Ping loop ({index+1}): {reason} — use /stop_loop to stop.'
                    allowed=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=target.id)],replied_user=False)
                else:
                    content=f'🔔 Direct-message loop ({index+1}): {reason} — use /stop_loop to stop.'
                    allowed=discord.AllowedMentions.none()
                try:await destination.send(content,allowed_mentions=allowed)
                except (discord.Forbidden,discord.NotFound,discord.HTTPException):
                    log.warning('Alert %s stopped: destination unavailable',record['id']);break
        finally:self.alerts.pop(record['id'],None)

    def stop(self,requester,target=None):
        count=0
        for record in list(self.alerts.values()):
            if requester not in OPERATORS and requester!=record['target']:continue
            if target is not None and record['target']!=target:continue
            record['stop'].set();count+=1
        return count

    async def close(self):
        tasks=[]
        for rec in list(self.alerts.values()):rec['stop'].set();tasks.append(rec['task'])
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
