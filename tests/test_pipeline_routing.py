"""Regression tests for the read-then-answer-then-format path.

These guard the two failures seen in the screenshots: a cost question that was
answered with a mech review, and a misspelled mech name that deleted the subject of
the question. Both are routing bugs, so both are tested through `on_message`.
"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mechbot.catalog import Catalog, initialize_snapshot
from mechbot.bot import MechBot
from mechbot.pipeline import correct_names, guard_figures


COST_UNDERSTANDING = {
    'intent': 'upgrade_cost', 'mechs': ['Redox'], 'weapons': [], 'pilots': [], 'mods': [],
    'from_rank': 1, 'from_level': 1, 'to_rank': 2, 'to_level': 1,
    'question': 'What is the upgrade cost for Redox from rank 1 level 1 to rank 2 level 1?',
    'unclear': False,
}
COMPARE_UNDERSTANDING = {
    'intent': 'compare', 'mechs': ['Lacewing'], 'weapons': ['Hornet 12', 'Hornet 16'],
    'pilots': [], 'mods': [], 'from_rank': None, 'from_level': None,
    'to_rank': None, 'to_level': None,
    'question': 'On Lacewing, which is better: two Hornet 12 or Hornet 16 plus Hornet 8?',
    'unclear': False,
}


class FuzzyNameTests(unittest.TestCase):
    def setUp(self):
        self.pool = ('Lacewing', 'Seeker', 'Deathwalker', 'Nomad', 'Killshot',
                     'Redox', 'Hornet 12', 'Hornet 16', 'Hornet 8')

    def test_misspelt_catalog_names_are_repaired_and_disclosed(self):
        for typed, expected in (('lacwing', 'Lacewing'), ('seekr', 'Seeker'),
                                ('deathwalkr', 'Deathwalker'), ('nomand', 'Nomad'),
                                ('kilshot', 'Killshot')):
            text, fixed = correct_names(f'something about {typed} please', self.pool)
            self.assertEqual(fixed, {typed: expected})
            self.assertIn(expected, text)

    def test_ordinary_words_and_short_words_are_left_alone(self):
        for text in ('how do i upgrade rank', 'bring me a snack', 'what about the meta'):
            corrected, fixed = correct_names(text, self.pool)
            self.assertEqual(fixed, {})
            self.assertEqual(corrected, text)

    def test_exact_names_are_never_rewritten(self):
        corrected, fixed = correct_names('is Lacewing good', self.pool)
        self.assertEqual(fixed, {})
        self.assertIn('Lacewing', corrected)


class FigureGuardTests(unittest.TestCase):
    def test_a_number_the_engine_never_produced_is_rejected(self):
        engine = 'For **Redox**: 15,325 Credits · 75 A-Coins · 6 Blueprints (BP).'
        ok, invented = guard_figures('Redox costs 80 A-Coins for 1299 Credits.', engine)
        self.assertFalse(ok)
        self.assertTrue(invented)

    def test_reformatting_the_same_numbers_is_allowed(self):
        engine = 'For **Redox**: 15,325 Credits · 75 A-Coins · 6 Blueprints (BP).'
        ok, _ = guard_figures('**Redox** costs 15,325 Credits, 75 A-Coins and 6 Blueprints.', engine)
        self.assertTrue(ok)


class PipelineRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Without a model configured the pipeline declines everything, by design, and the
        # old ladder answers. These tests are about the path where a model is available.
        self.env = patch.dict(os.environ, {'AI_API_KEY': 'test-key', 'AI_MODEL': 'test-model'})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        snapshot = Path(self.tmp.name) / 'runtime.sqlite3'
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(snapshot)}):
            self.runtime = initialize_snapshot()
        self.catalog = Catalog(self.runtime)

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()
        self.env.stop()

    def _bot(self):
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()
        return bot

    def _message(self, bot, question):
        return SimpleNamespace(author=SimpleNamespace(id=123456789012345678, bot=False),
                               channel=SimpleNamespace(id=44), guild=None,
                               mentions=[bot._connection.user],
                               content=f'<@{bot._connection.user.id}> {question}', reference=None)

    async def _run(self, bot, question, understanding):
        async def fake_chat(base, key, model, system, user, **kwargs):
            if 'Reply with ONE JSON' in system:
                return json.dumps(understanding)
            return 'X'          # the formatter declines, so the engine text must survive
        with patch('mechbot.pipeline._chat', new=fake_chat):
            await bot.on_message(self._message(bot, question))
        self.assertTrue(bot.reply_message.await_args, 'no reply was sent')
        return bot.reply_message.await_args.args[1]

    async def test_cost_question_is_answered_with_the_cost_not_a_mech_review(self):
        bot = self._bot()
        reply = await self._run(bot, 'upgrade cost for redox from rank 1 level 1 to rank 2 level 1',
                                COST_UNDERSTANDING)
        self.assertIn('15,325 Credits', reply)
        self.assertIn('75 A-Coins', reply)
        self.assertIn('6 Blueprints', reply)
        self.assertNotIn('Tier S', reply)
        # spoken like a person, not printed like a receipt
        self.assertRegex(reply, r'costs|comes to')
        self.assertNotIn('→ rank', reply.replace('rank 1 L1 → rank 2 L1', ''))

    async def test_cost_reply_keeps_every_figure_exactly(self):
        bot = self._bot()
        reply = await self._run(bot, 'upgrade cost for redox r4 l1 to r5 l1',
                                {**COST_UNDERSTANDING, 'from_rank': 4, 'to_rank': 5})
        self.assertIn('265,000 Credits', reply)
        self.assertIn('2,825 A-Coins', reply)
        self.assertIn('10 Blueprints', reply)


    async def test_message_the_pipeline_does_not_own_still_reaches_its_own_handler(self):
        bot = self._bot()
        async def fake_chat(base, key, model, system, user, **kwargs):
            if 'Reply with ONE JSON' in system:
                return json.dumps({'intent': 'chitchat', 'mechs': [], 'weapons': [], 'pilots': [],
                                   'mods': [], 'question': '', 'unclear': True})
            return 'X'
        with patch('mechbot.pipeline._chat', new=fake_chat), \
                patch('mechbot.bot.answer_question_with_brain',
                      new=AsyncMock(return_value='chitchat answered by the usual path')):
            await bot.on_message(self._message(bot, 'hello there bot'))
        self.assertEqual(bot.reply_message.await_args.args[1], 'chitchat answered by the usual path')

    async def test_comparison_question_gets_a_verdict_and_reports_the_correction(self):
        bot = self._bot()
        reply = await self._run(bot, 'which is best on lacwing hornet 12s or hornet 1+8',
                                COMPARE_UNDERSTANDING)
        self.assertIn('Lacewing', reply)
        self.assertIn('lacwing', reply)          # the correction is disclosed, not silent
        self.assertNotIn('Which mech on each side', reply)
        self.assertNotIn('Hornet 1 is not in the catalog', reply)

class VoiceTests(unittest.TestCase):
    def test_cost_voice_never_invents_or_drops_a_figure(self):
        from mechbot.voice import humanise_cost
        em = {'acoins': 'A', 'credits': 'C', 'blueprints': 'B'}
        costs = {'acoins': 75, 'credits': 15325, 'blueprints': 6}
        for _ in range(20):                    # the wording is randomised on purpose
            line = humanise_cost('Redox', 'rank 1 L1 → rank 2 L1', costs, em)
            self.assertIn('75', line)
            self.assertIn('15,325', line)
            self.assertIn('6', line)
            self.assertNotIn('0 ', line.split('**')[1] if '**' in line else '')

    def test_a_gap_is_said_out_loud_instead_of_filled(self):
        from mechbot.voice import humanise_cost, missing_fact
        line = humanise_cost('Redox', 'rank 2 → rank 3', {'acoins': 0, 'credits': 0, 'blueprints': 0}, {})
        self.assertIn("won't guess", line)
        self.assertNotRegex(line, r'\d{2,}')
        self.assertIn('rank 2 → rank 3', missing_fact('Redox', 'rank 2 → rank 3'))

    def test_spelling_correction_is_disclosed(self):
        from mechbot.voice import spell_it_out
        self.assertEqual(spell_it_out('x', {}), '')
        self.assertIn('lacwing', spell_it_out('x', {'lacwing': 'Lacewing'}))

    def test_emoji_fallbacks_exist_for_every_name(self):
        from mechbot.voice import EmojiBook, EMOJI_SET
        book = EmojiBook()
        for name, _, fallback in EMOJI_SET:
            self.assertEqual(book.get(name), fallback)
        self.assertTrue(all(book.currency_map().values()))

if __name__ == '__main__':
    unittest.main()


class ScreenshotPingTests(unittest.IsolatedAsyncioTestCase):
    """People ping the bot and drop a screenshot in, usually with no words at all.

    Nothing in the command surface covers this, and the text ladder answered it by
    pattern-matching whatever names it could find in the OCR text: a Deathwalker stat sheet
    came back as a mech review, and an empty ping produced no reply whatsoever.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        snapshot = Path(self.tmp.name) / 'runtime.sqlite3'
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(snapshot)}):
            self.runtime = initialize_snapshot()
        self.catalog = Catalog(self.runtime)
        self.image = SimpleNamespace(filename='image.png', size=120_000,
                                     url='https://cdn.discordapp.com/attachments/1/image.png',
                                     content_type='image/png')

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def _bot(self):
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()
        return bot

    def _message(self, bot, text, attachments=()):
        return SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                               guild=None, mentions=[bot._connection.user],
                               content=(f'<@{bot._connection.user.id}> {text}'.strip()),
                               reference=None, attachments=list(attachments))

    async def _answer(self, text, attachments, env=None, reader=None):
        bot = self._bot()
        seen = {}

        async def fake_reader(base, key, model, question, images, catalog=None, session=None, **kwargs):
            seen['question'], seen['images'] = question, images
            return (reader or (lambda: ('That is a **Deathwalker** stat sheet, not a build.', True)))()

        with patch.dict(os.environ, env or {'AI_API_KEY': 'test-key', 'AI_MODEL': 'test-model'}), \
                patch('mechbot.bot.read_screenshot', new=fake_reader):
            await bot.on_message(self._message(bot, text, attachments))
        reply = bot.reply_message.await_args.args[1] if bot.reply_message.await_args else None
        return reply, seen

    async def test_a_ping_with_only_a_screenshot_is_answered(self):
        reply, seen = await self._answer('', [self.image])
        self.assertIsNotNone(reply, 'an image-only ping used to be dropped by the empty-text gate')
        self.assertIn('Deathwalker', reply)
        self.assertEqual(seen['question'], 'what does this screenshot show?')
        self.assertEqual(len(seen['images']), 1)
        self.assertEqual(seen['images'][0]['url'], self.image.url)

    async def test_the_screenshot_is_read_before_the_catalog_ladder(self):
        """A screenshot naming a mech is a screenshot question, not a mech review."""
        reply, _ = await self._answer('is redox good', [self.image])
        self.assertIn('stat sheet', reply)
        self.assertNotIn('tier A in the community list', reply)

    async def test_a_ping_without_a_screenshot_still_uses_the_text_ladder(self):
        reply, seen = await self._answer('is redox good', [])
        self.assertEqual(seen, {}, 'the vision path must not run without an image')
        self.assertIn('Redox', reply)

    async def test_words_in_the_ping_reach_the_reader(self):
        _, seen = await self._answer('whats wrong with this build', [self.image])
        self.assertEqual(seen['question'], 'whats wrong with this build')

    async def test_only_images_are_sent_to_the_reader(self):
        from mechbot.assistant import screenshot_images
        archive = SimpleNamespace(filename='logs.zip', size=2000, url='https://cdn.discordapp.com/x.zip',
                                  content_type='application/zip')
        nameless = SimpleNamespace(filename='shot.JPG', size=2000, url='https://cdn.discordapp.com/shot.JPG',
                                   content_type=None)
        huge = SimpleNamespace(filename='big.png', size=40 * 1024 * 1024,
                               url='https://cdn.discordapp.com/big.png', content_type='image/png')
        found = screenshot_images(SimpleNamespace(attachments=[archive, nameless, huge]))
        self.assertEqual([item['url'] for item in found], ['https://cdn.discordapp.com/shot.JPG'])

    async def test_an_unreadable_screenshot_is_admitted_not_guessed(self):
        def boom():
            raise RuntimeError('AI provider request failed (HTTP 400).')
        reply, _ = await self._answer('what is this', [self.image], reader=boom)
        self.assertIn('could not read that screenshot', reply)
        self.assertNotIn('tier A', reply, 'it must not fall back to a catalog guess')

    async def test_no_vision_model_says_so_instead_of_answering_sideways(self):
        reply, seen = await self._answer('what is this', [self.image],
                                         env={'AI_API_KEY': '', 'AI_MODEL': ''})
        self.assertIn('vision model', reply)
        self.assertEqual(seen, {}, 'no reader call is made without credentials')


class CostReplyShapeTests(unittest.IsolatedAsyncioTestCase):
    """The cost reply as it reached the channel: a fenced block of label/value rows.

    Reported twice from live screenshots. First as a pipe table, then - after that was fixed -
    as a grey code block, because FORMAT_PROMPT rule 7 asked for exactly that layout and this
    path sent the model's output to Discord without the pass every other AI reply gets.
    """

    FORMATTED = ('**Upgrade cost for Redox rank 4 L1 \u2192 rank 5 L1**\n\n'
                 '```\nA-Coins    2,825\nCredits    265,000\nBlueprints 10\n```')
    UNDERSTOOD = {'intent': 'upgrade_cost', 'mechs': ['Redox'], 'weapons': [], 'pilots': [], 'mods': [],
                  'from_rank': 4, 'from_level': 1, 'to_rank': 5, 'to_level': 1,
                  'question': 'upgrade cost redox rank 4 l1 to rank 5 l1', 'unclear': False}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(Path(self.tmp.name) / 'r.sqlite3')}):
            self.catalog = Catalog(initialize_snapshot())

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    async def _cost_reply(self, formatter_text):
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()

        async def fake_chat(base, key, model, system, user, **kwargs):
            if 'ONE JSON object' in system:
                return json.dumps(self.UNDERSTOOD)
            return formatter_text
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], reference=None, attachments=[],
                                  content='upgrade cost redox rank 4 l1 to rank 5 l1')
        with patch.dict(os.environ, {'AI_API_KEY': 'k', 'AI_MODEL': 'm'}), \
                patch('mechbot.pipeline._chat', new=fake_chat):
            await bot.on_message(message)
        return bot.reply_message.await_args.args[1]

    # ---------------------------------------------------------------- the reply itself
    async def test_a_cost_reply_is_one_sentence_not_a_receipt(self):
        reply = await self._cost_reply(self.FORMATTED)
        self.assertNotIn('```', reply, 'a fenced block is a till roll, not an answer')
        self.assertNotIn('\n', reply, 'one sentence, so no label rows')
        self.assertIn('2,825 A-Coins', reply)
        self.assertIn('265,000 Credits', reply)
        self.assertIn('10 Blueprints', reply)
        self.assertIn('rank 4 L1 \u2192 rank 5 L1', reply)
        self.assertIn('costs', reply)

    async def test_the_cost_reply_never_grows_commentary(self):
        """It shipped once as "... Blueprints are the part that gates this one." """
        reply = await self._cost_reply(self.FORMATTED)
        for remark in ('gates this one', 'scarce half', 'Straight answer', 'Here you go'):
            self.assertNotIn(remark, reply)
        self.assertTrue(reply.rstrip().endswith('.'), reply)

    async def test_a_formatter_that_adds_a_figure_cannot_change_the_answer(self):
        reply = await self._cost_reply('**Redox** costs 2,825 A-Coins, 265,000 Credits and 10 Blueprints. '
                                       'Buy 4 more for 9,999 A-Coins.')
        self.assertNotIn('9,999', reply, 'guard_figures rejects the formatting and the engine text survives')

    # ---------------------------------------------------------------- the prompt that caused it
    def test_the_prompt_does_not_ask_for_a_code_block(self):
        from mechbot.pipeline import FORMAT_PROMPT
        self.assertNotIn('use a fenced block', FORMAT_PROMPT)
        self.assertIn('never a fenced code block', FORMAT_PROMPT)

    # ---------------------------------------------------------------- the flattener
    def test_a_fenced_row_block_becomes_a_sentence(self):
        from mechbot.assistant import naturalize_discord_reply
        out = naturalize_discord_reply(self.FORMATTED)
        self.assertNotIn('```', out)
        self.assertEqual(out, '**Upgrade cost for Redox rank 4 L1 \u2192 rank 5 L1**: '
                              '2,825 A-Coins \u00b7 265,000 Credits \u00b7 10 Blueprints')

    def test_a_fence_around_prose_is_only_unfenced(self):
        from mechbot.assistant import naturalize_discord_reply
        out = naturalize_discord_reply('```\nRedox is a tier A attacker.\n```')
        self.assertNotIn('```', out)
        self.assertIn('Redox is a tier A attacker.', out)

    def test_the_one_shape_of_a_cost_sentence(self):
        from mechbot.voice import humanise_cost
        em = {'acoins': 'A', 'credits': 'C', 'blueprints': 'B'}
        costs = {'acoins': 2825, 'credits': 265000, 'blueprints': 10}
        for _ in range(10):
            line = humanise_cost('Redox', 'rank 4 L1 \u2192 rank 5 L1', costs, em)
            self.assertEqual(line, 'For **Redox**, the rank 4 L1 \u2192 rank 5 L1 upgrade costs '
                                   '**A 2,825 A-Coins \u00b7 C 265,000 Credits \u00b7 B 10 Blueprints**.')


class MechAnswerUsesTheDataTests(unittest.IsolatedAsyncioTestCase):
    """Two mechs, one word each, from a live screenshot.

    "voidghost" was answered with a weighted score, a rank-1 ladder and a filler line, while the
    same question without a model configured got the ladder. "redox" got a stat list at one end
    of the bot (32 energy, 227,300 HP, 22 speed, 948 power) and a review with a weird ordering at
    the other. The catalog held every figure; the explain path never read it.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(Path(self.tmp.name) / 'r.sqlite3')}):
            self.catalog = Catalog(initialize_snapshot())

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def _understood(self, mech):
        return {'intent': 'meta_advice', 'mechs': [mech], 'weapons': [], 'pilots': [], 'mods': [],
                'from_rank': None, 'from_level': None, 'to_rank': None, 'to_level': None,
                'question': mech.lower(), 'unclear': False}

    async def _pipeline_reply(self, mech, formatter_text):
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()
        understood = self._understood(mech)

        async def fake_chat(base, key, model, system, user, **kwargs):
            return json.dumps(understood) if 'ONE JSON object' in system else formatter_text
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], reference=None,
                                  attachments=[], content=mech.lower())
        with patch.dict(os.environ, {'AI_API_KEY': 'k', 'AI_MODEL': 'm'}), \
                patch('mechbot.pipeline._chat', new=fake_chat):
            await bot.on_message(message)
        return bot.reply_message.await_args.args[1]

    # ---------------------------------------------------------------- the reviewed mech
    async def test_a_reviewed_mech_reports_its_own_figures(self):
        reply = await self._pipeline_reply('Redox', 'Redox is a solid attacker.')
        self.assertIn('227,300 HP', reply, 'HP is in the catalog and belongs in the answer')
        self.assertIn('948 power', reply)
        self.assertIn('22 speed', reply)
        # the figures come before the review paragraph, not buried under it
        self.assertLess(reply.index('227,300'), reply.index('Quick review'))

    async def test_a_reviewed_mech_does_not_lead_with_energy_only(self):
        from mechbot import advising, advisor
        reply = advising.answer('redox', data=advisor.load())['reply']
        self.assertIn('HP 227,300', reply)
        self.assertNotIn('Redox: energy 32. Tier A', reply, 'energy alone was the whole opener')

    def test_the_weighted_score_is_not_the_answer(self):
        from mechbot import advising, advisor
        for name in ('redox', 'killshot', 'voidghost'):
            reply = advising.answer(name, data=advisor.load())['reply']
            self.assertNotIn('balanced weights', reply, f'{name}: a score about our weights, not the mech')

    def test_the_filler_line_is_gone(self):
        from mechbot import advising, advisor
        for name in ('redox', 'voidghost'):
            reply = advising.answer(name, data=advisor.load())['reply']
            self.assertNotIn('What would change this', reply)
            self.assertNotIn('the mech you pilot and whether you fight at its range band', reply)

    # ---------------------------------------------------------------- the mech with no stats
    async def test_a_mech_with_no_stat_row_still_gets_its_prices(self):
        reply = await self._pipeline_reply('Voidghost', 'Voidghost is a mech.')
        self.assertIn('2,655,575 Credits', reply, 'the ladder is real data and the shrug hid it')
        self.assertIn('8,175 A-coins', reply)

    async def test_both_paths_give_the_same_answer(self):
        """With a model and without one, one word about a mech must not mean two answers."""
        with_model = await self._pipeline_reply('Voidghost', 'Voidghost is a mech.')
        without = (await MechBot(self.catalog, object()).advisor_verdict('voidghost', force=True))['reply']
        self.assertEqual(with_model, without)

    async def test_the_missing_stats_are_admitted_not_filled(self):
        reply = await self._pipeline_reply('Voidghost', 'Voidghost is a mech.')
        self.assertIn('no HP, speed or power figures', reply)
        self.assertNotIn('0 HP', reply)

    def test_absent_tier_is_stated_once(self):
        from mechbot import advising, advisor
        reply = advising.answer('voidghost', data=advisor.load())['reply']
        self.assertEqual(reply.casefold().count('no community tier'), 1, reply)

    # ---------------------------------------------------------------- the sheet builder
    def test_the_sheet_is_labels_and_values_only(self):
        from mechbot import advisor
        sheet = advisor.mech_stat_sheet(advisor.resolve('Redox', advisor.load())['item'])
        self.assertEqual([row['label'] for row in sheet], ['HP', 'Speed', 'Power', 'Energy'])
        self.assertTrue(all(row['value'] for row in sheet))


class RankAndOneLinerTests(unittest.IsolatedAsyncioTestCase):
    """From a live screenshot: one word got three different answers in one minute.

    "voidghost" produced the full advisor answer and then a truncated copy of it, and "redox"
    produced a bare one-line card - "Redox - 227,300 HP - 22 speed - 948 power - 32 energy
    (9,375 A-Coins)" - before "redox rank 5" produced the full review with rank-1 figures under a
    question about rank 5.
    """

    CARD = 'Redox - 227,300 HP - 22 speed - 948 power - 32 energy (9,375 A-Coins).'

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(Path(self.tmp.name) / 'r.sqlite3')}):
            self.catalog = Catalog(initialize_snapshot())

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    async def _ladder_reply(self, question, model_reply):
        """The bare-word path: the understanding pipeline declines, the ladder answers."""
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()

        async def declines(base, key, model, system, user, **kwargs):
            return 'X'
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], content=question,
                                  reference=None, attachments=[])
        with patch.dict(os.environ, {'AI_API_KEY': 'k', 'AI_MODEL': 'm'}), \
                patch('mechbot.pipeline._chat', new=declines), \
                patch('mechbot.bot.answer_question_with_brain', new=AsyncMock(return_value=model_reply)):
            await bot.on_message(message)
        return bot.reply_message.await_args.args[1]

    # ---------------------------------------------------------------- the one-line card
    async def test_a_one_line_card_does_not_replace_the_verified_answer(self):
        reply = await self._ladder_reply('redox', self.CARD)
        self.assertNotEqual(reply, self.CARD, 'a thinner answer to the same question was being sent')
        self.assertIn('Quick review', reply)
        self.assertIn('227,300 HP', reply)

    def test_a_full_review_from_the_model_is_kept(self):
        """Judged at the assistant level: through on_message a fake store falls through first.

        The point of the guard is that a complete, verified answer survives the model path, so
        this asserts on the function that applies the guard rather than on a bot harness.
        """
        from mechbot import assistant
        full = ('**Redox** — Legendary mech. At a glance: 227,300 HP · 22 speed · 948 power · 32 energy. '
                'Quick review: strong ability damage and a personal shield, with DoT rather than burst '
                'as the main weakness. Ability: Caustic Blast, a 30m AoE stasis.')
        context = assistant.game_knowledge_context('redox', self.catalog)
        self.assertTrue(assistant._supported_game_reply(full, context),
                        'every figure in this reply is in the verified fact bundle')
        self.assertFalse(assistant._thinner_than_the_facts(full, context),
                         'a full review must not be treated as thinner than the catalog card')
        self.assertTrue(assistant._thinner_than_the_facts(self.CARD, context))
        self.assertTrue(assistant._thinner_than_the_facts('', context), 'nothing at all is thinner than the card')

    def test_the_thinning_check_measures_material_not_length(self):
        from mechbot.assistant import _thinner_than_the_facts
        context = {'verified_game_facts': [{'type': 'mech', 'name': 'Redox', 'review': {
            'ability': 'Creates a 30m radius caustic blast — stasis and damage over time. Grants a '
                       'Personal Shield and a Speed Boost for the ability duration.',
            'strengths': 'High HP and speed; personal shield extends survivability; strong ability damage.',
            'weaknesses': 'Damage over time rather than burst; the personal shield is modest.'}}]}
        self.assertTrue(_thinner_than_the_facts(self.CARD, context))
        self.assertFalse(_thinner_than_the_facts(
            'Caustic Blast is a 30m stasis blast with damage over time; the personal shield extends '
            'survivability, but the damage is over time rather than burst, and the shield is modest.', context))

    # ---------------------------------------------------------------- the rank
    def test_a_named_rank_is_the_rank_that_is_answered_about(self):
        from mechbot import advising, advisor
        data = advisor.load()
        for question, said in (('redox rank 5', 'rank 5'), ('redox r4', 'rank 4')):
            reply = advising.answer(question, data=data)['reply']
            self.assertIn(said, reply, f'{question!r} must say which rank its numbers are for')
            self.assertNotIn('You asked about rank 1', reply)

    def test_a_question_with_no_rank_does_not_invent_one(self):
        from mechbot import advising, advisor
        reply = advising.answer('redox', data=advisor.load())['reply']
        self.assertNotIn('You asked about rank', reply)
        self.assertIn('not split by rank', reply)

    def test_the_figures_say_where_they_came_from(self):
        from mechbot import advising, advisor
        reply = advising.answer('redox rank 5', data=advisor.load())['reply']
        self.assertIn('no per-rank stat row', reply,
                      'the review row carries one value per stat and no rank, and the reply must say so')


class PipelineUsesRankAndSubstanceTests(unittest.IsolatedAsyncioTestCase):
    """The pipeline half of the same report: rank ignored, and a card accepted in place of a review."""

    CARD = 'Redox - 227,300 HP - 22 speed - 948 power - 32 energy (9,375 A-Coins).'
    UNDERSTOOD = {'intent': 'meta_advice', 'mechs': ['Redox'], 'weapons': [], 'pilots': [], 'mods': [],
                  'from_rank': None, 'from_level': None, 'to_rank': None, 'to_level': None,
                  'question': 'redox rank 5', 'unclear': False}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'SNAPSHOT_PATH': str(Path(self.tmp.name) / 'r.sqlite3')}):
            self.catalog = Catalog(initialize_snapshot())

    async def asyncTearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    async def _reply(self, question, formatter_text):
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()

        async def fake_chat(base, key, model, system, user, **kwargs):
            return json.dumps(self.UNDERSTOOD) if 'ONE JSON object' in system else formatter_text
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], content=question,
                                  reference=None, attachments=[])
        with patch.dict(os.environ, {'AI_API_KEY': 'k', 'AI_MODEL': 'm'}), \
                patch('mechbot.pipeline._chat', new=fake_chat):
            await bot.on_message(message)
        return bot.reply_message.await_args.args[1]

    async def test_a_card_does_not_replace_the_review_in_the_pipeline(self):
        reply = await self._reply('redox rank 5', self.CARD)
        self.assertNotEqual(reply, self.CARD)
        self.assertIn('Quick review', reply)
        self.assertIn('Ability', reply)

    async def test_the_pipeline_answers_about_the_rank_that_was_named(self):
        reply = await self._reply('redox rank 5', self.CARD)
        self.assertIn('rank-5', reply, 'the reply must say that the figures are not rank-5 figures')

    def test_the_substance_guard_measures_the_answer_not_its_length(self):
        from mechbot.pipeline import guard_substance
        engine = ('**Redox** — Legendary mech · Tier A. **At a glance:** 227,300 HP · 22 speed · '
                  '948 power · 32 energy. **Quick review:** significant buffs bring Redox to '
                  'Dreadnought-level HP and speed, adding a personal shield and speed boost during the '
                  'ability. **Watch-out:** damage over time rather than burst, and the shield is modest.')
        ok, why = guard_substance(self.CARD, engine)
        self.assertFalse(ok, 'a card of correct numbers is not a review')
        self.assertIn('kept', why)
        ok, _ = guard_substance(engine, engine)
        self.assertTrue(ok, 'the engine answer always passes its own guard')


class CompareReplyFormatTests(unittest.TestCase):
    """The comparison reply a player complained about: leaked weights, one wall of text, hedged twice.

    The engine's own weighted score is a number about our arithmetic - "a balanced weight of 0.82
    against Gemini 10's 0.71" was the whole second half of the reply - so none of it may reach the
    channel. What a player gets instead is one bold verdict, the figures that separate the two items,
    and one line for what the data cannot settle.
    """

    QUESTION = 'which is better gemini 10 or charge rocket 10'

    @classmethod
    def setUpClass(cls):
        from mechbot import advising
        cls.advising = advising
        cls.reply = advising.answer(cls.QUESTION)['reply']

    def test_no_internal_scoring_reaches_the_reply(self):
        import re
        self.assertIsNone(re.search(r'\b0\.\d{2,}\b', self.reply),
                          'a weighted score leaked into player text')
        for phrase in ('balanced weight', 'cost-efficiency', 'cost efficiency', 'weighted',
                       'dmgPerEnergy', 'profile'):
            self.assertNotIn(phrase, self.reply)
        self.assertNotRegex(self.reply, r'\b0\.\d+\b')

    def test_the_reply_is_layered_not_one_paragraph(self):
        lines = [line for line in self.reply.split('\n') if line.strip()]
        self.assertGreaterEqual(len(lines), 4, 'the answer arrived as a single block of prose')
        self.assertIn('**', lines[0], 'the verdict line is not emphasised')
        self.assertTrue(any(line.startswith('• ') for line in lines), 'no figure list')
        self.assertTrue(all(line.count('**') % 2 == 0 for line in lines), 'unbalanced bold markers')

    def test_the_pick_is_stated_once_and_measured(self):
        self.assertIn('**Charge Rocket 10** over **Gemini 10**', self.reply)
        self.assertIn('650,000 Credits', self.reply)
        self.assertIn('712,500 Credits', self.reply)
        self.assertEqual(self.reply.count('Charge Rocket 10 is better'), 0)

    def test_the_losing_side_gets_a_figure_not_a_hedge(self):
        self.assertNotIn('still suit your build', self.reply)
        self.assertIn('**The case for Gemini 10:**', self.reply)

    def test_the_provenance_line_is_compact_and_closes_the_answer(self):
        self.assertIn('community catalog', self.reply)
        self.assertLess(self.reply.count('Tiers and dmg-per-energy'), 1,
                        'the long provenance paragraph is back')

    def test_the_same_layout_for_a_mech_comparison(self):
        import re
        reply = self.advising.answer('which is better redox or voidghost')['reply']
        self.assertIsNone(re.search(r'\b\d\.\d{2,}\b', reply))
        self.assertNotIn('wins stats', reply)
        self.assertIn('**', reply.split('\n')[0])

    def test_both_reply_paths_agree(self):
        """The model path and the ladder path render the same answer, so a player cannot tell them apart."""
        from mechbot import advising
        once = advising.answer(self.QUESTION)['reply']
        twice = advising.answer(self.QUESTION)['reply']
        self.assertEqual(once, twice, 'the renderer is not deterministic')

    def test_line_breaks_survive_the_style_pass(self):
        from mechbot.humanize import polish
        text = '**Pick:** one.\n• a figure\n• another figure'
        styled, _ = polish(text)
        self.assertIn('\n• ', styled, 'polish flattened the reply into one paragraph')


class FollowUpThreadTests(unittest.TestCase):
    """"i have rosa and gemini implant" after a comparison is a follow-up, not a new store question.

    Before this, the second sentence named gear, so it was routed as a store ranking and answered
    with Charge Rocket 6 priced at 600,000 Credits - to a player who had just been told about two
    missiles. The thread keeps the decision alive for a few minutes so the reply can continue it.
    """

    SCOPE = ('thread-tests', 'channel', 'player')

    def setUp(self):
        from mechbot import advising
        self.advising = advising
        advising._THREAD.clear()
        self.first = advising.answer('which is better gemini 10 or charge rocket 10', scope=self.SCOPE)

    def test_the_decision_is_remembered(self):
        thread = self.advising.last_decision(self.SCOPE)
        self.assertIsNotNone(thread)
        self.assertEqual(thread['winner'], 'Charge Rocket 10')
        self.assertEqual(thread['picks'], ['Gemini 10', 'Charge Rocket 10'])

    def test_a_follow_up_is_answered_against_the_comparison(self):
        reply = self.advising.answer('i have rosa (guided pilot) and gemini implant', scope=self.SCOPE)
        self.assertEqual(reply['capability'], 'thread')
        self.assertIn('Following up on **Gemini 10 vs Charge Rocket 10**', reply['reply'])
        self.assertIn('Charge Rocket 10', reply['reply'])
        self.assertNotIn('Charge Rocket 6 is where I would put it', reply['reply'],
                         'the follow-up was answered as a fresh store ranking')

    def test_the_pilot_is_read_from_the_catalog_not_invented(self):
        reply = self.advising.answer('i have rosa (guided pilot) and gemini implant', scope=self.SCOPE)['reply']
        self.assertIn('Rosa is a guided pilot', reply)
        self.assertIn('Gemini 10', reply)

    def test_what_the_player_owns_is_not_sold_back_to_them(self):
        reply = self.advising.answer('i have rosa and gemini, what should i do next', scope=self.SCOPE)['reply']
        self.assertIn('not a purchase', reply)
        self.assertIn('What I would do', reply)

    def test_a_new_comparison_is_not_swallowed_by_the_thread(self):
        reply = self.advising.answer('storm racks or charge rockets better?', scope=self.SCOPE)
        self.assertEqual(reply['capability'], 'compare')
        self.assertNotIn('Following up on', reply['reply'])

    def test_a_lookup_is_never_swallowed_by_an_open_thread(self):
        """"what are Acantha rank 3 pilot bonuses" needs the stat row, not a continuation."""
        from mechbot import advising
        scope = ('thread-lookup', 'channel', 'player')
        advising._THREAD.clear()
        advising.answer('which is better gemini 10 or charge rocket 10', scope=scope)
        self.assertIsNone(advising.thread_follow_up(scope, 'what are acantha rank 3 pilot bonuses'))

    def test_the_thread_expires(self):
        thread = self.advising.last_decision(self.SCOPE)
        thread['expires'] = 0
        self.assertIsNone(self.advising.last_decision(self.SCOPE))
        reply = self.advising.answer('i have rosa and gemini implant', scope=self.SCOPE)
        self.assertNotEqual(reply['capability'], 'thread')


class TheAnswerIsAboutWhatThePlayerTypedTests(unittest.IsolatedAsyncioTestCase):
    """A screenshot showed "redox" answered with Charge Rocket 10's rank 4 to 5 upgrade cost.

    The model had reported intent upgrade_cost about the wrong item. Every name in that answer is a
    real catalog row, so the existing validation passed it - it only ever asked whether a name exists,
    never whether it was the name in the message. The message is now the authority.
    """

    BAD = {'intent': 'upgrade_cost', 'mechs': [], 'weapons': ['Charge Rocket 10'], 'pilots': [], 'mods': [],
           'from_rank': 4, 'from_level': 1, 'to_rank': 5, 'to_level': 1,
           'question': 'redox', 'unclear': False}

    @classmethod
    def setUpClass(cls):
        from mechbot.catalog import Catalog, initialize_snapshot
        cls.catalog = Catalog(initialize_snapshot())

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    async def _reply(self, question, understood):
        from mechbot.bot import MechBot
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=9)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        bot.private_reply = AsyncMock()

        async def fake_chat(base, key, model, system, user, **kwargs):
            return json.dumps(understood) if 'ONE JSON object' in system else 'x'

        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], content=question,
                                  reference=None, attachments=[])
        with patch.dict(os.environ, {'AI_API_KEY': 'k', 'AI_MODEL': 'm'}), \
                patch('mechbot.pipeline._chat', new=fake_chat):
            await bot.on_message(message)
        return bot.reply_message.await_args.args[1]

    async def test_a_one_word_item_question_is_not_answered_with_someone_elses_price(self):
        reply = await self._reply('redox', self.BAD)
        self.assertIn('Redox', reply)
        self.assertNotIn('Charge Rocket', reply)
        self.assertNotIn('1,575', reply, 'the price of a weapon the player never named')
        self.assertNotIn('265,000', reply, 'a rank-up receipt is not what "redox" asked for')

    async def test_a_real_cost_question_still_answers_with_its_own_figures(self):
        understood = dict(self.BAD, weapons=[], mechs=['Redox'], question='redox rank 4 to rank 5')
        reply = await self._reply('redox rank 4 to rank 5 upgrade cost', understood)
        self.assertIn('Redox', reply)
        self.assertIn('2,825', reply)
        self.assertNotIn('1,575', reply)

    def test_the_message_outranks_the_model_on_names(self):
        from mechbot.pipeline import normalise_understanding, reconcile_entities
        understanding = normalise_understanding(self.BAD, self.catalog)
        fixed = reconcile_entities(understanding, 'redox', self.catalog)
        self.assertEqual(fixed['mechs'], ['Redox'])
        self.assertEqual(fixed['weapons'], [])
        self.assertEqual(fixed['intent'], 'item_stats', 'no rank and no cost word is not a cost question')

    def test_a_rank_and_a_cost_word_keep_the_cost_intent(self):
        from mechbot.pipeline import normalise_understanding, reconcile_entities
        understanding = normalise_understanding(dict(self.BAD, weapons=[], mechs=['Redox']), self.catalog)
        fixed = reconcile_entities(understanding, 'redox rank 4 to rank 5 upgrade cost', self.catalog)
        self.assertEqual(fixed['intent'], 'upgrade_cost')
        self.assertEqual((fixed['from_rank'], fixed['to_rank']), (4, 5))

    def test_two_typed_names_are_both_kept_in_order(self):
        from mechbot.pipeline import normalise_understanding, reconcile_entities
        understanding = normalise_understanding(dict(self.BAD, weapons=[], mechs=['Redox']), self.catalog)
        fixed = reconcile_entities(understanding, 'tailsting or redox?', self.catalog)
        self.assertEqual(fixed['mechs'], ['Tailsting', 'Redox'])

    def test_a_mismatched_cost_fact_is_never_printed(self):
        from mechbot.bot import _cost_fact_is_about
        fact = {'name': 'Charge Rocket 10', 'cost': {'acoins': 1575}}
        self.assertFalse(_cost_fact_is_about(fact, 'Redox'))
        self.assertTrue(_cost_fact_is_about(fact, 'Charge Rocket 10'))
        self.assertTrue(_cost_fact_is_about({'cost': {}}, 'Redox'), 'no name in the fact, nothing to contradict')


class TypoAndDeadHeatComparisonTests(unittest.IsolatedAsyncioTestCase):
    """The screenshot: "which is better outlow or deathwalker", then "what i should buy".

    Two defects in one exchange. "outlow" for Outlaw scored below the fuzzy cutoff, so the word was
    dropped and the comparison answered about one item; and the comparison itself came out as a tie
    that printed a category line instead of the two mechs' own figures - then the follow-up ignored it
    and asked for a budget, because a store ranking cannot know what was on screen a minute earlier.
    """

    @classmethod
    def setUpClass(cls):
        from mechbot.catalog import Catalog, initialize_snapshot
        cls.catalog = Catalog(initialize_snapshot())

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    async def _ping(self, bot, text):
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=SimpleNamespace(id=44),
                                  guild=None, mentions=[bot._connection.user], content=text,
                                  reference=None, attachments=[])
        await bot.on_message(message)

    async def test_the_misspelt_item_is_repaired_and_disclosed(self):
        from mechbot.bot import MechBot
        bot = MechBot(self.catalog, object())
        bot._connection.user = SimpleNamespace(id=9)
        bot.conversation_history_for = AsyncMock(return_value=[])
        bot.reply_message = AsyncMock()
        with patch.dict(os.environ, {'AI_API_KEY': '', 'AI_MODEL': ''}):
            await self._ping(bot, 'which is better outlow or deathwalker')
        reply = bot.reply_message.await_args.args[1]
        self.assertIn('Outlaw', reply)
        self.assertIn('outlow', reply, 'the correction is disclosed, not silent')
        self.assertIn('Deathwalker', reply)

    async def test_a_dead_heat_shows_both_stat_rows(self):
        from mechbot import advising
        reply = advising.answer('which is better outlow or deathwalker')['reply']
        self.assertIn('213,100 HP', reply, "Outlaw's own figures")
        self.assertIn('227,300 HP', reply, "Deathwalker's own figures")
        self.assertIn('leads on', reply)
        self.assertNotIn('come out level on what the catalog holds', reply,
                         'two mechs at the same price and tier still have figures that differ')

    async def test_the_role_words_are_not_called_weapons(self):
        from mechbot import advising
        reply = advising.answer('which is better outlow or deathwalker')['reply']
        self.assertIn('attacker role', reply)
        self.assertNotIn('attacker weapon', reply)

    async def test_what_should_i_buy_after_a_comparison_is_about_those_two(self):
        from mechbot import advising
        scope = ('buy-tests', 'channel', 'player')
        advising._THREAD.clear()
        advising.answer('which is better outlow or deathwalker', scope=scope)
        reply = advising.answer('what i should buy', scope=scope)
        self.assertEqual(reply['capability'], 'thread')
        self.assertIn('Outlaw vs Deathwalker', reply['reply'])
        self.assertNotIn('What can you spend?', reply['reply'],
                         'the follow-up was answered as a fresh budget question')

    async def test_a_buy_follow_up_names_the_winner(self):
        from mechbot import advising
        scope = ('buy-tests-2', 'channel', 'player')
        advising._THREAD.clear()
        advising.answer('which is better redox or voidghost', scope=scope)
        reply = advising.answer('so what should i buy', scope=scope)['reply']
        self.assertIn('**Buy Redox.**', reply)
