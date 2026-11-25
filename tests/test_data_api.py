"""The data API: what the bot has collected, published read-only and without leaking anything private.

Four things these tests pin down, because each one is a promise the rest of the codebase already makes:

1. **Originals survive.** `/announcements` and `/forum` return the stored message text and the crawled
   original post byte for byte, so a consumer never has to wonder whether the API paraphrased the game.
2. **The ECR view is the bot's own judgement, not a second one.** `status`/`state`/`standing` must match
   `mechbot.facts.refresh_ecr_status` and `_ecr_state` exactly, and the per-item `advice` is the guide
   post's line about its own item.
3. **Nothing private is reachable.** The same database holds reminders, notes, chat memory and per-user
   payout rows; no route, filter or export may return any of them, and the scan asserts on every body a
   route can produce.
4. **Absence is shown as absence.** A tracker row with no cost line stays empty, a post that stated no
   date gets no `window`, and a table that has not been migrated answers with an explanation rather than
   an empty list that looks like "the game announced nothing".
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

import http.client

from mechbot import api
from mechbot import facts as fact_lib
from mechbot.api import API_PATH, DataApi, config as api_config


def traces(quote, post, span=6):
    """True when the quote is the post's own wording and not a paraphrase.

    Two properties, because neither alone is enough. Every word has to appear in the post (the quote is cut
    from the item block with its non-verdict clauses removed, so it can join two lines and order is not
    preserved), and a run of `span` consecutive words has to appear exactly as the post wrote it, which is
    what stops a word-salad quote from passing.
    """
    words = [word for word in re.findall(r"[a-z0-9']+", str(quote or '').lower()) if word != '…']
    stream = re.findall(r"[a-z0-9']+", str(post or '').lower())
    if not words or not stream:
        return False
    body = set(stream)
    for position, word in enumerate(words):
        if word in body:
            continue
        if position == len(words) - 1 and any(token.startswith(word) for token in stream):
            continue                      # `advice_for` cuts at a character limit, so the tail can be a stem
        return False
    flat = ' '.join(stream)
    spans = [' '.join(words[index:index + span]) for index in range(max(1, len(words) - span + 1))]
    return any(span_text in flat for span_text in spans)


def probe(port, path):
    """One blocking HTTP request, for the test that wants a real socket."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', path)
        reply = connection.getresponse()
        return reply.status, reply.read().decode('utf-8')
    finally:
        connection.close()


def squash(text):
    """Lower-case and strip everything but letters and digits, so a quote can be matched against the post it
    came from even though the reading pipeline normalises case and drops markdown."""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())
from mechbot.facts import build_facts, refresh_ecr_status
from mechbot.store import Store

ROOT = Path(__file__).resolve().parent.parent
WEEKLY_ECR = (ROOT / 'tests' / 'fixtures' / 'announcement_weekly_ecr.txt').read_text(encoding='utf-8')
MEGA_ECR = (ROOT / 'tests' / 'fixtures' / 'announcement_mega_ecr.txt').read_text(encoding='utf-8')
CODES_POST = ("Everyone:\nGRASSHOPPER\n- Grants you a rank3 mech Lacewing, 200 A-Coins, 100K Credits\n"
              "SPOOKYSHOT - Grants you five epic skins\nEvent runs March 1 to March 8, 2027.\n")

# Three shapes of the same announcement, because what matters is what the post did *not* say: a date with a
# year can be turned into a window, a date without one stays a mention, and neither becomes a guess.
EVENT_POST_DATED = ("## 🎉 New event is now live!\n\n"
                    " Frost Fury event runs from March 1, 2027 to March 8, 2027.\n\n"
                    "<@&1532422534442651751>\n")
EVENT_POST_YEARLESS = ("## 🎉 New event is now live!\n\n"
                       " Sandstorm event runs March 1 to March 8 with three new skins.\n\n"
                       "<@&1532422534442651751>\n")
EVENT_POST_UNDATED = ("## 🎉 New event is now live!\n\n"
                      " Frost Fury brings three new skins and a rank3 mech reward.\n\n"
                      "<@&1532422534442651751>\n")

FORUM_ARCHIVE = {'metadata': {'source': 'https://forum.plarium.com/mech-arena/', 'scraped_at_utc': '2026-11-25 00:00'},
                 'threads': [
                     {'rank': 1, 'topic_id': 1, 'title': 'ECR March rewards look weak',
                      'url': 'https://forum.plarium.com/mech-arena/702_game-discussion/1_ecr/',
                      'category': {'name': 'Game Discussion', 'id': 702, 'url': 'https://x/702', 'also_in': []},
                      'author': {'name': 'Pilot A', 'profile_url': 'https://p/1'},
                      'created': {'display': 'Nov 24, 2026, 20:23', 'iso': '2026-11-24 20:23'},
                      'last_activity': {'display': 'Nov 25, 2026, 02:05', 'iso': '2026-11-25 02:05', 'by': 'Pilot A'},
                      'stats': {'replies': 2, 'views': 6, 'likes': 0},
                      'original_post': {'text': 'The Vortex rewards in this ECR are not worth 800k credits.'}},
                     {'rank': 2, 'topic_id': 2, 'title': 'Patch 5.9 megathread',
                      'url': 'https://forum.plarium.com/mech-arena/700_news/2_patch/',
                      'category': {'name': 'News & Announcements', 'id': 700, 'url': 'https://x/700', 'also_in': []},
                      'author': {'name': 'Moderator', 'profile_url': 'https://p/2'},
                      'created': {'display': 'Nov 20, 2026, 09:00', 'iso': '2026-11-20 09:00'},
                      'last_activity': {'display': 'Nov 21, 2026, 10:00', 'iso': '2026-11-21 10:00', 'by': 'X'},
                      'stats': {'replies': 40, 'views': 900, 'likes': 12},
                      'original_post': {'text': 'Aegis shield cooldown reduced by one second.'}}]}

HISTORY = [{'name': 'Parasite', 'type': 'mech', 'start': '2025-10-20', 'end': '2025-10-22', 'items': 20,
            'cps': '300 ac', 'rank': 4, 'skewed': False},
           {'name': 'Thunderbolt 10', 'type': 'weapon', 'start': '2025-10-20', 'end': '2025-10-22', 'items': 12,
            'rank': 3, 'skewed': True},
           {'name': 'Aegis', 'type': 'mech', 'start': '2025-10-27', 'end': '2025-10-29', 'items': 20,
            'cps': '400 ac', 'rank': 4, 'skewed': False}]


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    """One seeded store, one temp forum archive, one running app per test."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = int(time.time())
        self.store = Store(sqlite_path=Path(self.temp.name) / 'api.sqlite3')
        await self.seed()
        archive = Path(self.temp.name) / 'forum.json'
        archive.write_text(json.dumps(FORUM_ARCHIVE), encoding='utf-8')
        history = Path(self.temp.name) / 'history.json'
        history.write_text(json.dumps(HISTORY), encoding='utf-8')
        manifest = Path(self.temp.name) / 'MANIFEST.json'
        manifest.write_text(json.dumps({'pack_build_date': '2026-11-23',
                                       'files': {'ecr_history_261.json': {'sha256': 'abc123', 'bytes': 38199,
                                                                          'trust': 'community'}}}),
                            encoding='utf-8')
        self.bot = SimpleNamespace(store=self.store, announcement_runner=None, forum_archive_runner=None,
                                   conversation_runner=None)
        self.api = DataApi(self.bot, forum_path=str(archive), history_path=str(history),
                           manifest_path=str(manifest), settings=dict(api.SAFE_DEFAULTS, enabled=True,
                                                                      max_limit=200, cache_seconds=30))
        await self.start_client()

    async def start_client(self, settings=None):
        if settings is not None:
            self.api.config = dict(api.SAFE_DEFAULTS, **settings)
        self.client = TestClient(TestServer(self.api.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()

    async def seed(self):
        posts = [
            {'channel_id': '111', 'message_id': '9001', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'game-updates', 'content': MEGA_ECR,
             'message_url': 'https://discord.com/channels/777/111/9001', 'posted_at': self.now - 3600,
             'edited_at': None, 'fetched_at': self.now - 3500},
            {'channel_id': '111', 'message_id': '9002', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'game-updates', 'content': WEEKLY_ECR,
             'message_url': 'https://discord.com/channels/777/111/9002', 'posted_at': self.now - 86400 * 9,
             'edited_at': None, 'fetched_at': self.now - 86400 * 9},
            {'channel_id': '222', 'message_id': '9003', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'code-drops', 'content': CODES_POST,
             'message_url': 'https://discord.com/channels/777/222/9003', 'posted_at': self.now - 86400 * 40,
             'edited_at': self.now - 86400 * 39, 'fetched_at': self.now - 86400 * 40},
            {'channel_id': '111', 'message_id': '9004', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'game-updates', 'content': EVENT_POST_DATED,
             'message_url': 'https://discord.com/channels/777/111/9004', 'posted_at': self.now - 86400 * 2,
             'edited_at': None, 'fetched_at': self.now - 86400 * 2},
            {'channel_id': '111', 'message_id': '9005', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'game-updates', 'content': EVENT_POST_YEARLESS,
             'message_url': 'https://discord.com/channels/777/111/9005', 'posted_at': self.now - 86400 * 3,
             'edited_at': None, 'fetched_at': self.now - 86400 * 3},
            {'channel_id': '111', 'message_id': '9006', 'guild_id': '777', 'guild_name': 'Mech Arena',
             'channel_name': 'game-updates', 'content': EVENT_POST_UNDATED,
             'message_url': 'https://discord.com/channels/777/111/9006', 'posted_at': self.now - 86400 * 4,
             'edited_at': None, 'fetched_at': self.now - 86400 * 4}]
        await self.store.upsert_announcements(posts)
        facts, _stats = build_facts(await self.store.recent_announcements(0, limit=50), now=self.now)
        self.assertTrue(facts, 'the fixtures must produce facts for the API to serve')
        await self.store.upsert_announcement_facts(facts, run_id='test-run')
        self.seeded_posts = posts

    # -- helpers ----------------------------------------------------------------------------
    async def get(self, path, **kwargs):
        response = await self.client.request('GET', path, **kwargs)
        body = await response.text()
        return response, body

    async def json(self, path, **kwargs):
        response, body = await self.get(path, **kwargs)
        self.assertEqual(response.status, 200, body[:400])
        return response, json.loads(body)

    def rows(self, payload):
        return payload['data']


class Originals(ApiTestCase):
    async def test_stored_message_text_comes_back_byte_for_byte(self):
        _response, payload = await self.json(API_PATH + '/announcements')
        by_id = {row['message_id']: row for row in self.rows(payload)}
        self.assertEqual(by_id['9001']['content'], MEGA_ECR)
        self.assertEqual(by_id['9002']['content'], WEEKLY_ECR)
        row = by_id['9001']
        self.assertEqual(row['content_characters'], len(MEGA_ECR))
        self.assertEqual(row['source']['kind'], 'discord_message')
        self.assertTrue(row['message_url'].startswith('https://discord.com/channels/777/111/'))
        self.assertTrue(row['posted_at_utc'].endswith('Z'))
        self.assertIsNone(by_id['9002']['edited_at_utc'])
        self.assertIsNotNone(by_id['9003']['edited_at_utc'])

    async def test_one_post_by_id_carries_its_curated_facts(self):
        _response, payload = await self.json(API_PATH + '/announcements/9001')
        row = payload['data']
        self.assertEqual(row['content'], MEGA_ECR)
        self.assertTrue(row['facts'])
        self.assertIn(row['facts'][0]['category'], fact_lib.CATEGORIES)

    async def test_an_unknown_message_id_is_a_json_404(self):
        response, body = await self.get(API_PATH + '/announcements/424242')
        self.assertEqual(response.status, 404)
        self.assertIn('No stored announcement', json.loads(body)['error'])

    async def test_paging_walks_the_whole_table_and_total_is_the_table_size(self):
        _response, payload = await self.json(API_PATH + '/announcements?limit=1')
        total = payload['meta']['total']
        self.assertEqual(total, len(self.seeded_posts))
        self.assertEqual(payload['meta']['next_offset'], 1)
        seen = {self.rows(payload)[0]['message_id']}
        for offset in range(1, total):
            _response, payload = await self.json(f'{API_PATH}/announcements?limit=1&offset={offset}')
            seen.add(self.rows(payload)[0]['message_id'])
        self.assertEqual(seen, {row['message_id'] for row in self.seeded_posts})
        _response, last = await self.json(f'{API_PATH}/announcements?limit=50&offset=0')
        self.assertIsNone(last['meta']['next_offset'])

    async def test_order_asc_is_the_reverse_of_desc(self):
        _response, desc = await self.json(API_PATH + '/announcements?order=desc&fields=message_id')
        _response, asc = await self.json(API_PATH + '/announcements?order=asc&fields=message_id')
        self.assertEqual([row['message_id'] for row in self.rows(desc)],
                         [row['message_id'] for row in self.rows(asc)][::-1])

    async def test_filters_narrow_by_time_channel_and_words(self):
        _response, payload = await self.json(API_PATH + '/announcements?channel_id=222&fields=message_id')
        self.assertEqual([row['message_id'] for row in self.rows(payload)], ['9003'])
        _response, payload = await self.json(API_PATH + '/announcements?q=SPOOKYSHOT&fields=message_id')
        self.assertEqual([row['message_id'] for row in self.rows(payload)], ['9003'])
        since = int(time.time()) - 86400 * 10
        expected = sorted(post['message_id'] for post in self.seeded_posts if post['posted_at'] >= since)
        _response, payload = await self.json(f'{API_PATH}/announcements?since={since}&fields=message_id')
        self.assertEqual(sorted(row['message_id'] for row in self.rows(payload)), expected)
        self.assertTrue(expected and len(expected) < len(self.seeded_posts),
                        'the filter has to cut something to mean anything')
        _response, payload = await self.json(f'{API_PATH}/announcements?until={since}&fields=message_id')
        self.assertEqual([row['message_id'] for row in self.rows(payload)], ['9003'])

    async def test_a_date_filter_is_accepted_and_a_nonsense_one_is_refused(self):
        _response, payload = await self.json(API_PATH + '/announcements?since=2020-01-01&until=2027-01-01')
        self.assertEqual(payload['meta']['total'], len(self.seeded_posts))
        response, body = await self.get(API_PATH + '/announcements?since=yesterday')
        self.assertEqual(response.status, 400)
        self.assertIn('unix timestamp', json.loads(body)['error'])

    async def test_a_category_filter_goes_through_the_facts_and_says_so_when_it_matches_nothing(self):
        expected = sorted(str(row['message_id']) for row in await self.store.announcement_facts(categories=('ecr',),
                                                                                                 limit=100))
        self.assertTrue(expected, 'a fixture post must classify as an ECR for this to mean anything')
        _response, payload = await self.json(API_PATH + '/announcements?category=ecr&fields=message_id')
        self.assertEqual(sorted(row['message_id'] for row in self.rows(payload)), expected)
        _response, payload = await self.json(API_PATH + '/announcements?category=gear_hub&fields=message_id')
        self.assertEqual(self.rows(payload), [])
        self.assertTrue(any('empty by records' in note for note in payload['meta']['caveats']),
                        payload['meta']['caveats'])

    async def test_an_unmigrated_table_is_explained_rather_than_returning_an_empty_list(self):
        store = Store(sqlite_path=Path(self.temp.name) / 'empty.sqlite3', supports_announcements=False,
                      supports_announcement_facts=False)
        self.addCleanup(store.close)
        quiet = SimpleNamespace(store=store)
        quiet_api = DataApi(quiet, forum_path='/nonexistent/forum.json', history_path='/nonexistent/history.json',
                            settings=dict(api.SAFE_DEFAULTS, enabled=True, max_limit=50, cache_seconds=0))
        client = TestClient(TestServer(quiet_api.create_app()))
        await client.start_server()
        self.addCleanup(client.close)
        response = await client.request('GET', API_PATH + '/announcements')
        payload = json.loads(await response.text())
        self.assertEqual(payload['data'], [])
        self.assertIn('has not been migrated', ' '.join(payload['meta']['caveats']))
        response = await client.request('GET', API_PATH + '/ecr')
        self.assertEqual(json.loads(await response.text())['data'], [])


class FactsAndEcrViews(ApiTestCase):
    async def test_json_columns_arrive_decoded(self):
        _response, payload = await self.json(API_PATH + '/facts')
        rows = self.rows(payload)
        self.assertTrue(rows)
        for row in rows:
            self.assertIsInstance(row['entities'], list)
            self.assertIsInstance(row['details'], dict)
            self.assertEqual(row['source']['table'], 'announcement_facts')
        ecr = next(row for row in rows if row['category'] == 'ecr')
        self.assertTrue(ecr['items'], 'the guide fixture has item blocks to parse')
        self.assertTrue(all(not entry.get('section') for entry in ecr['items']))

    async def test_the_ecr_view_matches_the_bots_own_standing_exactly(self):
        stored = [dict(row) for row in await self.store.announcement_facts(categories=('ecr',), limit=100)]
        now = int(time.time())
        expected = refresh_ecr_status(stored, now=now)
        _response, payload = await self.json(API_PATH + '/ecr')
        rows = self.rows(payload)
        self.assertEqual(len(rows), len(fact_lib.dedupe_facts(expected)))
        by_id = {row['fact_id']: row for row in rows}
        for fact in fact_lib.dedupe_facts(expected):
            view = by_id[fact['fact_id']]
            self.assertEqual(view['status'], fact['status'])
            state, why = fact_lib._ecr_state(fact, now=now, facts=expected)
            self.assertEqual(view['state'], state)
            self.assertEqual(view['standing'], ('Live — ' if state == 'live' else 'Outdated — ' if state == 'outdated'
                                               else 'Unknown — ') + why)

    async def test_items_keep_the_posts_own_words_and_attribute_their_advice(self):
        _response, payload = await self.json(API_PATH + '/ecr')
        rows = self.rows(payload)
        with_items = [row for row in rows if row['items']]
        self.assertTrue(with_items)
        advised = 0
        for row in with_items:
            for item in row['items']:
                self.assertTrue(item['verbatim'])
                if item['advice'] is None:
                    self.assertIsNone(item['advice_quote'])
                    continue
                advised += 1
                self.assertIn(item['advice'], ('worth_it', 'skip', 'depends'))
                self.assertEqual(item['advice_from'], 'the announcement post')
                self.assertTrue(item['advice_quote'], 'a verdict without its quote is unsourced')
                self.assertTrue(traces(item['advice_quote'], MEGA_ECR + WEEKLY_ECR),
                                'the quote has to be the post’s own wording: ' + repr(item['advice_quote']))
                self.assertIn(item['label'].lower(), (MEGA_ECR + WEEKLY_ECR).lower(),
                              'an item block is named by the post, not by the API')
        self.assertFalse(traces('Worth buying immediately, this is the best value in the game',
                                MEGA_ECR + WEEKLY_ECR), 'traces() has to reject a verdict the post never gave')
        self.assertGreater(advised, 0, 'the guide fixtures do contain buy/skip wording')
        advice_note = payload['meta']['source']['advice']
        self.assertIn('post’s own words', advice_note, 'the field has to say where the verdict came from')
        self.assertIn('clause', advice_note, '…and how the quote is cut, so a consumer does not read it as a '
                                             'contiguous span of the post')
        self.assertIn('not a recommendation', ' '.join(payload['meta']['caveats']))

    async def test_status_and_state_filters_work(self):
        _response, every = await self.json(API_PATH + '/ecr?limit=50')
        rows = self.rows(every)
        for state in {row['state'] for row in rows}:
            _response, filtered = await self.json(f'{API_PATH}/ecr?state={state}&limit=50')
            self.assertEqual({row['state'] for row in self.rows(filtered)}, {state})
        for status in {row['status'] for row in rows}:
            _response, filtered = await self.json(f'{API_PATH}/ecr?status={status}&limit=50')
            self.assertTrue(all(row['status'] == status for row in self.rows(filtered)))

    async def test_current_returns_at_most_one_row_per_kind(self):
        _response, payload = await self.json(API_PATH + '/ecr/current')
        kinds = [row['ecr_kind'] for row in self.rows(payload)]
        self.assertEqual(len(kinds), len(set(kinds)))
        self.assertTrue(kinds, 'the fixtures contain a current rotation')

    async def test_events_report_a_window_only_when_the_post_stated_dates(self):
        _response, payload = await self.json(API_PATH + '/events?limit=50')
        rows = self.rows(payload)
        self.assertTrue(rows, 'the seeded event posts must reach /events at all')
        dated = [row for row in rows if row['window'] is not None]
        yearless = [row for row in rows if row['window'] is None and row['event_date_label']]
        undated = [row for row in rows if not row['event_date_label']]
        self.assertTrue(dated and yearless and undated,
                        f'need a stated-date, a yearless-mention and a no-date post: {[r["event_date_label"] for r in rows]}')
        for row in dated:
            self.assertTrue(row['date_confirmed'])
            self.assertEqual(row['window']['state'], 'upcoming', 'the seeded window is in 2027')
            self.assertTrue(row['window']['start_utc'].endswith('Z'))
            self.assertTrue(row['window']['end_utc'].endswith('Z'))
            self.assertLess(row['window']['start_utc'], row['window']['end_utc'])
        for row in yearless + undated:
            self.assertFalse(row['date_confirmed'], 'nothing here names a year, so nothing here is confirmed')
        self.assertIn('without a year', ' '.join(row['date_note'] for row in yearless))
        self.assertIn('only the post date is known', ' '.join(row['date_note'] for row in undated))
        for row in rows:
            self.assertTrue(row['standing'].startswith(('Live', 'Outdated', 'Unknown')),
                            f"{row['title']}: {row['standing']}")

    async def test_an_unknown_field_name_is_a_400_listing_the_allowed_ones(self):
        response, body = await self.get(API_PATH + '/facts?fields=contnet')
        self.assertEqual(response.status, 400)
        payload = json.loads(body)
        self.assertIn('Unknown field(s): contnet', payload['error'])
        self.assertIn('title', payload['error'])

    async def test_an_unknown_category_is_refused_with_the_allowed_list(self):
        response, body = await self.get(API_PATH + '/facts?category=promotions')
        self.assertEqual(response.status, 400)
        self.assertIn('ecr, promo_code', json.loads(body)['error'])


class ForumAndHistory(ApiTestCase):
    async def test_forum_returns_the_original_post_text_and_the_crawl_caveats(self):
        _response, payload = await self.json(API_PATH + '/forum')
        rows = self.rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['original_post']['text'],
                         'The Vortex rewards in this ECR are not worth 800k credits.')
        self.assertTrue(rows[0]['original_post']['verbatim'])
        caveats = ' '.join(payload['meta']['caveats'])
        self.assertIn('no UTC conversion is claimed', caveats)
        self.assertIn('does not collect replies', caveats)
        self.assertIn('author’s', caveats)

    async def test_forum_filters_by_section_and_words(self):
        _response, payload = await self.json(API_PATH + '/forum?category=news&fields=title')
        self.assertEqual([row['title'] for row in self.rows(payload)], ['Patch 5.9 megathread'])
        _response, payload = await self.json(API_PATH + '/forum?q=vortex&fields=title')
        self.assertEqual([row['title'] for row in self.rows(payload)], ['ECR March rewards look weak'])

    async def test_forum_meta_reports_the_crawl_window_and_sections(self):
        _response, payload = await self.json(API_PATH + '/forum/meta')
        row = self.rows(payload)[0]
        self.assertEqual(row['source'], 'https://forum.plarium.com/mech-arena/')
        self.assertEqual(row['threads_in_file'], 2)
        self.assertEqual(payload['meta']['sections'], {'Game Discussion': 1, 'News & Announcements': 1})
        self.assertTrue(row['file']['bytes'] > 0)

    async def test_history_keeps_a_missing_cost_line_missing_and_says_how_many(self):
        _response, payload = await self.json(API_PATH + '/ecr/history?limit=50')
        rows = self.rows(payload)
        self.assertEqual(len(rows), len(HISTORY))
        thunder = next(row for row in rows if row['name'] == 'Thunderbolt 10')
        self.assertNotIn('cps', thunder)
        self.assertEqual(thunder['rank'], 3)
        self.assertIn('stays empty rather than estimated', ' '.join(payload['meta']['caveats']))
        self.assertIn('1 of 3 rows', ' '.join(payload['meta']['caveats']))
        self.assertEqual(payload['meta']['source']['pack']['sha256'], 'abc123')

    async def test_rotations_are_grouped_from_the_history_not_invented(self):
        _response, payload = await self.json(API_PATH + '/ecr/rotations')
        rows = self.rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['start'] for row in rows], ['2025-10-27', '2025-10-20'], 'the newest rotation leads')
        first = rows[1]
        self.assertEqual(first['days'], 3)
        self.assertEqual(first['rows'], 2)
        self.assertEqual(first['named_items'], 2)
        self.assertEqual(sorted(item['name'] for item in first['items']), ['Parasite', 'Thunderbolt 10'])
        self.assertFalse(first['inconsistent_dates'], 'both rows of that rotation agree on the window')
        self.assertTrue(payload['meta']['cadence'], 'three rows still give one observed gap')
        self.assertIn('recording gaps', ' '.join(payload['meta']['caveats']))

    async def test_history_dates_filter_on_the_start_of_a_rotation(self):
        _response, payload = await self.json(API_PATH + '/ecr/history?since=2025-10-25&fields=name')
        self.assertEqual([row['name'] for row in self.rows(payload)], ['Aegis'])
        response, body = await self.get(API_PATH + '/ecr/history?since=whenever')
        self.assertEqual(response.status, 400)


class Transport(ApiTestCase):
    async def test_ndjson_and_csv_are_one_row_per_record(self):
        response, body = await self.get(API_PATH + '/announcements?format=ndjson&fields=message_id')
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('application/x-ndjson'))
        lines = [line for line in body.strip().split('\n') if line]
        self.assertEqual(len(lines), len(self.seeded_posts))
        for line in lines:
            self.assertEqual(sorted(json.loads(line)), ['message_id'])
        response, body = await self.get(API_PATH + '/facts?format=csv')
        self.assertTrue(response.headers['Content-Type'].startswith('text/csv'))
        self.assertEqual(body.split('\r\n')[0].split(',')[0], 'fact_id')
        self.assertIn('message_id', body.split('\r\n')[0])
        self.assertGreaterEqual(len([line for line in body.strip().split('\n') if line.strip()]), 2)

    async def test_export_defaults_to_ndjson_and_covers_the_dataset(self):
        response, body = await self.get(API_PATH + '/export?dataset=facts')
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('application/x-ndjson'))
        rows = [json.loads(line) for line in body.strip().split('\n') if line.strip()]
        _response, payload = await self.json(API_PATH + '/export?dataset=facts&format=json')
        self.assertEqual(payload['meta']['export'], True)
        self.assertEqual(len(rows), len(self.rows(payload)))
        for dataset in ('announcements', 'ecr_history', 'forum'):
            response, body = await self.get(f'{API_PATH}/export?dataset={dataset}')
            self.assertEqual(response.status, 200, body[:200])
            self.assertTrue(body.strip(), dataset)
        response, body = await self.get(API_PATH + '/export?dataset=reminders')
        self.assertEqual(response.status, 400)
        self.assertIn('announcements, facts, ecr_history, forum', json.loads(body)['error'])

    async def test_caching_and_cors_headers_are_set_and_an_etag_resolves_to_304(self):
        response, _body = await self.get(API_PATH + '/facts')
        etag = response.headers['ETag']
        self.assertTrue(etag.startswith('W/"'))
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Cache-Control'], 'public, max-age=30')
        again, _body = await self.get(API_PATH + '/facts', headers={'If-None-Match': etag})
        self.assertEqual(again.status, 304)
        self.assertEqual(again.headers['ETag'], etag)

    async def test_only_get_is_served(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            response = await self.client.request(method, API_PATH + '/facts')
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.headers['Allow'], 'GET, OPTIONS')
        response = await self.client.request('OPTIONS', API_PATH + '/facts')
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Methods'], 'GET, OPTIONS')

    async def test_a_reader_token_locks_every_route_but_health(self):
        await self.client.close()
        await self.start_client(dict(self.api.config, token='let-me-read'))
        response, body = await self.get(API_PATH + '/announcements')
        self.assertEqual(response.status, 401)
        self.assertIn('reader token', json.loads(body)['error'])
        self.assertEqual((await self.client.request('GET', '/health')).status, 200)
        response, _body = await self.get(API_PATH + '/announcements', headers={'Authorization': 'Bearer nope'})
        self.assertEqual(response.status, 401)
        response, _body = await self.get(API_PATH + '/announcements', headers={'Authorization': 'Bearer let-me-read'})
        self.assertEqual(response.status, 200)
        response, _body = await self.get(API_PATH + '/announcements?token=let-me-read')
        self.assertEqual(response.status, 200)

    async def test_an_allow_list_replaces_the_wildcard(self):
        await self.client.close()
        await self.start_client(dict(self.api.config, allow_origin='https://archangle.netlify.app'))
        response, _body = await self.get(API_PATH + '/facts', headers={'Origin': 'https://archangle.netlify.app'})
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], 'https://archangle.netlify.app')
        response, _body = await self.get(API_PATH + '/facts', headers={'Origin': 'https://elsewhere.example'})
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    async def test_limits_are_clamped_and_nonsense_is_coerced_not_fatal(self):
        response, body = await self.get(API_PATH + '/announcements?limit=999999&offset=-5&order=sideways')
        payload = json.loads(body)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['meta']['limit'], self.api.config['max_limit'])
        self.assertEqual(payload['meta']['offset'], 0)
        response, body = await self.get(API_PATH + '/announcements?limit=abc')
        self.assertEqual(response.status, 200, body[:200])


class TheIndex(ApiTestCase):
    async def test_the_index_lists_every_route_with_counts_and_collector_state(self):
        _response, payload = await self.json(API_PATH)
        paths = [row['path'] for row in payload['endpoints']]
        for route in ('/announcements', '/facts', '/ecr', '/ecr/current', '/ecr/history', '/ecr/rotations',
                      '/events', '/forum', '/forum/meta', '/stats', '/export'):
            self.assertIn(API_PATH + route, paths)
        self.assertEqual(payload['counts']['announcements'], len(self.seeded_posts))
        self.assertEqual(payload['counts']['forum_threads'], 2)
        self.assertEqual(payload['counts']['ecr_history'], len(HISTORY))
        self.assertEqual(payload['read_only'], True)
        self.assertIn('not exposed', payload['private_data'])
        self.assertIn('announcement ingestion loop', payload['ingestion'])
        self.assertEqual(payload['ingestion']['announcement ingestion loop'], 'not started')
        self.assertEqual(payload['base_url'].endswith(API_PATH), True)

    async def test_the_html_index_lists_the_same_routes_for_a_browser(self):
        response = await self.client.request('GET', API_PATH, headers={'Accept': 'text/html'})
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        for fragment in ('/announcements', '/forum', 'collector state', 'revision', 'data API'):
            self.assertIn(fragment, body)
        self.assertNotIn('<script', body)

    async def test_stats_counts_agree_with_the_tables(self):
        _response, stats = await self.json(API_PATH + '/stats')
        data = stats['data']
        self.assertEqual(data['counts']['announcements'], len(self.seeded_posts))
        self.assertEqual(sum(row['posts'] for row in data['per_channel']), len(self.seeded_posts))
        self.assertEqual(sum(row['total'] for row in data['facts_by_category']),
                         len(await self.store.announcement_facts(limit=100)))
        self.assertEqual(data['pack']['manifest']['pack_build_date'], '2026-11-23')


class Privacy(ApiTestCase):
    """The whole database is not the product; these routes are."""

    async def test_no_private_table_name_appears_in_any_response(self):
        routes = [API_PATH, API_PATH + '/announcements?limit=50', API_PATH + '/announcements?format=csv',
                  API_PATH + '/announcements?format=ndjson&limit=50', API_PATH + '/announcements/9001',
                  API_PATH + '/facts?limit=50', API_PATH + '/facts?format=ndjson&limit=50',
                  API_PATH + '/ecr?limit=50', API_PATH + '/ecr/current', API_PATH + '/ecr/history?limit=50',
                  API_PATH + '/ecr/rotations?limit=50', API_PATH + '/events?limit=50', API_PATH + '/forum?limit=50',
                  API_PATH + '/forum/meta', API_PATH + '/stats', API_PATH + '/export?dataset=announcements',
                  API_PATH + '/export?dataset=facts', API_PATH + '/export?dataset=forum',
                  API_PATH + '/export?dataset=ecr_history', '/health', API_PATH + '/nonexistent']
        bodies = []
        for route in routes:
            response, body = await self.get(route)
            self.assertLess(response.status, 500, f'{route} -> {response.status}: {body[:200]}')
            bodies.append(body)
        joined = '\n'.join(bodies).casefold()
        for table in api.PRIVATE_TABLES:
            self.assertNotIn(table, joined, f'{table} appeared in a data API response')
        for phrase in ('your reminder', 'notes for user', 'conversation'):
            self.assertNotIn(phrase, joined)

    async def test_the_api_has_no_way_to_name_a_table_or_write(self):
        source = (ROOT / 'mechbot' / 'api.py').read_text(encoding='utf-8')
        self.assertNotIn('request.query.get(\'table', source)
        for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'DROP '):
            self.assertNotIn(verb, source.upper().replace('INSERT AND', ''))
        self.assertIn('only GET is served here', source)

    async def test_reminders_and_notes_are_unreachable_through_filters(self):
        self.spec = (int(time.time()) + 3600, 'once', None, None, None)
        await self.store.create_reminder('123456789012345678', 'take the ECR before it ends', self.spec, 'UTC')
        await self.store.add_note('123456789012345678', 'my private reminder note')
        for probe in ('take the ECR before it ends', 'my private reminder note', 'reminder'):
            _response, payload = await self.json(f'{API_PATH}/announcements?q={probe}&limit=50')
            self.assertEqual(self.rows(payload), [], probe)
            _response, payload = await self.json(f'{API_PATH}/facts?q={probe}&limit=50')
            self.assertEqual(self.rows(payload), [], probe)


class Startup(ApiTestCase):
    async def test_the_api_is_off_unless_a_deployment_asks_for_it(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(api_config()['enabled'])
        with patch.dict(os.environ, {'PORT': '8080'}, clear=True):
            self.assertTrue(api_config()['enabled'])
        with patch.dict(os.environ, {'PORT': '8080', 'DATA_API_ENABLED': 'false'}, clear=True):
            self.assertFalse(api_config()['enabled'])
        with patch.dict(os.environ, {'PORT': '8080', 'DATA_API_MODE': 'off'}, clear=True):
            self.assertFalse(api_config()['enabled'])

    async def test_a_running_api_reports_its_url_and_a_taken_port_is_the_callers_problem_not_a_crash(self):
        served = DataApi(self.bot, forum_path=self.api.forum_path, history_path=self.api.history_path,
                         settings=dict(api.SAFE_DEFAULTS, enabled=True, host='127.0.0.1', port=8137,
                                       max_limit=50, cache_seconds=0))
        url = await served.start()
        self.addCleanup(served.stop)
        self.assertEqual(url, 'http://127.0.0.1:8137' + API_PATH)
        # A real socket, not the test transport: this is what a deployment's health check will see.
        # Off the event loop: the API is served by this same loop, so a blocking read here would deadlock.
        status, body = await asyncio.to_thread(probe, 8137, API_PATH + '/health')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        status, body = await asyncio.to_thread(probe, 8137, API_PATH + '/facts')
        self.assertEqual(status, 200, 'a list route answers over a real socket too')
        busy = DataApi(self.bot, settings=dict(api.SAFE_DEFAULTS, enabled=True, host='127.0.0.1', port=8137,
                                               max_limit=50, cache_seconds=0))
        with self.assertRaises(OSError):
            await busy.start()
        # The bot wraps `start()` in a try/except, so a clash costs the API and not the Discord session;
        # `test_setup_hook_tolerates_a_taken_port` proves that wrap exists.

    async def test_setup_hook_tolerates_a_taken_port(self):
        import asyncio
        from unittest.mock import patch
        from mechbot.bot import MechBot
        bot = MechBot(SimpleNamespace(weapon_names=[], mech_names=[], pilot_names=[], mod_names=[]), self.store)
        with patch.dict(os.environ, {'DATA_API_ENABLED': 'true', 'DATA_API_PORT': '8139'}, clear=False):
            with patch.object(MechBot, 'command_sync_loop', return_value=asyncio.sleep(0)), \
                 patch.object(MechBot, 'delivery_loop', return_value=asyncio.sleep(0)), \
                 patch.object(MechBot, 'worker_delivery_loop', return_value=asyncio.sleep(0)), \
                 patch.object(MechBot, 'payout_loop', return_value=asyncio.sleep(0)), \
                 patch.object(MechBot, 'application_emoji_setup_loop', return_value=asyncio.sleep(0)), \
                 patch('mechbot.api.DataApi.start', side_effect=OSError('address in use')):
                await bot.setup_hook()
        self.assertIsNone(bot.data_api, 'the API is dropped, the bot is not')
        for task in (bot.command_sync_task, bot.runner, bot.worker_runner, bot.payout_runner, bot.emoji_runner):
            task.cancel()

    async def test_start_is_a_noop_when_disabled(self):
        off = DataApi(self.bot, settings=dict(api.SAFE_DEFAULTS, enabled=False, port=8138, max_limit=50,
                                             cache_seconds=0))
        self.assertIsNone(await off.start())
        await off.stop()
        self.assertIsNone(off.site)
        self.assertIsNone(off.runner)

    async def test_the_bot_command_reports_the_api_state(self):
        from unittest.mock import AsyncMock
        from mechbot.bot import MechBot, install_commands
        with patch.dict(os.environ, {'MESSAGE_CONTENT_ENABLED': 'true', 'ANNOUNCEMENT_INGESTION_ENABLED': 'false',
                                     'AI_API_KEY': '', 'AI_MODEL': ''}):
            bot = MechBot(SimpleNamespace(weapon_names=[], mech_names=[], pilot_names=[], mod_names=[]), self.store)
        install_commands(bot)
        self.assertIn('data_api', {command.name for command in bot.tree.get_commands()})
        sent = []
        bot.data_api = self.api
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k:
                                                                              sent.append(a[0]))),
                                      user=SimpleNamespace(id=1))
        await bot.tree.get_command('data_api').callback(interaction)
        self.assertTrue(sent, 'the command answered nothing')
        self.assertIn('/api/mech-arena/public', sent[0])
        self.assertIn('announcements', sent[0])
        self.assertIn(str(len(self.seeded_posts)), sent[0], 'the command reports how much is stored')
        self.api.config = dict(self.api.config, token='let-me-read')
        await bot.tree.get_command('data_api').callback(interaction)
        self.assertIn('readers: **required**', sent[-1])
        self.assertNotIn('let-me-read', sent[-1], 'the command must not print the token it just set')

    async def test_the_command_explains_how_to_turn_it_on_when_it_is_off(self):
        from unittest.mock import AsyncMock
        from mechbot.bot import MechBot, install_commands
        with patch.dict(os.environ, {'MESSAGE_CONTENT_ENABLED': 'true', 'ANNOUNCEMENT_INGESTION_ENABLED': 'false',
                                     'AI_API_KEY': '', 'AI_MODEL': ''}):
            bot = MechBot(SimpleNamespace(weapon_names=[], mech_names=[], pilot_names=[], mod_names=[]), self.store)
        install_commands(bot)
        bot.data_api = None
        sent = []
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k:
                                                                              sent.append(a[0]))),
                                      user=SimpleNamespace(id=1))
        await bot.tree.get_command('data_api').callback(interaction)
        self.assertIn('DATA_API_ENABLED', sent[0])


if __name__ == '__main__':
    unittest.main()
