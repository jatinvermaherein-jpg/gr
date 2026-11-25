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
