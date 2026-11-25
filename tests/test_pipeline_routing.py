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
