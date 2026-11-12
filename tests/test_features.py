import asyncio
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

import discord
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mechbot.store import Store, CONVERSATION_TTL_SECONDS, CONVERSATION_TURN_LIMIT
from mechbot.workers import WORKERS, WorkerStore, assignment_text, parse_worker_time, parse_worker_snooze
from mechbot.payouts import PayoutTracker, parse_snapshot
from mechbot.alerts import AlertManager, validate
from mechbot.bot import MechBot, install_commands, OPERATORS, REPLY_MENTIONS, help_text
from mechbot.assistant import (wants_invite_link, bot_identity_response, direct_item_lookup, should_answer_unmentioned,
                               parse_natural_calculation, natural_builds_request, discord_text,
                               naturalize_discord_reply, weapon_response, mech_response,
                               game_knowledge_context, answer_question_with_brain, upgrade_cost_response, general_ai)
from mechbot.announcements import (DEFAULT_ANNOUNCEMENT_CHANNELS, INITIAL_BACKFILL_LIMIT, wants_upcoming, message_text, upcoming_items,
                                   format_upcoming, wants_promo_codes, promo_code_items, format_promo_codes)
from mechbot.catalog import (ARCHIVE, Catalog, _candidate_fingerprint, apply_user_confirmed_ocr_review,
                             apply_user_confirmed_public_review, apply_user_confirmed_drive_ocr_review, initialize_snapshot)
from mechbot.timeparse import next_daily, parse_spec, parse_natural_reminder

UID='1515232384159715364'
DAY='2026-11-11'

class AnnouncementTests(unittest.TestCase):
    def test_upcoming_detection_and_explicit_year_safety(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.assertEqual(INITIAL_BACKFILL_LIMIT,100)
        now=datetime(2026,11,10,12,tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
        self.assertTrue(wants_upcoming("what's coming up next?"))
        self.assertTrue(wants_upcoming('next event'))
        for query in ('any event','Any events?','What events are in Mech Arena?',
                      'Do we have any events?','List upcoming events'):
            with self.subTest(query=query):self.assertTrue(wants_upcoming(query))
        self.assertFalse(wants_upcoming('What types of events are in Mech Arena?'))
        self.assertFalse(wants_upcoming('make me a reminder'))
        self.assertFalse(wants_upcoming('set a reminder for any event'))
        rows=[
            {'content':'Tournament starts November 15, 2026','posted_at':int(now-1000),'channel_name':'news','guild_name':'Game Server','message_url':'https://discord.com/channels/1/2/3'},
            {'content':'Event coming soon; date to be announced','posted_at':int(now-1000),'channel_name':'events','guild_name':'Game Server','message_url':'https://discord.com/channels/1/4/5'},
            {'content':'Event starts November 12','posted_at':int(now-1000),'channel_name':'events','guild_name':'Game Server','message_url':'https://discord.com/channels/1/4/6'},
            {'content':'Event was held November 9, 2026','posted_at':int(now-1000),'channel_name':'news','guild_name':'Game Server','message_url':'https://discord.com/channels/1/2/7'},
        ]
        items=upcoming_items(rows,now=now)
        self.assertEqual([x['date_label'] for x in items],['November 15, 2026','Date not stated','Year not stated'])
        response=format_upcoming(rows,enabled=True,now=now)
        self.assertIn('[Source: #news · Game Server](https://discord.com/channels/1/2/3)',response)
        self.assertIn('explicit year',response)
        self.assertNotIn('November 9, 2026',response)

    def test_promo_lookup_only_extracts_labeled_codes_and_preserves_caveats(self):
        self.assertTrue(wants_promo_codes('Any ECR promo codes?'))
        self.assertTrue(wants_promo_codes('latest ECR code'))
        self.assertTrue(wants_promo_codes('ECR promo codes'))
        self.assertTrue(wants_promo_codes('ANY ECR'))
        self.assertTrue(wants_promo_codes('PROMOCODE'))
        self.assertTrue(wants_promo_codes('promo code please'))
        self.assertFalse(wants_promo_codes('ECR promo codes by MRX'))
        self.assertFalse(wants_promo_codes('What is the next event?'))
        self.assertFalse(wants_promo_codes('What is ECR?'))
        self.assertTrue(should_answer_unmentioned('latest ECR code'))
        self.assertFalse(should_answer_unmentioned('ECR promo codes by MRX'))
        rows=[
            {'content':'ECR Promo codes: `SAVE2026`, `BONUS-10`'+chr(10)+'Valid until December 31, 2026',
             'posted_at':200,'channel_id':'1','channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/200'},
            {'content':'Promo code: SAVE2026','posted_at':100,'channel_name':'old-events',
             'guild_name':'Game Server','message_url':'https://discord.com/channels/1/2/100'},
            {'content':'Try SAVE2026 in the shop','posted_at':300,'channel_name':'chat',
             'message_url':'https://discord.com/channels/1/2/300'},
        ]
        items=promo_code_items(rows)
        self.assertEqual([item['code'] for item in items],['SAVE2026','BONUS-10'])
        self.assertEqual(items[0]['posted_at'],200)
        self.assertEqual(items[0]['status'],'validity_unknown')
        response=format_promo_codes(rows,enabled=True)
        self.assertIn('`SAVE2026`',response)
        self.assertIn('`BONUS-10`',response)
        self.assertIn('validity not confirmed',response)
        self.assertIn('[Source: #events · Game Server](https://discord.com/channels/1/2/200)',response)
        self.assertIn('not validated in-game',response)
        expired=promo_code_items([{'content':'Promo code: OLD2024 has expired','posted_at':400}])
        self.assertEqual(expired[0]['status'],'marked_expired')
        compact=promo_code_items([{'content':'PROMOCODE: NEW2026','posted_at':450}])
        self.assertEqual([item['code'] for item in compact],['NEW2026'])
        self.assertEqual(promo_code_items([{'content':'Try NEW2026 now','posted_at':500}]),[])

    def test_announcement_message_text_reads_embeds_without_pinging(self):
        embed=SimpleNamespace(title='@everyone Event notice',description='Starts soon',fields=[])
        msg=SimpleNamespace(content='Update @here <@123456789012345678>',embeds=[embed])
        text=message_text(msg)
        self.assertIn('Update @\u200bhere [member]',text)
        self.assertIn('@\u200beveryone Event notice',text)
        self.assertIn('Starts soon',text)


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

    def test_plain_language_calculation_and_build_lookup_parsers(self):
        self.assertEqual(parse_natural_calculation('What is 2 + 3?'),'2 + 3')
        self.assertEqual(parse_natural_calculation('calculate 12 multiplied by 4'),'12 * 4')
        self.assertEqual(parse_natural_calculation('2+2'),'2+2')
        self.assertIsNone(parse_natural_calculation('What is the best mech?'))
        self.assertTrue(should_answer_unmentioned('2+2'))
        self.assertEqual(natural_builds_request('Any builds for Redox?',['Redox']),
                         {'mechs':['Redox'],'query':'Redox','ambiguous':False,'unresolved':False})
        self.assertEqual(natural_builds_request('Show the latest builds',[]),
                         {'mechs':[],'query':None,'ambiguous':False,'unresolved':False})
        self.assertTrue(natural_builds_request('Show builds for an unknown mech',[])['unresolved'])
        self.assertIsNone(natural_builds_request('Redox is amazing',['Redox']))

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

    def test_identity_credits_and_exact_item_shortcuts(self):
        self.assertIn('ARCHΛNGLE',bot_identity_response('Who are you?'))
        builder=bot_identity_response('Who build you?')
        self.assertIn('Nub',builder)
        self.assertIn('confirmed code-author',builder)
        self.assertNotIn('built by Nub',builder)
        self.assertIn('ᧁꪖꪶꪖ𝓽ꫀ𝘘',bot_identity_response('Who maintains the database?'))
        self.assertIn('MRX',bot_identity_response('Who does the ECR event guides?'))
        self.assertEqual(direct_item_lookup('please review EM Rifle 10 rank 4 level 2',['EM Rifle 10']),'EM Rifle 10')
        self.assertIsNone(direct_item_lookup('compare EM Rifle 10 with Pulse Cannon 8',['EM Rifle 10']))

    def test_discord_text_stays_under_limit_at_natural_boundary(self):
        result=discord_text(('A clean sentence.\n'+'many words here '*200))
        self.assertLessEqual(len(result),1900)
        self.assertTrue(result.endswith('…'))

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

    def test_upgrade_cost_reply_uses_registered_currency_emojis(self):
        response=upgrade_cost_response('Redox',{
            'from_rank':5,'from_level':1,'to_rank':5,'to_level':1,'no_op':True,
            'cost':{'acoins':0,'credits':0,'blueprints':0}},
            {'acoins':'<:archangle_a_coins_v2:101>','credits':'<:archangle_credits_v2:102>',
             'blueprints':'<:archangle_blueprints_v2:103>'})
        self.assertIn('<:archangle_a_coins_v2:101> 0 A-Coins',response)
        self.assertIn('<:archangle_credits_v2:102> 0 Credits',response)
        self.assertIn('<:archangle_blueprints_v2:103> 0 Blueprints (BP)',response)

    def test_upgrade_cost_reply_prices_only_blueprints_still_missing(self):
        fact={'from_rank':4,'from_level':1,'to_rank':5,'to_level':1,
              'cost':{'acoins':2825,'credits':265000,'blueprints':10},
              'rarity':'legendary','blueprint_acoins_per_blueprint':240}
        partial=upgrade_cost_response('Redox',fact,blueprints_owned=4)
        self.assertIn('4 of 10 required BP already owned',partial)
        self.assertIn('remaining 6 BP at 240 A-Coins each costs **1,440 A-Coins**',partial)
        self.assertIn('total A-Coins including BP purchases: **4,265 A-Coins**',partial)
        none=upgrade_cost_response('Redox',fact,blueprints_owned=0)
        self.assertIn('Assuming you own 0 of 10 required BP',none)
        self.assertIn('remaining 10 BP at 240 A-Coins each costs **2,400 A-Coins**',none)
        self.assertIn('total A-Coins including BP purchases: **5,225 A-Coins**',none)
        unknown=upgrade_cost_response('Redox',{**fact,'blueprint_acoins_per_blueprint':None},blueprints_owned=0)
        self.assertIn('I don’t have a confirmed A-Coins price',unknown)
        self.assertNotIn('BP at',unknown)
        self.assertNotIn('5,225',unknown)

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
        self.assertIn('\n\n**Quick review:**\n',response)
        self.assertIn('• 6,774 damage/shot',response)
        self.assertIn('7,125 A-Coins',response)
        self.assertNotIn('rank 2',response.lower())
        self.assertLess(response.count('\n'),20)

    def test_weapon_response_has_spaced_sections_and_keeps_long_perk_text(self):
        perk_desc=('A compact first sentence. The explanation continues in a longer second sentence '
                   'that should remain intact even when the combined text exceeds the previous display '
                   'limit of one hundred and eighty characters, without getting clipped before its final phrase.')
        self.assertGreater(len(perk_desc),180)
        response=weapon_response({
            'matches':[{'name':'Charge Rocket 10','variant_number':10,'database_category':'missile'}],
            'review':{'rarity':'Legendary','tier':'S','reasoning':'Quick review text.',
                'weaknesses':'A caution worth noting.','perk':'Whirlwind','perkDesc':perk_desc},
            'verified_stats':[{'stat_name':'Damage Per Shot','value':'2364','rank':1,'level':None}],
            'listing':{'cost':{'amount':650000,'currency':'Credits'}}})
        self.assertIn('**Charge Rocket 10** — 10-energy Legendary missile · Tier S.',response)
        self.assertIn('\n\n**Quick review:**\nQuick review text.',response)
        self.assertIn('\n\n**Watch-out:**\nA caution worth noting.',response)
        self.assertIn('\n\n**Perk: Whirlwind**\n'+perk_desc,response)
        self.assertIn('\n\n**Key stats** · Rank 1 · Level not recorded\n• 2,364 damage/shot',response)
        self.assertIn('\n\n**Listed purchase price:** 650,000 Credits *(not a rank-up cost)*',response)
        self.assertLessEqual(len(response),1900)

    def test_redox_upgrade_context_has_no_cost_evidence(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        questions=(
            'How much will it cost to upgrade Redox from rank 4 to 5?',
            'How much for Redox rank 4 L1 to rank 5 L1?',
            'Redox R4 L1 -> R5 L1 price?',
        )
        for question in questions:
            with self.subTest(question=question):
                context=game_knowledge_context(question,Catalog())
                self.assertTrue(context['game_related'])
                self.assertTrue(context['upgrade_cost_question'])
                self.assertEqual(context['rank_change'],['4','5'])
                self.assertFalse(context['enough_verified_facts'])
        exact=game_knowledge_context('UPGRADE COST FOR REDOX R4 L1 TO R5 L1',Catalog())
        self.assertEqual((exact['from_level'],exact['to_level']),(1,1))

    def test_invite_request_detection(self):
        self.assertTrue(wants_invite_link('Can you send me the invite link?'))
        self.assertTrue(wants_invite_link('I need to add the bot to my server'))
        self.assertFalse(wants_invite_link('Add a reminder for 8pm'))

    def test_unmentioned_question_gate_and_side_effect_safety(self):
        for text in ('How much to upgrade Redox from rank 4 to 5?',
                     'Could you explain that setup?', 'What is the best mech?',
                     'Redox stats', 'When is the next event?', 'any event',
                     'What types of events are in Mech Arena?'):
            with self.subTest(text=text):self.assertTrue(should_answer_unmentioned(text,['Redox']))
        for text in ('Redox is amazing', 'remind me in 20 seconds to write notes',
                     'Can you remind me in 20 seconds to write notes?',
                     'remember that I prefer long range builds', '/upgrade_cost Redox 4 5',
                     'stop my loops', 'set a reminder for any event',
                     'What do you think about <@123456789012345678>?'):
            with self.subTest(text=text):self.assertFalse(should_answer_unmentioned(text,['Redox']))

    def test_operator_command_help_is_personalized_in_any_server(self):
        public=help_text(123456789012345678,1509560571563868160)
        self.assertNotIn('/ping_loop',public)
        self.assertNotIn('/dm_loop',public)
        self.assertNotIn('/stop_user_loops',public)
        for guild_id in (1509560571563868160,999999999999999999):
            operator=help_text(1442058583129722986,guild_id)
            self.assertIn('/ping_loop',operator)
            self.assertIn('/dm_loop',operator)
            self.assertIn('/stop_user_loops',operator)
        self.assertNotIn('/ping_loop',help_text(1442058583129722986,None))

    def test_command_registration(self):
        class Placeholder:pass
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'false','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Placeholder(),Placeholder())
        install_commands(bot)
        self.assertFalse(bot.intents.message_content)
        self.assertFalse(bot.ambient_channel_chat_enabled)
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            ambient_bot=MechBot(Placeholder(),Placeholder())
            self.assertTrue(ambient_bot.intents.message_content)
            self.assertTrue(ambient_bot.ambient_channel_chat_enabled)
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'false','ANNOUNCEMENT_INGESTION_ENABLED':'true'}):
            announcement_bot=MechBot(Placeholder(),Placeholder())
            self.assertTrue(announcement_bot.intents.message_content)
            self.assertFalse(announcement_bot.ambient_channel_chat_enabled)
        commands={x.name for x in bot.tree.get_commands()}
        expected={'help','invite','worker_setup','worker_time','ping_loop','dm_loop','stop_loop','stop_user_loops','upcoming','promo_codes','upgrade_cost'}
        self.assertTrue(expected<=commands)
        self.assertFalse(any(name.startswith('emergency') for name in commands))
        guild=SimpleNamespace(id=1509560571563868160)
        self.assertFalse({'ping_loop','dm_loop','stop_user_loops'}&{x.name for x in bot.tree.get_commands(guild=guild)})
        self.assertTrue(all(bot.tree.get_command(name) for name in ('ping_loop','dm_loop','stop_user_loops')))
        self.assertEqual(OPERATORS,{1442058583129722986,1444891876376186901})
        self.assertTrue(REPLY_MENTIONS.replied_user)
        self.assertFalse(REPLY_MENTIONS.everyone or REPLY_MENTIONS.roles or REPLY_MENTIONS.users)
        for name in ('ping_loop','dm_loop'):
            params={x.name:x for x in bot.tree.get_command(name).parameters}
            self.assertEqual(params['interval_seconds'].min_value,5)
            self.assertEqual(params['duration_seconds'].max_value,1800)
        cost_params={x.name:x for x in bot.tree.get_command('upgrade_cost').parameters}
        self.assertEqual(cost_params['from_rank'].min_value,1)
        self.assertEqual(cost_params['to_rank'].max_value,6)
        self.assertEqual(cost_params['blueprints_owned'].min_value,0)
        self.assertEqual(cost_params['blueprints_owned'].max_value,1000000)
        files=bot.currency_icon_files()
        self.assertEqual({f.filename for f in files},{'emoji_a_coins.png','emoji_credits.png','emoji_blueprints.png'})
        for file in files:file.close()
        for filename in ('emoji_a_coins.png','emoji_credits.png','emoji_blueprints.png'):
            raw=(Path(__file__).resolve().parents[1]/'data'/filename).read_bytes()
            self.assertEqual((int.from_bytes(raw[16:20],'big'),int.from_bytes(raw[20:24],'big')),(128,128))
            self.assertEqual(raw[25],6)  # RGBA: real transparency, not a baked checkerboard
            self.assertLess(len(raw),256*1024)
    def test_dst(self):
        # 02:30 is nonexistent on 2026-03-08 in New York; skip that day.
        from datetime import datetime,timezone
        now=datetime(2026,3,8,5,0,tzinfo=timezone.utc).timestamp()
        result=datetime.fromtimestamp(next_daily(2,30,'America/New_York',now),timezone.utc)
        self.assertEqual(result.isoformat(),'2026-03-09T06:30:00+00:00')

class AIQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_upgrade_cost_skips_ai_and_is_blocked(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        question='How much will it cost to upgrade Redox from rank 4 to 5?'
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It costs 12,000 A-Coins.')) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,Catalog())
        ai.assert_not_awaited()
        self.assertIn('Redox from rank 4 to rank 5',answer)
        self.assertIn('won’t guess',answer)
        self.assertNotIn('12,000',answer)
        self.assertNotIn('calculator',answer.lower())
        self.assertNotIn('database',answer.lower())

    async def test_unverified_event_questions_never_fall_through_to_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        question='What types of events are in Mech Arena?'
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='Arena Blitz and Co-Op Assault happen every week.')) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,Catalog())
        ai.assert_not_awaited()
        self.assertIn('verified list of Mech Arena event types',answer)
        self.assertIn('won’t invent',answer)
        self.assertNotIn('Arena Blitz',answer)

    async def test_event_archive_excerpt_is_returned_without_ai_rewrite(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
            def search_confirmed_archive(self,question,entities,limit):
                return [{'kind':'public_document','title':'Official event note',
                    'source':'plarium_forum','url':'https://forum.plarium.com/topic/123/',
                    'excerpt':'The post says the event begins on its stated date.'}]
        question='What does the event announcement say?'
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='Arena Blitz starts every Monday.')) as ai:
            answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,Catalog())
        ai.assert_not_awaited()
        self.assertIn('The post says the event begins on its stated date.',answer)
        self.assertIn('[Official event note](https://forum.plarium.com/topic/123/)',answer)
        self.assertNotIn('Arena Blitz',answer)

    async def test_ai_payload_receives_only_the_last_five_turn_pairs(self):
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return None
            async def json(self,content_type=None):return {'choices':[{'message':{'content':'Short answer.'}}]}
        class Session:
            def post(self,*args,**kwargs):self.kwargs=kwargs;return Response()
        history=[]
        for idx in range(6):
            history.extend([{'role':'user','content':f'question {idx}'},
                            {'role':'assistant','content':f'answer {idx} '+'x'*1600}])
        session=Session()
        await general_ai('https://gateway.invalid/v1','key','model','follow-up',session=session,
                         conversation_history=history)
        payload=session.kwargs['json']
        sent=json.loads(payload['messages'][1]['content'])['recent_conversation']
        self.assertEqual(len(sent),10)
        self.assertEqual(sent[0],{'role':'user','content':'question 1'})
        self.assertEqual(sent[-1]['content'][:9],'answer 5 ')
        self.assertLessEqual(max(len(item['content']) for item in sent),1500)
        self.assertNotIn('other_user',repr(sent))

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

    async def test_same_rank_level_calculation_and_identical_position_return_verified_zero(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):runtime=initialize_snapshot()
            catalog=Catalog(runtime)
            question='what is upgrade cost of rank 5 l1 to rank 5 l1 for redox.'
            with patch('mechbot.assistant.general_ai',new=AsyncMock()) as ai:
                answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
            ai.assert_not_awaited()
            self.assertIn('same rank and level',answer)
            self.assertIn('0 A-Coins',answer)
            self.assertIn('0 Credits',answer)
            self.assertIn('0 Blueprints (BP)',answer)
            self.assertNotIn('no confirmed',answer.lower())
            catalog.close()

    async def test_confirmed_exact_level_cost_includes_all_three_resources_without_ai(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):
                runtime=initialize_snapshot()
            catalog=Catalog(runtime)
            question='UPGRADE COST FOR REDOX R4 L1 TO R5 L1'
            with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='It costs 999 Credits.')) as ai:
                answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
            ai.assert_not_awaited()
            self.assertIn('rank 4 L1 → rank 5 L1',answer)
            self.assertIn('2,825 A-Coins',answer)
            self.assertIn('265,000 Credits',answer)
            self.assertIn('10 Blueprints (BP)',answer)
            self.assertNotIn('999',answer)
            followup='what about blueprints?'
            history=[{'role':'user','content':question},{'role':'assistant','content':answer}]
            with patch('mechbot.assistant.general_ai',new=AsyncMock()) as ai:
                followup_answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',
                    followup,catalog,conversation_history=history)
            ai.assert_not_awaited()
            self.assertIn('rank 4 L1 → rank 5 L1',followup_answer)
            self.assertIn('10 Blueprints (BP)',followup_answer)
            catalog.close()


class QuestionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_guild_mirrors_include_restricted_commands_in_any_server(self):
        bot=MechBot(object(),object());install_commands(bot)
        with patch.object(bot.tree,'sync',new=AsyncMock()):
            for guild_id in (1509560571563868160,999999999999999999):
                await bot.sync_guild_commands(guild_id)
                commands={x.name for x in bot.tree.get_commands(guild=SimpleNamespace(id=guild_id))}
                self.assertTrue({'ping_loop','dm_loop','stop_user_loops'}<=commands)
                self.assertIn('help',commands)

    async def test_operator_loop_callbacks_reject_nonoperators_in_any_guild(self):
        bot=MechBot(object(),object());install_commands(bot)
        unauthorized=SimpleNamespace(id=123456789012345678)
        member=SimpleNamespace(id=223456789012345678,bot=False)
        for name,args in (('ping_loop',(member,5,10,'check this')),
                          ('dm_loop',(member,5,10,'check this')),
                          ('stop_user_loops',(member,))):
            response=SimpleNamespace(send_message=AsyncMock())
            interaction=SimpleNamespace(guild=SimpleNamespace(id=999999999999999999),user=unauthorized,
                                        response=response)
            command=bot.tree.get_command(name)
            await command.callback(interaction,*args)
            response.send_message.assert_awaited_once()
            self.assertIn('authorized operator IDs',response.send_message.await_args.args[0])

    async def test_legacy_emergency_phrase_does_not_cancel_loops(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.conversation_history_for=AsyncMock(return_value=[])
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=44),guild=None,
            mentions=[bot_user],content=f'<@{bot_user.id}> stop emergency',reference=None)
        with (patch.object(bot.alerts,'stop') as stop,
              patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(return_value=None))):
            await bot.on_message(message)
        stop.assert_not_called()

    async def test_upgrade_cost_command_passes_owned_blueprints_to_purchase_estimate(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
            def upgrade_cost(self,*args):
                self.called=args
                return {'item':'Redox','from_rank':4,'from_level':1,'to_rank':5,'to_level':1,
                        'cost':{'acoins':2825,'credits':265000,'blueprints':10},
                        'rarity':'legendary','blueprint_acoins_per_blueprint':240,
                        'assumed_rank_transition':False,'no_op':False}
        catalog=Catalog()
        bot=MechBot(catalog,object());install_commands(bot)
        bot.currency_emojis={'acoins':'<:a:1>','credits':'<:c:2>','blueprints':'<:b:3>'}
        response=SimpleNamespace(send_message=AsyncMock())
        interaction=SimpleNamespace(response=response)
        await bot.tree.get_command('upgrade_cost').callback(interaction,'Redox',4,5,1,1,4)
        self.assertEqual(catalog.called,('Redox',4,5,1,1))
        response.send_message.assert_awaited_once()
        sent=response.send_message.await_args
        self.assertIn('1,440 A-Coins',sent.args[0])
        self.assertIn('4,265 A-Coins',sent.args[0])
        self.assertTrue(sent.kwargs['ephemeral'])
        allowed=sent.kwargs['allowed_mentions']
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)

    async def test_promo_response_reads_only_configured_cached_channels(self):
        posts=[
            {'channel_id':str(DEFAULT_ANNOUNCEMENT_CHANNELS[0]),'content':'ECR promo code: SAFE2026',
             'posted_at':200,'channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/200'},
            {'channel_id':'999999999999999999','content':'Promo code: OUTSIDE2026',
             'posted_at':300,'channel_name':'other','guild_name':'Other Server',
             'message_url':'https://discord.com/channels/3/4/300'},
        ]
        store=SimpleNamespace(supports_announcements=True,recent_announcements=AsyncMock(return_value=posts))
        with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):
            bot=MechBot(object(),store)
        result=await bot.promo_codes_response()
        store.recent_announcements.assert_awaited_once_with(0,limit=2000)
        self.assertIn('SAFE2026',result)
        self.assertNotIn('OUTSIDE2026',result)

    async def test_promo_command_answers_on_request_ephemerally(self):
        bot=MechBot(object(),object());install_commands(bot)
        bot.promo_codes_response=AsyncMock(return_value='Recent codes')
        response=SimpleNamespace(defer=AsyncMock())
        followup=SimpleNamespace(send=AsyncMock())
        interaction=SimpleNamespace(response=response,followup=followup)
        await bot.tree.get_command('promo_codes').callback(interaction)
        response.defer.assert_awaited_once_with(ephemeral=True)
        followup.send.assert_awaited_once()
        self.assertEqual(followup.send.await_args.args[0],'Recent codes')
        self.assertTrue(followup.send.await_args.kwargs['ephemeral'])
        allowed=followup.send.await_args.kwargs['allowed_mentions']
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)

    async def test_opted_in_ambient_promo_query_is_unpinged_and_skips_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.promo_codes_response=AsyncMock(return_value='Recent codes')
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='ANY ECR',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.promo_codes_response.assert_awaited_once()
        bot.reply_message.assert_awaited_once_with(message,'Recent codes',mention_author=False)

    async def test_plain_language_calculation_is_local_and_unpinged(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='What is 40 plus 2?',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.reply_message.assert_awaited_once_with(message,'Result: 42',mention_author=False)

    async def test_plain_language_build_lookup_uses_known_mech_only(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='Any builds for Redox?',reference=None)
        with (patch('mechbot.bot.fetch_builds',new=AsyncMock(return_value={'builds':[{'name':'Redox setup'}]})) as fetch,
              patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain):
            await bot.on_message(message)
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1],'Redox')
        self.assertIn('Redox setup',bot.reply_message.await_args.args[1])
        self.assertFalse(bot.reply_message.await_args.kwargs['mention_author'])
        brain.assert_not_awaited()

    async def test_plain_language_upgrade_cost_is_deterministic(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
            def upgrade_cost(self,*args):
                return {'item':'Redox','from_rank':4,'from_level':7,'to_rank':5,'to_level':1,
                        'cost':{'acoins':2825,'credits':265000,'blueprints':10},
                        'rarity':'legendary','blueprint_acoins_per_blueprint':240,
                        'assumed_rank_transition':True,'no_op':False}
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false',
                                    'AI_API_KEY':'','AI_MODEL':''}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='How much does it cost to upgrade Redox from rank 4 to 5?',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(wraps=answer_question_with_brain)) as brain:
            await bot.on_message(message)
        brain.assert_awaited_once()
        self.assertIn('2,825 A-Coins',bot.reply_message.await_args.args[1])
        self.assertFalse(bot.reply_message.await_args.kwargs['mention_author'])

    async def test_explicit_ambient_reminder_is_private_and_never_pings(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.create_reminder=AsyncMock(return_value='Reminder #2 set for shortly from now.')
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='Could you remind me in 20 seconds to check the event?',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        bot.create_reminder.assert_awaited_once_with(author.id,'in 20 seconds','check the event')
        self.assertEqual(author.send.await_count,2)
        allowed=author.send.await_args.kwargs['allowed_mentions']
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)
        self.assertEqual(bot.reply_message.await_count,1)
        self.assertFalse(bot.reply_message.await_args.kwargs['mention_author'])
        brain.assert_not_awaited()

    async def test_ambient_private_reply_failure_still_does_not_ping(self):
        bot=MechBot(object(),object())
        bot.reply_message=AsyncMock()
        error=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'DM blocked')
        author=SimpleNamespace(send=AsyncMock(side_effect=error))
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999))
        await bot.private_reply(message,'Reminder set.',mention_author=False)
        bot.reply_message.assert_awaited_once_with(
            message,'I could not DM you. Please enable DMs and try again.',mention_author=False)

    async def test_application_currency_emojis_are_registered_from_transparent_assets(self):
        class Response:
            def __init__(self,status,body):self.status=status;self.body=body
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return None
            async def json(self,content_type=None):return self.body
        class Session:
            def __init__(self,*args,**kwargs):self.posts=[]
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return None
            def get(self,*args,**kwargs):return Response(200,{'items':[]})
            def post(self,*args,**kwargs):
                payload=kwargs['json'];self.posts.append(payload)
                return Response(201,{'id':str(len(self.posts)),'name':payload['name']})
        bot=MechBot(object(),object())
        bot._connection.application_id=1570061533898211408
        session=Session()
        with (patch.dict(os.environ,{'DISCORD_TOKEN':'fake-token'}),
              patch('mechbot.bot.aiohttp.ClientSession',return_value=session)):
            emojis=await bot.ensure_application_currency_emojis()
        self.assertEqual(set(emojis),{'acoins','credits','blueprints'})
        self.assertEqual([payload['name'] for payload in session.posts],[
            'archangle_a_coins_v2','archangle_credits_v2','archangle_blueprints_v2'])
        self.assertEqual(len(session.posts),3)
        self.assertTrue(all(payload['image'].startswith('data:image/png;base64,') for payload in session.posts))
        self.assertTrue(all(len(payload['image'])>1000 for payload in session.posts))

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
        store=SimpleNamespace(supports_conversations=True,add_conversation_turn=AsyncMock(),
                              conversation_history=AsyncMock(return_value=[]))
        bot=MechBot(Catalog(),store)
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
        bot.private_reply.assert_awaited_once_with(message,'Reminder #1 set for shortly from now.',mention_author=True)
        author.send.assert_awaited_once()
        brain.assert_not_awaited()
        store.conversation_history.assert_not_awaited()
        store.add_conversation_turn.assert_not_awaited()

    async def test_natural_private_notes_are_not_read_or_saved_as_conversation(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        store=SimpleNamespace(supports_conversations=True,add_conversation_turn=AsyncMock(),
                              conversation_history=AsyncMock(return_value=[]),list_notes=AsyncMock(return_value=[]),
                              add_note=AsyncMock(return_value=4))
        bot=MechBot(Catalog(),store)
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.private_reply=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=44),guild=SimpleNamespace(id=99),
            mentions=[bot_user],content=f'<@{bot_user.id}> remember that I prefer long range builds',
            reference=None,reply=AsyncMock())
        await bot.on_message(message)
        store.add_note.assert_awaited_once_with(author.id,'I prefer long range builds')
        store.conversation_history.assert_not_awaited()
        store.add_conversation_turn.assert_not_awaited()

    async def test_any_event_request_uses_announcement_lookup_not_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.reply_message=AsyncMock()
        bot.upcoming_response=AsyncMock(return_value='No verified event announcement is saved.')
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[bot_user],content=f'<@{bot_user.id}> any event',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.upcoming_response.assert_awaited_once_with()
        bot.reply_message.assert_awaited_once_with(message,'No verified event announcement is saved.',mention_author=True)

    async def test_direct_replies_work_and_bystander_reply_chains_are_ignored_by_default(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'false','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        channel=SimpleNamespace(id=444)
        directed=SimpleNamespace(author=author,channel=channel,guild=SimpleNamespace(id=999),mentions=[],
            content='Could you explain that setup?',
            reference=SimpleNamespace(message_id=10,resolved=SimpleNamespace(author=bot_user)),reply=AsyncMock())
        bystander=SimpleNamespace(author=author,channel=channel,guild=SimpleNamespace(id=999),mentions=[],
            content='What is the next event?',
            reference=SimpleNamespace(message_id=11,resolved=SimpleNamespace(author=SimpleNamespace(id=333))),reply=AsyncMock())
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(return_value='AI answer')) as brain:
            await bot.on_message(directed)
            self.assertEqual(brain.await_count,1)
            await bot.on_message(bystander)
            self.assertEqual(brain.await_count,1)
        directed.reply.assert_awaited_once()
        bystander.reply.assert_not_awaited()

    async def test_opted_in_ambient_question_gets_unpinged_reply_and_scoped_history(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        store=SimpleNamespace(supports_conversations=True,conversation_history=AsyncMock(return_value=[]),
                              add_conversation_turn=AsyncMock())
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),store)
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        author=SimpleNamespace(id=123456789012345678,bot=False,send=AsyncMock())
        ambient=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='How much will Redox cost to upgrade from rank 4 to 5?',reference=None,reply=AsyncMock())
        reply_chain=SimpleNamespace(author=author,channel=ambient.channel,guild=ambient.guild,mentions=[],
            content='What is the next event?',reference=SimpleNamespace(message_id=12,
                resolved=SimpleNamespace(author=SimpleNamespace(id=333))),reply=AsyncMock())
        plain_statement=SimpleNamespace(author=author,channel=ambient.channel,guild=ambient.guild,mentions=[],
            content='Redox is amazing',reference=None,reply=AsyncMock())
        repeated=SimpleNamespace(author=author,channel=ambient.channel,guild=ambient.guild,mentions=[],
            content='What about Redox power?',reference=None,reply=AsyncMock())
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(return_value='AI answer')) as brain:
            await bot.on_message(ambient)
            await bot.on_message(reply_chain)
            await bot.on_message(plain_statement)
            await bot.on_message(repeated)
        brain.assert_awaited_once()
        ambient.reply.assert_awaited_once()
        repeated.reply.assert_not_awaited()
        self.assertFalse(ambient.reply.await_args.kwargs['mention_author'])
        self.assertEqual(store.conversation_history.await_count,1)
        self.assertEqual(store.add_conversation_turn.await_count,1)
        reply_chain.reply.assert_not_awaited()
        plain_statement.reply.assert_not_awaited()
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
            counts=db.execute("SELECT source,review_status,trust,count(*) FROM archive_documents WHERE source IN ('mech_arena_database','plarium_forum','upgrade_calculator') GROUP BY source,review_status,trust ORDER BY source").fetchall()
            self.assertEqual(counts,[('mech_arena_database','verified','user_confirmed',52),
                ('plarium_forum','verified','user_confirmed',59),
                ('upgrade_calculator','verified','user_confirmed',36)])
            imported_at=db.execute("SELECT value FROM metadata WHERE key='drive_ocr_imported_at_utc'").fetchone()[0]
            from datetime import datetime
            self.assertIsNotNone(datetime.fromisoformat(imported_at).tzinfo)
            for table,column,where in (
                ('archive_documents','first_seen_utc',"source IN ('google_drive_archive','user_chat_upload')"),
                ('archive_documents','last_checked_utc',"source IN ('google_drive_archive','user_chat_upload')"),
                ('archive_image_ocr','processed_at_utc',"source_url LIKE 'drive://%' OR source_url LIKE 'upload://%'"),
                ('archive_ingest_queue','created_at_utc',"document_id IN (SELECT id FROM archive_documents WHERE source IN ('google_drive_archive','user_chat_upload'))"),
            ):
                values={row[0] for row in db.execute(f'SELECT {column} FROM {table} WHERE {where}')}
                self.assertEqual(values,{imported_at})
            # The user's new approval promotes only the pinned raw OCR sources.
            self.assertEqual(db.execute("SELECT source,review_status,trust,count(*) FROM archive_documents WHERE source IN ('google_drive_archive','user_chat_upload') GROUP BY source,review_status,trust ORDER BY source").fetchall(),
                [('google_drive_archive','verified','user_confirmed',2),('user_chat_upload','verified','user_confirmed',1)])
            self.assertEqual(db.execute("SELECT archive_name,review_status,count(*) FROM archive_image_ocr WHERE source_url LIKE 'drive://%' GROUP BY archive_name,review_status ORDER BY archive_name").fetchall(),
                [('MA SS.zip','verified',48),('PILOT.zip','verified',36)])
            self.assertEqual(db.execute("SELECT review_status,count(*) FROM archive_image_ocr WHERE source_url LIKE 'upload://%' GROUP BY review_status").fetchall(),
                [('verified',6)])
            self.assertEqual(db.execute("SELECT count(*) FROM archive_structured_rows r JOIN archive_documents d ON d.id=r.document_id WHERE d.source IN ('mech_arena_database','plarium_forum','upgrade_calculator')").fetchone()[0],1338)
            self.assertEqual(db.execute("SELECT collection,count(*) FROM archive_structured_rows WHERE collection IN ('drive_screenshot_ocr','user_upload_screenshot_ocr') GROUP BY collection ORDER BY collection").fetchall(),
                [('drive_screenshot_ocr',84),('user_upload_screenshot_ocr',6)])
            self.assertEqual(db.execute("SELECT collection,count(*) FROM archive_structured_rows WHERE collection IN ('drive_mech_stat_candidates','drive_pilot_stat_candidates') GROUP BY collection ORDER BY collection").fetchall(),
                [('drive_mech_stat_candidates',451),('drive_pilot_stat_candidates',72)])
            self.assertEqual(db.execute("SELECT collection,count(*) FROM archive_structured_rows WHERE collection IN ('drive_screenshot_ocr','user_upload_screenshot_ocr') AND json_extract(data_json,'$.review_status')='verified' GROUP BY collection ORDER BY collection").fetchall(),
                [('drive_screenshot_ocr',84),('user_upload_screenshot_ocr',6)])
            candidate=json.loads(db.execute("SELECT data_json FROM archive_structured_rows WHERE collection='drive_mech_stat_candidates' ORDER BY id LIMIT 1").fetchone()[0])
            self.assertEqual(candidate['review_status'],'needs_review')
            self.assertEqual(candidate['candidate_status'],'needs_review')
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
            cost_l1=catalog.upgrade_cost('Redox',4,5,1,1)
            self.assertEqual(cost_l1['cost'],{'credits':265000,'acoins':2825,'blueprints':10})
            self.assertEqual((cost_l1['rarity'],cost_l1['blueprint_acoins_per_blueprint']),('legendary',240))
            epic=catalog.upgrade_cost('Panther',4,5,1,1)
            self.assertEqual((epic['rarity'],epic['blueprint_acoins_per_blueprint']),('epic_c',48))
            same=catalog.upgrade_cost('Redox',5,5,1,1)
            self.assertEqual(same['cost'],{'credits':0,'acoins':0,'blueprints':0})
            self.assertTrue(same['no_op'])
            same_rank_level=catalog.upgrade_cost('Redox',5,5,1,2)
            self.assertEqual(same_rank_level['cost'],{'credits':37500,'acoins':0,'blueprints':2})
            self.assertFalse(same_rank_level['no_op'])
            self.assertIsNone(catalog.upgrade_cost('Redox',5,5))
            self.assertIsNone(catalog.upgrade_cost('Redox',5,5,2,1))
            context=game_knowledge_context('UPGRADE COST FOR REDOX R4 L1 TO R5 L1',catalog)
            self.assertEqual((context['rank_change'],context['from_level'],context['to_level']),(['4','5'],1,1))
            self.assertEqual(catalog.weapon('Dreadshot 8')['listing']['list'],'Dreadshot 8')
            self.assertIsNone(catalog.weapon('Dreadshot 10')['listing'])
            self.assertIsNone(catalog.upgrade_cost('Dreadshot 10',4,5))
            hits=catalog.search_confirmed_archive('what happened to Voidghost?',['Voidghost'],limit=4)
            self.assertTrue(any(x.get('title')=='Voidghost Update: Next Steps' for x in hits))
            raw_ocr=catalog._user_confirmed_raw_ocr_records()
            self.assertEqual(len(raw_ocr),90)
            pilot_hits=catalog.search_confirmed_archive('CROW pilot bonus',['CROW'],limit=8)
            self.assertTrue(any(x.get('kind')=='user_confirmed_raw_ocr' and 'CROW' in x.get('excerpt','')
                                for x in pilot_hits))
            chat_hits=catalog.search_confirmed_archive('Redox legendary mech',['Redox'],limit=8)
            safe_chat=[x for x in chat_hits if x.get('source')=='user_chat_upload']
            self.assertTrue(safe_chat)
            for private_text in ('whoinputted','gatater','Nub ANKR','ARCHANGLE APP'):
                self.assertNotIn(private_text.lower(),safe_chat[0]['excerpt'].lower())
            self.assertIn('Redox',safe_chat[0]['excerpt'])
            catalog.close()

    def test_changed_archive_does_not_get_implicitly_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'staging.sqlite3'
            with gzip.open(ARCHIVE,'rb') as source,open(path,'wb') as target:
                shutil.copyfileobj(source,target)
            self.assertEqual(apply_user_confirmed_public_review(path,expected_fingerprint='not-the-reviewed-snapshot'),0)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_documents WHERE review_status='pending' AND source IN ('mech_arena_database','plarium_forum','upgrade_calculator')").fetchone()[0],147)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_documents WHERE source='google_drive_archive' AND review_status='verified' AND trust='user_confirmed'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_image_ocr WHERE archive_name IN ('PILOT.zip','MA SS.zip','chat_uploads') AND review_status='verified'").fetchone()[0],90)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_structured_rows WHERE collection IN ('drive_pilot_stat_candidates','drive_mech_stat_candidates') AND json_extract(data_json,'$.candidate_status')='needs_review'").fetchone()[0],523)
            db.close()


class DataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.db')
    async def asyncTearDown(self):
        await self.store.close();self.temp.cleanup()
    async def test_announcement_upsert_and_message_cursor(self):
        base={'channel_id':'100','guild_id':'200','guild_name':'Public Server','channel_name':'news',
              'message_url':'https://discord.com/channels/200/100/1001','edited_at':None,'fetched_at':300}
        first={**base,'message_id':'1000','content':'Old event','posted_at':100}
        latest={**base,'message_id':'1001','content':'Event starts November 15, 2026','posted_at':200}
        await self.store.upsert_announcements([first,latest])
        self.assertEqual(await self.store.announcement_cursor(100),1001)
        rows=await self.store.recent_announcements(0)
        self.assertEqual(len(rows),2)
        latest['content']='Updated event starts November 20, 2026'
        await self.store.upsert_announcements([latest])
        rows=await self.store.recent_announcements(150)
        self.assertEqual(rows[0]['content'],'Updated event starts November 20, 2026')
        self.assertEqual(await self.store.announcement_cursor(100),1001)
    async def test_conversation_memory_scope_turn_cap_and_five_hour_expiry(self):
        now=int(time.time())
        for idx in range(CONVERSATION_TURN_LIMIT+2):
            self.assertTrue(await self.store.add_conversation_turn('user-a','guild-a','channel-a',
                f'question {idx}',f'answer {idx}',created_at=now+idx))
        await self.store.add_conversation_turn('user-a','guild-a','channel-b','other channel','separate answer',created_at=now)
        await self.store.add_conversation_turn('user-a','guild-b','channel-a','other guild','separate answer',created_at=now)
        await self.store.add_conversation_turn('user-b','guild-a','channel-a','other user','separate answer',created_at=now)
        history=await self.store.conversation_history('user-a','guild-a','channel-a',now=now+10)
        self.assertEqual(len(history),CONVERSATION_TURN_LIMIT*2)
        self.assertEqual([item['content'] for item in history if item['role']=='user'],
                         [f'question {i}' for i in range(2,7)])
        self.assertEqual([item['content'] for item in history if item['role']=='assistant'],
                         [f'answer {i}' for i in range(2,7)])
        self.assertEqual((await self.store.conversation_history('user-a','guild-a','channel-b',now=now+10))[0]['content'],'other channel')
        self.assertEqual((await self.store.conversation_history('user-a','guild-b','channel-a',now=now+10))[0]['content'],'other guild')
        self.assertEqual((await self.store.conversation_history('user-b','guild-a','channel-a',now=now+10))[0]['content'],'other user')
        await self.store.delete_expired_conversation_turns(now=now+CONVERSATION_TTL_SECONDS+10)
        remaining=await self.store.fetch('SELECT id FROM conversation_turns')
        self.assertEqual(remaining,[])
        self.assertFalse(await self.store.add_conversation_turn('late','guild-a','channel-a','expired','expired',
            created_at=now-CONVERSATION_TTL_SECONDS))
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
    async def test_ping_loop_followup_is_a_scoped_ping_and_can_be_stopped(self):
        class User:
            id=123456789012345678
            mention='<@123456789012345678>'
        started=asyncio.Event()
        release=asyncio.Event()
        async def hold_delivery(*args,**kwargs):
            started.set()
            await release.wait()
        destination=SimpleNamespace(send=AsyncMock(side_effect=hold_delivery))
        mgr=AlertManager()
        with patch('mechbot.alerts.MIN_INTERVAL',1):
            key=mgr.start(User(),'channel',destination,1,3,9,'Test')
            self.assertFalse(mgr.available(User.id,'channel'))
            await asyncio.wait_for(started.wait(),timeout=2)
            self.assertEqual(destination.send.await_count,1)
            content=destination.send.await_args.args[0]
            allowed=destination.send.await_args.kwargs['allowed_mentions']
            self.assertIn(User.mention,content)
            self.assertFalse(allowed.everyone or allowed.roles or allowed.replied_user)
            self.assertEqual([u.id for u in allowed.users],[User.id])
            self.assertEqual(mgr.stop(User.id,User.id),1)
            release.set()
            await mgr.alerts[key]['task']
            self.assertEqual(destination.send.await_count,1)
        await mgr.close()

    async def test_targeted_stop_cancels_dm_and_channel_loops(self):
        class User:
            def __init__(self,uid):self.id=uid;self.mention=f'<@{uid}>'
        a=User(123456789012345678);b=User(223456789012345678)
        anchor=type('Anchor',(),{'reply':AsyncMock()})()
        mgr=AlertManager()
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
