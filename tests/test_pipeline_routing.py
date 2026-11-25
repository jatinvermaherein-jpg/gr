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
