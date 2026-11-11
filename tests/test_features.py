import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mechbot.store import Store
from mechbot.workers import WORKERS, WorkerStore, assignment_text, parse_worker_time, parse_worker_snooze
from mechbot.payouts import PayoutTracker, parse_snapshot
from mechbot.emergency import EmergencyManager, validate
from mechbot.bot import MechBot, install_commands
from mechbot.assistant import wants_invite_link, naturalize_discord_reply
from mechbot.timeparse import next_daily

UID='1515232384159715364'
DAY='2026-11-11'

class ParserTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(len(WORKERS),9)
        mechs=[m['name'] for w in WORKERS.values() for m in w['mechs']]
        self.assertEqual((len(mechs),len(set(mechs))),(47,47))
        self.assertNotIn('Biform',mechs)
        self.assertIn('Voidghost (S)',assignment_text('1413148554092089354'))
    def test_parser(self):
        self.assertEqual(parse_worker_time('8pm Asia/Kolkata'),('8pm','Asia/Kolkata'))
        self.assertEqual(parse_worker_time('change my reminder time to 9pm'),('9pm',None))
        self.assertEqual(parse_worker_snooze('remind me again in 10 minutes'),600)
        self.assertIsNone(parse_worker_time('what is 8pm?'))
        with self.assertRaises(ValueError):parse_worker_snooze('remind me again in 0 minutes')
    def test_api(self):
        b={'as_of':DAY,'rules':{'timezone':'Asia/Kolkata'},'pilots':[{'discord_id':UID,'today':{'day':DAY,'builds':11}},{'discord_id':'123','today':{'day':DAY,'builds':10}}]}
        self.assertEqual(parse_snapshot(b),(DAY,{UID:11}))
        b['rules']['timezone']='UTC'
        with self.assertRaises(ValueError):parse_snapshot(b)
    def test_safe_limits(self):
        validate(5,1500)  # 300 repeats at the fastest interval
        validate(6,1800)  # 300 repeats across the maximum duration
        with self.assertRaises(ValueError):validate(4,100)
        with self.assertRaises(ValueError):validate(5,1505)  # 301 repeats
        with self.assertRaises(ValueError):validate(60,1801)
    def test_natural_discord_style_cleanup(self):
        self.assertEqual(naturalize_discord_reply('Certainly! Here is the answer. Hope this helps.'),'Here is the answer.')
        self.assertEqual(naturalize_discord_reply('The answer is 42.'),'The answer is 42.')

    def test_invite_request_detection(self):
        self.assertTrue(wants_invite_link('Can you send me the invite link?'))
        self.assertTrue(wants_invite_link('I need to add the bot to my server'))
        self.assertFalse(wants_invite_link('Add a reminder for 8pm'))

    def test_command_registration(self):
        class Placeholder:pass
        bot=MechBot(Placeholder(),Placeholder());install_commands(bot)
        self.assertFalse(bot.intents.message_content)
        commands={x.name for x in bot.tree.get_commands()}
        self.assertTrue({'invite','worker_setup','worker_time','emergency_channel','emergency_dm','emergency_stop','emergency_stop_user'}<=commands)
        for name in ('emergency_channel','emergency_dm'):
            params={x.name:x for x in bot.tree.get_command(name).parameters}
            self.assertEqual(params['interval_seconds'].min_value,5)
            self.assertEqual(params['duration_seconds'].max_value,1800)
    def test_dst(self):
        # 02:30 is nonexistent on 2026-03-08 in New York; skip that day.
        from datetime import datetime,timezone
        now=datetime(2026,3,8,5,0,tzinfo=timezone.utc).timestamp()
        result=datetime.fromtimestamp(next_daily(2,30,'America/New_York',now),timezone.utc)
        self.assertEqual(result.isoformat(),'2026-03-09T06:30:00+00:00')

class DataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.db')
    async def asyncTearDown(self):
        await self.store.close();self.temp.cleanup()
    async def test_worker_schedule_snooze_and_lease(self):
        w=WorkerStore(self.store);now=1800000000
        await w.set_daily(UID,'8pm','Asia/Kolkata',now)
        before=await w.daily(UID)
        _,due=await w.snooze(UID,600,now)
        self.assertEqual(due,now+600)
        self.assertEqual((await w.daily(UID))['due_at'],before['due_at'])
        await w.set_daily(UID,'9pm','Asia/Kolkata',now)
        self.assertEqual(len(await self.store.fetch("SELECT * FROM worker_reminders WHERE kind='snooze'")),1)
        rows=await w.claim(now+600)
        self.assertEqual(len(rows),1)
        self.assertEqual(await w.claim(now+600),[])
        row=rows[0]
        await w.mark(row,'dm','sent');await w.mark(row,'channel','failed')
        await w.finish(row,now+600,'Channel delivery failed')
        self.assertEqual((await self.store.fetch("SELECT status FROM worker_reminders WHERE kind='snooze'"))[0]['status'],'completed')
        self.assertTrue(await w.pause(UID));self.assertEqual((await w.daily(UID))['status'],'paused')
        with self.assertRaises(ValueError):await w.snooze(UID,600,now)
    async def test_payout_high_water_restart_and_retry(self):
        p=PayoutTracker(self.store)
        await p.observe(UID,DAY,10)
        token,previous=await p.claim(UID,DAY,10,1000)
        self.assertEqual(previous,0)
        self.assertIsNone(await p.claim(UID,DAY,10,1001))
        await p.failed(UID,DAY,token)
        self.assertIsNone(await p.claim(UID,DAY,10,1119))
        token,_=await p.claim(UID,DAY,10,1120)
        await p.delivered(UID,DAY,10,token)
        self.assertEqual(await p.pending(),[])
        await p.observe(UID,DAY,9)
        self.assertEqual(await p.pending(),[])
        await p.observe(UID,DAY,13)
        self.assertEqual((await p.pending())[0]['observed_count'],13)
        token,prev=await p.claim(UID,DAY,13,1200)
        self.assertEqual(prev,10)
        await p.delivered(UID,DAY,13,token)
        await p.observe(UID,'2026-11-12',10)
        self.assertEqual(len(await p.pending()),1)
    async def test_emergency_reply_and_stop(self):
        class User:
            id=123456789012345678
            mention='<@123456789012345678>'
        anchor=type('Anchor',(),{'reply':AsyncMock()})()
        mgr=EmergencyManager()
        with patch('mechbot.emergency.MIN_INTERVAL',1):
            key=mgr.start(User(),'channel',anchor,1,2,9,'Test')
            self.assertFalse(mgr.available(User.id,'channel'))
            await asyncio.sleep(1.1)
            self.assertEqual(anchor.reply.await_count,1)
            self.assertEqual(mgr.stop(User.id,User.id),1)
            await mgr.alerts[key]['task']
            self.assertEqual(anchor.reply.await_count,1)
        await mgr.close()

    async def test_targeted_stop_cancels_dm_and_channel_only(self):
        class User:
            def __init__(self,uid):self.id=uid;self.mention=f'<@{uid}>'
        a=User(123456789012345678);b=User(223456789012345678)
        anchor=type('Anchor',(),{'reply':AsyncMock()})()
        mgr=EmergencyManager()
        channel=mgr.start(a,'channel',anchor,5,30,1442058583129722986,'Test')
        dm=mgr.start(a,'dm',anchor,5,30,1442058583129722986,'Test')
        other=mgr.start(b,'channel',anchor,5,30,1442058583129722986,'Test')
        self.assertEqual(mgr.stop(b.id,a.id),0)  # unauthorized user cannot stop others
        self.assertEqual(mgr.stop(1442058583129722986,a.id),2)
        self.assertTrue(mgr.alerts[channel]['stop'].is_set())
        self.assertTrue(mgr.alerts[dm]['stop'].is_set())
        self.assertFalse(mgr.alerts[other]['stop'].is_set())
        await mgr.close()
        self.assertEqual(anchor.reply.await_count,0)

if __name__=='__main__':unittest.main()
