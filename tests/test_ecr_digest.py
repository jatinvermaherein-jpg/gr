"""Tests for the curated ECR / promo digests and the currency-emoji asset fix.

These pin the four behaviours that were broken:

1. ``codes_in`` extracted nothing from the archived code post, because the post writes a
   bare code on one line and its rewards on the next, with no "promo code" keyword.
2. The ECR reply reported "no ECR guide post in the saved announcements" even though 261
   rows of history ship with the bot.
3. ``voice.EMOJI_SET`` asked for filenames that are not in ``data/``, so the understanding
   pipeline silently used plain glyphs.
4. Rotations the tracker logged without item names must never be given invented names.
"""

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mechbot import ecr_digest as digest
from mechbot import voice
from mechbot import bot as bot_module

ROOT = Path(__file__).resolve().parent.parent
ECR_ROWS = json.loads((ROOT / 'data' / 'advisor' / 'ecr_history_261.json').read_text(encoding='utf-8'))

# The archived post's real shape: bare code, then the rewards on the following line.
ARCHIVED_POST = """New Players:
GRASSHOPPER
- Grants you a rank3 mech Lacewing, 200 A-Coins, 100K Credits and a Prodigy Crate
CHILLBOT
- grants you 200 A-Coins, 100K Credits, a Prodigy Crate and a Rank 3 Blizzfrost
────────────────────────────────────
For Returning Players:
IMBACKBABY
- Grants Rank 3 Blockhorn, 200 A-Coins, and 1 Prodigy Crate
────────────────────────────────────
Everyone:
MIDGAMECHANNEL4ALL
(available till 1st Dec) - Grants you 100k credits, 1 gold crate, 1 amateur crate
SPOOKYSHOT - Grants you five epic skins (Sanguine Army for Ares, Tengu)
"""


class CodeExtractionTests(unittest.TestCase):
    def test_wrapped_code_and_rewards_are_rejoined(self):
        """The bug: a bare code on its own line produced no match at all."""
        codes = digest.extract_codes(ARCHIVED_POST)
        self.assertTrue(codes, 'the archived post shape must yield codes')
        self.assertEqual([c['code'] for c in codes],
                         ['GRASSHOPPER', 'CHILLBOT', 'IMBACKBABY', 'MIDGAMECHANNEL4ALL', 'SPOOKYSHOT'])

    def test_audience_headings_are_attributed(self):
        by_code = {c['code']: c['audience'] for c in digest.extract_codes(ARCHIVED_POST)}
        self.assertEqual(by_code['GRASSHOPPER'], 'New Players')
        self.assertEqual(by_code['IMBACKBABY'], 'Returning Players')
        self.assertEqual(by_code['SPOOKYSHOT'], 'Everyone')

    def test_availability_window_survives_the_wrap(self):
        codes = {c['code']: c for c in digest.extract_codes(ARCHIVED_POST)}
        self.assertEqual(codes['MIDGAMECHANNEL4ALL']['availability'], '1st Dec')
        self.assertNotIn('available till', codes['MIDGAMECHANNEL4ALL']['rewards'])

    def test_prose_is_not_mistaken_for_a_code(self):
        codes = digest.extract_codes('NOTE - this list changes\nSOURCES: see the thread\n')
        self.assertEqual(codes, [])

    def test_lowercase_tokens_are_ignored(self):
        """A code is shouted; a sentence is not."""
        self.assertEqual(digest.extract_codes('Grasshopper - grants a mech'), [])


class PromoDigestTests(unittest.TestCase):
    def _snapshot(self):
        from mechbot.catalog import initialize_snapshot
        dest = Path(os.environ['SNAPSHOT_PATH'])
        if not dest.is_file():
            initialize_snapshot()
        return dest

    def test_digest_reads_the_confirmable_posts(self):
        with patch.dict(os.environ, {'SNAPSHOT_PATH': '/tmp/mechbot_test_ecr_digest.sqlite3'}):
            dest = self._snapshot()
        rows = digest._promo_documents(db_path=str(dest))
        self.assertTrue(rows, 'the promo posts must be reachable in the runtime snapshot')
        self.assertTrue(all(r['review_status'] == 'verified' for r in rows))

    def test_no_script_or_site_source_leaks_in(self):
        with patch.dict(os.environ, {'SNAPSHOT_PATH': '/tmp/mechbot_test_ecr_digest.sqlite3'}):
            dest = self._snapshot()
        for row in digest._promo_documents(db_path=str(dest)):
            self.assertEqual(row['source'], 'plarium_forum')
            self.assertNotIn(row['category'], ('script', 'client_source', 'sitemap'))

    def test_digest_groups_by_audience_and_states_trust(self):
        with patch.dict(os.environ, {'SNAPSHOT_PATH': '/tmp/mechbot_test_ecr_digest.sqlite3'}):
            dest = self._snapshot()
        text = digest.promo_digest(db_path=str(dest))
        self.assertIn('New Players', text)
        self.assertIn('Returning Players', text)
        self.assertIn('Everyone', text)
        self.assertIn('GRASSHOPPER', text)
        self.assertIn(digest.PROMO_TRUST, text)
        self.assertIn('Plarium has not confirmed', text)

    def test_missing_data_returns_none_rather_than_inventing(self):
        self.assertIsNone(digest.promo_digest(db_path='/tmp/does-not-exist.sqlite3'))


class RotationTests(unittest.TestCase):
    def test_rotations_group_the_flat_history(self):
        grouped = digest.rotations(ECR_ROWS)
        self.assertEqual(len(grouped), 139)
        self.assertEqual(sum(len(r['rows']) for r in grouped), len(ECR_ROWS))

    def test_digest_says_how_stale_the_newest_named_rotation_is(self):
        text = digest.rotation_digest(ECR_ROWS)
        self.assertIn('by name', text)
        self.assertIn('83 days ago', text)
        self.assertIn('cannot tell you what is live right now', text)

    def test_unnamed_rotations_are_never_given_names(self):
        """35 rotations carry name '?'; they must be described, not filled in."""
        text = digest.rotation_digest(ECR_ROWS)
        self.assertIn('the names stop', text)
        self.assertIn('I will not invent them', text)
        self.assertNotIn('None (', text)

    def test_unpriced_rotations_do_not_claim_a_price(self):
        text = digest.rotation_digest(ECR_ROWS)
        for line in text.splitlines():
            if 'Nothing named' in line:
                self.assertNotIn('A-Coins', line)
                self.assertNotIn('Credits', line)

    def test_cadence_excludes_recording_gaps(self):
        rhythm = digest.cadence(ECR_ROWS)
        self.assertIsNotNone(rhythm)
        self.assertLessEqual(rhythm['longest'], 60, 'gaps over 60 days are tracker holes, not schedule')


class ItemHistoryTests(unittest.TestCase):
    def test_price_trend_is_reported_from_observed_values(self):
        text = digest.item_history('Parasite', ECR_ROWS)
        self.assertIn('4 recorded Crate Rushes', text)
        self.assertIn('300 A-Coins/spin in 2025-10-20', text)
        self.assertIn('350 A-Coins/spin in 2026-08-24', text)

    def test_future_appearance_is_not_promised(self):
        self.assertIn('cannot say when it comes back', digest.item_history('Parasite', ECR_ROWS))

    def test_unknown_item_returns_none(self):
        self.assertIsNone(digest.item_history('Not A Real Mech', ECR_ROWS))

    def test_resolve_item_prefers_the_longest_name(self):
        self.assertEqual(digest.resolve_item('is oracle 6 in the ecr', ECR_ROWS), 'Oracle 6')

    def test_resolve_item_needs_a_whole_token(self):
        self.assertIsNone(digest.resolve_item('parasitic behaviour', ECR_ROWS))

    def test_named_item_routes_to_its_history(self):
        self.assertIn('Parasite', digest.ecr_answer('was parasite ever in the ecr', ECR_ROWS))

    def test_general_question_routes_to_the_rotation_view(self):
        self.assertIn('history on record', digest.ecr_answer('what is the current ecr', ECR_ROWS))


class CurrencyEmojiTests(unittest.TestCase):
    def test_every_emoji_file_exists(self):
        """The bug: EMOJI_SET named acoin.png / credits.png / blueprint.png, none shipped."""
        for name, filename, fallback in voice.EMOJI_SET:
            self.assertTrue((ROOT / 'data' / filename).is_file(),
                            f'{filename} is referenced by voice.EMOJI_SET but not shipped')
            self.assertTrue(fallback, f'{name} needs a plain-text fallback')

    def test_both_uploaders_use_the_same_files(self):
        voice_files = [filename for _, filename, _ in voice.EMOJI_SET]
        bot_files = [filename for _, _, filename in bot_module.APPLICATION_CURRENCY_EMOJIS]
        self.assertEqual(voice_files, bot_files)

    def test_application_emoji_names_are_unchanged(self):
        """Renaming these would re-upload duplicates on an existing deployment."""
        self.assertEqual([n for _, n, _ in bot_module.APPLICATION_CURRENCY_EMOJIS],
                         ['archangle_a_coins_v2', 'archangle_credits_v2', 'archangle_blueprints_v2'])

    def test_attachment_fallbacks_point_at_real_files(self):
        for label, filename in bot_module.CURRENCY_ASSETS:
            self.assertTrue((ROOT / 'data' / filename).is_file(), f'{label} asset {filename} missing')

    def test_emoji_book_degrades_to_glyphs_without_a_token(self):
        book = voice.EmojiBook(application_id=123)
        with patch.dict(os.environ, {'DISCORD_TOKEN': ''}, clear=False):
            import asyncio
            self.assertEqual(asyncio.run(book.ensure()), {})
        mapping = book.currency_map()
        self.assertEqual(set(mapping), {'acoins', 'credits', 'blueprints'})
        self.assertTrue(all(mapping.values()), 'a plain fallback must always be available')


if __name__ == '__main__':
    unittest.main()


class CompoundQuestionTests(unittest.TestCase):
    """A message asking two separate things must not be answered once."""

    def setUp(self):
        from mechbot import compound
        self.compound = compound

    def test_two_announcement_topics_fan_out(self):
        self.assertEqual(self.compound.should_fan_out('what ecr is live and what are the promo codes'),
                         ['ecr', 'promo'])

    def test_the_case_that_used_to_fall_through_every_gate(self):
        """'promo codes and the ecr this week' matched neither gate and got no answer."""
        found = self.compound.should_fan_out('promo codes and the ecr this week')
        self.assertIn('promo', found)
        self.assertIn('ecr', found)

    def test_interrogative_survives_the_split(self):
        """Consuming the conjunction used to strip the question word the gates need."""
        self.assertEqual(self.compound.should_fan_out('is there an ecr and any new promo?'),
                         ['ecr', 'promo'])

    def test_contracted_question_words_are_boundaries(self):
        self.assertEqual(self.compound.should_fan_out('any promo codes and whats coming up next'),
                         ['promo', 'upcoming'])

    def test_single_topic_is_not_split(self):
        """One subject asked two ways is one answer, not two."""
        self.assertEqual(self.compound.should_fan_out('ECR promo codes'), [])
        self.assertEqual(self.compound.should_fan_out('what is the ecr'), [])

    def test_two_items_in_one_question_are_not_split(self):
        """The advisor already handles these; splitting them would break a working feature."""
        for question in ('storm racks and charge rockets', 'compare oracle and redox',
                         'oracle or redox', 'should I buy storm racks and charge rockets'):
            self.assertEqual(self.compound.should_fan_out(question), [],
                             f'{question!r} is one question about two items')

    def test_side_effects_are_never_fanned_out(self):
        """One message must never create two reminders or notes."""
        for question in ('remind me tomorrow and what is the ecr',
                         'note that I have 3000 ac and what are the promo codes',
                         'set my timezone to Asia/Kolkata and what is the ecr'):
            self.assertEqual(self.compound.should_fan_out(question), [],
                             f'{question!r} contains a side effect')

    def test_join_drops_empty_sections(self):
        text = self.compound.join([('ECR', 'ecr body'), ('Promo', ''), ('Events', None)])
        self.assertEqual(text, 'ecr body', 'a single surviving section needs no heading')

    def test_join_labels_multiple_sections(self):
        text = self.compound.join([('ECR', 'ecr body'), ('Promo', 'promo body')])
        self.assertIn('**1. ECR**', text)
        self.assertIn('**2. Promo**', text)

    def test_unanswered_clauses_are_named(self):
        note = self.compound.unanswered_note(['what is the ecr', 'who won the tournament'])
        self.assertIn('who won the tournament', note)
        self.assertIn('rather say that than guess', note)

    def test_topic_labels_cover_every_topic(self):
        for topic, _ in self.compound.TOPIC_PATTERNS:
            self.assertIn(topic, self.compound.TOPIC_LABELS)


class EcrSummaryRendererTests(unittest.TestCase):
    """One renderer for tracker rows and ingested announcements."""

    def setUp(self):
        from mechbot import ecr_summary
        self.S = ecr_summary

    def _tracker(self):
        return [self.S.from_tracker_row(r) for r in ECR_ROWS]

    def test_tracker_row_shape(self):
        rec = self.S.from_tracker_row({'name': 'Parasite', 'type': 'mech', 'start': '2026-08-24',
                                       'end': '2026-08-26', 'items': 20, 'cps': '350 ac',
                                       'rank': 4, 'skewed': False})
        self.assertEqual(rec['name'], 'Parasite')
        self.assertEqual(rec['price'], '350 A-Coins')
        self.assertEqual(rec['pool'], 20)
        self.assertEqual(rec['rank'], 4)

    def test_unpadded_dates_parse(self):
        self.assertEqual(str(self.S._date('2026-12-7')), '2026-12-07')

    def test_currency_kinds_are_not_conflated(self):
        self.assertEqual(self.S._price_from_cps('30000 cr')[0], '30,000 Credits')
        self.assertEqual(self.S._price_from_cps('$1')[0], '$1 cash')
        self.assertEqual(self.S._price_from_cps('inf'), ('', ''))

    def test_render_states_freshness_for_every_rotation(self):
        text = self.S.render(self._tracker(), now=__import__('datetime').date(2026, 11, 25))
        self.assertIn('ended 3 Sep 2026', text)
        self.assertIn('83 days ago', text)

    def test_scheduled_rotation_is_not_called_live(self):
        text = self.S.render(self._tracker(), now=__import__('datetime').date(2026, 11, 25))
        self.assertIn('scheduled from', text)
        self.assertNotIn('scheduled from 24 Dec 2026 · live', text)

    def test_unnamed_rotations_are_described_not_invented(self):
        text = self.S.render(self._tracker(), now=__import__('datetime').date(2026, 11, 25))
        self.assertIn('logged without item names', text)
        self.assertIn('cannot say what was in them', text)

    def test_single_item_rotation_does_not_repeat_its_name(self):
        text = self.S.render(self._tracker(), now=__import__('datetime').date(2026, 11, 25))
        tailsting = [block for block in text.split('\n\n') if 'Tailsting' in block][0]
        self.assertEqual(tailsting.count('Tailsting'), 1, 'the name should appear once, in the heading')

    def test_item_view_reports_a_price_move(self):
        text = self.S.render_one(self._tracker(), 'Parasite')
        self.assertIn('Price moved', text)
        self.assertIn('300 A-Coins', text)
        self.assertIn('350 A-Coins', text)

    def test_item_view_of_an_item_never_in_a_rush_is_none(self):
        self.assertIsNone(self.S.render_one(self._tracker(), 'Not A Real Mech'))

    def test_ingested_announcement_renders_in_the_same_format(self):
        import time
        from mechbot import facts
        post = ('# Crate Rush: Blizzfrost\n## Blizzfrost\n### Defensive brawler\n'
                'Blizzfrost returns. Rank 3. 250 A-Coins per spin.\n'
                '## Storm Racks\nStorm Racks is the weapon pick. Rank 5. 350 A-Coins per spin.\n'
                '## Buy or skip\nWorth it if you do not own Blizzfrost.\n')
        now = int(time.time())
        built, _ = facts.build_facts([{'message_id': '9', 'channel_id': '1', 'channel_name': 'e',
                                       'guild_name': 'g', 'message_url': 'u', 'content': post,
                                       'posted_at': now, 'kind': 'post'}], now=now)
        records = self.S.from_fact(built[0])
        names = [r['name'] for r in records]
        self.assertIn('Blizzfrost', names)
        self.assertIn('Storm Racks', names)
        self.assertNotIn('Buy or skip', names, 'the verdict is not a third item')
        text = self.S.render(records, title='**Crate Rush**')
        self.assertIn('Buy or skip:', text)

    def test_crate_rush_without_the_word_ecr_is_still_an_ecr(self):
        """This was the ingestion bug: modern posts say 'Crate Rush' and never 'ECR'."""
        from mechbot import facts
        for title in ('Crate Rush: Parasite', 'CRATE RUSH IS HERE', 'Crate Rush: Blizzfrost'):
            category, kind, _ = facts.classify(title)
            self.assertEqual(category, 'ecr', f'{title!r} must classify as an ECR announcement')

    def test_an_unrelated_post_is_still_other(self):
        from mechbot import facts
        self.assertEqual(facts.classify('Just chatting about nothing')[0], 'other')


class RealAnnouncementTests(unittest.TestCase):
    """The two announcement formats that actually get posted to the channel.

    Both fixtures are verbatim copies of real posts. They broke the first renderer in five
    places, and each of those is pinned here.
    """

    FIXTURES = Path(__file__).resolve().parent / 'fixtures'

    def _parse(self, name):
        from mechbot import facts
        text = (self.FIXTURES / name).read_text(encoding='utf-8')
        blocks = facts.entries(text, note_limit=facts.ECR_NOTE_LIMIT)
        return text, blocks

    def test_role_mentions_do_not_leak_into_notes(self):
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        for block in blocks:
            self.assertNotIn('<@&', block['note'])
            self.assertNotIn('<@&', block['label'])

    def test_bullets_are_kept_separately(self):
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        self.assertTrue(blocks[0]['bullets'], 'each • line must survive as its own bullet')
        self.assertIn('Max Credits - 800K', blocks[0]['bullets'])

    def test_rank_in_the_heading_is_extracted(self):
        from mechbot import ecr_summary as S
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        hero, items, _, _ = S.guide_sections(blocks)
        self.assertEqual(hero['name'], 'VORTEX')
        self.assertEqual(hero['rank'], 4)
        self.assertEqual(items[0]['name'], 'SUPPRESSOR 10')

    def test_hero_is_recognised_as_a_mech_not_a_new_item(self):
        from mechbot import ecr_summary as S
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        hero, _, _, _ = S.guide_sections(blocks)
        self.assertEqual(hero['kind'], 'mech')

    def test_condition_headings_are_strategy_not_items(self):
        from mechbot import ecr_summary as S
        _, blocks = self._parse('announcement_mega_ecr.txt')
        hero, items, conditions, _ = S.guide_sections(blocks)
        self.assertEqual(hero['name'], 'INGRID MEGA ECR')
        self.assertEqual(items, [], 'no second item exists in this post')
        self.assertEqual(len(conditions), 2)
        self.assertIn('If you already own Ingrid', conditions[0]['heading'])

    def test_condition_headings_never_reach_the_title(self):
        from mechbot import facts
        text, blocks = self._parse('announcement_mega_ecr.txt')
        category, kind, _ = facts.classify(text)
        title = facts.title_from(text, category, kind, blocks)
        self.assertIn('INGRID', title)
        self.assertNotIn('If you', title)

    def test_verdict_is_read_per_bullet_not_from_the_whole_note(self):
        from mechbot import ecr_summary as S
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        hero, items, _, _ = S.guide_sections(blocks)
        word, quote = hero['verdict']
        self.assertEqual(word, 'worth')
        self.assertLess(len(quote), 120, 'the quote must be the sentence, not the whole note')

    def test_credit_figure_is_expanded(self):
        from mechbot import ecr_summary as S
        _, blocks = self._parse('announcement_weekly_ecr.txt')
        hero, _, _, _ = S.guide_sections(blocks)
        self.assertEqual(hero['credits'], [800000])
        self.assertIn('800,000', S.render_post(blocks))

    def test_playoff_maths_is_extracted(self):
        from mechbot import ecr_summary as S
        text, blocks = self._parse('announcement_mega_ecr.txt')
        facts_found = dict(S.post_facts(S.fact_source_text({'details': {'entries': blocks}})))
        self.assertEqual(facts_found.get('Per spin'), '50 Playoff Points')
        self.assertEqual(facts_found.get('Spin maths'), '70 spins = 3,500 Playoff Points')

    def test_duplicate_compensation_is_extracted(self):
        from mechbot import ecr_summary as S
        found = S.post_facts('If you obtain Ingrid while already owning her, you can '
                             'receive  2,500 A-Coins as duplicate compensation.')
        self.assertIn(('Duplicate', '2,500 A-Coins if already owned'), found)

    def test_guide_post_renders_strategy_and_figures(self):
        from mechbot import ecr_summary as S
        text, blocks = self._parse('announcement_mega_ecr.txt')
        body = S.render_announcement(blocks, title='**Mega ECR**',
                                     text=S.fact_source_text({'details': {'entries': blocks}}))
        self.assertIn('If you already own Ingrid', body)
        self.assertIn('Figures the post states', body)
        self.assertIn('3,500 Playoff Points', body)

    def test_neither_post_is_classified_as_other(self):
        from mechbot import facts
        for name in ('announcement_weekly_ecr.txt', 'announcement_mega_ecr.txt'):
            text = (self.FIXTURES / name).read_text(encoding='utf-8')
            self.assertEqual(facts.classify(text)[0], 'ecr', f'{name} must classify as ECR')


class ReportedReplyDefectTests(unittest.TestCase):
    """Three defects taken from live screenshots of the running bot."""

    # ---------------------------------------------------------------- the table reply
    def test_column_header_table_becomes_one_sentence(self):
        """'| A-Coins | Credits | Blueprints |' + '| 2,825 | 265,000 | 10 |'.

        The old handler printed the header as a label line and the values as a number
        line, so a player saw 'A-Coins · Credits · Blueprints' followed by
        '2,825 · 265,000 · 10'.
        """
        from mechbot.assistant import naturalize_discord_reply
        table = ('**Redox Upgrade Cost (R4 L1 → R5 L1)**\n\n'
                 '| A-Coins | Credits | Blueprints |\n|---|---|---|\n| 2,825 | 265,000 | 10 |')
        out = naturalize_discord_reply(table)
        self.assertNotIn('|', out)
        self.assertIn('2,825 A-Coins', out)
        self.assertIn('265,000 Credits', out)
        self.assertIn('10 Blueprints', out)
        self.assertIn('Redox Upgrade Cost', out, 'the heading becomes the subject of the sentence')

    def test_label_value_table_keeps_one_line_per_row(self):
        """The other shape must not be zipped into garbage."""
        from mechbot.assistant import naturalize_discord_reply
        out = naturalize_discord_reply('| Item | Cost |\n|---|---|\n| A-Coins | 5,650 |\n| Credits | 715,000 |')
        self.assertIn('A-Coins · 5,650', out)
        self.assertIn('Credits · 715,000', out)
        self.assertNotIn('5,650 A-Coins', out)

    def test_a_sentence_with_no_table_is_untouched(self):
        from mechbot.assistant import naturalize_discord_reply
        line = 'For **Redox**, the rank 2 L1 → rank 5 L1 upgrade costs **5,275 A-Coins**.'
        self.assertEqual(naturalize_discord_reply(line), line)

    # ---------------------------------------------------------------- the builds miss
    def test_build_for_a_named_mech_is_a_request(self):
        """'build for Deathwalker for Squad power 14000' fell through to the OCR fallback."""
        from mechbot.assistant import natural_builds_request
        result = natural_builds_request('build for Deathwalker for Squad power 14000',
                                        ['Deathwalker', 'Redox'])
        self.assertIsNotNone(result)
        self.assertEqual(result['query'], 'Deathwalker')
        self.assertEqual(result['sp_range'], (14000, 14000))

    def test_build_request_without_a_question_mark_still_routes(self):
        from mechbot.assistant import natural_builds_request
        for question in ('build for deathwalker', 'deathwalker builds 14000 sp',
                         'whats a good build for deathwalker'):
            self.assertIsNotNone(natural_builds_request(question, ['Deathwalker']),
                                 f'{question!r} must route to the builds API')

    def test_a_message_that_names_no_mech_is_still_not_a_build_request(self):
        from mechbot.assistant import natural_builds_request
        self.assertIsNone(natural_builds_request('build me a sandwich', []))

    # ---------------------------------------------------------------- the OCR dump
    SHEET = ('Back DEATHWALKER\n'
             'Rank *********** ********** ***********\n'
             'Energy Capacity 18 12 16 20 24 28 32\n'
             'HP 12000 41600 64400 93300 129900 175800 227300\n'
             'Speed 19 km/h 19 km/h 19 km/h\n'
             'Power 177 193 300 435 608 823 1064\n'
             'Ability Cooldown 15 s 15 s 15 s\n')

    def test_screenshot_rows_are_parsed_per_rank(self):
        from mechbot.assistant import _screenshot_stats
        stats = dict(_screenshot_stats(self.SHEET))
        self.assertEqual(len(stats['HP']), 7)
        self.assertEqual(stats['HP'][0], '12000')
        self.assertNotIn('Rank', stats, 'the star ladder is not a stat')

    def test_a_specific_stat_question_is_answered_with_that_figure(self):
        from mechbot.assistant import _screenshot_stat_for
        label, value = _screenshot_stat_for('deathwalker hp at rank 5', self.SHEET)
        self.assertEqual(label, 'HP')
        self.assertIn('129900', value)

    def test_a_build_question_against_a_screenshot_is_not_answered_with_the_dump(self):
        """It used to paste the whole OCR page under 'Text from a user-provided screenshot'."""
        from mechbot.assistant import _archive_fallback
        out = _archive_fallback({'question': 'build for Deathwalker for Squad power 14000',
                                 'archive_material': [{'kind': 'user_confirmed_raw_ocr',
                                                       'excerpt': self.SHEET}]})
        self.assertIn('not a build', out)
        self.assertNotIn('12000 41600 64400', out, 'the raw ladder must not be pasted')
        self.assertLess(len(out), 400, 'the reply must stay a message, not a page')

    def test_the_screenshot_reply_offers_the_figure_it_does_have(self):
        from mechbot.assistant import _archive_fallback
        out = _archive_fallback({'question': 'how much hp does deathwalker have',
                                 'archive_material': [{'kind': 'user_confirmed_raw_ocr',
                                                       'excerpt': self.SHEET}]})
        self.assertIn('HP', out)
        self.assertIn('227300', out)


class EcrReplyReadingTests(unittest.TestCase):
    """The ECR answer as a player reads it, judged on the real weekly guide.

    The reported reply printed the guide's opening question as the item description, dropped
    the only price in the post ("Max Credits - 800K"), and then printed a separate verdict line
    per item that said the wrong thing: a clear "pick it up in the Gear Hub, don't spend A-Coins
    on it" came out as "only if it fits your build". Every test here is a line of that reply.
    """

    NOW = 1795350000
    DAY = 86400

    def _reply(self, extra_rows=(), **kwargs):
        from mechbot import facts
        weekly = (Path(__file__).parent / 'fixtures' / 'announcement_weekly_ecr.txt').read_text(encoding='utf-8')
        rows = [{'channel_id': 1, 'message_id': 'weekly', 'posted_at': self.NOW - 90000, 'edited_at': None,
                 'channel_name': 'x', 'guild_name': 'Mech Arena', 'message_url': '', 'content': weekly}]
        rows.extend(extra_rows)
        built, _ = facts.build_facts(rows, now=self.NOW)
        return facts.format_ecr_reply(built, now=self.NOW, **kwargs), built

    # ---------------------------------------------------------------- the verdict
    def test_the_only_price_in_the_post_is_in_the_reply(self):
        reply, _ = self._reply()
        self.assertIn('800K', reply, '"Max Credits - 800K" is the number the purchase turns on')

    def test_a_free_route_is_not_read_as_a_skip(self):
        """"Don't spend A-Coins on it - worth picking it up from the Gear Hub" is one yes."""
        from mechbot import facts
        verdict, _ = facts.advice_for("Available for Credits in the Gear Hub! Great opportunity to get it "
                                      "for FREE via free token Don't spend A-Coins on it Worth picking up "
                                      "from the Gear Hub!")
        self.assertEqual(verdict, 'worth_it')
        reply, _ = self._reply()
        self.assertIn('SUPPRESSOR 10', reply)
        self.assertNotIn('SUPPRESSOR 10: **only if it fits your build**', reply)

    def test_it_is_your_choice_is_not_a_verdict(self):
        from mechbot import facts
        self.assertIsNone(facts.advice_for("Get or Skip, it's your choice!"))
        # ...and when the post does advise, the choice line is not quoted as the advice
        self.assertTrue(facts.advice_for("Worth the Credits cz it's available for A-coins in Gear Hub "
                                        "Good mech, but very far from meta Get or Skip, it's your choice!"))

    def test_a_caveat_and_a_price_both_survive_in_the_item_block(self):
        reply, _ = self._reply()
        block = reply.split('1. **VORTEX (R4)**')[1].split('2. **')[0]
        self.assertIn('800K', block, '"Max Credits - 800K" is the number the purchase turns on')
        self.assertIn('far from meta', block, 'the caveat is the other half of the advice')
        self.assertIn('**Verdict:**', block)

    def test_each_item_is_named_once_before_the_roll_up(self):
        # The roll-up table names every item a second time on purpose; the body must not.
        reply, _ = self._reply()
        body = reply.split('**FINAL VERDICT**')[0]
        self.assertEqual(body.count('VORTEX (R4)'), 1, 'the item was named twice, once per block')
        self.assertEqual(body.count('SUPPRESSOR 10'), 1)
        table = reply.split('**FINAL VERDICT**')[1]
        self.assertEqual(table.count('VORTEX (R4)'), 1)
        self.assertEqual(table.count('SUPPRESSOR 10'), 1)

    def test_the_reply_never_quotes_a_fragment(self):
        reply, _ = self._reply()
        for bad in ('Skip,!', '— “Credits”', '“Don\'t”', '…'):
            self.assertNotIn(bad, reply)

    # ---------------------------------------------------------------- the closed window
    def test_an_expired_rotation_is_one_line_not_three(self):
        rows = [{'channel_id': 1, 'message_id': 'one', 'posted_at': self.NOW - 3 * self.DAY,
                 'edited_at': None, 'channel_name': 'x', 'guild_name': 'Mech Arena', 'message_url': '',
                 'content': '# Credits ECR is here!! \U0001F4B0\n## The one-day rush starts now [member]'}]
        reply, _ = self._reply(rows)
        block = reply.split('**Earlier**')[1]
        self.assertIn('window has passed', block)
        self.assertNotIn('No buy or skip advice', block, 'a closed window is not worth three lines')
        self.assertNotIn('one-day ECR · one-day ECR', reply, 'the kind was printed twice in the heading')

    def test_the_tail_names_what_is_hidden(self):
        # Old enough to be history against the weekly rhythm, so they belong behind the door.
        seasonal = [{'channel_id': 1, 'message_id': f'seasonal{index}', 'posted_at': self.NOW - age * self.DAY,
                     'edited_at': None, 'channel_name': 'x', 'guild_name': 'Mech Arena', 'message_url': '',
                     'content': f'# Seasonal ECR\n## {name} \U0001F47E (R4)\n### Tracker\n'
                                '\u2022 Just skip and save your A-coins.'}
                    for index, (age, name) in enumerate(((70, 'ORACLE 16'), (95, 'SAVANT 10')))]
        reply, built = self._reply(seasonal)
        self.assertIn('earlier ECR post(s) are saved but hidden', reply)
        self.assertIn('ORACLE 16', reply, 'five unnamed posts is a door with nothing written on it')
        self.assertIn('ask for \u201cprevious ECRs\u201d', reply)



class EcrItemSummaryTests(unittest.TestCase):
    """The reply is a summary of the rotation, not a transcript of the post.

    The shape the owner asked for, one numbered block per featured item:

        1. VORTEX (R4)
        Availability: Gear Hub – A-Coins
        • Max Credits: 800K
        • A decent mech, but still far from the current meta.
        Verdict: Worth the Credits...

    ...then a FINAL VERDICT roll-up that answers "which one, and with what" in one look. Before
    this, both items were printed as one long bullet each, with the verdict word embedded in the
    middle of quoted clauses and no summary anywhere.
    """

    NOW = 1795350000
    DAY = 86400

    def _reply(self, content=None):
        from mechbot import facts
        body = content or (Path(__file__).parent / 'fixtures' / 'announcement_weekly_ecr.txt').read_text(encoding='utf-8')
        built, _ = facts.build_facts([{'channel_id': 1, 'message_id': 'weekly',
                                       'posted_at': self.NOW - 90000, 'edited_at': None,
                                       'channel_name': 'x', 'guild_name': 'Mech Arena',
                                       'message_url': '', 'content': body}], now=self.NOW)
        return facts.format_ecr_reply(built, now=self.NOW), built

    def test_each_item_is_a_numbered_block_of_its_own(self):
        reply, _ = self._reply()
        self.assertIn('1. **VORTEX (R4)**', reply)
        self.assertIn('2. **SUPPRESSOR 10**', reply)

    def test_every_block_states_availability_and_a_verdict(self):
        reply, _ = self._reply()
        for block in reply.split('**FINAL VERDICT**')[0].split('\n\n')[1:]:
            if '**Availability:**' not in block:
                continue
            self.assertIn('**Availability:** Gear Hub', block)
            self.assertIn('**Verdict:**', block)

    def test_an_a_coin_route_and_a_credits_route_are_told_apart(self):
        # VORTEX is "available for A-coins in Gear Hub"; SUPPRESSOR 10 is "available for Credits
        # in the Gear Hub" with a free token on top. One line each, read off the post.
        reply, _ = self._reply()
        vortex = reply.split('1. **VORTEX (R4)**')[1].split('2. **')[0]
        suppressor = reply.split('2. **SUPPRESSOR 10**')[1].split('**FINAL VERDICT**')[0]
        self.assertIn('Gear Hub \u2013 A-Coins', vortex)
        self.assertIn('Credits or free token', suppressor)
        self.assertNotIn('Credits or free token', vortex)

    def test_a_ruled_out_currency_does_not_become_the_availability(self):
        # "Don't spend A-Coins on it" names A-Coins to rule them out. Read as a route, it turned a
        # free Gear Hub weapon into an A-Coin purchase.
        reply, _ = self._reply()
        suppressor = reply.split('2. **SUPPRESSOR 10**')[1].split('**FINAL VERDICT**')[0]
        self.assertNotIn('Availability:** Gear Hub \u2013 A-Coins', suppressor)

    def test_the_bullets_are_the_posts_own_sentences_one_per_line(self):
        reply, _ = self._reply()
        vortex = reply.split('1. **VORTEX (R4)**')[1].split('2. **')[0]
        self.assertIn('\u2022 Max Credits: 800K', vortex)
        self.assertIn('\u2022 Good mech, but very far from meta', vortex)
        self.assertNotIn(' · ', vortex, 'the old reply joined every clause with middle dots')

    def test_the_roll_up_answers_which_item_and_with_what(self):
        reply, _ = self._reply()
        tail = reply.split('**FINAL VERDICT**')[1]
        self.assertIn('Item | Recommendation | Currency', tail)
        self.assertRegex(tail, r'VORTEX \(R4\) \| Worth Getting \| A-Coins')
        self.assertRegex(tail, r'SUPPRESSOR 10 \| Worth Getting \| Credits / Free Token')

    def test_the_roll_up_follows_the_items_and_the_items_follow_the_post(self):
        reply, _ = self._reply()
        self.assertLess(reply.index('1. **VORTEX'), reply.index('2. **SUPPRESSOR'))
        self.assertLess(reply.index('SUPPRESSOR 10'), reply.index('**FINAL VERDICT**'))

    def test_no_emoji_reaches_the_reply(self):
        reply, _ = self._reply()
        for glyph in ('\U0001F381', '\U0001F6E1', '\U0001F4A5', '\U0001F4A1', '\u2699', '\u2728',
                      '\U0001FAE0', '\U0001F3C6', '\U0001F525', '\u26A1'):
            self.assertNotIn(glyph, reply, f'{glyph!r} must be stripped from an ECR answer')

    def test_a_post_that_states_no_route_says_so_instead_of_guessing(self):
        reply, _ = self._reply('# Current Weekly ECR Guide\n## ORACLE 16 \U0001F47E (R4)\n'
                               '### The worst tracker is here\n\u2022 Oracle is a very trash weapon.\n'
                               '\u2022 Just skip and save your A-coins.')
        self.assertNotIn('Availability:**', reply.split('**FINAL VERDICT**')[0])
        self.assertIn('ORACLE 16 (R4) | Skip | Not stated', reply)

    def test_a_skipped_item_is_marked_skip_in_both_places(self):
        reply, _ = self._reply('# Current Weekly ECR Guide\n## ORACLE 16 \U0001F47E (R4)\n'
                               '### The worst tracker is here\n\u2022 Just skip and save your A-coins.\n'
                               '## TAILSTING \U0001F9E9\n### Spider meta\n\u2022 Worth it if you run spiders.')
        self.assertIn('**Verdict:** Skip it', reply)
        self.assertIn('ORACLE 16 (R4) | Skip | Not stated', reply)
        self.assertIn('TAILSTING | Optional | Not stated', reply)   # "worth it if..." is conditional



class EcrLiveOrOutdatedTests(unittest.TestCase):
    """The bot must say whether a saved rotation is the live one or an archived one.

    Every ECR answer is built from saved posts, so the same reply shape has to carry the
    difference: a weekly guide posted yesterday is the rotation running now, and a guide the
    channel stopped replacing a month ago is history however tidy it looks. Both signals come
    from the data - the post's own date against the channel's measured rhythm, and any window
    the post states - never from a page of the calendar.
    """

    NOW = 1795350000   # Nov 22, 2026
    DAY = 86400

    def _facts(self, *posts):
        from mechbot import facts
        rows = [{'channel_id': 1, 'message_id': f'p{index}', 'posted_at': age, 'edited_at': None,
                 'channel_name': 'x', 'guild_name': 'Mech Arena', 'message_url': '', 'content': body}
                for index, (age, body) in enumerate(posts)]
        built, _ = facts.build_facts(rows, now=self.NOW)
        return facts, built

    WEEKLY_GUIDE = ('# Current Weekly ECR Guide\n## VORTEX (R4)\n### here\n'
                    '\u2022 Max Credits - 800K\n\u2022 Worth the Credits.')

    def test_the_rotation_running_now_is_reported_as_live(self):
        facts, built = self._facts((self.NOW - 2 * self.DAY, self.WEEKLY_GUIDE))
        reply = facts.format_ecr_reply(built, now=self.NOW)
        self.assertIn('**Status:** Live', reply)

    def test_a_rotation_the_channel_stopped_replacing_is_outdated(self):
        # A weekly guide left in place for 40 days: the cadence in the saved set says this kind
        # is announced about weekly, so it cannot still be the rotation running now.
        older = (self.NOW - 47 * self.DAY, self.WEEKLY_GUIDE)
        newer = (self.NOW - 40 * self.DAY, self.WEEKLY_GUIDE.replace('VORTEX', 'ORACLE 16'))
        facts, built = self._facts(older, newer)
        reply = facts.format_ecr_reply(built, now=self.NOW)
        self.assertIn('**Status:** Outdated', reply)
        self.assertIn('announced about', reply, 'the reason is the measured rhythm, said out loud')

    def test_a_rotation_whose_stated_window_has_shut_is_outdated(self):
        body = ('# Credits ECR is here!\nIt runs Nov 18 to Nov 19, then closes.\n'
                '## ORACLE 16 (R4)\n\u2022 Just skip and save your A-coins.')
        facts, built = self._facts((self.NOW - 4 * self.DAY, body))
        reply = facts.format_ecr_reply(built, now=self.NOW)
        self.assertIn('Outdated', reply)

    def test_previous_ecrs_are_each_judged_not_just_listed(self):
        facts, built = self._facts((self.NOW - 2 * self.DAY, self.WEEKLY_GUIDE),
                                   (self.NOW - 60 * self.DAY, self.WEEKLY_GUIDE.replace('VORTEX', 'SAVANT 10')))
        reply = facts.format_ecr_reply(built, now=self.NOW, include_older=True)
        other = reply.split('**Other ECR posts in the saved announcements**')[1]
        self.assertIn('**outdated**', other)
        self.assertNotIn('**live**', other)

    def test_other_announcements_carry_a_standing_too(self):
        facts, built = self._facts((self.NOW - 200 * self.DAY,
                                    '# Patch notes\nRebalance: Voidghost nerfed.'))
        listing = facts.format_fact_list(built, 'Patches', now=self.NOW)
        self.assertIn('**Status:** Outdated as a live statement', listing)


class SavedEcrListingTests(unittest.TestCase):
    """The listing path (format_fact_list) was worse than the reply: one post read as three."""

    NOW = 1795350000
    DAY = 86400

    def _listing(self):
        from mechbot import facts
        weekly = (Path(__file__).parent / 'fixtures' / 'announcement_weekly_ecr.txt').read_text(encoding='utf-8')
        built, _ = facts.build_facts([{'channel_id': 1, 'message_id': 'weekly',
                                       'posted_at': self.NOW - 90000, 'edited_at': None,
                                       'channel_name': 'x', 'guild_name': 'Mech Arena',
                                       'message_url': '', 'content': weekly}], now=self.NOW)
        return facts.format_fact_list(built, '\U0001F381 Saved ECR posts', limit=4, now=self.NOW)

    def test_the_listing_says_the_verdict_once_per_item(self):
        listing = self._listing()
        self.assertEqual(listing.count('worth the spend'), 2, 'once for VORTEX, once for SUPPRESSOR 10')
        self.assertIn('800K', listing)

    def test_bold_emphasis_is_not_listed_as_an_item(self):
        listing = self._listing()
        items_line = next(line for line in listing.split('\n') if 'Items:' in line)
        self.assertNotIn('FREE', items_line)
        self.assertNotIn("Don't", items_line)
        self.assertIn('VORTEX', items_line)

    def test_the_guides_emoji_never_reaches_a_sentence(self):
        listing = self._listing()
        self.assertNotIn('\U0001FAE0', listing)      # 🫠 mid-sentence read as a glitch
        self.assertNotIn('\U0001F914', listing)      # 🤔


class EventRotationTests(unittest.TestCase):
    """Schedules rotate; an expired one must not read as something running now."""

    NOW = 1795350000   # Nov 22, 2026
    DAY = 86400

    def _facts(self, *posts):
        from mechbot import facts
        rows = [{'channel_id': 1, 'message_id': f'e{index}', 'posted_at': self.NOW - age * self.DAY,
                 'edited_at': None, 'channel_name': 'x', 'guild_name': 'Mech Arena',
                 'message_url': '', 'content': body}
                for index, (age, body) in enumerate(posts)]
        built, _ = facts.build_facts(rows, now=self.NOW)
        return facts, built

    def test_a_stated_window_is_read_as_a_window(self):
        from mechbot import facts
        self.assertEqual(facts.stated_dates('Starts Nov 24, ends Nov 27.'), ['Nov 24', 'Nov 27'])
        self.assertEqual(facts.stated_dates('Playoff runs Nov 24 - Nov 27'), ['Nov 24 - Nov 27'])

    def test_a_post_that_states_a_window_is_an_event_not_other(self):
        facts, built = self._facts((3, '# Arena Playoff is here!\nStarts Oct 12, ends Oct 18. Play to earn points.'))
        self.assertEqual(built[0]['category'], 'event')
        self.assertEqual(facts._window_state(built[0], self.NOW), 'over')

    def test_an_expired_event_says_so_and_a_future_one_says_that(self):
        facts, built = self._facts((3, '# Arena Playoff is here!\nStarts Oct 12, ends Oct 18.'),
                                   (2, '# New Event: Accolade Challenge!\nStarts Nov 24, ends Nov 27.'))
        reply = facts.format_announcement_reply(built, now=self.NOW)
        self.assertIn('this window has passed', reply)
        self.assertIn('starts later', reply)
        self.assertLess(reply.index('Accolade'), reply.index('Playoff'), 'what shuts next comes first')

    def test_a_rebalance_is_balance_news_not_an_event(self):
        facts, built = self._facts((1, '# Rebalance 7th Wave is now live\nOnyx EMP reduced to 2s, Outlaw nerfed.'))
        self.assertEqual(built[0]['category'], 'balance_change')

    def test_a_summary_does_not_repeat_the_title(self):
        facts, built = self._facts((1, '# Rebalance 7th Wave is now live\nOnyx EMP reduced to 2s, Outlaw nerfed.'))
        reply = facts.format_announcement_reply(built, now=self.NOW)
        self.assertIn('Onyx EMP reduced to 2s', reply)
        self.assertNotIn('is now live Onyx EMP', reply)


class RotationScheduleTests(unittest.TestCase):
    """A rotation that has not started is not running, and a schedule question is not an item one."""

    def _records(self):
        from mechbot import ecr_summary as S
        return [S.from_tracker_row({'start': '2026-12-24', 'end': '2026-12-27', 'type': 'pilot',
                                    'name': '?', 'rank': '', 'cps': '', 'items': '', 'chances': ''}),
                S.from_tracker_row({'start': '2026-11-23', 'end': '2026-11-25', 'type': 'mech',
                                    'name': 'Tailsting', 'rank': '3', 'cps': '100',
                                    'items': 'inf', 'chances': '1%'})]

    def test_a_future_rotation_is_not_listed_as_running(self):
        from datetime import date
        from mechbot import ecr_summary as S
        text = S.render(self._records(), now=date(2026, 11, 24))
        running = text.split('**Running now**')[1].split('**Scheduled next**')[0]
        self.assertNotIn('24 Dec', running, 'a December rotation was printed under Running now')
        self.assertIn('Scheduled next', text)
        self.assertIn('24 Dec', text.split('**Scheduled next**')[1])

    def test_a_schedule_question_is_not_answered_with_one_item(self):
        from mechbot.bot import _wants_one_item
        for question in ('ecr', 'current ecr', 'previous ecrs', 'whats the next ecr'):
            self.assertFalse(_wants_one_item(question), question)
        for question in ('when was tailsting in the ecr', 'tailsting history'):
            self.assertTrue(_wants_one_item(question), question)
