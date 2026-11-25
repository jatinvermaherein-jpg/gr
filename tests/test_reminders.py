"""Tests for reminders: wording in, DM out.

The reminder path is the one place the bot promises to do something later, so these cover the whole
lifecycle against a real (temporary) database rather than only the parser: prose that players actually
type, the timezone policy that decides whether "8pm" means the player's 8pm, and the delivery loop that
has to send the DM and reschedule a recurring reminder. No network, no model.
"""
import asyncio
import datetime
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from mechbot.bot import MechBot
from mechbot.store import Store
from mechbot.timeparse import (clock_time, needs_wall_clock, next_daily, next_occurrence, next_weekday,
                               normalize_duration_spec, parse_natural_reminder, parse_spec)

# A fixed Wednesday afternoon in UTC, so clock-time expectations are absolute rather than "whenever".
NOW = int(datetime.datetime(2026, 11, 25, 14, 0, tzinfo=datetime.timezone.utc).timestamp())
IST = 'Asia/Kolkata'


def utc(stamp):
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)


class Phrasing(unittest.TestCase):
    def test_the_time_can_go_before_or_after_the_task(self):
        for text in ('remind me in 20 minutes to check the ECR',
                     'remind me to check the ECR in 20 minutes',
                     'can you remind me in 20 min to check the ECR, thanks'):
            with self.subTest(text=text):
                self.assertEqual(parse_natural_reminder(text), ('in 20 minutes', 'check the ECR'))

    def test_worded_amounts_become_the_same_spec_as_digits(self):
        cases = (('in an hour', 'in 1 hour', 3600), ('in half an hour', 'in 30 minutes', 1800),
                 ('in a couple of minutes', 'in 2 minutes', 120), ('in two days', 'in 2 days', 172800),
                 ('in 90 sec', 'in 90 seconds', 90))
        for text, spec, seconds in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_natural_reminder(f'remind me {text} to log builds')[0], spec)
                self.assertEqual(parse_spec(spec, 'UTC', now=NOW)[:3], (NOW + seconds, 'once', seconds))

    def test_clock_times_and_days_are_parsed_not_refused(self):
        cases = (('remind me at 8pm to check ECR', 'at 8pm', 'check ECR'),
                 ('remind me at 20:30 to queue the ECR', 'at 20:30', 'queue the ECR'),
                 ('remind me tonight at 9 to log builds', 'tonight at 9', 'log builds'),
                 ('remind me tomorrow to file the report', 'tomorrow', 'file the report'),
                 ('remind me on friday at 9am to draft notes', 'on friday at 9am', 'draft notes'),
                 ('remind me at noon to submit', 'at noon', 'submit'),
                 ('remind me 2026-12-01 09:00 to run the raid', '2026-12-01 09:00', 'run the raid'))
        for text, spec, content in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_natural_reminder(text), (spec, content))

    def test_recurring_shapes_survive_the_rewrite(self):
        self.assertEqual(parse_natural_reminder('remind me every 2 hours to hydrate'),
                         ('every 2 hours', 'hydrate'))
        self.assertEqual(parse_natural_reminder('remind me every friday at 9am to post the recap'),
                         ('every friday at 9am', 'post the recap'))
        self.assertEqual(parse_natural_reminder('remind me to log builds every day at 8pm'),
                         ('every day at 8pm', 'log builds'))

    def test_the_players_own_capitalisation_is_kept_in_the_note(self):
        parsed = parse_natural_reminder('remind me in an hour to check the ECR for Savant 10')
        self.assertEqual(parsed[1], 'check the ECR for Savant 10')

    def test_a_request_needs_both_a_time_and_a_task(self):
        for text in ('remind me in 20 minutes', 'remind me to write notes', 'what is the best mech?',
                     'remind me'):
            with self.subTest(text=text):
                self.assertIsNone(parse_natural_reminder(text))

    def test_questions_about_reminders_and_refusals_are_not_new_reminders(self):
        for text in ("don't remind me again", 'did you remind me yesterday', 'why did my reminder not fire',
                     'can you remind my friend to call me', 'remind me later', 'is my reminder still set?'):
            with self.subTest(text=text):
                self.assertIsNone(parse_natural_reminder(text))


class Scheduling(unittest.TestCase):
    def test_a_clock_time_picks_the_next_occurrence_in_the_players_zone(self):
        due, kind, interval, hour, minute = parse_spec('at 8pm', IST, now=NOW)
        self.assertEqual((kind, interval, hour, minute), ('once', None, None, None))
        # 8pm in Kolkata is 14:30 UTC that day; scheduled as UTC it would land at 1:30 in the night.
        self.assertEqual(utc(due).strftime('%Y-%m-%d %H:%M'), '2026-11-25 14:30')

    def test_a_clock_time_already_past_rolls_to_tomorrow(self):
        due, *_ = parse_spec('at 8am', IST, now=NOW)
        self.assertEqual(utc(due).strftime('%Y-%m-%d %H:%M'), '2026-11-26 02:30')

    def test_tonight_and_tomorrow_default_to_a_stated_hour(self):
        self.assertEqual(parse_spec('tonight', IST, now=NOW)[0], parse_spec('at 9pm', IST, now=NOW)[0])
        self.assertEqual(utc(parse_spec('tomorrow', IST, now=NOW)[0]).strftime('%Y-%m-%d %H:%M'),
                         '2026-11-26 03:30')

    def test_weekday_reminders_land_on_the_requested_day(self):
        due, kind, interval, *_ = parse_spec('friday at 9am', IST, now=NOW)
        self.assertEqual((kind, interval), ('once', None))
        self.assertEqual((utc(due).strftime('%a %Y-%m-%d %H:%M'), due > NOW), ('Fri 2026-11-27 03:30', True))

    def test_weekly_uses_a_seven_day_interval_the_scheduler_already_understands(self):
        due, kind, interval, hour, minute = parse_spec('every friday at 9am', IST, now=NOW)
        self.assertEqual((kind, interval, hour, minute), ('interval', 604800, 9, 0))
        following = next_occurrence(kind, due, interval, hour, minute, IST, due + 60)
        self.assertEqual(utc(following).strftime('%a %H:%M'), 'Fri 03:30')
        self.assertEqual(following - due, 604800)

    def test_an_absolutely_past_time_is_refused_not_scheduled(self):
        with self.assertRaises(ValueError):
            parse_spec('2020-01-01 09:00', IST, now=NOW)

    def test_short_recurrence_is_still_refused(self):
        with self.assertRaises(ValueError):
            parse_spec('every 20 sec', 'UTC', now=1000)

    def test_bare_digits_still_mean_a_delay(self):
        self.assertEqual(parse_spec('20s', 'UTC', now=1000), (1020, 'once', 20, None, None))

    def test_duration_words_normalise_once_and_stay_idempotent(self):
        once = normalize_duration_spec('in 20 sec')
        self.assertEqual(once, 'in 20 seconds')
        self.assertEqual(normalize_duration_spec(once), once)

    def test_next_daily_skips_a_day_when_the_clock_time_has_passed(self):
        self.assertEqual(utc(next_daily(6, 30, IST, NOW)).strftime('%Y-%m-%d %H:%M'), '2026-11-26 01:00')

    def test_next_weekday_never_returns_the_present(self):
        due = next_weekday(2, 14, 0, 'UTC', NOW)  # Wednesday, at the very minute that has just struck
        self.assertGreater(due, NOW)
        self.assertEqual(utc(due).strftime('%a %H:%M'), 'Wed 14:00')

    def test_clock_time_accepts_the_spellings_players_use(self):
        for text, expected in (('8pm', (20, 0)), ('08:30', (8, 30)), ('8:30pm', (20, 30)), ('8 pm', (20, 0))):
            with self.subTest(text=text):
                self.assertEqual(clock_time(text), expected)
        with self.assertRaises(ValueError):
            clock_time('25pm')


class TimezonePolicy(unittest.TestCase):
    def test_a_delay_needs_no_timezone_but_a_wall_clock_does(self):
        for spec in ('in 20 seconds', '20s', 'in an hour', 'every 2 hours'):
            with self.subTest(spec=spec):
                self.assertFalse(needs_wall_clock(spec))
        for spec in ('daily at 8pm', 'at 8pm', '8pm', 'tomorrow', 'every friday at 9am', '2026-12-01 09:00'):
            with self.subTest(spec=spec):
                self.assertTrue(needs_wall_clock(spec))


class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(sqlite_path=os.path.join(self.directory.name, 'reminders.sqlite3'))
        self.sent = []
        self.replies = []
        self.peer = SimpleNamespace(id=42,
                                     send=AsyncMock(side_effect=lambda text, **kwargs: self.sent.append(text)))

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    def build_bot(self):
        catalog = SimpleNamespace(weapon_names=[], mech_names=[], pilot_names=[], mod_names=[])
        bot = MechBot(catalog, self.store)
        bot_user = SimpleNamespace(id=998877665544332211)
        bot._connection.user = bot_user
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = lambda: False
        bot.get_user = lambda uid: self.peer

        async def capture(message, text, **kwargs):
            self.replies.append(text)
        bot.reply_message = capture
        return bot, bot_user

    async def ping(self, bot, bot_user, content):
        author = SimpleNamespace(id=42, bot=False,
                                 send=AsyncMock(side_effect=lambda text, **kwargs: self.replies.append(text)))
        message = SimpleNamespace(author=author, channel=object(), guild=None, mentions=[bot_user],
                                  content=f'<@{bot_user.id}> {content}', reference=None, reply=AsyncMock(),
                                  created_at=None)
        with patch('mechbot.bot.answer_question_with_brain', new=AsyncMock(return_value=None)) as brain:
            await bot.on_message(message)
        return brain

    async def test_a_pinged_reminder_is_stored_and_never_needs_a_model(self):
        bot, bot_user = self.build_bot()
        await self.store.set_timezone(42, IST)
        brain = await self.ping(bot, bot_user, 'remind me at 8pm to check the ECR')
        brain.assert_not_awaited()
        rows = await self.store.fetch('SELECT content, kind, zone, due_at FROM reminders')
        self.assertEqual([(row['content'], row['kind'], row['zone']) for row in rows],
                         [('check the ECR', 'once', IST)])
        self.assertEqual(utc(rows[0]['due_at']).strftime('%H:%M'), '14:30')

    async def test_a_clock_request_without_a_timezone_asks_instead_of_guessing(self):
        bot, bot_user = self.build_bot()
        await self.ping(bot, bot_user, 'remind me daily at 8pm to log builds')
        self.assertEqual((await self.store.fetch('SELECT COUNT(*) AS n FROM reminders'))[0]['n'], 0)
        self.assertIn('timezone', ' '.join(self.replies).lower())
        # A relative delay still works with no profile at all, and points at the same setting.
        await self.ping(bot, bot_user, 'remind me in 5 minutes to check the queue')
        rows = await self.store.fetch('SELECT content, zone FROM reminders')
        self.assertEqual([row['content'] for row in rows], ['check the queue'])
        self.assertIn('Set your timezone', ' '.join(self.replies))

    async def test_the_delivery_loop_sends_the_dm_and_completes_the_row(self):
        bot, _ = self.build_bot()
        await self.store.set_timezone(42, 'UTC')
        await bot.create_reminder(42, 'in 20 seconds', 'check the event')
        await self.store.execute('UPDATE reminders SET due_at=$1', int(time.time()) - 5)
        task = asyncio.create_task(bot.delivery_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        self.assertEqual(self.sent, ['⏰ Reminder: check the event'])
        self.assertEqual((await self.store.fetch('SELECT status FROM reminders'))[0]['status'], 'completed')

    async def test_a_recurring_reminder_advances_instead_of_being_consumed(self):
        bot, _ = self.build_bot()
        await self.store.set_timezone(42, 'UTC')
        await bot.create_reminder(42, 'every 2 hours', 'hydrate')
        patched = int(time.time()) - 5
        await self.store.execute('UPDATE reminders SET due_at=$1', patched)
        task = asyncio.create_task(bot.delivery_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        self.assertEqual(self.sent, ['⏰ Reminder: hydrate'])
        row = (await self.store.fetch('SELECT status, due_at FROM reminders'))[0]
        self.assertEqual(row['status'], 'pending')
        # The next slot is one interval after the missed one, so a restart never stacks up catch-up DMs.
        self.assertEqual(row['due_at'], patched + 7200)

    async def test_a_closed_dm_channel_marks_the_reminder_failed_with_a_reason(self):
        await self.store.set_timezone(42, 'UTC')
        await self.store.create_reminder(42, 'check the event', (int(time.time()) - 5, 'once', 20, None, None),
                                        'UTC')
        response = SimpleNamespace(status=403, reason='Forbidden',
                                   request=SimpleNamespace(method='POST', url='https://discord.com/x'))
        self.peer.send = AsyncMock(side_effect=discord.Forbidden(response, 'DMs are closed'))
        bot, _ = self.build_bot()
        task = asyncio.create_task(bot.delivery_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        row = (await self.store.fetch('SELECT status, last_error FROM reminders'))[0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('DMs', row['last_error'])

    async def test_the_reminder_cap_is_stated_as_a_limit(self):
        bot, _ = self.build_bot()
        await self.store.set_timezone(42, 'UTC')
        due = int(time.time()) + 600
        for index in range(25):
            await self.store.create_reminder(42, f'note {index}', (due + index, 'once', 600, None, None), 'UTC')
        with self.assertRaises(ValueError) as caught:
            await bot.create_reminder(42, 'in 5 minutes', 'one more')
        self.assertIn('25 active or failed reminders', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
