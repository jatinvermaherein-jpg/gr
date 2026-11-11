"""Bounded emergency alerts. Stop on failures, cancellation, restart, or deadline."""
import asyncio
import logging
import time
import uuid
import discord

log=logging.getLogger('mechbot.emergency')
OPERATORS={1444891876376186901,1442058583129722986}
MIN_INTERVAL=5
MAX_DURATION=1800
MAX_PINGS=300


def validate(interval,duration):
    if not MIN_INTERVAL<=interval<=MAX_DURATION:raise ValueError(f'Interval must be at least {MIN_INTERVAL} seconds and at most 30 minutes.')
    if not interval<=duration<=MAX_DURATION:raise ValueError('Duration must be at least the interval and at most 30 minutes.')
    if duration//interval>MAX_PINGS:raise ValueError(f'This would exceed {MAX_PINGS} repeated pings. Increase the interval or reduce the duration.')


class EmergencyManager:
    def __init__(self):self.alerts={}

    def available(self,target_id,mode):
        return not any(x['target']==target_id and x['mode']==mode for x in self.alerts.values()) and len(self.alerts)<5

    def start(self,target,mode,anchor,interval,duration,creator,reason):
        validate(interval,duration)
        if not self.available(target.id,mode):raise ValueError('An alert is already active for that target and destination, or the five-alert limit has been reached.')
        key=uuid.uuid4().hex[:8]
        record={'id':key,'target':target.id,'mode':mode,'creator':creator,'stop':asyncio.Event()}
        self.alerts[key]=record
        record['task']=asyncio.create_task(self._run(record,target,anchor,interval,duration,reason))
        return key

    async def _run(self,record,target,anchor,interval,duration,reason):
        deadline=time.monotonic()+duration
        try:
            for index in range(1,duration//interval+1):
                delay=min(interval,max(0,deadline-time.monotonic()))
                try:await asyncio.wait_for(record['stop'].wait(),timeout=delay);break
                except asyncio.TimeoutError:pass
                if record['stop'].is_set() or time.monotonic()>deadline+1:break
                if record['mode']=='channel':
                    content=f'{target.mention} 🚨 Emergency alert ({index}): {reason} — use /emergency_stop to stop.'
                    allowed=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=target.id)],replied_user=False)
                else:
                    content=f'🚨 Emergency alert ({index}): {reason} — use /emergency_stop to stop.'
                    allowed=discord.AllowedMentions.none()
                try:await anchor.reply(content,mention_author=False,allowed_mentions=allowed,fail_if_not_exists=True)
                except (discord.Forbidden,discord.NotFound,discord.HTTPException):
                    log.warning('Alert %s stopped: anchor or destination unavailable',record['id']);break
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
