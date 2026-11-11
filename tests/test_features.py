import asyncio
import gzip
import os
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mechbot.store import Store
from mechbot.workers import WORKERS, WorkerStore, assignment_text, parse_worker_time, parse_worker_snooze
from mechbot.payouts import PayoutTracker, parse_snapshot
from mechbot.emergency import EmergencyManager, validate
from mechbot.bot import MechBot, install_commands
from mechbot.assistant import (wants_invite_link, naturalize_discord_reply, weapon_response, mech_response,
                               game_knowledge_context, answer_question_with_brain)
from mechbot.catalog import (ARCHIVE, Catalog, _candidate_fingerprint, apply_user_confirmed_ocr_review,
                             apply_user_confirmed_public_review, initialize_snapshot)
from mechbot.timeparse import next_daily, parse_spec, parse_natural_reminder

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
    def test_natural_language_short_reminders(self):
        self.assertEqual(parse_natural_reminder('can you remind me in 20 sec to write notes'),('in 20 seconds','write notes'))
        self.assertEqual(parse_natural_reminder('please remind me in 20s to write notes'),('in 20 seconds','write notes'))
        self.assertEqual(parse_natural_reminder('could you please remind me in 2 minutes to check email'),('in 2 minutes','check email'))
        self.assertEqual(parse_spec('20s','UTC',now=1000),(1020,'once',20,None,None))
        with self.assertRaises(ValueError):parse_spec('every 20 sec','UTC',now=1000)

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

    def test_lookup_replies_are_professional(self):
        weapon=weapon_response({'matches':[{'name':'Dreadshot 10','variant_number':10}],
            'verified_stats':[{'stat_name':'Damage','value':'8,000','rank':4,'level':None}],
            'candidate_count':91})
        self.assertIn('**Dreadshot 10**',weapon)
        self.assertIn('8,000',weapon)
        for internal in ('identity source','calculator','database','source:','91 screenshot'):
            self.assertNotIn(internal,weapon.lower())
        mech=mech_response([{'name':'Redox','sources':'calculator + database'}])
        self.assertIn('**Redox**',mech)
        self.assertNotIn('calculator',mech.lower())
        self.assertNotIn('database',mech.lower())

    def test_weapon_lookup_is_a_short_review_not_a_full_rank_dump(self):
        response=weapon_response({
            'matches':[{'name':'EM Rifle 10','variant_number':10,'database_category':'sniper'}],
            'review':{'rarity':'Legendary','tier':'C','perk':'Power Shot',
                'perkDesc':'This weapon is capable of Double Damage shots.',
                'reasoning':'A high-skill sniper. Precise single shots can hit hard.',
                'weaknesses':'A single-shot magazine punishes misses.'},
            'verified_stats':[
                {'stat_name':'Damage Per Shot','value':'6774','rank':1,'level':None},
                {'stat_name':'Double Damage Chance','value':'5 %','rank':1,'level':None},
                {'stat_name':'Fire Rate','value':'10 rpm','rank':1,'level':None},
                {'stat_name':'Damage Per Shot','value':'14422','rank':2,'level':None}],
            'listing':{'cost':{'amount':7125,'currency':'A-Coins'}}})
        self.assertIn('**Quick review:**',response)
        self.assertIn('6,774 damage/shot',response)
        self.assertIn('7,125 A-Coins',response)
        self.assertNotIn('rank 2',response.lower())
        self.assertLess(response.count('\n'),8)

    def test_redox_upgrade_context_has_no_cost_evidence(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        context=game_knowledge_context('How much will it cost to upgrade Redox from rank 4 to 5?',Catalog())
        self.assertTrue(context['game_related'])
        self.assertTrue(context['upgrade_cost_question'])
        self.assertEqual(context['rank_change'],['4','5'])
        self.assertFalse(context['enough_verified_facts'])

    def test_invite_request_detection(self):
        self.assertTrue(wants_invite_link('Can you send me the invite link?'))
        self.assertTrue(wants_invite_link('I need to add the bot to my server'))
        self.assertFalse(wants_invite_link('Add a reminder for 8pm'))

    def test_command_registration(self):
        class Placeholder:pass
        bot=MechBot(Placeholder(),Placeholder());install_commands(bot)
        self.assertFalse(bot.intents.message_content)
        commands={x.name for x in bot.tree.get_commands()}
        self.assertTrue({'help','invite','worker_setup','worker_time','emergency_channel','emergency_dm','emergency_stop','emergency_stop_user'}<=commands)
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

class AIQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_is_first_but_unsupported_upgrade_cost_is_blocked(self):
        from unittest.mock import AsyncMock, patch
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        question='How much will it cost to upgrade Redox from rank 4 to 5?'
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It costs 12,000 A-Coins.')) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,Catalog())
        ai.assert_awaited_once()
        self.assertIn('Redox from rank 4 to 5',answer)
        self.assertIn('won’t guess',answer)
        self.assertNotIn('12,000',answer)
        self.assertNotIn('calculator',answer.lower())
        self.assertNotIn('database',answer.lower())

    async def test_ai_can_answer_from_reviewed_facts(self):
        from unittest.mock import AsyncMock, patch
        class Catalog:
            weapon_names=['Pulse Cannon 8']
            mech_names=[]
            def weapon(self,name,rank=None,level=None):
                self.assertion=(name,rank,level)
                return {'matches':[{'name':'Pulse Cannon 8','variant_number':8}],
                    'verified_stats':[{'stat_name':'Damage','value':'1,250','rank':4,'level':None}],
                    'candidate_count':0}
        catalog=Catalog()
        question='How much damage does Pulse Cannon 8 do at rank 4?'
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It deals 1,250 damage at rank 4.')) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
        ai.assert_awaited_once()
        self.assertEqual(catalog.assertion,('Pulse Cannon 8',4,None))
        context=ai.await_args.args[4]
        self.assertEqual(context['intent'],'game_fact_question')
        self.assertEqual(context['verified_game_facts'][0]['stats'][0]['value'],'1,250')
        self.assertIn('1,250',answer)
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It deals 4 damage at rank 4.')):
            unsupported=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
        self.assertIsNone(unsupported)

    async def test_missing_ai_key_still_abstains_on_unknown_game_cost(self):
        from unittest.mock import AsyncMock, patch
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        with patch('mechbot.assistant.general_ai',new=AsyncMock()) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','','test-model',
                'How much does Redox cost to upgrade from rank 4 to 5?',Catalog())
        ai.assert_not_awaited()
        self.assertIn('I don’t have a confirmed upgrade price',answer)

    async def test_confirmed_archive_cost_overrides_ai_guess(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):
                runtime=initialize_snapshot()
            catalog=Catalog(runtime)
            question='How much to upgrade Redox from rank 4 to 5?'
            with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It costs 999 Credits.')) as ai:
                answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
            ai.assert_awaited_once()
            self.assertIn('2,825 A-Coins',answer)
            self.assertIn('rank 4 L7',answer)
            self.assertNotIn('999',answer)
            catalog.close()


class QuestionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_are_published_globally_if_test_guild_is_missing(self):
        bot=MechBot(object(),object())
        with (
            patch.object(bot.tree,'sync',new=AsyncMock()) as sync,
            patch.object(bot,'get_guild',return_value=None),
            patch.object(bot,'wait_until_ready',new=AsyncMock()),
            patch.object(bot,'is_closed',side_effect=[False,False,True]),
            patch('mechbot.bot.asyncio.sleep',new=AsyncMock()),
        ):
            await bot.command_sync_loop('1509560571563868160')
        sync.assert_awaited_once_with()

    async def test_polite_20_second_reminder_is_created_before_ai_fallback(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.create_reminder=AsyncMock(return_value='Reminder #1 set for shortly from now.')
        bot.private_reply=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        message=SimpleNamespace(author=author,channel=object(),guild=None,mentions=[bot_user],
            content=f'<@{bot_user.id}> can you remind me in 20 sec to write notes',reference=None,reply=AsyncMock())
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        bot.create_reminder.assert_awaited_once_with(author.id,'in 20 seconds','write notes')
        bot.private_reply.assert_awaited_once_with(message,'Reminder #1 set for shortly from now.')
        author.send.assert_awaited_once()
        brain.assert_not_awaited()

    async def test_natural_game_question_hits_ai_before_mech_fallback(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
            def mech(self,name):raise AssertionError('direct mech fallback ran before the AI brain')
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        question='How much will it cost to upgrade Redox from rank 4 to 5?'
        message=SimpleNamespace(author=author,channel=object(),guild=None,mentions=[bot_user],
            content=f'<@{bot_user.id}> {question}',reference=None,reply=AsyncMock())
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(return_value='I don’t have a reviewed upgrade price yet.')) as brain:
            await bot.on_message(message)
        brain.assert_awaited_once()
        self.assertEqual(brain.await_args.args[3],question)
        message.reply.assert_awaited_once()
        self.assertIn('I don’t have a reviewed upgrade price',message.reply.await_args.args[0])


class CatalogReviewTests(unittest.TestCase):
    def test_user_confirmation_promotes_only_matching_snapshot_and_keeps_identity_gate(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'game.sqlite3'
            db=sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE weapons(id INTEGER PRIMARY KEY,name TEXT,family TEXT,variant_number INTEGER);
                CREATE TABLE mechs(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE screenshots(id INTEGER PRIMARY KEY,source_path TEXT,sha256 TEXT,weapon_id INTEGER,identity_status TEXT);
                CREATE TABLE weapon_stat_candidates(id INTEGER PRIMARY KEY,screenshot_id INTEGER,stat_name TEXT,raw_label TEXT,source_row_number INTEGER,rank_column INTEGER,rank INTEGER,level INTEGER,star_color TEXT,candidate_value TEXT,status TEXT,reviewed_by TEXT,reviewed_at TEXT,review_note TEXT);
                CREATE TABLE review_events(id INTEGER PRIMARY KEY,candidate_id INTEGER,action TEXT,reviewer TEXT,reviewed_at TEXT,note TEXT);
            ''')
            db.execute('INSERT INTO metadata VALUES(?,?)',('screenshot_zip_sha256','unit-test-archive'))
            db.execute('INSERT INTO weapons VALUES(1,\'Railgun 8\',\'Railgun\',8)')
            db.execute('INSERT INTO screenshots VALUES(1,\'reviewed.png\',\'sha-a\',1,\'exact_catalog_match\')')
            db.execute('INSERT INTO screenshots VALUES(2,\'unresolved.png\',\'sha-b\',1,\'identity_review\')')
            db.execute("INSERT INTO weapon_stat_candidates VALUES(1,1,'Damage','Damage',1,1,1,NULL,'yellow','100','unverified',NULL,NULL,NULL)")
            db.execute("INSERT INTO weapon_stat_candidates VALUES(2,2,'Damage','Damage',1,1,1,NULL,'yellow','200','unverified',NULL,NULL,NULL)")
            db.commit()
            _,fingerprint=_candidate_fingerprint(db)
            db.close()
            self.assertEqual(apply_user_confirmed_ocr_review(path,archive_sha='unit-test-archive',values_sha='wrong',candidate_count=2),0)
            count=apply_user_confirmed_ocr_review(path,archive_sha='unit-test-archive',values_sha=fingerprint,candidate_count=2,reviewed_at='2026-11-10')
            self.assertEqual(count,2)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT status,count(*) FROM weapon_stat_candidates GROUP BY status").fetchall(),[('verified',2)])
            self.assertEqual(db.execute('SELECT count(*) FROM review_events').fetchone()[0],2)
            db.close()
            catalog=__import__('mechbot.catalog',fromlist=['Catalog']).Catalog(path)
            result=catalog.weapon('Railgun 8')
            self.assertEqual([x['value'] for x in result['verified_stats']],['100'])
            catalog.close()


class PublicArchiveTests(unittest.TestCase):
    def test_confirmed_archive_drives_weapon_review_costs_and_forum_retrieval(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):
                runtime=initialize_snapshot()
            db=sqlite3.connect(runtime)
            counts=db.execute("SELECT source,review_status,trust,count(*) FROM archive_documents GROUP BY source,review_status,trust ORDER BY source").fetchall()
            self.assertEqual(counts,[('mech_arena_database','verified','user_confirmed',52),
                ('plarium_forum','verified','user_confirmed',59),
                ('upgrade_calculator','verified','user_confirmed',36)])
            self.assertEqual(db.execute("SELECT count(*) FROM archive_structured_rows").fetchone()[0],1338)
            db.close()
            catalog=Catalog(runtime)
            weapon=catalog.weapon('EM Rifle 10')
            self.assertEqual(weapon['review']['tier'],'C')
            self.assertEqual(weapon['listing']['cost'],{'amount':7125,'currency':'A-Coins'})
            response=weapon_response(weapon)
            self.assertIn('**Quick review:**',response)
            self.assertIn('6,774 damage/shot',response)
            self.assertNotIn('rank 2',response.lower())
            cost=catalog.upgrade_cost('Redox',4,5)
            self.assertEqual(cost['cost']['acoins'],2825)
            self.assertEqual((cost['from_level'],cost['to_level']),(7,1))
            self.assertEqual(catalog.weapon('Dreadshot 8')['listing']['list'],'Dreadshot 8')
            self.assertIsNone(catalog.weapon('Dreadshot 10')['listing'])
            self.assertIsNone(catalog.upgrade_cost('Dreadshot 10',4,5))
            hits=catalog.search_confirmed_archive('what happened to Voidghost?',['Voidghost'],limit=4)
            self.assertTrue(any(x.get('title')=='Voidghost Update: Next Steps' for x in hits))
            catalog.close()

    def test_changed_archive_does_not_get_implicitly_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'staging.sqlite3'
            with gzip.open(ARCHIVE,'rb') as source,open(path,'wb') as target:
                shutil.copyfileobj(source,target)
            self.assertEqual(apply_user_confirmed_public_review(path,expected_fingerprint='not-the-reviewed-snapshot'),0)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_documents WHERE review_status='pending'").fetchone()[0],147)
            db.close()


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
