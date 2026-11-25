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
from mechbot.alerts import AlertManager, MAX_ACTIVE_LOOPS, validate
from mechbot.bot import MechBot, install_commands, OPERATORS, REPLY_MENTIONS, HELP, help_text, configured_announcement_channels
from mechbot.assistant import (message_chunks, GENERAL_ASSISTANT_PROMPT, wants_invite_link, bot_identity_response, direct_item_lookup, should_answer_unmentioned,
                               parse_natural_calculation, natural_builds_request, builds_response, _archive_attribution,
                               _archive_fallback, _supported_game_reply, _comparison_fallback, discord_text,
                               _quotes_verified_cost, naturalize_discord_reply, weapon_response, mech_response, mod_response,
                               game_knowledge_context, answer_question_with_brain, upgrade_cost_response, general_ai,
                               parse_build_sp_range, builds_response_parts, natural_builds_request, pilot_response,
                               _build_author_profile_url)
from mechbot.announcements import (DEFAULT_ANNOUNCEMENT_CHANNELS, INITIAL_BACKFILL_LIMIT, EDIT_REFRESH_LIMIT,
                                   wants_upcoming, wants_ecr_info,
                                   message_text, upcoming_items, ecr_event_items, format_upcoming, format_ecr_events,
                                   wants_promo_codes, promo_code_items, format_promo_codes)
from mechbot.forum_archive import (FORUM_ARCHIVE_PATH, format_forum_request, format_forum_search,
                                   format_recent_forum_threads, recent_forum_threads,
                                   refresh_forum_archive, search_forum_threads, wants_forum_lookup)
from mechbot.catalog import (ARCHIVE, Catalog, _candidate_fingerprint, apply_user_confirmed_ocr_review,
                             apply_user_confirmed_public_review, apply_user_confirmed_drive_ocr_review, initialize_snapshot)
from mechbot.timeparse import next_daily, parse_spec, parse_natural_reminder
from mechbot.source_summaries import combined_source_summary, wants_patch_bug_lookup
from mechbot.bot import ANNOUNCEMENT_BACKFILL_TOTAL_LIMIT
from mechbot import facts as fact_layer

UID='1515232384159715364'
DAY='2026-11-11'

class AnnouncementTests(unittest.TestCase):
    def test_upcoming_detection_and_explicit_year_safety(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.assertEqual(INITIAL_BACKFILL_LIMIT,100)
        self.assertEqual(set(DEFAULT_ANNOUNCEMENT_CHANNELS),{1524319795791659080,1570083896823906364})
        self.assertNotIn(1512089401193271539,DEFAULT_ANNOUNCEMENT_CHANNELS)
        with patch.dict(os.environ,{'ANNOUNCEMENT_CHANNEL_IDS':
                '1524319795791659080,1512089401193271539,1570083896823906364'}):
            self.assertEqual(configured_announcement_channels(),DEFAULT_ANNOUNCEMENT_CHANNELS)
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
        self.assertEqual([x['date_label'] for x in items],['November 15, 2026','Date not stated','November 12'])
        response=format_upcoming(rows,enabled=True,now=now)
        self.assertIn('**Event date:** November 15, 2026',response)
        self.assertIn('**Event date mentioned:** November 12 (year not stated)',response)
        self.assertIn('**Event date not stated · Post date:** Nov 10, 2026',response)
        self.assertIn('publication date, not the event date',response)
        self.assertNotIn('discord.com/channels/',response)
        self.assertNotIn('#news',response)
        self.assertNotIn('November 9, 2026',response)

    def test_posting_dates_in_body_are_not_mistaken_for_event_dates(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,11,10,12,tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
        rows=[
            {'content':'Event post published on November 9, 2026.','posted_at':int(now-1000)},
            {'content':'Event post published on November 9, 2026. The tournament starts December 15, 2026.',
             'posted_at':int(now-1000)},
            {'content':'Event schedule was announced on November 9, 2026.','posted_at':int(now-1000)},
            {'content':'The event dates were announced on November 9, 2026, and the tournament starts December 15, 2026.',
             'posted_at':int(now-1000)},
            {'content':'Post published on November 9. The tournament starts December 15.',
             'posted_at':int(now-1000)},
            {'content':'ECR event announcement on November 9, 2026.','posted_at':int(now-1000)},
            {'content':'ECR event announcement on November 9, 2026. The ECR event starts December 16, 2026.',
             'posted_at':int(now-1000)},
        ]
        items=upcoming_items(rows,now=now)
        self.assertEqual(len(items),7)
        response=format_upcoming(rows,enabled=True,now=now)
        self.assertIn('Event date not stated',response)
        self.assertIn('December 15, 2026',response)
        self.assertIn('**Post date:** Nov 10, 2026',response)
        self.assertIn('Event date:** December 16, 2026',response)
        self.assertNotIn('Event date:** November 9, 2026',response)
        self.assertNotIn('Event post published on November 9, 2026',response)
        self.assertNotIn('schedule was announced on November 9, 2026',response)
        self.assertIn('Event date:** December 15, 2026',response)
        self.assertIn('Event date mentioned:** December 15 (year not stated)',response)
        self.assertNotIn('event dates were',response.casefold())

    def test_promo_lookup_only_extracts_labeled_codes_and_preserves_caveats(self):
        self.assertTrue(wants_promo_codes('Any ECR promo codes?'))
        self.assertTrue(wants_promo_codes('latest ECR code'))
        self.assertTrue(wants_promo_codes('ECR promo codes'))
        self.assertTrue(wants_promo_codes('latest ECR code?'))
        self.assertTrue(wants_promo_codes('PROMOCODE'))
        self.assertTrue(wants_promo_codes('promo code please'))
        self.assertFalse(wants_promo_codes('ANY ECR'))
        self.assertFalse(wants_promo_codes('latest ECR'))
        self.assertFalse(wants_promo_codes('current ECR'))
        self.assertFalse(wants_promo_codes('What is the next event?'))
        self.assertFalse(wants_promo_codes('What is ECR?'))
        for query in ('ECR','ANY ECR','latest ECR','current ECR','What is ECR?',
                      'Event Crate Rush schedule','ECR event details'):
            with self.subTest(query=query):self.assertTrue(wants_ecr_info(query))
        self.assertFalse(wants_ecr_info('latest ECR promo codes'))
        self.assertTrue(should_answer_unmentioned('latest ECR'))
        self.assertTrue(should_answer_unmentioned('ANY ECR'))
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
        self.assertIn('posted Jan 1, 1970',response)
        self.assertNotIn('https://discord.com/channels/',response)
        self.assertIn('not verified in-game',response)
        expired=promo_code_items([{'content':'Promo code: OLD2024 has expired','posted_at':400}])
        self.assertEqual(expired[0]['status'],'marked_expired')
        compact=promo_code_items([{'content':'PROMOCODE: NEW2026','posted_at':450}])
        self.assertEqual([item['code'] for item in compact],['NEW2026'])
        self.assertEqual(promo_code_items([{'content':'Try NEW2026 now','posted_at':500}]),[])
        self.assertEqual(promo_code_items([{'content':'Promo code: SAFE2026','posted_at':'not-a-time'}])[0]['posted_at'],0)
        self.assertEqual(len(ecr_event_items([{'content':'ECR event starts December 10, 2026.',
                                               'posted_at':'not-a-time'}])),1)

    def test_ecr_event_updates_are_separate_from_promo_code_posts(self):
        rows=[
            {'content':'Event Crate Rush update: next event starts November 20. New crate rewards will be announced.',
             'posted_at':1794225600,'channel_id':'1','channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/200'},
            {'content':'ECR Promo codes: `SAVE2026`','posted_at':1794225700,'channel_id':'1',
             'channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/300'},
        ]
        items=ecr_event_items(rows)
        self.assertEqual(len(items),1)
        self.assertIn('next event starts November 20',items[0]['excerpt'])
        response=format_ecr_events(rows,enabled=True)
        self.assertIn('Event Crate Rush summary',response)
        self.assertIn('next event starts November 20',response)
        self.assertIn('**Event date mentioned:** November 20 (year not stated)',response)
        self.assertIn('**Post date:** Nov 9, 2026',response)
        self.assertNotIn('SAVE2026',response)
        self.assertNotIn('https://discord.com/channels/',response)
        self.assertNotIn('#events',response)
        blocked=format_ecr_events([],enabled=True,access_states={1:'missing'})
        self.assertIn('ECR event update',blocked)
        self.assertNotIn('promo-code post',blocked)

    def test_announcement_message_text_reads_embeds_without_pinging(self):
        embed=SimpleNamespace(title='@everyone Event notice',description='Starts soon',fields=[])
        msg=SimpleNamespace(content='Update @here <@123456789012345678>',embeds=[embed])
        text=message_text(msg)
        self.assertIn('Update @\u200bhere [member]',text)
        self.assertIn('@\u200beveryone Event notice',text)
        self.assertIn('Starts soon',text)


class SourceSummaryTests(unittest.TestCase):
    @staticmethod
    def _post(title, content, created='2026-11-09 10:00', url='https://forum.plarium.com/mech-arena/702_discussion/123/'):
        return {'title':title,'content':content,'excerpt':content,'url':url,
                'created':{'iso':created,'display':'Nov 9, 2026, 10:00'}}

    def test_event_summary_combines_sources_and_labels_post_dates_separately(self):
        from datetime import datetime, timezone
        now=datetime(2026,11,10,12,tzinfo=timezone.utc).timestamp()
        forum_posts=[
            self._post('Tournament schedule','The tournament starts December 15, 2026. Rewards include a new crate.'),
            self._post('Event teaser','The next event is still being prepared; rewards will be announced soon.',
                       url='https://forum.plarium.com/mech-arena/702_discussion/124/'),
        ]
        announcements=[
            {'content':'Event schedule: The next event date is not stated; new rewards will be announced soon.',
             'posted_at':int(now-86400),'channel_id':'123','channel_name':'private-channel',
             'message_url':'https://discord.com/channels/1/2/3'}]
        with patch('mechbot.source_summaries.search_forum_threads',return_value=forum_posts):
            result=combined_source_summary('upcoming events',announcements,topic='events',now=now)
        self.assertIn('Tournament schedule',result)
        self.assertIn('December 15, 2026',result)
        self.assertIn('Event date not stated · Post date:',result)
        self.assertIn('Nov 9, 2026',result)
        self.assertIn('publication date, not the event date',result)
        self.assertNotIn('private-channel',result)
        self.assertNotIn('discord.com/channels/',result)
        self.assertNotIn('forum.plarium.com',result)
        self.assertNotIn('(forum post)',result)

    def test_promo_code_summary_combines_sources_but_uses_only_labeled_codes(self):
        forum_posts=[
            self._post('Promo code drop','Promo code: FORUM2026 is available for a limited time.'),
            self._post('Chat mention','Try RANDOM2026 in the shop.',url='https://forum.plarium.com/mech-arena/702_discussion/125/'),
        ]
        announcements=[{'content':'ECR promo code: NEWS2026','posted_at':1794200000,
                        'channel_name':'private-channel','message_url':'https://discord.com/channels/1/2/4'}]
        with patch('mechbot.source_summaries.search_forum_threads',return_value=forum_posts):
            result=combined_source_summary('latest promo codes',announcements,topic='promo',now=1794297600)
        self.assertIn('FORUM2026',result)
        self.assertIn('NEWS2026',result)
        self.assertNotIn('RANDOM2026',result)
        self.assertNotIn('forum.plarium.com',result)
        self.assertNotIn('discord.com/channels/',result)
        self.assertIn('validity',result)

    def test_patch_and_bug_summary_searches_both_sources(self):
        self.assertTrue(wants_patch_bug_lookup('Any recent patch notes or bug reports?'))
        forum_posts=[self._post('Known bug report','The Guardian shield can fail to activate after respawn.')]
        announcements=[{'content':'Patch notes: Version 4.2 fixes the Guardian shield crash.',
                        'posted_at':1794200000,'message_url':'https://discord.com/channels/1/2/5'}]
        with patch('mechbot.source_summaries.search_forum_threads',return_value=forum_posts):
            result=combined_source_summary('Any recent patch notes or bug reports?',announcements,
                                           topic='issues',now=1794297600)
        self.assertIn('Known bug report',result)
        self.assertIn('Patch notes',result)
        self.assertIn('Guardian',result)
        self.assertNotIn('forum.plarium.com',result)
        self.assertNotIn('discord.com/channels/',result)


class ForumArchiveTests(unittest.TestCase):
    @staticmethod
    def _thread(title,url,category,text,created,author='Player',role=''):
        return {
            'title':title,'url':url,'category':{'name':category},
            'author':{'name':author,'role':role},
            'created':{'display':created,'iso':'2026-11-12 10:00'},
            'original_post':{'text':text},
        }

    def test_search_and_recent_listing_are_summaries_not_a_schedule(self):
        with tempfile.TemporaryDirectory() as td:
            archive=Path(td)/'forum.json'
            rows=[
                self._thread('Patch notes: Skystrike balance changes',
                    'https://forum.plarium.com/mech-arena/854_news/123456/',
                    'News & Announcements',
                    'The Skystrike balance changes arrive next month.\n\n@everyone please read the post.',
                    'Nov 12, 2026, 10:00','Forum Manager','Community Manager'),
                self._thread('Players discuss Orion',
                    'https://forum.plarium.com/mech-arena/702_game-discussion/123457/',
                    'Game Discussion','Orion feels strong in this match.',
                    'Nov 11, 2026, 10:00'),
                self._thread('Hidden bad link',
                    'https://evil.example/mech-arena/854_news/123458/',
                    'News & Announcements','Skystrike changed.',
                    'Nov 10, 2026, 10:00'),
            ]
            archive.write_text(json.dumps({'metadata':{},'threads':rows}),encoding='utf-8')
            matches=search_forum_threads('Skystrike balance',limit=5,path=archive)
            self.assertEqual(len(matches),1)
            self.assertEqual(matches[0]['author']['role'],'Community Manager')
            self.assertEqual(matches[0]['url'],rows[0]['url'])
            result=format_forum_search('Skystrike balance',path=archive)
            self.assertIn('Patch notes: Skystrike balance changes',result)
            self.assertIn('Skystrike balance changes arrive next month',result)
            self.assertIn('**Post date:** Nov 12, 2026',result)
            self.assertNotIn('forum.plarium.com',result)
            self.assertNotIn('Community Manager',result)
            mentions=format_forum_search('everyone',path=archive)
            self.assertIn('@\u200beveryone',mentions)
            self.assertIn('live event schedule',result)
            self.assertNotIn('evil.example',result)
            latest=format_forum_request('latest forum posts',path=archive)
            self.assertIn('Recent saved forum highlights',latest)
            self.assertIn('live event schedule',latest)
            self.assertIn('Patch notes: Skystrike balance changes',latest)
            self.assertEqual(recent_forum_threads(limit=1,path=archive)[0]['title'],rows[0]['title'])

    def test_supplied_snapshot_is_readable_but_not_claimed_as_current_or_upcoming(self):
        posts=recent_forum_threads(limit=10,path=FORUM_ARCHIVE_PATH)
        self.assertEqual(len(posts),10)
        self.assertEqual(len(json.loads(FORUM_ARCHIVE_PATH.read_text(encoding='utf-8'))['threads']),150)
        self.assertTrue(all(post['url'].startswith('https://forum.plarium.com/mech-arena/') for post in posts))
        response=format_recent_forum_threads(limit=3,path=FORUM_ARCHIVE_PATH)
        self.assertIn('Recent saved forum highlights',response)
        self.assertIn('live event schedule',response)

    def test_every_thread_is_judged_live_or_outdated_from_its_own_date(self):
        """A saved thread is a statement about the game on the day it was written, not now."""
        import tempfile
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc)
        def stamp(days):
            return f'{(today - timedelta(days=days)).strftime("%b")} {(today - timedelta(days=days)).day}, {(today - timedelta(days=days)).year}, 10:00'
        def thread(title, topic, body, days):
            row = self._thread(title, f'https://forum.plarium.com/mech-arena/702_game-discussion/{topic}/',
                               'Game Discussion', body, stamp(days))
            row['created']['iso'] = (today - timedelta(days=days)).strftime('%Y-%m-%d %H:%M')
            return row
        rows = [thread('Fresh thread', 1, 'Voidghost is broken right now.', 2),
                thread('Older thread', 2, 'Voidghost is broken.', 200)]
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'archive.json'
            archive.write_text(json.dumps({'metadata': {}, 'threads': rows}), encoding='utf-8')
            reply = format_forum_request('latest forum posts', path=archive)
        self.assertIn('**Status:** Live discussion', reply)
        self.assertIn('**Status:** Outdated', reply)
        self.assertIn('**LIVE OR OUTDATED**', reply)
        self.assertIn('1 of 2 threads still speaks for the current build', reply)

    def test_an_old_thread_is_never_described_as_current_opinion(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc)
        old = today - timedelta(days=400)
        row = self._thread('A year-old tier list', 'https://forum.plarium.com/mech-arena/702_game-discussion/3/',
                           'Game Discussion', 'Voidghost is the best mech.', f'{old.strftime("%b")} {old.day}, {old.year}, 10:00')
        row['created']['iso'] = old.strftime('%Y-%m-%d %H:%M')
        rows = [row]
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'archive.json'
            archive.write_text(json.dumps({'metadata': {}, 'threads': rows}), encoding='utf-8')
            reply = format_forum_request('forum', path=archive)
        self.assertIn('Outdated', reply)
        self.assertIn('months ago', reply)   # 400 days reads as months, never as 'recent'

    def test_forum_lookup_detection_is_explicit_and_ambient_safe(self):
        for query in ('latest forum posts','What is the forum saying about Skystrike?',
                      'what are players saying about Surge?','forum build guides'):
            with self.subTest(query=query):
                self.assertTrue(wants_forum_lookup(query))
                self.assertTrue(should_answer_unmentioned(query))
        self.assertFalse(wants_forum_lookup('I visited the forum yesterday.'))
        self.assertFalse(should_answer_unmentioned('I visited the forum yesterday.'))


class ForumScraperIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_atomically_replaces_snapshot_and_rejects_incomplete_result(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            destination=root/'threads.json'
            destination.write_text('{"last_good": true}',encoding='utf-8')
            rows=[{'topic_id':i+1,'title':f'Live thread {i}','url':f'https://forum.plarium.com/mech-arena/702_discussion/{i+1}/',
                   'category':{'name':'Game Discussion'},'author':{'name':'Player','role':''},
                   'created':{'display':f'Nov 12, 2026, 10:{i%60:02d}','iso':f'2026-11-12 10:{i%60:02d}'},
                   'original_post':{'text':f'Live post {i}.'}} for i in range(150)]
            payload=repr(json.dumps({'metadata':{'threads_returned':150,'target_threads':150,
                'crawl_complete':True,'listing_pages_failed':0,
                'scraped_at_utc':'2026-11-10 12:00:00 UTC'},'threads':rows}))
            script=root/'fake_scraper.py'
            script.write_text(
                'import json,sys\n'
                'prefix=sys.argv[sys.argv.index("--out")+1]\n'
                f'data=json.loads({payload})\n'
                'with open(prefix+".json","w",encoding="utf-8") as f: json.dump(data,f)\n',
                encoding='utf-8')
            count=await refresh_forum_archive(path=destination,scraper_path=script)
            self.assertEqual(count,150)
            self.assertEqual(recent_forum_threads(limit=1,path=destination)[0]['title'],'Live thread 0')
            good_snapshot=destination.read_bytes()
            incomplete=repr(json.dumps({'metadata':{'threads_returned':150,'target_threads':150,
                'crawl_complete':False,'listing_pages_failed':0,
                'scraped_at_utc':'2026-11-10 12:00:00 UTC'},'threads':rows}))
            script.write_text(
                'import json,sys\n'
                'prefix=sys.argv[sys.argv.index("--out")+1]\n'
                f'data=json.loads({incomplete})\n'
                'with open(prefix+".json","w",encoding="utf-8") as f: json.dump(data,f)\n',
                encoding='utf-8')
            with self.assertRaises(RuntimeError):
                await refresh_forum_archive(path=destination,scraper_path=script)
            self.assertEqual(destination.read_bytes(),good_snapshot)
            script.write_text(
                'import json,sys\n'
                'prefix=sys.argv[sys.argv.index("--out")+1]\n'
                'with open(prefix+".json","w",encoding="utf-8") as f: json.dump({"threads":[]},f)\n',
                encoding='utf-8')
            with self.assertRaises(RuntimeError):
                await refresh_forum_archive(path=destination,scraper_path=script)
            self.assertEqual(destination.read_bytes(),good_snapshot)


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
                         {'mechs':['Redox'],'query':'Redox','mode':None,'best_requested':False,
                          'list_requested':False,'sp_range':None,'ambiguous':False,'unresolved':False,'needs_mech':False})
        self.assertEqual(natural_builds_request('Show the latest builds',[]),
                         {'mechs':[],'query':None,'mode':None,'best_requested':False,
                          'list_requested':True,'sp_range':None,'ambiguous':False,'unresolved':False,'needs_mech':False})
        self.assertTrue(natural_builds_request('Show builds for an unknown mech',[])['unresolved'])
        best=natural_builds_request('Best Surge build for 5v5?',['Surge'])
        self.assertEqual((best['query'],best['mode'],best['needs_mech']),('Surge','5v5',False))
        latest=natural_builds_request('Show recent builds for Surge?',['Surge'])
        self.assertTrue(latest['list_requested'])
        self.assertFalse(latest['best_requested'])
        self.assertTrue(natural_builds_request('What is the best build?',[])['needs_mech'])
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
    def test_reply_carries_no_decoration_and_no_table_grid(self):
        # Reported from a live reply: a padded pipe table and stray emoji. Only the three
        # game currency marks may appear, and a table becomes one line per row.
        table='| Item | Cost |\n|---|---|\n| A-Coins | 5,650 |\n| Credits | 715,000 |'
        out=naturalize_discord_reply(table)
        self.assertNotIn('|',out)
        self.assertNotIn('---',out)
        self.assertIn('A-Coins · 5,650',out)
        self.assertIn('Credits · 715,000',out)
        decorated=naturalize_discord_reply('Done! 🔥 It costs 🪙 5,650 A-Coins and 💎 715,000 Credits 📘.')
        self.assertNotIn('🔥',decorated)
        for mark in ('🪙','💎','📘'):
            self.assertIn(mark,decorated)                      # currency marks survive, distinct
        self.assertEqual(decorated.count('🪙'),1)
        self.assertEqual(decorated.count('💎'),1)
        self.assertNotIn('\u2019',naturalize_discord_reply('It doesn\u2019t work.'))  # curly -> straight
        self.assertEqual(naturalize_discord_reply("It doesn't work."),"It doesn't work.")

    def test_identity_credits_and_exact_item_shortcuts(self):
        self.assertIn('ARCHΛNGLE',bot_identity_response('Who are you?'))
        builder=bot_identity_response('Who build you?')
        self.assertIn('confirmed code-author',builder)
        self.assertIn('Nub',builder)
        self.assertIn('maintains the Mech Arena game database',builder)
        self.assertIn('MRX',builder)
        self.assertIn('handles ECR event guides',builder)
        for user_id in (1442058583129722986,1444891876376186901,1442422409087942678):
            url=f'https://discord.com/users/{user_id}'
            self.assertIn(f'[{url}]({url})',builder)
            self.assertNotIn(f'<@{user_id}>',builder)
        database=bot_identity_response('Who provided your database?')
        self.assertIn('ᧁꪖꪶꪖ𝓽ꫀ𝘳',database)
        self.assertIn('[https://discord.com/users/1444891876376186901](https://discord.com/users/1444891876376186901)',database)
        self.assertNotIn('<@1444891876376186901>',database)
        manager=bot_identity_response('Who runs the bot?')
        self.assertIn('[https://discord.com/users/1442058583129722986](https://discord.com/users/1442058583129722986)',manager)
        self.assertNotIn('<@1442058583129722986>',manager)
        event_credits=bot_identity_response('Who manages ECR guides?')
        self.assertIn('MRX',event_credits)
        self.assertIn('handles ECR event guides',event_credits)
        self.assertIn('[https://discord.com/users/1442422409087942678](https://discord.com/users/1442422409087942678)',event_credits)
        self.assertIn('ECR event information is separate from promo codes',event_credits)
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

    def test_a_cost_answer_is_written_by_the_bot_not_reassembled_by_the_model(self):
        # Seen live: a table of the three figures with the item, the rank/level transition
        # and the currency marks all missing. A reply that drops any amount is not an answer.
        ctx={'verified_game_facts':[{'type':'upgrade_cost','name':'Redox','from_rank':4,'from_level':1,
             'to_rank':6,'to_level':1,'cost':{'acoins':5650,'credits':715000,'blueprints':29}}]}
        self.assertTrue(_quotes_verified_cost('5,650 A-Coins · 715,000 Credits · 29 Blueprints',ctx))
        self.assertFalse(_quotes_verified_cost('5,650 A-Coins and 715,000 Credits',ctx))
        self.assertFalse(_quotes_verified_cost('A solid mech for the price.',ctx))
    def test_upgrade_cost_never_presents_a_balance_as_the_price(self):
        # A reply that shows the account's holdings where the cost belongs is worse than an
        # error: it reads as a price. The transition must always be named, and the figures
        # must be the table's, not anything the caller passes in alongside them.
        fact={'from_rank':4,'from_level':1,'to_rank':6,'to_level':1,
              'cost':{'acoins':5650,'credits':715000,'blueprints':29}}
        reply=upgrade_cost_response('Redox',fact)
        self.assertIn('rank 4 L1 → rank 6 L1',reply)                   # the transition is stated
        self.assertIn('5,650 A-Coins',reply)
        self.assertIn('715,000 Credits',reply)
        self.assertIn('29 Blueprints',reply)
        self.assertNotIn('|',reply)                                    # no table pipes in a chat line
        self.assertNotIn('Upgrade (R',reply)

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
                     'What types of events are in Mech Arena?', 'latest forum posts about Surge'):
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
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'false'},clear=True):
            default_bot=MechBot(Placeholder(),Placeholder())
        self.assertTrue(default_bot.announcement_ingestion_enabled)
        self.assertTrue(default_bot.forum_sync_enabled)
        self.assertTrue(default_bot.intents.message_content)
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
        expected={'help','invite','worker_setup','worker_time','ping_loop','dm_loop','stop_loop','stop_user_loops','upcoming','forum','ecr','promo_codes','pilot','mod','upgrade_cost'}
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
        mech_params={x.name:x for x in bot.tree.get_command('mech').parameters}
        pilot_params={x.name:x for x in bot.tree.get_command('pilot').parameters}
        self.assertEqual(mech_params['rank'].max_value,7)
        self.assertEqual(pilot_params['rank'].max_value,7)
        self.assertIn('mod',commands)
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

class BuildsTests(unittest.TestCase):
    def setUp(self):
        self.payload={'total':3,'builds':[
            {'mech':'Surge','weapon_1':'Compound Beam 10','weapon_2':'Compound Beam 10',
             'same_weapon':True,'sp_mode':'range','sp_min':6000,'sp_max':8000,
             'label':'tournament opener','note':'best build for FFA & 5v5',
             'author':{'display':'Pixel Predator'},'created_at':300},
            {'mech':'Surge','weapon_1':'Revoker 8','weapon_2':'Revoker 10',
             'same_weapon':False,'sp_mode':'range','sp_min':4000,'sp_max':6000,
             'label':'','note':'best for FFA','created_at':200},
            {'mech':'Surge','weapon_1':'Revoker 10','weapon_2':'Revoker 10',
             'same_weapon':True,'sp_mode':'range','sp_min':6000,'sp_max':8000,
             'label':'Midgame Meta','note':'good for beacons; top build for FFA','created_at':100},
        ]}

    def test_sp_range_parser_accepts_explicit_ranges_and_bare_followups(self):
        self.assertEqual(parse_build_sp_range('Best Surge build for 5v5 at 6,000–8,000 SP'),(6000,8000))
        self.assertEqual(parse_build_sp_range('SP 6–8k'),(6000,8000))
        self.assertEqual(parse_build_sp_range('7000',allow_bare=True),(7000,7000))
        self.assertEqual(parse_build_sp_range('6k-8k',allow_bare=True),(6000,8000))
        self.assertIsNone(parse_build_sp_range('Best Surge build for 5v5'))
        self.assertIsNone(parse_build_sp_range('5v5',allow_bare=True))

    def test_mech_lookup_returns_a_real_best_tagged_loadout(self):
        result=builds_response(self.payload,mech='Surge',best=True)
        self.assertIn('Compound Beam 10 ×2',result)
        self.assertIn('SP 6,000–8,000',result)
        self.assertIn('tournament opener',result)
        self.assertIn('best build for FFA & 5v5',result)
        self.assertIn('not a verified meta ranking',result)
        self.assertEqual(result.count('Surge'),1)
        self.assertNotIn('mech: Surge',result)

    def test_mode_hint_selects_matching_build_and_reports_missing_mode(self):
        matched=builds_response(self.payload,mech='Surge',mode='5v5',best=True)
        self.assertIn('Compound Beam 10 ×2',matched)
        self.assertNotIn('No post mentions 5v5',matched)
        unmatched=builds_response(self.payload,mech='Surge',mode='CPC',best=True)
        self.assertIn('No post mentions CPC',unmatched)
        self.assertIn('Compound Beam 10 ×2',unmatched)

    def test_unfiltered_feed_shows_weapon_slots_and_power_range(self):
        result=builds_response(self.payload,best=False)
        self.assertIn('Recent community build submissions (not ranked)',result)
        self.assertIn('Compound Beam 10 ×2',result)
        self.assertIn('Revoker 8 + Revoker 10',result)
        self.assertIn('SP 4,000–6,000',result)
        self.assertNotIn('mech: Surge',result)

    def test_sp_filter_returns_every_overlapping_build_with_short_summaries(self):
        parts=builds_response_parts(self.payload,mech='Surge',sp_range=(7000,7000))
        result='\n'.join(parts)
        self.assertEqual(len(parts),1)
        self.assertIn('Found 2 matching build(s)',result)
        self.assertIn('Compound Beam 10 ×2',result)
        self.assertIn('Revoker 10 ×2',result)
        self.assertNotIn('Revoker 8 + Revoker 10',result)
        self.assertIn('SP 6,000–8,000',result)
        self.assertIn('no ratings or votes',result)

    def test_unknown_mech_does_not_reuse_another_mechs_build(self):
        result=builds_response(self.payload,mech='Redox',best=True)
        self.assertIn('no submitted builds for Redox',result)
        self.assertNotIn('Compound Beam',result)

    def test_submitter_is_shown_without_an_invented_profile_link(self):
        item={'mech':'Surge','weapon_1':'Revoker 10','weapon_2':'Revoker 10','sp_min':6000,'sp_max':8000,
              'author':{'id':'author-42','display':'Pixel Predator','handle':'pixel'}}
        payload={'builds':[item]}
        result=builds_response(payload,mech='Surge',best=True)
        self.assertIn('**Submitted by:** Pixel Predator (pixel)',result)
        self.assertNotIn('portico-hq-live.netlify.app',result)
        self.assertEqual(_build_author_profile_url(item),'')

    def test_profile_link_is_omitted_until_source_verifies_a_public_route(self):
        author={'id':'author-42','display':'Pixel Predator','handle':'pixel'}
        base={'mech':'Surge','author':author}
        self.assertEqual(_build_author_profile_url(base),'')
        for candidate in ('http://portico-hq-live.netlify.app/users/42',
                          'https://evil.example/users/42',
                          'https://portico-hq-live.netlify.app/api/mech-arena/avatar/42',
                          'https://portico-hq-live.netlify.app/users/42?embed=true',
                          'https://portico-hq-live.netlify.app/member/verified',
                          'https://discord.com/users/not-a-snowflake',
                          'https://discord.com/users/1442422409087942678?redirect=example'):
            with self.subTest(candidate=candidate):
                item={'mech':'Surge','author':{**author,'profile_url':candidate}}
                self.assertEqual(_build_author_profile_url(item),'')
                result=builds_response({'builds':[item]},mech='Surge',best=True)
                self.assertIn('Submitted by',result)
                self.assertNotIn('https://',result)

    def test_build_author_links_require_an_explicit_discord_identity(self):
        galater={'mech':'Surge','weapon_1':'Revoker 10','author':{'display':'GΛ—ΛTЯ','handle':'@galater'}}
        self.assertEqual(_build_author_profile_url(galater),'')
        result=builds_response({'builds':[galater]},mech='Surge',best=True)
        self.assertIn('GΛ—ΛTЯ (@\u200bgalater)',result)
        self.assertNotIn('@galater',result)
        self.assertNotIn('https://discord.com/users/',result)
        from_api={'mech':'Surge','weapon_1':'Revoker 10',
                  'author':{'display':'Verified submitter','discord_id':'1442422409087942678'}}
        url='https://discord.com/users/1442422409087942678'
        self.assertEqual(_build_author_profile_url(from_api),url)
        result=builds_response({'builds':[from_api]},mech='Surge',best=True)
        self.assertIn(f'Verified submitter ([{url}]({url}))',result)
        self.assertNotIn('<@1442422409087942678>',result)
        by_profile_url={'author':{'profile_url':'https://discord.com/users/1444891876376186901'}}
        self.assertEqual(_build_author_profile_url(by_profile_url),'https://discord.com/users/1444891876376186901')
        # Handles alone, including MRX, do not establish a source author's Discord identity.
        self.assertEqual(_build_author_profile_url({'author':{'display':'MRX'}}),'')


class ArchivePresentationTests(unittest.TestCase):
    def test_raw_json_sources_and_ingestion_timestamps_are_not_shown(self):
        machine={'kind':'public_document','source':'upgrade_calculator','title':'list.json',
                 'date':'2026-11-10T06:47:01+00:00','url':'https://mecharena.infohubhq.in/list.json',
                 'excerpt':'Revoker weapon data'}
        self.assertEqual(_archive_attribution(machine),'')
        fallback=_archive_fallback({'archive_material':[machine,{'kind':'structured_data',
            'source':'upgrade_calculator','title':'list.json','data':{'name':'Revoker 12','energy':12,
            'rarity':'legendary'}}]})
        self.assertIn('Revoker 12',fallback)
        self.assertNotIn('list.json',fallback)
        self.assertNotIn('on_images',fallback)
        self.assertNotIn('2026-11-10T06:47:01',fallback)

    def test_human_page_sources_use_friendly_labels_without_ingestion_dates(self):
        page={'source':'mech_arena_database','title':'MA Database',
              'date':'2026-11-10T06:47:01+00:00',
              'url':'https://database.mecharenadev.com/frontend/pages/general-tips.html'}
        result=_archive_attribution(page)
        self.assertIn('[Mech Arena Database](https://database.mecharenadev.com/frontend/pages/general-tips.html)',result)
        self.assertNotIn('2026-11-10T06:47:01',result)
        self.assertNotIn('MA Database',result)

    def test_user_screenshot_attribution_hides_internal_archive_filename(self):
        entry={'source':'google_drive_archive','title':'MA SS.zip — mech stats screenshots',
               'url':'https://drive.google.com/file/d/abc/view','date':'2026-11-11T19:37:46.367Z'}
        result=_archive_attribution(entry)
        self.assertIn('user-provided screenshot text',result)
        self.assertNotIn('MA SS.zip',result)
        self.assertNotIn('2026-11-11T19:37:46',result)

    def test_ai_answers_that_leak_raw_files_or_ingestion_timestamps_are_rejected(self):
        context={'verified_game_facts':[{'type':'weapon','name':'Revoker 12','stats':[]}]}
        self.assertTrue(_supported_game_reply('Revoker 12 pairs well with Surge.',context))
        self.assertFalse(_supported_game_reply('Revoker 12 pairs well. list.json',context))
        self.assertFalse(_supported_game_reply('Revoker 12 pairs well. 2026-11-10T06:47:01+00:00',context))


class LongReplyTests(unittest.TestCase):
    """/help grew past Discord's 2,000-character message and was being cut, then rejected."""

    def test_help_is_sent_as_whole_messages_rather_than_a_truncated_one(self):
        chunks=message_chunks(discord_text(HELP,limit=1900*6))
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(chunk)<=1900 for chunk in chunks),[len(chunk) for chunk in chunks])
        joined='\n'.join(chunks)
        for line in HELP.splitlines():
            if line.strip():self.assertIn(line,joined)

    def test_a_line_longer_than_a_message_splits_at_a_word(self):
        chunks=message_chunks('word '*900)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(chunk)<=1900 for chunk in chunks))
        self.assertTrue(all(not chunk.endswith('-') for chunk in chunks))

    def test_a_short_reply_stays_one_message(self):
        self.assertEqual(message_chunks('Reminder set.'),['Reminder set.'])
        self.assertEqual(message_chunks(''),[''])


class LongReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_channel_reply_path_sends_the_remainder_as_extra_messages(self):
        from mechbot.bot import MechBot
        bot=MechBot(object(),object())
        sent=[]
        async def reply(text,mention_author=True,allowed_mentions=None,**kwargs): sent.append(('reply',text))
        async def send(text,allowed_mentions=None,**kwargs): sent.append(('send',text))
        message=SimpleNamespace(reply=reply,channel=SimpleNamespace(send=send),author=SimpleNamespace(id=1))
        await bot.reply_message(message,'\n'.join(f'line {index} — {"x"*120}' for index in range(30)))
        self.assertEqual([kind for kind,_ in sent][0],'reply')
        self.assertGreater(len(sent),1)
        self.assertTrue(all(len(text)<=1900 for _,text in sent))

    async def test_the_help_command_answers_in_ephemeral_followups(self):
        from mechbot.bot import MechBot, install_commands
        bot=MechBot(object(),object())
        install_commands(bot)
        response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock())
        followup=SimpleNamespace(send=AsyncMock())
        interaction=SimpleNamespace(response=response,followup=followup,user=SimpleNamespace(id=1),guild_id=None)
        await bot.tree.get_command('help').callback(interaction)
        response.send_message.assert_awaited_once()
        first=response.send_message.await_args.args[0]
        self.assertLessEqual(len(first),1900)
        self.assertGreaterEqual(followup.send.await_count,1)
        for call in [response.send_message.await_args]+list(followup.send.await_args_list):
            self.assertLessEqual(len(call.args[0]),1900)


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

    async def test_abbreviated_max_upgrade_query_calculates_blizzfrost_cost(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):runtime=initialize_snapshot()
            catalog=Catalog(runtime)
            question='Upgrade cost of Blizzfrost r5 lv7 to max'
            context=game_knowledge_context(question,catalog)
            self.assertEqual(context['rank_change'],['5','6'])
            self.assertEqual((context['from_level'],context['to_level']),(7,7))
            with patch('mechbot.assistant.general_ai',new=AsyncMock()) as ai:
                answer=await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',question,catalog)
            ai.assert_not_awaited()
            self.assertIn('rank 5 L7 → rank 6 L7',answer)
            self.assertIn('2,825 A-Coins',answer)
            self.assertIn('1,735,000 Credits',answer)
            self.assertIn('75 Blueprints (BP)',answer)
            self.assertIn('assuming “max” means rank 6, level 7',answer)
            self.assertNotIn('won’t guess',answer)
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

    async def test_dm_loop_schedules_followups_to_the_member_not_the_initial_message(self):
        bot=MechBot(object(),object());install_commands(bot)
        calls=[]
        async def send_dm(*args,**kwargs):
            calls.append(('send',args,kwargs))
            return SimpleNamespace(send=AsyncMock())
        member=SimpleNamespace(id=223456789012345678,bot=False,send=AsyncMock(side_effect=send_dm))
        def start(*args):
            calls.append(('start',args,{}))
            return 'abcd1234'
        response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock())
        followup=SimpleNamespace(send=AsyncMock())
        interaction=SimpleNamespace(guild=SimpleNamespace(id=999999999999999999),
            user=SimpleNamespace(id=1442058583129722986),response=response,followup=followup)
        with patch.object(bot.alerts,'start',side_effect=start) as start_loop:
            await bot.tree.get_command('dm_loop').callback(interaction,member,5,10,'Check this')
        member.send.assert_awaited_once()
        start_loop.assert_called_once()
        self.assertEqual(calls[0][0],'send')
        self.assertEqual(calls[1][0],'start')
        self.assertIs(calls[1][1][0],member)
        self.assertEqual(calls[1][1][1],'dm')
        self.assertIs(calls[1][1][2],member)
        response.defer.assert_awaited_once_with(ephemeral=True)
        followup.send.assert_awaited_once()

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

    async def test_first_announcement_read_walks_the_whole_channel(self):
        # The first pass must not sample the channel: every saved message is the fact layer's source.
        calls=[]
        async def history(limit,after=None,oldest_first=False):
            calls.append({'limit':limit,'after':after,'oldest_first':oldest_first})
            yield SimpleNamespace(id=900000000000000001,content='Event update: starts soon.',embeds=[],
                created_at=SimpleNamespace(timestamp=lambda:1700000000),edited_at=None,
                jump_url='https://discord.com/channels/1509560571563868160/1524319795791659080/900000000000000001')
        guild=SimpleNamespace(id=1509560571563868160,name='Game Server',me=SimpleNamespace())
        channel=SimpleNamespace(id=1524319795791659080,name='announcements',guild=guild,history=history,
            permissions_for=lambda member:SimpleNamespace(view_channel=True,read_message_history=True))
        store=SimpleNamespace(supports_announcements=True,announcement_cursor=AsyncMock(return_value=None),
                              upsert_announcements=AsyncMock())
        with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):bot=MechBot(object(),store)
        with patch.object(bot,'get_channel',return_value=channel):
            saved=await bot.ingest_announcement_channel(channel.id)
        self.assertEqual(saved,1)
        self.assertEqual([call['limit'] for call in calls],[ANNOUNCEMENT_BACKFILL_TOTAL_LIMIT,EDIT_REFRESH_LIMIT])
        self.assertTrue(calls[0]['oldest_first'] is True)   # full backfill walks forward from the start
        self.assertTrue(calls[1]['oldest_first'] is False)  # the edit-refresh tail reads the newest posts
        store.announcement_cursor.assert_awaited_once_with(channel.id)
        store.upsert_announcements.assert_awaited_once()
        record=store.upsert_announcements.await_args.args[0][0]
        self.assertEqual(record['channel_id'],str(channel.id))
        self.assertIn('Event update',record['content'])

    async def test_announcement_ingestion_distinguishes_view_and_history_permission_failures(self):
        channel_id=1524319795791659080
        for permission_state,expected in (
            ({'view_channel':False,'read_message_history':True},'view_channel_missing'),
            ({'view_channel':True,'read_message_history':False},'read_message_history_missing'),
        ):
            with self.subTest(expected=expected):
                store=SimpleNamespace(supports_announcements=True,announcement_cursor=AsyncMock(),
                                      upsert_announcements=AsyncMock())
                guild=SimpleNamespace(id=1,name='Test Server',me=SimpleNamespace())
                channel=SimpleNamespace(id=channel_id,name='events',guild=guild,
                    history=AsyncMock(),permissions_for=lambda member:SimpleNamespace(**permission_state))
                with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):bot=MechBot(object(),store)
                with patch.object(bot,'get_channel',return_value=channel):
                    self.assertEqual(await bot.ingest_announcement_channel(channel_id),0)
                self.assertEqual(bot.announcement_access[channel_id],expected)
                store.announcement_cursor.assert_not_awaited()

    async def test_ecr_response_explains_exact_channel_failures_and_configured_scope(self):
        store=SimpleNamespace(supports_announcements=True,recent_announcements=AsyncMock(return_value=[]))
        with (patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}),
              patch('mechbot.bot.ANNOUNCEMENT_CHANNELS',(1524319795791659080,1570083896823906364))):
            bot=MechBot(object(),store)
        bot.announcement_access={
            1524319795791659080:'permission_denied_history_read',
            1570083896823906364:'available',
        }
        result=await bot.ecr_response()
        self.assertIn('`1524319795791659080`',result)
        self.assertIn('403 Forbidden while reading message history',result)
        self.assertIn('`1570083896823906364`',result)
        self.assertIn('history read and storage succeeded',result)
        self.assertIn('1512089401193271539',result)
        self.assertNotIn('`1512089401193271539` —',result)
        self.assertIn('ANNOUNCEMENT_CHANNEL_IDS',result)

    async def test_ecr_response_explains_disabled_ingestion_setting(self):
        store=SimpleNamespace(supports_announcements=True,recent_announcements=AsyncMock(return_value=[]))
        with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'false','MESSAGE_CONTENT_ENABLED':'false'},clear=True):
            bot=MechBot(object(),store)
        result=await bot.ecr_response()
        self.assertIn('ANNOUNCEMENT_INGESTION_ENABLED=false',result)
        self.assertIn('Message Content intent',result)
        self.assertIn('Configured channel IDs',result)

    async def test_ecr_response_reads_only_configured_event_posts(self):
        posts=[
            {'channel_id':str(DEFAULT_ANNOUNCEMENT_CHANNELS[0]),
             'content':'Event Crate Rush update: next event opens soon.',
             'posted_at':int(time.time()),'channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/200'},
            {'channel_id':str(DEFAULT_ANNOUNCEMENT_CHANNELS[0]),
             'content':'ECR Promo codes: `SAVE2026`','posted_at':int(time.time()),
             'channel_name':'events','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/2/201'},
            {'channel_id':'999999999999999999','content':'ECR event in another server',
             'posted_at':int(time.time()),'channel_name':'other','guild_name':'Other Server',
             'message_url':'https://discord.com/channels/9/9/202'},
        ]
        store=SimpleNamespace(supports_announcements=True,recent_announcements=AsyncMock(return_value=posts))
        with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):
            bot=MechBot(object(),store)
        result=await bot.ecr_response()
        self.assertIn('Event Crate Rush update',result)
        self.assertNotIn('SAVE2026',result)
        self.assertNotIn('Other Server',result)
        store.recent_announcements.assert_awaited_once()

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
            mentions=[],content='latest promo code',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.promo_codes_response.assert_awaited_once()
        bot.reply_message.assert_awaited_once_with(message,'Recent codes',mention_author=False)

    async def test_opted_in_ambient_ecr_query_uses_event_lookup_not_promo_codes(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.ecr_response=AsyncMock(return_value='Saved ECR event')
        bot.promo_codes_response=AsyncMock(return_value='Promo codes')
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='latest ECR',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.ecr_response.assert_awaited_once()
        bot.promo_codes_response.assert_not_awaited()
        bot.reply_message.assert_awaited_once_with(message,'Saved ECR event',mention_author=False)

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

    async def test_plain_language_build_lookup_asks_for_sp_then_filters_matches(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Redox']
        payload={'builds':[
            {'mech':'Redox','weapon_1':'Revoker 10','weapon_2':'Revoker 10',
             'sp_min':6000,'sp_max':8000,'note':'solid long-range setup'},
            {'mech':'Redox','weapon_1':'Disc Launcher 8','weapon_2':'Disc Launcher 8',
             'sp_min':4000,'sp_max':6000,'note':'low-SP option'},
        ]}
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        channel=SimpleNamespace(id=444)
        guild=SimpleNamespace(id=999)
        request=SimpleNamespace(author=author,channel=channel,guild=guild,mentions=[],
            content='Any builds for Redox?',reference=None)
        with (patch('mechbot.bot.fetch_builds',new=AsyncMock(return_value=payload)) as fetch,
              patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain):
            await bot.on_message(request)
            fetch.assert_not_awaited()
            self.assertIn('What SP range',bot.reply_message.await_args.args[1])
            bot.reply_message.reset_mock()
            followup=SimpleNamespace(author=author,channel=channel,guild=guild,mentions=[],
                content='7000',reference=None)
            await bot.on_message(followup)
            fetch.assert_awaited_once()
        result=bot.reply_message.await_args.args[1]
        self.assertIn('Redox community builds',result)
        self.assertIn('Revoker 10 ×2',result)
        self.assertNotIn('Disc Launcher 8 ×2',result)
        self.assertFalse(bot.reply_message.await_args.kwargs['mention_author'])
        brain.assert_not_awaited()

    async def test_best_build_question_filters_all_matching_sp_builds(self):
        class Catalog:
            weapon_names=[]
            mech_names=['Surge']
        payload={'builds':[
            {'mech':'Surge','weapon_1':'Compound Beam 10','weapon_2':'Compound Beam 10',
             'same_weapon':True,'sp_min':6000,'sp_max':8000,'label':'tournament opener',
             'note':'best build for FFA & 5v5','created_at':300},
            {'mech':'Surge','weapon_1':'Revoker 8','weapon_2':'Revoker 10',
             'same_weapon':False,'sp_min':4000,'sp_max':6000,'note':'best for FFA','created_at':200},
            {'mech':'Surge','weapon_1':'Revoker 10','weapon_2':'Revoker 10',
             'same_weapon':True,'sp_min':6500,'sp_max':7500,'label':'Midgame Meta',
             'note':'good for beacons','created_at':100},
        ]}
        with patch.dict(os.environ,{'MESSAGE_CONTENT_ENABLED':'true','ANNOUNCEMENT_INGESTION_ENABLED':'false'}):
            bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[],content='Best Surge build at 7,000 SP?',reference=None)
        with (patch('mechbot.bot.fetch_builds',new=AsyncMock(return_value=payload)) as fetch,
              patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain):
            await bot.on_message(message)
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1],'Surge')
        result=bot.reply_message.await_args.args[1]
        self.assertIn('Found 2 matching build(s)',result)
        self.assertIn('Compound Beam 10 ×2',result)
        self.assertIn('Revoker 10 ×2',result)
        self.assertNotIn('Revoker 8 + Revoker 10',result)
        self.assertIn('SP 6,000–8,000',result)
        self.assertFalse(bot.reply_message.await_args.kwargs['mention_author'])
        self.assertTrue(bot.reply_message.await_args.kwargs['suppress_embeds'])
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

    async def test_identity_answers_use_profile_links_without_pings_or_embeds(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        author=SimpleNamespace(id=123456789012345678,bot=False)
        questions=(
            ('Who maintains your database?',(1444891876376186901,)),
            ('Who runs the bot?',(1442058583129722986,)),
            ('Who handles the ECR guides?',(1442422409087942678,)),
            ('Who built you?',(1442058583129722986,1444891876376186901,1442422409087942678)),
        )
        for question,user_ids in questions:
            message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
                mentions=[bot_user],content=f'<@{bot_user.id}> {question}',reference=None,reply=AsyncMock())
            await bot.on_message(message)
            message.reply.assert_awaited_once()
            reply=message.reply.await_args
            for user_id in user_ids:
                url=f'https://discord.com/users/{user_id}'
                self.assertIn(f'[{url}]({url})',reply.args[0])
                self.assertNotIn(f'<@{user_id}>',reply.args[0])
            allowed=reply.kwargs['allowed_mentions']
            self.assertFalse(allowed.users or allowed.everyone or allowed.roles)
            self.assertTrue(reply.kwargs['suppress_embeds'])

    async def test_discord_profile_links_suppress_embeds_on_regular_replies(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        message=SimpleNamespace(reply=AsyncMock())
        url='https://discord.com/users/1442422409087942678'
        await bot.reply_message(message,f'Submitted by MRX ([{url}]({url}))',mention_author=False)
        reply=message.reply.await_args
        self.assertTrue(reply.kwargs['suppress_embeds'])
        self.assertFalse(reply.kwargs['allowed_mentions'].users)
        self.assertFalse(reply.kwargs['allowed_mentions'].everyone or reply.kwargs['allowed_mentions'].roles)

    async def test_upcoming_response_reads_posts_from_any_configured_channel(self):
        rows=[
            {'channel_id':str(DEFAULT_ANNOUNCEMENT_CHANNELS[1]),
             'content':'Tournament starts December 15, 2099', 'posted_at':int(time.time()),
             'channel_name':'special-announcements','guild_name':'Game Server',
             'message_url':'https://discord.com/channels/1/1570083896823906364/200'},
            {'channel_id':'1512089401193271539',
             'content':'Tournament starts December 16, 2099', 'posted_at':int(time.time()),
             'channel_name':'removed','guild_name':'Other Server',
             'message_url':'https://discord.com/channels/9/1512089401193271539/201'},
            {'channel_id':'999999999999999999',
             'content':'Tournament starts December 17, 2099', 'posted_at':int(time.time()),
             'channel_name':'unconfigured','guild_name':'Other Server',
             'message_url':'https://discord.com/channels/9/999999999999999999/202'},
        ]
        store=SimpleNamespace(supports_announcements=True,recent_announcements=AsyncMock(return_value=rows))
        with (patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}),
              patch('mechbot.bot.ANNOUNCEMENT_CHANNELS',DEFAULT_ANNOUNCEMENT_CHANNELS)):
            bot=MechBot(object(),store)
            result=await bot.upcoming_response()
        self.assertIn('December 15, 2099',result)
        self.assertRegex(result,r'\*\*Post date:\*\* [A-Z][a-z]{2} \d{1,2}, 20\d{2}')
        self.assertNotIn('#special-announcements',result)
        self.assertNotIn('December 16, 2099',result)
        self.assertNotIn('December 17, 2099',result)
        store.recent_announcements.assert_awaited_once()

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
        bot.upcoming_response.assert_awaited_once_with('any event')
        bot.reply_message.assert_awaited_once_with(message,'No verified event announcement is saved.',mention_author=True)

    async def test_patch_and_bug_questions_use_combined_sources_not_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.reply_message=AsyncMock()
        bot.patch_bug_response=AsyncMock(return_value='Saved patch and bug summary')
        author=SimpleNamespace(id=123456789012345678,bot=False)
        text='Any known bugs in the latest patch?'
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[bot_user],content=f'<@{bot_user.id}> {text}',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.patch_bug_response.assert_awaited_once_with(text)
        bot.reply_message.assert_awaited_once_with(message,'Saved patch and bug summary',mention_author=True)

    async def test_forum_lookup_uses_stored_forum_response_not_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
        bot=MechBot(Catalog(),object())
        bot_user=SimpleNamespace(id=987654321098765432)
        bot._connection.user=bot_user
        bot.reply_message=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[bot_user],content=f'<@{bot_user.id}> latest forum posts about Surge',reference=None)
        with (patch.object(bot,'forum_response',return_value='Stored forum results') as forum_response,
              patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain):
            await bot.on_message(message)
        brain.assert_not_awaited()
        forum_response.assert_called_once_with('latest forum posts about Surge')
        bot.reply_message.assert_awaited_once_with(message,'Stored forum results',mention_author=True)

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

    def test_verified_screenshot_overlay_preserves_exact_cells_and_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(Path(td)/'runtime.sqlite3')}):
                catalog=Catalog(initialize_snapshot())
            try:
                common={'run_id':'unit-overlay','run_reviewed_on':'2026-11-10','run_user_confirmed':True,
                    'expected_cell_count':2,'direct_verified_cell_count':2,'reconstructed_cell_count':0,
                    'user_confirmation_note':'User confirmed the complete screenshot rebuild.',
                    'row_type':'direct','cell_status':'visually_matched','user_confirmed':True,
                    'value_unit':'as_displayed','source_path':'MA SS/Screenshot.png',
                    'source_screenshot_sha256':'screen-sha','source_archive_sha256':'archive-sha',
                    'card_id':'card-1','row_key':'row-1','cell_key':'cell-1','review_notes':'Visually compared.',
                    'cell_reviewed_on':'2026-11-10'}
                rows=[dict(common,archive_name='MA SS.zip',entity_title='REDOX',stat_label='HP',rank_column=3,
                           value='169,200',row_key='mech-row',cell_key='mech-cell'),
                      dict(common,archive_name='PILOT.zip',entity_title='ACANTHA',stat_label='Mech HP',rank_column=1,
                           value='+4%',value_unit='percentage',source_path='PILOT/Screenshot.png',
                           source_screenshot_sha256='pilot-sha',row_key='pilot-row',cell_key='pilot-cell')]
                self.assertEqual(catalog.load_verified_stat_overlay(rows),2)
                mech=catalog.mech_profile('Redox',rank=3)
                self.assertEqual([(x['stat_name'],x['rank'],x['value']) for x in mech['verified_stats']],
                                 [('HP',3,'169,200')])
                self.assertEqual(mech['verified_stats'][0]['provenance']['source_screenshot_sha256'],'screen-sha')
                self.assertIn('169,200',mech_response(mech,rank=3))
                self.assertIn('user-confirmed mech screenshot-stat overlay',mech_response(mech,rank=3))
                pilot=catalog.pilot_profile('Acantha',rank=1)
                self.assertEqual(pilot['verified_stats'][0]['value'],'+4%')
                self.assertIn('+4%',pilot_response(pilot,rank=1))
                context=game_knowledge_context('What are Redox rank 3 stats?',catalog)
                fact=next(x for x in context['verified_game_facts'] if x.get('type')=='mech')
                self.assertEqual(fact['stats'][0]['value'],'169,200')
                self.assertEqual(fact['stat_sources'][0]['source_screenshot_sha256'],'screen-sha')
            finally:
                catalog.close()

    def test_incomplete_or_unconfirmed_overlay_is_not_published(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(Path(td)/'runtime.sqlite3')}):
                catalog=Catalog(initialize_snapshot())
            try:
                row={'run_id':'partial','run_reviewed_on':'2026-11-10','run_user_confirmed':True,
                     'user_confirmed':True,'expected_cell_count':2,'direct_verified_cell_count':2,
                     'reconstructed_cell_count':0,'archive_name':'PILOT.zip','entity_title':'ACANTHA',
                     'stat_label':'Mech HP','rank_column':1,'value':'+4%','value_unit':'percentage',
                     'row_type':'direct','source_path':'PILOT/image.png','source_screenshot_sha256':'sha',
                     'source_archive_sha256':'archive','card_id':'card','row_key':'row','cell_key':'cell'}
                self.assertEqual(catalog.load_verified_stat_overlay([row]),0)
                self.assertEqual(catalog.pilot_profile('Acantha')['verified_stats'],[])
                row['expected_cell_count']=1
                row['user_confirmed']=False
                self.assertEqual(catalog.load_verified_stat_overlay([row]),0)
                self.assertEqual(catalog.pilot_profile('Acantha')['verified_stats'],[])
            finally:
                catalog.close()

    def test_mod_purchase_level_costs_and_pilot_progression_costs_are_retrievable(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(Path(td)/'runtime.sqlite3')}):
                catalog=Catalog(initialize_snapshot())
            try:
                profile=catalog.mod_profile('Perimeter ML','epic')
                self.assertEqual(len(profile['matches']),1)
                mod=profile['matches'][0]
                self.assertEqual(mod['purchase_cost'],380)
                self.assertEqual(mod['level_costs'][2]['basic_mod_parts'],130)
                self.assertIn('380 Basic Mod Parts',mod_response(profile))
                upgrade=catalog.mod_upgrade_cost('Perimeter ML','epic',1,3)
                self.assertEqual(upgrade['cost']['basic_mod_parts'],410)
                self.assertIn('Upgrade L1 → L3: 410 Basic Mod Parts',
                              mod_response({'matches':[mod],'upgrade_cost':upgrade}))
                pilot_cost=catalog.upgrade_cost('Acantha',1,1,1,2)
                self.assertEqual(pilot_cost['item_type'],'pilot')
                self.assertEqual(pilot_cost['cost'],{'acoins':8,'marks':0,'xp':800})
                self.assertIn('8 A-Coins',upgrade_cost_response('Acantha',pilot_cost))
                self.assertIn('800 XP',upgrade_cost_response('Acantha',pilot_cost))
                context=game_knowledge_context('How much to upgrade Acantha from rank 1 level 1 to rank 1 level 2?',catalog)
                fact=next(x for x in context['verified_game_facts'] if x.get('type')=='upgrade_cost')
                self.assertEqual(fact['item_type'],'pilot')
                self.assertEqual(fact['cost']['xp'],800)
            finally:
                catalog.close()


class PilotLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_pilot_lookup_uses_only_verified_listings_and_hides_ocr_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot=Path(td)/'runtime.sqlite3'
            with patch.dict(os.environ,{'SNAPSHOT_PATH':str(snapshot)}):
                catalog=Catalog(initialize_snapshot())
            try:
                self.assertIn('Acantha',catalog.pilot_names)
                profile=catalog.pilot_profile('Acantha')
                self.assertEqual(profile['listing']['name'],'Acantha')
                self.assertNotIn('raw_ocr',profile)
                result=pilot_response(profile)
                self.assertIn('Acantha',result)
                self.assertIn('Weapon specialty:',result)
                self.assertIn('Listed acquisition price:',result)
                self.assertIn('won’t guess',result)
                self.assertNotIn('User-approved screenshot text',result)
                self.assertNotIn('bonus damage',result.casefold())
                context=game_knowledge_context('What are Acantha pilot bonuses?',catalog)
                self.assertTrue(any(fact.get('type')=='pilot' for fact in context['verified_game_facts']))
                self.assertEqual(context['archive_material'],[])
                self.assertFalse(any(fact.get('type')=='confirmed_archive_material'
                                     for fact in context['verified_game_facts']))
                hits=catalog.search_confirmed_archive('Crow pilot bonus',['Crow'],limit=8)
                self.assertFalse(any(hit.get('archive_name')=='PILOT.zip' for hit in hits))
            finally:
                catalog.close()

    async def test_natural_pilot_lookup_passes_requested_rank_and_skips_ai(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
            pilot_names=['Acantha']
            def __init__(self):self.profile_calls=[]
            def pilot_profile(self,name,rank=None):
                self.profile_calls.append((name,rank))
                return {'matches':[{'name':'Acantha','rarity':'epic','skill':'Assault',
                                    'cost':{'amount':0,'currency':'Paid'}}],'verified_stats':[]}
        catalog=Catalog()
        bot=MechBot(catalog,object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.conversation_turn_reply=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[bot.user],content=f'<@{bot.user.id}> What are Acantha rank 3 pilot bonuses?',reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        self.assertEqual(catalog.profile_calls,[('Acantha',3)])
        bot.conversation_turn_reply.assert_awaited_once()
        self.assertIn('Acantha',bot.conversation_turn_reply.await_args.args[2])

    async def test_natural_pilot_comparison_continues_to_ai_path(self):
        class Catalog:
            weapon_names=[]
            mech_names=[]
            pilot_names=['Acantha','Crow']
            def pilot_profile(self,*args,**kwargs):raise AssertionError('comparison must not be a direct listing lookup')
        bot=MechBot(Catalog(),object())
        bot._connection.user=SimpleNamespace(id=987654321098765432)
        bot.conversation_turn_reply=AsyncMock()
        author=SimpleNamespace(id=123456789012345678,bot=False)
        question=f'<@{bot.user.id}> Acantha vs Crow, which should I use?'
        message=SimpleNamespace(author=author,channel=SimpleNamespace(id=444),guild=SimpleNamespace(id=999),
            mentions=[bot.user],content=question,reference=None)
        with patch('mechbot.bot.answer_question_with_brain',new=AsyncMock(return_value='AI comparison.')) as brain:
            await bot.on_message(message)
        brain.assert_awaited_once()
        self.assertIn('Acantha vs Crow',brain.await_args.args[3])
        self.assertEqual(bot.conversation_turn_reply.await_args.args[2],'AI comparison.')

    def test_comparison_fallback_only_lists_matching_user_confirmed_values(self):
        context={'verified_game_facts':[
            {'type':'mech','name':'Redox','stats':[{'stat':'HP','value':'169,200','rank':3,'value_unit':'as_displayed'}]},
            {'type':'mech','name':'Surge','stats':[{'stat':'HP','value':'188,500','rank':3,'value_unit':'as_displayed'}]},
        ]}
        result=_comparison_fallback('Compare Redox and Surge at rank 3',context)
        self.assertIn('Redox 169,200',result)
        self.assertIn('Surge 188,500',result)
        self.assertIn('Higher values are a numeric comparison only',result)
        self.assertNotIn('higher displayed value: Surge',result)


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
            self.assertIn('6774 damage/shot',response)
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
            self.assertFalse(any(x.get('archive_name')=='PILOT.zip' for x in pilot_hits))
            crow=catalog.pilot_profile('Crow')
            self.assertEqual(crow['listing']['name'],'Crow')
            self.assertNotIn('raw_ocr',crow)
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
    async def test_verified_stat_overlay_uses_the_user_confirmed_latest_run_and_keeps_provenance(self):
        class Connection:
            def __init__(self):self.sql=''
            async def fetch(self,sql,*args):
                self.sql=sql
                return [{'run_id':'unit-run','source_path':'PILOT/Screenshot.png',
                         'source_screenshot_sha256':'screen-sha','value':'+4%'}]
        class Acquire:
            def __init__(self,connection):self.connection=connection
            async def __aenter__(self):return self.connection
            async def __aexit__(self,*args):return False
        class Pool:
            def __init__(self):self.connection=Connection()
            def acquire(self):return Acquire(self.connection)
        pool=Pool()
        store=Store(pool=pool,supports_verified_stat_overlay=True)
        rows=await store.verified_stat_overlay()
        self.assertEqual(rows[0]['source_screenshot_sha256'],'screen-sha')
        self.assertIn('JOIN mech_bot_app.stat_rebuild_sources',pool.connection.sql)
        self.assertIn('u.user_confirmed IS TRUE',pool.connection.sql)
        self.assertIn('ORDER BY reviewed_on DESC,created_at DESC LIMIT 1',pool.connection.sql)
        self.assertIn('source_fragment_candidate_row_ids',pool.connection.sql)
        disabled=Store(pool=pool,supports_verified_stat_overlay=False)
        self.assertEqual(await disabled.verified_stat_overlay(),[])

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
    def test_public_payout_totals_body_is_not_a_per_pilot_snapshot(self):
        # The live endpoint publishes totals only; the per-pilot list is private.
        # Reading it as a pilot list raised ValueError every poll minute.
        live={'rules':{'timezone':'Asia/Kolkata','coins_per_build':400,'min_builds_per_day':10,'daily_cap':None},
              'as_of':'2026-11-25','note':'Today is live and still open.',
              'totals':{'unpaid_coins':0,'paid_coins':0,'today_builds':0,'today_coins':0,'qualified_today':0}}
        self.assertEqual(parse_snapshot(live),('2026-11-25',{}))
        self.assertEqual(parse_snapshot({**live,'pilots':[]}),('2026-11-25',{}))
        with self.assertRaises(ValueError):parse_snapshot({'as_of':'2026-11-25','rules':{'timezone':'UTC'}})
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
    async def test_loop_limits_preserve_one_per_destination_and_five_global(self):
        class User:
            def __init__(self,uid):self.id=uid;self.mention=f'<@{uid}>'
        self.assertEqual(MAX_ACTIVE_LOOPS,5)
        members=[User(323456789012345678+i) for i in range(6)]
        destination=SimpleNamespace(send=AsyncMock())
        mgr=AlertManager()
        channel_key=mgr.start(members[0],'channel',destination,5,30,9,'Channel test')
        self.assertFalse(mgr.available(members[0].id,'channel'))
        with self.assertRaisesRegex(ValueError,'already active'):
            mgr.start(members[0],'channel',destination,5,30,9,'Duplicate channel')
        dm_key=mgr.start(members[0],'dm',destination,5,30,9,'DM test')
        self.assertIn(channel_key,mgr.alerts)
        self.assertIn(dm_key,mgr.alerts)
        keys=[channel_key,dm_key]
        for index,member in enumerate(members[1:4],start=1):
            keys.append(mgr.start(member,'channel',destination,5,30,9,f'User {index}'))
        self.assertEqual(len(mgr.alerts),MAX_ACTIVE_LOOPS)
        self.assertFalse(mgr.available(members[5].id,'channel'))
        with self.assertRaisesRegex(ValueError,'five-loop limit'):
            mgr.start(members[5],'channel',destination,5,30,9,'Global cap')
        await mgr.close()
        self.assertFalse(mgr.alerts)

    async def test_dm_loop_sends_scheduled_followups_to_the_member(self):
        class User:
            id=323456789012345678
            def __init__(self):self.send=AsyncMock()
        member=User()
        await member.send('Initial DM loop notice')
        mgr=AlertManager()
        with patch('mechbot.alerts.MIN_INTERVAL',1):
            key=mgr.start(member,'dm',member,1,2,9,'Please check this')
            task=mgr.alerts[key]['task']
            await asyncio.wait_for(task,timeout=2.5)
        self.assertEqual(member.send.await_count,2)
        followup=member.send.await_args_list[-1]
        self.assertIn('Direct-message loop (2)',followup.args[0])
        self.assertIn('Please check this',followup.args[0])
        allowed=followup.kwargs['allowed_mentions']
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)
        self.assertFalse(mgr.alerts)
        await mgr.close()

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
            self.assertFalse(mgr.available(User.id,'channel'))  # duplicate destination loops remain blocked
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

class AnnouncementFactTests(unittest.IsolatedAsyncioTestCase):
    """The curated fact layer: every saved post is read, facts are typed, and answers use the table."""

    WEEKLY = ('# ✨・━━━━━━━━・✨\n# 🎁 Current Weekly ECR Guide!\n# ✨・━━━━━━━━・✨\n'
              '## ORACLE 16 👾 (R4) [Image - 1]\n### The worst Tracker is here! 🥀\n'
              '• Oracle is a very trash weapon.\n• Just skip and save your A-coins.\n'
              '## SAVANT 10 🚀 [Image - 2]\n### The triple attacker is here! ✨️\n'
              '• Probably skip cz the 16 Energy variant is available for credits in the Gear Hub.\n'
              '[member]\n## ✨・━━━・✨\n## 🏆 MECH ARENA 🔥\n## Stay updated! Stay ahead! ⚡')
    SEASONAL = ('# 🎁 Current ECR Guide!\n# 🎯 SEASONAL ECR – CHARGE ROCKET 12\n## 💥 ECR PRIZE POOL OVERVIEW\n'
                'Even tho **Bounce Critter 12** is displayed as the main weapon, **Charge Rocket 12 & Lacewing** '
                'is the actual **primary reward** to focus on in this ECR.\n'
                'The prize pool features five Credits mechs, five A-coin mechs and two grand prizes: '
                'Charge Rocket 12 – Main weapon reward, Lacewing – Main mech reward.')

    def setUp(self):
        self.channel=str(DEFAULT_ANNOUNCEMENT_CHANNELS[0])
        self.now=1795320000   # Mon 23 Nov 2026, after the newest saved post

    def rows(self):
        return [
            {'channel_id':self.channel,'message_id':'1573683291448541305','posted_at':1795265734,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'https://discord.com/channels/g/c/1573683291448541305',
             'content':'# Credits ECR is here!! 💰 ## The one-day rush starts now [member]'},
            {'channel_id':self.channel,'message_id':'1572969304595505175','posted_at':1795095506,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'https://discord.com/channels/g/c/1572969304595505175',
             'content':self.WEEKLY},
            {'channel_id':self.channel,'message_id':'1572969250396831836','posted_at':1795095493,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':'Image - 2'},
            {'channel_id':self.channel,'message_id':'1572969226313007306','posted_at':1795095487,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':'Image - 1'},
            {'channel_id':self.channel,'message_id':'1568279719814627358','posted_at':1793977422,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'https://discord.com/channels/g/c/1568279719814627358',
             'content':self.SEASONAL},
            {'channel_id':self.channel,'message_id':'1573307343691710504','posted_at':1795176101,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'https://discord.com/channels/g/c/1573307343691710504',
             'content':'# 🎁 New Promo Code! ## CODE - D1SCORD150K ### Available for all players! ## Rewards: '
                       '- 150,000 Credits! - 150 Elite Mod Parts! - A Champion Crate! [member]'},
            {'channel_id':self.channel,'message_id':'1573307343691710505','posted_at':1795176111,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':'hii'},
        ]

    def test_every_post_becomes_one_fact_and_attachment_stubs_merge(self):
        facts,stats=fact_layer.build_facts(self.rows(),now=self.now)
        self.assertEqual(stats['messages_read'],7)
        # Two image stubs plus the bare greeting fold into the post they accompany; none becomes an item.
        self.assertEqual(stats['stub_messages'],3)
        self.assertEqual(stats['orphans'],0)
        self.assertEqual(stats['noise_messages'],0)
        self.assertEqual(stats['facts_written'],4)
        self.assertTrue(all(fact['category']!='other' for fact in facts))
        weekly=[fact for fact in facts if fact['ecr_kind']=='weekly'][0]
        self.assertEqual(sorted(weekly['details']['merged_attachment_messages']),
                         ['1572969226313007306','1572969250396831836'])
        self.assertEqual(weekly['entities'],['ORACLE 16 (R4)','SAVANT 10'])
        self.assertNotIn('Image',weekly['title'])
        self.assertTrue(weekly['summary'].startswith('The worst Tracker is here! Oracle is a very trash weapon'))
        self.assertIn('D1SCORD150K',[code for fact in facts for code in fact['details']['codes']])
        for fact in facts:
            self.assertTrue(fact['provenance'].startswith('saved announcement '))
            self.assertIn(fact['message_id'],fact['provenance'])
            self.assertTrue(fact['details']['classification_basis'])

    def test_gear_hub_mention_inside_an_ecr_guide_does_not_retype_the_post(self):
        # Real data: weekly guides compare prices with the Gear Hub; that must not become a shop listing.
        guide={'channel_id':self.channel,'message_id':'1571902798272921751','posted_at':1794841231,'edited_at':None,
               'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
               'content':'# 🎁 Current Weekly ECR Guide!\n## TAILSTING 🧩 (R4)\n### The Spider Meta on the lineup! 🔥\n'
                         '• Tailsting is a Very Meta Mech.\n• Cheaper than in Gear Hub. Max - 6,650 A-coins + token.\n'
                         '## STRIKE ROCKET 6 🚀\n### The smallest rocket is here!\n• Available for same price in gear hub. Max - 600k Credits.'}
        shop={'channel_id':self.channel,'message_id':'1545356557338808370','posted_at':1788512114,'edited_at':None,
              'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
              'content':'# New Items in Gear Hub ❤️‍🔥\n\nCitadel - 9375 A-coins\nTailsting - 9375 A-coins\nVersa all varient unlocked'}
        facts,_=fact_layer.build_facts([guide,shop],now=self.now)
        by_id={fact['message_id']:fact for fact in facts}
        self.assertEqual(by_id['1571902798272921751']['category'],'ecr')
        self.assertEqual(by_id['1571902798272921751']['ecr_kind'],'weekly')
        self.assertEqual(by_id['1545356557338808370']['category'],'gear_hub')
        self.assertEqual(by_id['1571902798272921751']['entities'],['TAILSTING (R4)','STRIKE ROCKET 6'])

    def test_the_spend_check_quotes_the_guides_own_verdict(self):
        day=86400
        post={'channel_id':self.channel,'message_id':'400','posted_at':self.now-2*day,'edited_at':None,
              'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
              'content':'# ✦・Current Weekly ECR Guide!・✦\n## ORACLE 16 👾 (R4)\n### The worst Tracker is here!\n'
                        '• Oracle is a very trash weapon.\n• Just skip and save your A-coins.\n'
                        '## TAILSTING 🧩\n### The spider meta\n• If you have the Acoins, you are suggested to go for it.\n'
                        '## STRIKE ROCKET 6 🚀\n### Small rocket\n• Suggestion - Skip unless you need it.'}
        facts,_=fact_layer.build_facts([post],now=self.now)
        fact=facts[0]
        verdicts=[entry.get('advice') for entry in fact['details']['entries']]
        self.assertEqual(verdicts[0],'skip')                          # ORACLE 16: trash, skip
        self.assertEqual(verdicts[1],'depends')                        # TAILSTING: only if you have A-coins
        self.assertEqual(verdicts[2],'skip')                           # "skip unless you need it" is a skip
        self.assertIn('ORACLE 16',fact['details']['entries'][0]['label'])
        reply=fact_layer.format_ecr_reply(facts,now=self.now)
        self.assertIn('**Verdict:** Skip it',reply)                   # one verdict line per item
        self.assertIn('skip and save your A-coins',reply)             # quotes the post, does not editorialise
        self.assertNotIn('skip it because',reply.lower())
        silent={'channel_id':self.channel,'message_id':'401','posted_at':self.now-day,'edited_at':None,
                'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':'# A-coins Rush ECR is here! 🧩'}
        silent_facts,_=fact_layer.build_facts([silent],now=self.now)
        self.assertIn('No buy or skip advice',fact_layer.format_ecr_reply(silent_facts,now=self.now))
        listing=fact_layer.format_fact_list(silent_facts,'📅 Saved',now=self.now)
        self.assertNotIn('buy or skip advice',listing)              # lists stay quiet when there is nothing to say
        self.assertEqual(fact_layer.advice_for('New Offer by Plarium!'),None)

    def test_the_freshness_window_comes_from_the_channels_own_rhythm(self):
        # A currency rush that returns about monthly must not be declared history after a week, while a
        # weekly guide left un-replaced for three weeks is history. Cadence is measured, not asserted.
        day=86400
        rows=[]
        for index,offset in enumerate((120, 90, 60, 30)):            # a ~monthly rhythm in the archive
            rows.append({'channel_id':self.channel,'message_id':str(500+index),'posted_at':self.now-offset*day,
                         'edited_at':None,'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
                         'content':'# ✦ Seasonal ECR ✦\n## HELLFIRE 16 🔥\n### Big spider\n• Worth the credits if you run spider.'})
        facts,stats=fact_layer.build_facts(rows,now=self.now)
        self.assertEqual(stats['facts_written'],4)
        cadence,measured=fact_layer.ecr_cadence_days(facts,'seasonal')
        self.assertTrue(measured)
        self.assertEqual(cadence,63.0)                               # slowest ordinary 30-day gap x 2
        newest=[fact for fact in facts if fact['message_id']=='503'][0]
        reply=fact_layer.format_ecr_reply([newest],now=self.now)     # 30 days old, monthly rhythm
        self.assertIn('**Current ECR**',reply)
        ancient=fact_layer.format_ecr_reply([dict(newest,posted_at=newest['posted_at']-40*day)],now=self.now)
        self.assertNotIn('**Current ECR**',ancient)                  # ~70 days: now only history
        weekly=[{'channel_id':self.channel,'message_id':'600','posted_at':self.now-21*day,'edited_at':None,
                 'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':self.WEEKLY}]
        weekly_facts,_=fact_layer.build_facts(weekly,now=self.now)
        self.assertNotIn('**Current ECR**',fact_layer.format_ecr_reply(weekly_facts,now=self.now))

    def test_a_closed_one_day_window_is_not_reported_as_current_anymore(self):
        # The dump freezes status; the answer must be live, so an expired rush cannot keep printing
        # as 'Current ECR' until some later dump happens to run.
        day=86400
        posted=self.now-9*day
        old={'channel_id':self.channel,'message_id':'1573661746961317918','posted_at':posted,'edited_at':None,
             'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
             'content':'# A-coins Rush ECR is here! 🧩'}
        facts,_=fact_layer.build_facts([old],now=posted+3600)
        self.assertEqual(facts[0]['status'],'current')                     # true at dump time
        open_window=fact_layer.format_ecr_reply(facts,now=posted+3600)
        closed=fact_layer.format_ecr_reply(facts,now=self.now)            # same rows, later clock
        self.assertIn('**Current ECR**',open_window)
        self.assertNotIn('**Current ECR**',closed)
        self.assertIn('**Last ECR announcement**',closed)
        self.assertIn('window has passed',closed)
        bundle=fact_layer.facts_prompt_bundle(facts,now=self.now)
        self.assertEqual(bundle[0]['status'],'ended')                      # the model sees the live status
        self.assertIn('Outdated',
                      fact_layer.format_fact_list(facts,'🎁 ECR',now=self.now) or '')

    def test_a_stale_rotation_is_history_not_current_state(self):
        # Live data had a 5-week-old 'special' ECR guide with no newer post of that kind, which read as
        # '**Also running**'. Age relative to the kind's cadence must downgrade it to a dated announcement.
        day=86400
        weekly={'channel_id':self.channel,'message_id':'300','posted_at':self.now-3*day,'edited_at':None,
                'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'','content':self.WEEKLY}
        special={'channel_id':self.channel,'message_id':'301','posted_at':self.now-38*day,'edited_at':None,
                 'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
                 'content':'# Current Special ECR Guide\n## DEATHWALKER 12 ⚡\n### The big spider is here! 🔥\n'
                           '### 💡 How to increase your chances of getting it\n-# Spend tokens before Credits.'}
        facts,_=fact_layer.build_facts([weekly,special],now=self.now)
        by_id={fact['message_id']:fact for fact in facts}
        self.assertNotIn('How to increase',by_id['301']['title'])   # advice headings never name a rotation
        self.assertIn('Spend tokens before Credits',by_id['301']['summary'])   # advice still summarises fine
        self.assertEqual(by_id['301']['title'],'Special ECR · DEATHWALKER 12')
        reply=fact_layer.format_ecr_reply(facts,now=self.now)
        self.assertIn('**Current ECR** · weekly rotation',reply)
        self.assertNotIn('Also running',reply)                       # the stale special is not called current
        listing=fact_layer.format_fact_list(facts,'🎁 Saved ECR posts',limit=6,now=self.now)
        self.assertIn('DEATHWALKER 12',listing)                      # still reported, just not as live state

    def test_reposted_announcements_are_listed_once_but_stay_recorded(self):
        first={'channel_id':self.channel,'message_id':'1573305410721415280','posted_at':1795175640,'edited_at':None,
               'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
               'content':'# 🎁 New Promo Code!\n## CODE - D1SCORD150K\n### Available for all players!\n## Rewards: - 150,000 Credits!'}
        again={'channel_id':self.channel,'message_id':'1573307343691710504','posted_at':1795176101,'edited_at':None,
               'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
               'content':'# 🎁 New Promo Code!\n## CODE - D1SCORD150K\n### Available for all players!\n## Rewards: - 150,000 Credits!'}
        facts,stats=fact_layer.build_facts([first,again],now=self.now)
        self.assertEqual(stats['facts_written'],2)          # both messages are stored as facts
        reply=fact_layer.format_fact_list(fact_layer.search_facts(facts,None,('promo_code',)),'🧾 Promo codes',
                                          show_codes=True)
        # The code is printed once, in its own bullet, and is not also left inside the
        # summary sentence - which made a one-code post read as though it carried two.
        block=reply.split('**LIVE OR OUTDATED**')[0]
        bullets=[line for line in block.split('\n') if line.strip().startswith('\u2022')]
        self.assertEqual(sum('D1SCORD150K' in line for line in bullets),1)
        self.assertEqual(reply.count('`D1SCORD150K`'),1)
        newest=[fact for fact in facts if fact['message_id']=='1573307343691710504'][0]
        self.assertEqual(newest['details']['also_announced_in'][0]['message_id'],'1573305410721415280')

    def test_ecr_rules_one_day_expiry_and_only_same_kind_supersede(self):
        facts,_=fact_layer.build_facts(self.rows(),now=self.now)
        by_kind={fact['ecr_kind']:fact for fact in facts if fact['category']=='ecr'}
        self.assertEqual(by_kind['one_day']['status'],'current')
        # The one-day rush is posted 23 Nov 04:15 UTC: still inside 24 h at 23 Nov ~14:00 UTC.
        expired=fact_layer.ecr_status('one_day',1795265734,1795265734+86400+6*3600,False)
        self.assertEqual(expired[0],'ended')
        self.assertIn('24 hours',expired[1])
        self.assertEqual(by_kind['seasonal']['status'],'current')
        self.assertEqual(by_kind['weekly']['status'],'current')
        # A weekly rotation is not replaced by a one-day or seasonal post.
        older=[{'channel_id':self.channel,'message_id':'111','posted_at':1794000000,'content':self.WEEKLY},
               {'channel_id':self.channel,'message_id':'222','posted_at':1795265734,'content':'# Credits ECR is here!! 💰 ## The one-day rush starts now [member]'}]
        pair,_=fact_layer.build_facts(older,now=self.now)
        statuses={fact['message_id']:fact['status'] for fact in pair}
        self.assertEqual(statuses['111'],'current')
        self.assertEqual(statuses['222'],'current')

    def test_ecr_reply_reports_the_latest_and_hides_older_until_asked(self):
        facts,_=fact_layer.build_facts(self.rows(),now=self.now)
        plain=fact_layer.format_ecr_reply(facts,now=self.now)
        self.assertIn('**Current ECR**',plain)
        self.assertIn('ask for “previous ECRs”',plain)
        self.assertNotIn('SEASONAL ECR',plain.split('are saved but hidden')[0])
        expanded=fact_layer.format_ecr_reply(facts,now=self.now,include_older=True)
        self.assertIn('**Other ECR posts in the saved announcements**',expanded)
        self.assertIn('**live**',expanded)
        self.assertLessEqual(len(plain),1900)
        self.assertFalse(fact_layer.wants_older_ecr('what is the current ecr?'))
        self.assertTrue(fact_layer.wants_older_ecr('which ecrs ran last month?'))

    def test_ecr_reply_does_not_narrate_the_weekday_schedule(self):
        facts,_=fact_layer.build_facts(self.rows(),now=self.now)
        text=fact_layer.format_ecr_reply(facts,now=self.now)+'\n'+fact_layer.format_announcement_reply(facts,now=self.now)
        for forbidden in ('Monday','Thursday','Mon/Thu','expires on a weekly'):
            self.assertNotIn(forbidden,text)

    def test_ai_facts_are_kept_only_with_verbatim_evidence(self):
        rows=[{'message_id':'700','posted_at':1795095506,'content':self.WEEKLY}]
        fabricated=[{'message_id':'700','category':'ecr','ecr_kind':'weekly','title':'Oracle sale',
                     'summary':'Ends Friday at 6pm.','entities':['Oracle 16'],'stated_dates':['Friday'],
                     'evidence':'the ECR ends on Friday at 6pm with a 50% discount'}]
        accepted,rejected=fact_layer.validate_ai_facts(fabricated,rows)
        self.assertEqual(accepted,[])
        self.assertEqual(rejected[0]['reason'],'evidence is not a verbatim quote from the post')
        grounded=[{'message_id':'700','category':'ecr','ecr_kind':'weekly','title':'Weekly ECR · Oracle 16 & Savant 10',
                   'summary':'Oracle 16 (R4) and Savant 10 are the featured items.','entities':['Oracle 16'],
                   'stated_dates':[],'evidence':'Probably skip cz the 16 Energy variant is available for credits'}]
        accepted,_=fact_layer.validate_ai_facts(grounded,rows)
        self.assertEqual(len(accepted),1)
        facts,_=fact_layer.build_facts(rows,now=self.now)
        merged=fact_layer.merge_ai_facts(facts,accepted,now=self.now)
        self.assertEqual(merged[0]['extractor'],'model+source-checked')
        self.assertEqual(merged[0]['details']['ai']['evidence'],grounded[0]['evidence'])
        self.assertNotIn('Friday',merged[0]['summary'])
        self.assertEqual(fact_layer.parse_json_array('noise [ {"a":1} ] tail'),[{'a':1}])
        self.assertEqual(fact_layer.parse_json_array('nothing here'),[])

    async def test_fact_table_roundtrip_filters_and_stats(self):
        facts,_=fact_layer.build_facts(self.rows(),now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(sqlite_path=os.path.join(directory,'facts.sqlite3'))
            try:
                self.assertEqual(await store.upsert_announcement_facts(facts,run_id='dump-1'),len(facts))
                stored=await store.announcement_facts()
                self.assertEqual(len(stored),len(facts))
                self.assertIsInstance(stored[0]['entities'],list)
                self.assertIsInstance(stored[0]['details'],dict)
                self.assertEqual({row['run_id'] for row in stored},{'dump-1'})
                ecr_only=await store.announcement_facts(categories=('ecr',))
                self.assertEqual(len(ecr_only),3)
                self.assertEqual(await store.announcement_facts(channel_ids=('999',)),[])
                self.assertEqual(await store.announcement_facts(since=self.now+3600),[])
                stats=await store.announcement_fact_stats()
                self.assertEqual(sum(int(row['total']) for row in stats),len(facts))
                # Re-running the dump updates in place instead of duplicating facts.
                self.assertEqual(await store.upsert_announcement_facts(facts,run_id='dump-2'),len(facts))
                self.assertEqual(len(await store.announcement_facts()),len(facts))
                self.assertEqual({row['run_id'] for row in await store.announcement_facts()},{'dump-2'})
                self.assertEqual(await store.clear_announcement_facts(channel_ids=(self.channel,)),len(facts))
                self.assertEqual(await store.announcement_facts(),[])
            finally:
                await store.close()

    async def test_refresh_dumps_facts_and_skips_when_sources_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(sqlite_path=os.path.join(directory,'facts.sqlite3'))
            try:
                self.assertEqual(await store.upsert_announcement_facts([]),0)   # empty dumps write nothing
                with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true','ANNOUNCEMENT_AI_EXTRACTION':'false'}):
                    bot=MechBot(object(),store)
                bot._announcement_source_rows=AsyncMock(return_value=self.rows())
                first=await bot.maybe_refresh_announcement_facts()
                self.assertEqual(first['status'],'ok')
                self.assertEqual(first['facts_written'],4)
                self.assertEqual(first['messages_read'],7)
                self.assertEqual(first['ai']['error'],'skipped')
                self.assertEqual(first['ai_state'],'skipped')
                self.assertTrue(first['run_id'].startswith('facts-'))
                stored=await store.announcement_facts(categories=('promo_code',))
                self.assertEqual(stored[0]['title'],'New Promo Code!')
                self.assertIn('D1SCORD150K',stored[0]['details']['codes'])
                # The loop can call this every sync; only a changed source set re-dumps.
                state=await bot._fact_dump_state()
                self.assertTrue(state.startswith(first['signature']))
                self.assertIsNone(await bot.maybe_refresh_announcement_facts())
                # A new announcement changes the source set, so the dump runs again on the next sync.
                rows=self.rows()+[{'channel_id':self.channel,'message_id':'1573999999999999999',
                    'posted_at':self.now-60,'edited_at':None,'channel_name':'game-updates','guild_name':'Mech Arena',
                    'message_url':'','content':'# 🎁 New Promo Code! ## CODE - TRICKORFEAR ### Rewards: '
                    '- Eclipse Mech - Legendary Eclipse Skin - 13 Fortune Keys! [member]'}]
                bot._announcement_source_rows=AsyncMock(return_value=rows)
                second=await bot.maybe_refresh_announcement_facts()
                self.assertEqual(second['status'],'ok')
                self.assertNotEqual(second['signature'],first['signature'])
                self.assertEqual(second['facts_written'],5)
                # An edit to an existing post is a new source state too, via edited_at.
                edited=[dict(row) for row in rows]
                edited[0]['edited_at']=self.now
                bot._announcement_source_rows=AsyncMock(return_value=edited)
                third=await bot.maybe_refresh_announcement_facts()
                self.assertEqual(third['status'],'ok')
                self.assertTrue((await bot._fact_dump_state()).endswith(':skipped'))
            finally:
                await store.close()

    async def test_answers_read_the_fact_table_before_the_raw_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(sqlite_path=os.path.join(directory,'facts.sqlite3'))
            try:
                rows=self.rows()
                await store.upsert_announcements([{**row,'guild_id':'1','fetched_at':self.now} for row in rows])
                facts,_=fact_layer.build_facts(rows,now=self.now)
                await store.upsert_announcement_facts(facts,run_id='dump-1')
                with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):
                    bot=MechBot(object(),store)
                with patch('mechbot.bot.time.time',return_value=self.now):
                    ecr=await bot.ecr_response()
                    self.assertIn('**Current ECR**',ecr)
                    self.assertIn('ORACLE 16 (R4)',ecr)
                    self.assertNotIn('Event date mentioned',ecr)     # not the raw-post formatter
                    self.assertNotIn('game-updates',ecr)            # no channel name leakage
                    promo=await bot.promo_codes_response()
                    self.assertIn('`D1SCORD150K`',promo)
                    self.assertIn('redemption not verified',promo)
                    upcoming=await bot.upcoming_response()
                    self.assertIn('What the saved announcements state',upcoming)
                    # Asking for earlier rotations reaches the older list.
                    previous=await bot.ecr_response('which ecrs came before this one?')
                    self.assertIn('**Other ECR posts in the saved announcements**',previous)
                # With no fact table the answer degrades to the raw saved posts, not to an error.
                store.supports_announcement_facts=False
                fallback=await bot.ecr_response()
                self.assertNotIn('**Current ECR**',fallback)
                self.assertIn('Credits ECR',fallback)
            finally:
                await store.close()

    async def test_ai_pass_records_model_checked_facts_and_survives_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(sqlite_path=os.path.join(directory,'facts.sqlite3'))
            with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true','AI_API_KEY':'test-key',
                                        'AI_MODEL':'test-model','ANNOUNCEMENT_AI_EXTRACTION':'true'}):
                bot=MechBot(object(),store)
                try:
                    rows=self.rows()
                    facts,_=fact_layer.build_facts(rows,now=self.now)
                    target=[fact for fact in facts if fact['ecr_kind']=='one_day'][0]
                    grounded=[{'message_id':target['message_id'],'category':target['category'],
                               'ecr_kind':target['ecr_kind'],'title':'Credits ECR',
                               'summary':'One-day credits rush announced.','entities':[],'stated_dates':[],
                               'evidence':'The one-day rush starts now','model':'test-model'}]
                    bot.extract_facts_with_ai=AsyncMock(return_value={'batches':1,'accepted':len(grounded),
                        'rejected':0,'error':None,'facts':grounded})
                    bot._announcement_source_rows=AsyncMock(return_value=rows)
                    stats=await bot.maybe_refresh_announcement_facts()
                    self.assertEqual(stats['ai_state'],'ok')
                    self.assertEqual(stats['ai']['accepted'],1)
                    stored=await store.announcement_facts()
                    checked=[row for row in stored if row['extractor']=='model+source-checked']
                    self.assertEqual(len(checked),1)
                    self.assertEqual(checked[0]['model'],'test-model')
                    self.assertEqual(checked[0]['details']['ai']['evidence'],'The one-day rush starts now')
                    self.assertTrue((await bot._fact_dump_state()).endswith(':ok'))
                    # A provider failure never blocks the deterministic dump...
                    more=rows+[{'channel_id':self.channel,'message_id':'1573888888888888888',
                        'posted_at':self.now-30,'edited_at':None,'channel_name':'game-updates','guild_name':'Mech Arena',
                        'message_url':'','content':'# 🎁 New Promo Code! ## CODE - TRICKORFEAR ### Rewards: '
                        '- Eclipse Mech - Legendary Eclipse Skin - 13 Fortune Keys! [member]'}]
                    bot._announcement_source_rows=AsyncMock(return_value=more)
                    bot.extract_facts_with_ai=AsyncMock(return_value={'batches':0,'accepted':0,'rejected':0,
                        'error':'RuntimeError','facts':[]})
                    failed=await bot.maybe_refresh_announcement_facts()
                    self.assertEqual(failed['status'],'ok')
                    self.assertEqual(failed['ai_state'],'error')
                    self.assertEqual(len(await store.announcement_facts()),len(facts)+1)
                    self.assertTrue((await bot._fact_dump_state()).endswith(':error'))
                    # ...and the failed messages are retried on the next pass, not skipped forever.
                    bot.extract_facts_with_ai=AsyncMock(return_value={'batches':1,'accepted':0,'rejected':0,
                        'error':None,'facts':[]})
                    retried=await bot.maybe_refresh_announcement_facts()
                    self.assertEqual(retried['ai_state'],'ok')
                    self.assertTrue((await bot._fact_dump_state()).endswith(':ok'))
                    self.assertEqual(retried['ai']['batches'],1)   # the retried pass really called the model
                finally:
                    await store.close()

    async def test_ai_endpoint_roundtrip_accepts_only_quoted_facts(self):
        """Point the extraction pass at a local OpenAI-compatible endpoint and check what it stores."""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = _json.loads(self.rfile.read(length).decode('utf-8'))
                seen.append(body)
                posts = _json.loads(body['messages'][1]['content'].split('Messages:', 1)[1])
                quoted = 'The worst Tracker is here!'   # copied straight out of the sent text
                payload = [
                    {'message_id': posts[0]['message_id'], 'category': 'ecr', 'ecr_kind': 'weekly',
                     'title': 'Weekly ECR · Oracle 16 & Savant 10',
                     'summary': 'Oracle 16 and Savant 10 are the featured items in this rotation.',
                     'entities': ['Oracle 16'], 'stated_dates': [], 'evidence': quoted},
                    {'message_id': posts[0]['message_id'], 'category': 'event', 'title': 'Weekend double credits',
                     'summary': 'Credits x2 all weekend.', 'entities': [], 'stated_dates': ['Saturday'],
                     'evidence': 'double credits every weekend for all players'},
                ]
                text = '```json\n' + _json.dumps(payload) + '\n```'
                reply = _json.dumps({'choices': [{'message': {'role': 'assistant', 'content': text}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                return

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rows = [{'message_id': '9001', 'posted_at': 1795095506,
                     'content': '# 🎁 Current Weekly ECR Guide!\n## ORACLE 16 (R4)\n### The worst Tracker is here!\n'
                                '• Just skip and save your A-coins.'},
                    {'message_id': '9002', 'posted_at': 1795095493, 'content': 'Image - 1'}]
            result = await fact_layer.ai_extract_facts(rows, base='http://127.0.0.1:%d/v1' % server.server_port,
                                                       key='local-test-key', model='local-test-model')
            self.assertEqual(result['accepted'], 1)
            self.assertEqual(result['rejected'], 1)        # the invented fact could not cite the post
            self.assertEqual(result['batches'], 1)
            self.assertEqual(result['facts'][0]['evidence'], 'The worst Tracker is here!')
            self.assertEqual(len(seen), 1)
            prompt = seen[0]['messages'][1]['content']
            self.assertIn('9001', prompt)
            self.assertNotIn('"9002"', prompt)              # the stub was merged, not sent separately
            self.assertIn('verbatim substring', seen[0]['messages'][1]['content'])

            # And the whole dump path: model output lands in the fact table, labelled as model-checked.
            with tempfile.TemporaryDirectory() as directory:
                store = Store(sqlite_path=os.path.join(directory, 'facts.sqlite3'))
                try:
                    facts, _ = fact_layer.build_facts([{**row, 'channel_id': self.channel, 'channel_name': 'game-updates',
                                                        'guild_name': 'Mech Arena', 'message_url': ''}
                                                       for row in rows], now=self.now)
                    merged = fact_layer.merge_ai_facts(facts, [{**result['facts'][0], 'model': 'local-test-model'}],
                                                        now=self.now)
                    await store.upsert_announcement_facts(merged, run_id='dump-ai')
                    stored = await store.announcement_facts()
                    self.assertEqual([row['extractor'] for row in stored], ['model+source-checked'])
                    self.assertEqual(stored[0]['model'], 'local-test-model')
                    self.assertNotIn('weekend', json.dumps(stored, ensure_ascii=False).lower())
                    self.assertEqual(stored[0]['title'], 'Weekly ECR · Oracle 16 & Savant 10')
                    # A model claim that contradicts the deterministic reading never overwrites the type.
                    disputed = fact_layer.merge_ai_facts(facts, [{**result['facts'][0], 'category': 'event',
                                                                  'model': 'local-test-model'}], now=self.now)
                    self.assertEqual(disputed[0]['extractor'], 'deterministic')
                    self.assertFalse(disputed[0]['details']['ai_agrees'])
                    self.assertEqual(disputed[0]['category'], 'ecr')
                finally:
                    await store.close()
        finally:
            server.shutdown()
            server.server_close()

    async def test_general_ai_path_receives_the_curated_fact_digest(self):
        """A free-text announcement question gives the model the dated fact digest, not model memory."""
        class Catalog:
            weapon_names=[]; mech_names=[]; pilot_names=[]; mod_names=[]
        facts,_=fact_layer.build_facts([{'message_id':'5001','posted_at':1795095506,'channel_id':self.channel,
            'channel_name':'game-updates','guild_name':'Mech Arena','message_url':'',
            'content':'# 🎁 Current Weekly ECR Guide!\n## ORACLE 16 👾 (R4)\n### The worst Tracker is here!\n'
                       '• Just skip and save your A-coins.'}],now=self.now)
        digest=fact_layer.facts_prompt_bundle(facts,now=self.now)
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='The saved post lists Oracle 16 (R4).')) as ai:
            await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',
                                            'what does the current ecr have?',Catalog(),announcement_facts=digest)
        self.assertTrue(ai.await_args)
        context=ai.await_args.args[4] if len(ai.await_args.args)>4 else ai.await_args.kwargs['context']
        self.assertEqual(context['saved_announcement_facts'][0]['ecr_kind'],'weekly')
        self.assertEqual(context['saved_announcement_facts'][0]['date'],'Nov 19, 2026')
        self.assertNotIn('weekend',json.dumps(context,ensure_ascii=False).lower())
        # Without facts the payload stays exactly as before.
        with patch('mechbot.assistant.general_ai',new=AsyncMock(return_value='ok')) as plain:
            await answer_question_with_brain('https://gateway.invalid/v1','test-key','test-model',
                                            'what does the current ecr have?',Catalog())
        ctx=plain.await_args.args[4] if len(plain.await_args.args)>4 else plain.await_args.kwargs['context']
        self.assertNotIn('saved_announcement_facts',ctx)
        self.assertIn('saved_announcement_facts',GENERAL_ASSISTANT_PROMPT)
        self.assertIn('never state a rotation schedule',GENERAL_ASSISTANT_PROMPT)

    async def test_missing_fact_table_is_reported_not_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(sqlite_path=os.path.join(directory,'facts.sqlite3'),supports_announcement_facts=False)
            try:
                with patch.dict(os.environ,{'ANNOUNCEMENT_INGESTION_ENABLED':'true'}):
                    bot=MechBot(object(),store)
                stats=await bot.refresh_announcement_facts()
                self.assertEqual(stats,{'status':'fact_table_missing'})
                self.assertEqual(await bot._announcement_fact_rows(('ecr',)),[])
                self.assertIsNone(await bot.maybe_refresh_announcement_facts())
            finally:
                await store.close()

    def test_dump_prompt_carries_rules_and_every_merged_post(self):
        rows=[{'message_id':'1','posted_at':1795095506,'content':self.WEEKLY},
              {'message_id':'2','posted_at':1795095493,'content':'Image - 1'}]
        posts,orphans=fact_layer.group_posts(rows)
        self.assertEqual(len(posts),1); self.assertEqual(orphans,[])
        self.assertEqual(posts[0]['_stubs'],['2'])
        prompt=fact_layer.extract_prompt(posts)
        self.assertIn('verbatim substring',prompt)
        self.assertIn('never write the schedule',prompt.lower())
        self.assertIn('"1"',prompt)
        self.assertNotIn('"message_id":"2"',prompt)                    # stub folded into its post
        facts,_=fact_layer.build_facts(rows,now=self.now)
        bundle=fact_layer.facts_prompt_bundle(facts,now=self.now)
        self.assertEqual(len(bundle),1)
        self.assertEqual(bundle[0]['ecr_kind'],'weekly')
        self.assertIn('date',bundle[0])

if __name__=='__main__':unittest.main()
