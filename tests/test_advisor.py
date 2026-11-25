"""Tests for the advisor: the deterministic engine, the voice layer, and the rules that bound them.

These are deliberately data-driven against the vendored pack (`data/advisor/`), so a change to the pack
shows up as a failed number here rather than as a wrong answer in the channel. Nothing in this file
needs a network, a database or a model: the advisor's whole point is that it works without one.
"""
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from mechbot import advisor, advising
from mechbot import facts as fact_layer
from mechbot.humanize import acceptable, audit, polish

DATA = Path(__file__).resolve().parents[1] / 'data' / 'advisor'

# A guide post in the shape the announcement channel actually uses, so the ECR verdict path is exercised
GUIDE_POST = """@everyone **WEEKLY ECR GUIDE**
## ORACLE 16 (R4)
The worst Tracker is here! Oracle is a very trash weapon Just skip and save your A-coins
## TAILSTING (R4)
The Spider Meta on the lineup! Tailsting is a Very Meta Mech Cheaper than in Gear Hub. Max 9,375 A-coins
"""


def guide_facts():
    entries = fact_layer.entries(GUIDE_POST)
    return [{'fact_id': 'test-ecr', 'category': 'ecr', 'status': 'current', 'posted_at': 1795320000,
             'message_id': '1', 'message_url': 'https://discord.com/channels/1/2/3',
             'title': 'Weekly ECR guide', 'details': {'entries': entries}}]


class PackIntegrity(unittest.TestCase):
    def test_manifest_records_every_vendored_file(self):
        manifest = json.loads((DATA / 'MANIFEST.json').read_text())
        self.assertEqual(manifest['pack_build_date'], '2026-11-23')
        self.assertGreaterEqual(len(manifest['files']), 13)
        for name, record in manifest['files'].items():
            with self.subTest(name=name):
                self.assertEqual(len(record['sha256']), 64)
                self.assertTrue((DATA / name).exists())

    def test_no_banned_source_is_referenced_anywhere_in_the_advisor(self):
        # the binding rules: cleanroom candidates are never fact, the private pack file is never read,
        # the other Supabase project and the UDP material never appear in shipped code
        banned = ('weapon_stat_candidates', '04_PRIVATE', 'efatnpyhlgfipcslzfab', 'SERVER_INTELLIGENCE',
                  'cleanroom')
        for module in ('advisor.py', 'advising.py', 'humanize.py'):
            source = (Path(__file__).resolve().parents[1] / 'mechbot' / module).read_text()
            for word in banned:
                with self.subTest(module=module, word=word):
                    self.assertNotIn(word, source)

    def test_staleness_is_measured_not_assumed(self):
        data = advisor.load()
        self.assertIsNotNone(data.staleness_days())

    def test_every_vendored_file_carries_a_trust_tier(self):
        data = advisor.load()
        manifest = json.loads((DATA / 'MANIFEST.json').read_text())
        for name in manifest['files']:
            with self.subTest(name=name):
                self.assertTrue(data.trust_for(name), f'{name} has no trust tier')
        self.assertEqual(data.trust_for('verified_stat_cells_3520.csv'), 'user-confirmed')
        self.assertEqual(data.trust_for('weapon_catalog_tiered.json'), 'community')


class Resolution(unittest.TestCase):
    def setUp(self):
        self.data = advisor.load()

    def test_variant_resolves_and_family_tier_is_named_as_such(self):
        found = advisor.resolve('Oracle 16', self.data)
        self.assertEqual(found['status'], 'ok')
        self.assertEqual(found['item']['tier'], 'A')
        self.assertTrue(any('family review' in note for note in found['notes']), found['notes'])

    def test_nonexistent_variant_is_refused_with_the_real_ones(self):
        found = advisor.resolve('Arc Torrent 8', self.data)
        self.assertEqual(found['status'], 'refused')
        self.assertIn('does not exist', found['notes'][0])
        self.assertEqual(found['candidates'], ['Arc Torrent 6', 'Arc Torrent 10', 'Arc Torrent 12'])

    def test_energy_conflict_is_reported_and_the_confirmed_value_preferred(self):
        found = advisor.resolve('Dreadshot 10', self.data)
        self.assertEqual(found['item']['energy'], 8.0)
        self.assertTrue(any('energy 8' in note and 'name says 10' in note for note in found['notes']),
                        found['notes'])

    def test_prose_mentions_survive_lowercase_and_typos(self):
        for text, expected in (('is the tailsting worth my a-coins?', ['Tailsting']),
                               ('what is the aegis like?', ['Aegis']),
                               ('should i spend on the railgun 12 or save', ['Railgun 12'])):
            with self.subTest(text=text):
                self.assertEqual(advisor._mention_names(text, self.data), expected)

    def test_a_sentence_is_never_guessed_into_an_item(self):
        for junk in ('aegis like', 'next one', 'the best gun'):
            with self.subTest(junk=junk):
                self.assertEqual(advisor.resolve(junk, self.data, strict=True)['status'], 'unknown')

    def test_two_near_equal_names_come_back_ambiguous(self):
        found = advisor.resolve('ember gune', self.data)
        self.assertEqual(found['status'], 'ambiguous')
        self.assertTrue(found['candidates'])


class Scoring(unittest.TestCase):
    def setUp(self):
        self.data = advisor.load()

    def test_ladder_totals_are_the_pack_numbers(self):
        # the ladder is keyed per rarity *and* per weapon energy, so each item gets its own column
        oracle = advisor.cost_from(advisor.resolve('Oracle 16', self.data)['item'], 1, self.data)
        self.assertEqual(oracle['class'], 'legendary_weapon_16')
        self.assertEqual((oracle['credits'], oracle['acoins'], oracle['blueprints']),
                         (2664575, 6700, 122))
        aegis = advisor.cost_from(advisor.resolve('Aegis', self.data)['item'], 1, self.data)
        self.assertEqual((aegis['credits'], aegis['acoins'], aegis['blueprints']), (1258500, 3760, 270))
        self.assertEqual(aegis['steps'], 42)
        # a rank-4 item has 18 rungs left, not 42
        partway = advisor.cost_from(advisor.resolve('Aegis', self.data)['item'], 4, self.data)
        self.assertEqual(partway['steps'], 24)

    def test_missing_parts_renormalise_instead_of_counting_as_zero(self):
        item = advisor.resolve('Savant 10', self.data)['item']
        scored = advisor.score(item, None, 'balanced', self.data)
        self.assertIsNone(scored['parts']['stats'])
        self.assertIn('stats', ''.join(scored['flags']))

    def test_role_changes_the_order(self):
        tank = advisor.score(advisor.resolve('Aegis', self.data)['item'], {'role': 'tank'}, 'balanced',
                             self.data)['parts']['role']
        neutral = advisor.score(advisor.resolve('Aegis', self.data)['item'], None, 'balanced',
                                self.data)['parts']['role']
        self.assertEqual(tank, 1.0)
        self.assertGreater(tank, neutral)
        # and a mismatched ask costs the same item points
        mismatched = advisor.score(advisor.resolve('Aegis', self.data)['item'], {'role': 'support'}, 'balanced',
                                   self.data)['parts']['role']
        self.assertLess(mismatched, tank)

    def test_verified_cells_are_used_and_labelled(self):
        item = advisor.resolve('Aegis', self.data)['item']
        stats, detail = advisor.stat_score(item, 1, self.data)
        self.assertEqual(detail['basis'], 'user-confirmed cells')
        self.assertTrue(any('HP 13500' in cell for cell in detail['cells']), detail['cells'])


class Capabilities(unittest.TestCase):
    def setUp(self):
        self.data = advisor.load()
        self.facts = guide_facts()

    def test_compare_wins_on_cost_and_quotes_the_loser_s_advantage(self):
        result = advisor.compare('Oracle 16', 'Savant 10', 'is oracle 16 better than savant 10?', self.facts,
                                 self.data)
        self.assertEqual(result['verdict'], 'B')
        self.assertIn('cost_efficiency', [row['label'] for row in result['decisive_factors']])
        self.assertIn('dmgPerEnergy', result['counter_argument'])
        self.assertIn('community (tier list)', result['tiers_used'])

    def test_tie_is_reported_as_a_tie(self):
        result = advisor.compare('Dreadshot 8', 'Dreadshot 10', 'dreadshot 8 or 10', self.facts, self.data)
        self.assertEqual(result['verdict'], 'TIE')
        self.assertEqual(result['picks'], ['Dreadshot 8', 'Dreadshot 10'])

    def test_invest_with_nothing_to_go_on_asks_two_questions(self):
        result = advisor.rank_investments('what should I buy?', self.facts, self.data)
        self.assertEqual(result['verdict'], 'ASK')
        self.assertEqual(len(result['followups']), 2)
        self.assertEqual(result['ranked'], [])

    def test_invest_ranks_scout_mechs_and_lists_a_do_not_invest_reason(self):
        result = advisor.rank_investments('i main scout, what mech should I invest in?', self.facts, self.data)
        self.assertTrue(result['ranked'])
        self.assertEqual(result['verdict'], 'Nomad')
        self.assertTrue(all(row['kind'] == 'mech' for row in result['ranked']), result['ranked'])
        self.assertTrue(result['do_not_invest'])
        self.assertTrue(all(row['reason'] for row in result['do_not_invest']))

    def test_budget_plan_never_overspends_any_currency(self):
        question = ('I have 4000 ac, 300k credits and 120 blueprints, I own Aegis and Oracle 6, '
                    'what should I invest in?')
        result = advisor.plan_budget(question, self.facts, self.data)
        self.assertTrue(result['plan'])
        for currency, amount in result['spent'].items():
            with self.subTest(currency=currency):
                self.assertLessEqual(amount, result['budget'].get(currency, 0))
        self.assertTrue(result['next_milestone'])
        self.assertTrue(result['do_not_buy'])

    def test_f2p_plan_stays_inside_the_ac_number_it_was_given(self):
        result = advisor.plan_budget('plan for a f2p player with 500 ac and 80k credits', self.facts, self.data)
        self.assertEqual(result['profile'], 'budget')
        self.assertLessEqual(result['spent']['acoins'], 500)

    def test_1v1_is_labelled_as_a_model_and_shows_its_work(self):
        result = advisor.simulate_1v1('Tailsting with Revoker 16', 'Aegis with Oracle 16', 'mid', self.data)
        self.assertEqual(result['confidence'], 'MEDIUM')
        self.assertEqual(len(result['sensitivity']), 3)
        self.assertIsInstance(result['win_pct'], int)
        self.assertTrue(result['modelled_estimate'])
        self.assertIn('user-confirmed (screenshot stat cells)', result['tiers_used'])
        self.assertTrue(any('modelled' in caveat for caveat in result['caveats']))

    def test_ecr_advice_carries_the_guide_s_own_verdict(self):
        result = advisor.ecr_advice(facts=self.facts, question='is the ECR deal worth it this week?',
                                    data=self.data)
        verdicts = {pick['resolved']: pick['advice'] for pick in result['picks']}
        self.assertEqual(verdicts['Oracle 16'], 'skip')
        self.assertEqual(verdicts['Tailsting'], 'worth_it')
        priced = {pick['resolved']: pick['ecr_value'].get('discount_pct') for pick in result['picks']}
        self.assertTrue(priced['Oracle 16'] > 90, priced)

    def test_explain_of_a_starter_mech_names_a_rival(self):
        result = advisor.explain('Puma', self.facts, self.data)
        self.assertIn('not worth maxing', result['verdict'])
        self.assertTrue(result['alternative']['name'])

    def test_sim_1v1_without_weapons_says_so_instead_of_inventing_ttk(self):
        result = advisor.simulate_1v1('Aegis', 'Puma', 'mid', self.data)
        self.assertIsNone(result['ttk'])
        self.assertIn('no weapons to model', result['decisive_factor'])


class Voice(unittest.TestCase):
    def setUp(self):
        self.facts = guide_facts()

    def test_answers_carry_no_unsourced_numbers(self):
        questions = ('is oracle 16 better than savant 10?', 'should I buy Revoker 16 or Ember Gun?',
                     'I have 4000 ac, 300k credits and 120 blueprints, what should I buy?',
                     'who wins 1v1: Tailsting with Revoker 16 vs Aegis with Oracle 16?',
                     'is the ECR deal worth it this week?', 'is Oracle 16 good?', 'what should I buy?')
        for question in questions:
            result = advising.answer(question, facts=self.facts)
            allowed = set(result['envelope'].get('numbers') or [])
            with self.subTest(question=question):
                self.assertEqual(result['guard'], 'pass')
                for token in re.findall(r'\d[\d,]*(?:\.\d+)?%?', result['reply']):
                    plain = token.replace(',', '').rstrip('.').rstrip('%')
                    self.assertTrue(token in allowed or plain in allowed or f'{plain}%' in allowed,
                                    f'{token} is not in the envelope for: {question}')

    def test_the_guard_refuses_a_number_the_engine_never_produced(self):
        reply, verdict = advising._guard('It costs 9,999 A-coins and gains 47%.', {'numbers': {'1'}})
        self.assertEqual(verdict, 'blocked')
        self.assertNotIn('9,999', reply)
        self.assertNotIn('47', reply)

    def test_replies_fit_discord(self):
        for question in ('i main scout, what mech should I invest in?',
                         'Tailsting + Hornet 16 vs Parasite + Charge Rocket 16, long range',
                         'should I max my Puma?'):
            with self.subTest(question=question):
                self.assertLessEqual(len(advising.answer(question, facts=self.facts)['reply']), advising.MAX_ANSWER)

    def test_routing_is_prose_only(self):
        for text, capability in (('is oracle 16 better than savant 10?', 'compare'),
                                 ('should I pull this ECR?', 'ecr_advice'),
                                 ('I have 500 ac, what do I buy?', 'plan_budget'),
                                 ('who wins: Tailsting with Revoker 16 or Aegis with Oracle 16?',
                                  'simulate_1v1'),
                                 ('is it worth it?', 'clarify_item')):
            with self.subTest(text=text):
                self.assertEqual(advising.route(text), capability)

    def test_the_gate_does_not_intercept_every_mention(self):
        for text, wanted in (('should I buy the Savant 10 or wait?', True), ('hello there', False),
                             ('what is the weather like', False), ('@bot is the tailsting worth my ac', True)):
            with self.subTest(text=text):
                self.assertIs(advising.wants_advisor(text), wanted)

    def test_a_refused_variant_is_answered_before_any_comparison(self):
        result = advising.answer('Arc Torrent 8 or Arc Torrent 10?', facts=self.facts)
        self.assertIn('Arc Torrent 8 is not in the catalog', result['reply'])
        self.assertIn('it comes in 6, 10, 12', result['reply'])

    def test_the_guide_quote_travels_verbatim(self):
        result = advising.answer('is the ECR deal worth it this week?', facts=self.facts)
        self.assertIn('"The worst Tracker is here!', result['reply'])

    def test_works_with_no_model_configured(self):
        # the mention path is a plain function call: no session, no key, no await
        result = advising.answer('what is the railgun 12 like?', facts=self.facts)
        self.assertIn('Railgun 12', result['reply'])
        self.assertIn('tier', result['reply'])


class Humanizing(unittest.TestCase):
    def test_stock_ai_phrasing_is_removed(self):
        text = ('Delving into the meta, it leverages seamless damage. Great question! Let me know if that '
                'helps. Overall, this answer compares the items.')
        styled, tells = polish(text)
        self.assertNotIn('Great question', styled)
        self.assertNotIn('Let me know', styled)
        self.assertNotIn('Delving', styled)
        self.assertNotIn('!', styled)
        self.assertIn('this answer compares the items', styled)
        self.assertNotIn('leveraging', styled.lower())
        self.assertNotIn('Overall,', styled)

    def test_one_informal_reply_is_left_alone(self):
        text = 'Tailsting is the pick, full stop. It costs 9,375 A-coins and wins the range war.'
        styled, tells = polish(text)
        self.assertEqual(styled, text.strip())
        self.assertEqual(tells, [])

    def test_polish_never_moves_a_number(self):
        samples = ('Overall, it is 1,299 damage per energy at rank 1 - which is 90% of the tier.',
                   'Great question! The Tailsting costs 9,375 A-coins. Let me know if that helps.',
                   'It is worth noting that the ladder is 2,655,575 Credits, 8,175 A-coins and 123 blueprints.',
                   'Delving into it: the crate is 350 A-coins, seamless value at 95.8% off the shelf price.')
        for original in samples:
            styled, _tells = polish(original)
            with self.subTest(original=original[:40]):
                self.assertEqual(sorted(re.findall(r'\d[\d,.]*%?', styled)),
                                 sorted(re.findall(r'\d[\d,.]*%?', original)))

    def test_quoted_announcement_text_is_never_paraphrased(self):
        original = 'The guide says: "Oracle is a very trash weapon Just skip and save your A-coins" - and means it.'
        styled, _tells = polish(original)
        self.assertIn('"Oracle is a very trash weapon Just skip and save your A-coins"', styled)


class MentionPath(unittest.IsolatedAsyncioTestCase):
    """The requirement the whole build exists for: a player pings the bot and types prose."""

    async def test_a_ping_about_a_buy_is_answered_by_the_engine_with_no_ai(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from mechbot.bot import MechBot

        class Catalog:
            weapon_names = []
            mech_names = []
            pilot_names = []
            mod_names = []

        with patch.dict(os.environ, {'MESSAGE_CONTENT_ENABLED': 'true', 'ANNOUNCEMENT_INGESTION_ENABLED': 'false',
                                     'AI_API_KEY': '', 'AI_MODEL': ''}):
            bot = MechBot(Catalog(), object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        bot.conversation_turn_reply = AsyncMock()
        author = SimpleNamespace(id=123456789012345678, bot=False)
        message = SimpleNamespace(author=author, channel=SimpleNamespace(id=444), guild=SimpleNamespace(id=999),
                                  mentions=[bot.user],
                                  content=f'<@{bot.user.id}> is Oracle 16 better than Savant 10?', reference=None)
        with patch('mechbot.bot.answer_question_with_brain', new=AsyncMock()) as brain:
            await bot.on_message(message)
        brain.assert_not_awaited()
        bot.conversation_turn_reply.assert_awaited_once()
        reply = bot.conversation_turn_reply.await_args.args[2]
        self.assertIn('Savant 10', reply)
        # The tier is named against the community list, and the same provenance closes the answer
        # in one compact line instead of the long form under every comparison.
        self.assertIn('tier A in the community list', reply)
        self.assertIn('community catalog', reply)
        self.assertLessEqual(len(reply), advising.MAX_ANSWER)


class CommandPath(unittest.IsolatedAsyncioTestCase):
    """The mention path is the primary one; the slash command is the same engine behind a second door."""

    def test_advise_is_registered(self):
        import discord
        from mechbot.bot import HELP, MechBot, install_commands
        bot = MechBot(object(), object())
        install_commands(bot)
        names = {command.name for command in bot.tree.get_commands()}
        self.assertIn('advise', names)
        self.assertIn('is Oracle 16 worth 8275 ac', HELP)

    async def test_the_command_shares_the_pending_thread_and_refuses_stray_chat(self):
        # One engine, one memory of what it still needs, whichever door the player used.
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from mechbot.bot import MechBot, install_commands
        bot = MechBot(object(), object())
        install_commands(bot)
        command = bot.tree.get_command('advise')
        scope = ('g-cmd', 'c-cmd', 'u-cmd')
        advising.forget(scope)
        self.addCleanup(advising.forget, scope)

        async def ask(text):
            interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                         followup=SimpleNamespace(send=AsyncMock()), guild_id=scope[0],
                                         channel=SimpleNamespace(id=scope[1]),
                                         user=SimpleNamespace(id=scope[2]))
            await command.callback(interaction, text)
            return interaction.followup.send.await_args.args[0]

        first = await ask('what should i invest in?')
        self.assertIn('What can you spend', first)
        second = await ask('1500 ac')
        self.assertIn('Spend it in this order', second)
        self.assertIn('Reading that', second)
        stray = await ask('who are you')
        self.assertIn('Nothing in that sentence', stray)
        # An answer deferred through the command still respects the two-question cap.
        record = advising.pending_question(scope)
        self.assertTrue(record is None or record['asks'] <= advising.FOLLOWUP_LIMIT)

    def test_the_gate_rejects_a_plain_price_lookup(self):
        # the catalog answers upgrade costs from the user's real rank; the advisor must not steal that
        self.assertFalse(advising.wants_advisor('How much will Redox cost to upgrade from rank 4 to 5?'))
        self.assertFalse(advising.wants_advisor('How much does it cost to upgrade Redox from rank 4 to 5?'))
        self.assertTrue(advising.wants_advisor('is Redox worth upgrading to rank 5?'))



class GearNamedInThePlural(unittest.TestCase):
    """A player writes "storm racks", the catalog says "Storm Rack", and neither side is wrong.

    Every case here is a sentence the bot was shown failing in a channel: the plural hid the item from
    the matcher, the gate then refused to consult the advisor, and the player was asked for a screenshot
    of something the bot already had data for.
    """

    def setUp(self):
        self.data = advisor.load()

    def test_plural_names_resolve_to_the_singular_catalog_entry(self):
        for text, expected in (('storm racks or charge rockets', ['Storm Rack', 'Charge Rocket']),
                               ('storm racks 10 or charge rockets 10', ['Storm Rack 10', 'Charge Rocket 10']),
                               ('should I max my Pumas?', ['Puma']),
                               ('is the tailstings worth it', ['Tailsting'])):
            with self.subTest(text=text):
                self.assertEqual(advisor.gear_mentions(text, self.data), expected)

    def test_plurals_that_are_not_names_stay_unmatched(self):
        # Folding "lands" to "land" would be a guess, not a match: nothing may resolve unless the game
        # actually calls something by that name.
        for text in ('land wings boxes crashes', 'what is the best build for me', 'ranks and levels'):
            with self.subTest(text=text):
                self.assertEqual(advisor._mention_names(text, self.data), [])

    def test_a_plural_choice_reaches_the_advisor_and_answers_itself(self):
        for text in ('on what should i invest ? storm racks or charge rockets',
                     'storm racks 10  or charge rockets 10'):
            with self.subTest(text=text):
                self.assertTrue(advising.wants_advisor(text))
                result = advising.answer(text, facts=guide_facts())
                self.assertEqual(result['capability'], 'compare')
                self.assertEqual(result['guard'], 'pass')
                self.assertIn('Charge Rocket', result['reply'])
                self.assertIn('Storm Rack', result['reply'])

    def test_names_keep_their_capitalisation_in_prose(self):
        # The renderer used to upper-case a sentence by lower-casing the rest of it.
        result = advising.answer('storm racks or charge rockets', facts=guide_facts())
        self.assertNotIn('Charge rocket', result['reply'])
        self.assertNotIn('Storm rack', result['reply'])

    def test_a_bare_item_name_is_a_question_not_a_dead_end(self):
        self.assertTrue(advising.wants_advisor('voidghost'))
        result = advising.answer('voidghost', facts=guide_facts())
        self.assertEqual(result['capability'], 'explain')
        self.assertIn('Voidghost', result['reply'])
        self.assertIn('energy 24', result['reply'])
        self.assertIn('No community tier', result['reply'])

    def test_a_cost_to_max_question_is_answered_from_the_ladder(self):
        for text in ('what does it cost to max Charge Rocket 10?', 'how much to max Redox'):
            with self.subTest(text=text):
                self.assertTrue(advising.wants_advisor(text))
                reply = advising.answer(text, facts=guide_facts())['reply']
                self.assertIn('ladder from rank', reply)
                self.assertRegex(reply, r'\d[\d,]* Credits')

    def test_the_ladder_starts_where_the_player_says_they_are(self):
        plain = advising.answer('how much to max Redox', facts=guide_facts())['envelope']
        sitting = advising.answer('how much to max Redox, i am rank 4', facts=guide_facts())['envelope']
        self.assertNotEqual(plain.get('cost_to_max', {}).get('from_rank'),
                            sitting.get('cost_to_max', {}).get('from_rank'))
        self.assertEqual(sitting['cost_to_max']['from_rank'], 4)

    def test_a_rank_named_as_a_goal_is_not_read_as_a_starting_point(self):
        # "worth upgrading to rank 5" prices rank 1 to max; "i am rank 4" is where they actually are.
        for text, expected in (('is Redox worth upgrading to rank 5?', 1),
                               ('how much to max Redox, i am rank 4', 4),
                               ('i already have it at rank 5', 5),
                               ('should i take this to rank 6?', 1),
                               ('aegis rank 3 or puma rank 3', 1)):
            with self.subTest(text=text):
                self.assertEqual(advisor._ctx(text)['rank'], expected)


class FollowUpContext(unittest.TestCase):
    """When the engine asks something, the four-word answer has to be understood as an answer.

    The advisor is allowed two questions. That allowance is only worth anything if "500 ac" is read
    against the question it replies to - and only if it is *not* read that way when the player has
    moved on to something else.
    """

    SCOPE = ('guild-1', 'channel-1', 'user-1')

    def setUp(self):
        advising.forget(self.SCOPE)
        self.addCleanup(advising.forget, self.SCOPE)

    def ask(self, question):
        result = advising.answer(question, facts=guide_facts())
        advising.remember_question(self.SCOPE, question, result)
        return result

    def test_the_engine_still_asks_and_says_what_it_needs(self):
        result = self.ask('what should i invest in?')
        record = advising.pending_question(self.SCOPE)
        self.assertTrue(record)
        self.assertEqual(record['question'], 'what should i invest in?')
        self.assertIn('budget', record['slots'])
        self.assertTrue(any('spend' in row.lower() for row in record['followups']))

    def test_a_budget_reply_is_answered_together_with_the_question_it_answers(self):
        self.ask('what should i invest in?')
        merged = advising.carried_question(self.SCOPE, '1500 ac')
        self.assertEqual(merged, 'what should i invest in? 1500 ac')
        result = advising.answer(merged, facts=guide_facts())
        self.assertEqual(result['capability'], 'plan_budget')
        self.assertEqual(result['guard'], 'pass')
        # A plan needs a budget: the sentence only has one because the first question was carried in.
        self.assertIn('Spend it in this order', result['reply'])

    def test_an_unrelated_short_message_is_not_swallowed(self):
        self.ask('what should i invest in?')
        for reply in ('hello there', 'good morning everyone', 'nice'):
            with self.subTest(reply=reply):
                self.assertIsNone(advising.carried_question(self.SCOPE, reply))

    def test_a_reply_naming_a_different_item_is_a_new_question(self):
        self.ask('what should i invest in?')
        self.assertIsNone(advising.carried_question(self.SCOPE, 'Oracle 16 or Savant 10?'))

    def test_a_long_message_is_a_new_question_not_an_answer(self):
        self.ask('what should i invest in?')
        ramble = ('what about the skins instead, I have no money at all and my clan is arguing about '
                  'whether the store is worth it this week')
        self.assertIsNone(advising.carried_question(self.SCOPE, ramble))

    def test_an_answered_question_stops_being_pending(self):
        self.ask('what should i invest in?')
        merged = advising.carried_question(self.SCOPE, '1500 ac')
        result = advising.answer(merged, facts=guide_facts())
        advising.remember_question(self.SCOPE, merged, result)
        self.assertIsNone(advising.pending_question(self.SCOPE))

    def test_two_questions_is_the_limit(self):
        question = 'what should i invest in?'
        result = self.ask(question)
        for _round in range(advising.FOLLOWUP_LIMIT + 2):
            merged = advising.carried_question(self.SCOPE, 'support') or 'support'
            result = advising.answer(f'{question} {merged}', facts=guide_facts())
            advising.remember_question(self.SCOPE, merged, result)
        record = advising.pending_question(self.SCOPE)
        self.assertTrue(record is None or record['asks'] <= advising.FOLLOWUP_LIMIT)

    def test_the_thread_expires(self):
        self.ask('what should i invest in?')
        record = advising.pending_question(self.SCOPE)
        record['expires'] = 0
        self.assertIsNone(advising.pending_question(self.SCOPE))
        self.assertIsNone(advising.carried_question(self.SCOPE, '1500 ac'))

    def test_an_answer_with_no_subject_does_not_claim_one(self):
        # "what should I invest in?" ranks the store rather than one item, so the acknowledgement must
        # not invent a subject for the carried reply.
        self.ask('what should i invest in?')
        merged = advising.carried_question(self.SCOPE, '1500 ac')
        result = advising.answer(merged, facts=guide_facts())
        self.assertIsInstance(advising.advisor_subject(result), str)


class FollowUpOnMention(unittest.IsolatedAsyncioTestCase):
    """The carry has to work on the @-mention path, which is the only path players use."""

    async def test_the_ping_path_carries_the_thread_itself(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from mechbot.bot import MechBot

        class Catalog:
            weapon_names = []
            mech_names = []
            pilot_names = []
            mod_names = []

        with patch.dict(os.environ, {'MESSAGE_CONTENT_ENABLED': 'true', 'ANNOUNCEMENT_INGESTION_ENABLED': 'false',
                                     'AI_API_KEY': '', 'AI_MODEL': ''}):
            bot = MechBot(Catalog(), object())
        bot._connection.user = SimpleNamespace(id=987654321098765432)
        replies = []
        bot.conversation_turn_reply = AsyncMock(side_effect=lambda message, user_text, text, **kwargs:
                                                replies.append(text))
        author = SimpleNamespace(id=123456789012345678, bot=False)
        scope = ('1', '1', '1')
        advising.forget(scope)

        async def ping(content):
            message = SimpleNamespace(author=author, channel=SimpleNamespace(id=1), guild=SimpleNamespace(id=1),
                                      mentions=[bot.user], content=f'<@{bot.user.id}> {content}', reference=None)
            with patch('mechbot.bot.answer_question_with_brain', new=AsyncMock()) as brain:
                await bot.on_message(message)
            return brain

        brain = await ping('what should i invest in?')
        brain.assert_not_awaited()
        self.assertIn('What can you spend', replies[-1])
        # A player who answers with a number gets a plan, not a fresh question.
        await ping('1500 ac')
        self.assertIn('Spend it in this order', replies[-1])
        self.assertNotIn('What can you spend', replies[-1])


class EmptyCatalogIsNotAnAnswer(unittest.IsolatedAsyncioTestCase):
    """The bot must not tell a player it knows nothing about gear the pack does cover."""

    @classmethod
    def setUpClass(cls):
        from mechbot.catalog import Catalog, initialize_snapshot
        cls.catalog = Catalog(initialize_snapshot())

    def test_the_real_catalog_still_shrugs_at_the_voidghost(self):
        from mechbot.assistant import mech_response
        profile = self.catalog.mech_profile('Voidghost')
        self.assertIn('I don’t have confirmed detailed stats', mech_response(profile))

    async def test_the_advisor_answers_where_the_catalog_shrugs(self):
        from mechbot.bot import MechBot
        bot = MechBot(self.catalog, SimpleNamespace(supports_announcement_facts=False))
        verdict = await bot.advisor_reply('voidghost')
        self.assertTrue(verdict)
        self.assertIn('Voidghost', verdict)
        self.assertNotIn('screenshot', verdict.lower())




if __name__ == '__main__':
    unittest.main()


class WhatReachesTheAdvisor(unittest.TestCase):
    """A spend question with no item in it is still the advisor's, an earning question is not."""

    def test_bare_spend_questions_are_routed(self):
        for text in ('what should i invest in?', 'which should i buy with my credits',
                     'what do i buy first', 'where do i put my credits', 'should i save for the hub'):
            with self.subTest(text=text):
                self.assertTrue(advising.wants_advisor(text))
                result = advising.answer(text, facts=guide_facts())
                self.assertIn(result['capability'], ('rank_investments', 'plan_budget'))
                self.assertTrue(result['reply'].strip())

    def test_how_to_earn_a_currency_is_left_to_the_other_answers(self):
        for text in ('how do I get free a-coins fast', 'how do I make credits fast',
                     'how can i earn more a-coins', 'how do I grind blueprints',
                     'i need more credits how do i get them'):
            with self.subTest(text=text):
                self.assertFalse(advising.wants_advisor(text))

    def test_quality_wording_needs_an_item_to_score(self):
        self.assertTrue(advising.wants_advisor('is the voidghost good'))
        self.assertFalse(advising.wants_advisor('what is a good mech'))   # no item to rank by role
        self.assertFalse(advising.wants_advisor('hello there'))
        self.assertFalse(advising.wants_advisor('what is the weather like'))

    def test_an_unrankable_spend_shaped_sentence_falls_through(self):
        # The gate is deliberately generous; `route` is what decides, and a sentence it cannot rank is
        # refused here so the caller's other paths still get the message.
        self.assertFalse(advising.wants_advisor('how do I invite you to my clan'))
        result = advising.answer('tell me a joke about mechs')
        self.assertEqual(result['capability'], 'unknown')
