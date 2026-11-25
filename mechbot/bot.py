"""Discord bot entrypoint. Requires DISCORD_TOKEN and persistent PostgreSQL in production."""
import asyncio
import ast
import base64
import hashlib
import logging
import operator
import os
import re
import time
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from .catalog import Catalog, initialize_snapshot
from .store import Store
from .timeparse import parse_spec, parse_natural_reminder, next_occurrence, needs_wall_clock, tzinfo
from .pipeline import (understand, format_reply, correct_names, engine_answer, normalise_understanding,
                       guard_figures, fuzzy_mentions, FUZZY_CUTOFF)
from .advising import (wants_advisor, answer as advisor_answer, advisor_subject, carried_question,
                        remember_question)
from .assistant import GAME_WORDS, find_mentions, message_chunks, direct_item_lookup, bot_identity_response, IDENTITY_PROFILE_IDS, wants_invite_link, should_answer_unmentioned, parse_natural_calculation, natural_builds_request, parse_build_sp_range, read_screenshot, screenshot_images, game_knowledge_context, weapon_response, mech_response, pilot_response, mod_response, fetch_builds, builds_response, builds_response_parts, answer_question_with_brain, upgrade_cost_response, safe_text, discord_text, naturalize_discord_reply
from .announcements import DEFAULT_ANNOUNCEMENT_CHANNELS, ANNOUNCEMENT_SYNC_SECONDS, INCREMENTAL_LIMIT, EDIT_REFRESH_LIMIT, message_text, wants_upcoming, format_upcoming, wants_ecr_info, format_ecr_events, wants_promo_codes
from .workers import WorkerStore, get_worker, assignment_text, parse_worker_time, parse_worker_snooze, WORKERS, GUILD_ID, CHANNEL_ID, ROLE_NAME, REFERENCE_URL
from .payouts import PayoutTracker, fetch_snapshot, RECIPIENT, BASE, fetch_totals
from .alerts import AlertManager, OPERATORS, validate as validate_alert
from .forum_archive import (FORUM_SYNC_RETRY_SECONDS, format_forum_request, forum_sync_seconds,
                            refresh_forum_archive, wants_forum_lookup)
from .source_summaries import combined_source_summary, wants_patch_bug_lookup
from .voice import EmojiBook, humanise_cost, spell_it_out, assume_note, missing_fact
from .facts import (ai_extract_facts, build_facts, facts_prompt_bundle, merge_ai_facts, format_ecr_reply, format_fact_list, search_facts, wants_older_ecr, refresh_ecr_status)
from .ecr_digest import ecr_answer as ecr_digest_answer, promo_digest
from . import ecr_summary as ecr_render
from .ecr_digest import resolve_item as ecr_digest_resolve
from . import compound

log=logging.getLogger('mechbot')
REPLY_MENTIONS=discord.AllowedMentions(everyone=False,users=False,roles=False,replied_user=True)
AMBIENT_CHAT_COOLDOWN_SECONDS=8
BUILD_SP_PROMPT_TTL_SECONDS=600

def env_enabled(name,default=False):
    value=os.getenv(name)
    if value is None:return default
    return value.strip().casefold() in ('1','true','yes','on')

DISABLED_ANNOUNCEMENT_CHANNELS=frozenset((1512089401193271539,))


def configured_announcement_channels():
    raw=os.getenv('ANNOUNCEMENT_CHANNEL_IDS','').replace(';',',')
    values=[]
    for item in raw.split(','):
        if not item.strip():continue
        try:channel_id=int(item.strip())
        except ValueError:
            log.warning('Ignoring an invalid announcement channel ID; no value or secret was logged.')
            continue
        if channel_id>0 and channel_id not in DISABLED_ANNOUNCEMENT_CHANNELS and channel_id not in values:values.append(channel_id)
    return tuple(values) or DEFAULT_ANNOUNCEMENT_CHANNELS

ANNOUNCEMENT_CHANNELS=configured_announcement_channels()
# Curated facts distilled from every saved announcement: the dump writes them, the answers read them.
ANNOUNCEMENT_FACT_SOURCE_LIMIT=2000
# Safety cap on one history walk; a channel longer than this still gets every message over successive passes.
ANNOUNCEMENT_BACKFILL_TOTAL_LIMIT=5000
ANNOUNCEMENT_FACT_REPLY_LIMIT=200
ANNOUNCEMENT_AI_BATCH_SIZE=4
ANNOUNCEMENT_AI_MAX_BATCHES=30
ANNOUNCEMENT_AI_TIMEOUT_SECONDS=45
ANNOUNCEMENT_FACT_STATE_KEY='announcement_fact_dump'
# Fine-grained permissions: View Channels, Send Messages, Embed Links, Read History,
# Manage Roles, Use Slash Commands, and Send Messages in Threads. No Administrator.
INVITE_URL='https://discord.com/oauth2/authorize?client_id=1570061533898211408&permissions=277293911040&integration_type=0&scope=bot%20applications.commands'
BOT_ROOT=Path(__file__).resolve().parent.parent
CURRENCY_ASSETS=(('A-Coins','emoji_a_coins.png'),('Credits','emoji_credits.png'),('Blueprints (BP)','emoji_blueprints.png'))
# One canonical asset set. Both uploaders (voice.EmojiBook and the application-emoji loop
# below) read these filenames, and the names are the ones voice.EMOJI_SET uses, so the
# understanding pipeline and the /upgrade_cost path resolve to the same three images.
EMOJI_FILES={'acoins':'emoji_a_coins.png','credits':'emoji_credits.png','blueprints':'emoji_blueprints.png'}
# Names are pinned exactly as they exist in the live application, so an existing upload is
# reused rather than duplicated on the next boot.
APPLICATION_CURRENCY_EMOJIS=(('acoins','archangle_a_coins_v2','emoji_a_coins.png'),
                             ('credits','archangle_credits_v2','emoji_credits.png'),
                             ('blueprints','archangle_blueprints_v2','emoji_blueprints.png'))
HELP='''**Mech Arena helper**
Use `/help` anytime to see this command list.
• `/invite` — get the link to add me to a server.
• `/weapon name rank level` — a concise weapon review, key stats, perk and listed purchase price.
• `/upgrade_cost item from_rank to_rank [from_level to_level] [blueprints_owned]` — calculate mech, weapon or pilot upgrade costs; Blueprint purchases are optional.
• `/mech name [rank]` — a short mech review with user-confirmed rank stats and listed purchase price.
• `/pilot name [rank]` — a verified pilot listing and exact stat progression by rank.
• `/mod name [from_level to_level]` — mod purchase and level-up costs.
• `/builds [mech] [sp_range]` — browse recent builds or filter all mech builds by your SP range.
• `/upcoming` — summarize what the curated announcement facts and saved forum posts state; event dates stay distinct from post dates.
• `/forum [query]` — search and summarize saved Plarium Forum discussions; not a live event schedule.
• `/ecr [previous]` — the latest ECR from the curated announcement facts (what the post actually stated) plus the post’s own buy/skip advice per featured item; pass `previous: true` to also list the earlier rotations on record. ECR info is separate from promo codes.
• `/advise <question>` — the gear advisor: whether a buy or crate is worth it, what to spend a stated budget on, or who wins a 1v1 on paper. It reads the vendored community catalog plus the announcement guide’s own buy/skip line, states the trust tier of every figure, and invents nothing; it works the same with no AI model configured. **You do not need the command:** the same answers come from asking in chat, e.g. `@bot is Oracle 16 worth 8275 ac`, `@bot I have 4000 ac and 300k credits, what should I buy`, `@bot should I pull this ECR`. Singular or plural names both work (`@bot storm racks or charge rockets`), so does a bare item name (`@bot voidghost`), and if it asks you for a budget or your role, just answer with “1500 ac” or “i play close range” in the next message.
• `/facts_status` — how many curated announcement facts are stored and when the last dump ran.
• `/promo_codes` — summarize explicitly labeled codes found in the curated announcement facts and saved forum posts; validity is not guaranteed.
• `/recent` — read recent posts in a channel you can access.
• `/timezone` — set your IANA timezone; default is UTC.
• `/remind`, `/reminders`, `/cancel` — private DM reminders. Recurring, daily, weekly and clock-time reminders all work, in plain prose too: `@bot remind me at 8pm to check the ECR`.
• `/note` — add, list or delete private notes.
• `/worker_time`, `/worker_status`, `/worker_snooze`, `/worker_pause` — manage assigned database-task reminders.
• `/worker_setup` — set up the worker role and send the initial time request.
• `/stop_loop` — stop your own active ping loops.
• `/calc` — calculate simple arithmetic; you can also ask “what’s 240 plus 60?” or “calculate 240 / 60”.
• Ask naturally for “latest ECR event” or “latest promo code”—these are separate lookups—plus upgrade costs, builds, event dates, or reminders such as “remind me in 20 minutes to check the event”. A reminder needs a when and a what, in any wording and either order: “remind me at 8pm to check the ECR”, “remind me to file the report tomorrow”, “remind me every friday at 9am to post a recap”. Delivered privately by DM; a clock time uses your `/timezone`, so set it once.
• Channel questions — when the server has opted into natural chat, ask normally without a bot mention. Only apparent questions/lookups and explicit, fully specified reminder requests get a reply; ambient replies do not ping the author and have a short per-user cooldown.
You can also mention me: `@bot remind me in 20 seconds to check the event`, `@bot review EM Rifle 10`, `@bot how much to upgrade Redox from rank 4 to 5?`, or `@bot how do I invite you?`. Reviews and upgrade costs use confirmed game data; missing details are never guessed.'''


def help_text(user_id=None,guild_id=None):
    if user_id in OPERATORS and guild_id is not None:
        restricted=('• `/ping_loop member interval_seconds duration_seconds reason` — send an immediate, single-user ping, then bounded follow-up pings.\n'
                    '• `/dm_loop member interval_seconds duration_seconds reason` — start a bounded DM loop.\n'
                    'A member may have one channel loop and one DM loop; five loops can run in total.\n'
                    '• `/stop_user_loops member` — stop that member’s channel and DM loops.\n')
        return HELP.replace('• `/stop_loop`',restricted+'• `/stop_loop`')
    return HELP


def calculate(expr):
    if len(expr)>100:raise ValueError('Expression is too long.')
    ops={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Mod:operator.mod,ast.USub:operator.neg,ast.UAdd:operator.pos}
    tree=ast.parse(expr,mode='eval')
    if sum(1 for _ in ast.walk(tree))>40:raise ValueError('Expression is too complex.')
    def visit(n):
        if isinstance(n,ast.Expression):return visit(n.body)
        if isinstance(n,ast.Constant) and type(n.value) in (int,float):v=n.value
        elif isinstance(n,ast.BinOp) and type(n.op) in ops:v=ops[type(n.op)](visit(n.left),visit(n.right))
        elif isinstance(n,ast.UnaryOp) and type(n.op) in ops:v=ops[type(n.op)](visit(n.operand))
        else:raise ValueError('Only +, -, *, /, %, and parentheses are supported.')
        if abs(v)>1e15:raise ValueError('Result is too large.')
        return v
    try:return visit(tree)
    except (ZeroDivisionError,OverflowError):raise ValueError('Cannot calculate that expression.')


# A question about one item names it; a question about the schedule does not. Without this,
# "current ecr" fuzzy-matched a mech name and the whole answer became that one old rotation.
_ONE_ITEM_WORDS = re.compile(r'\b(?:when|last|history|appearances?|times|ever|was|did)\b', re.I)
_SCHEDULE_WORDS = re.compile(r'\b(?:current|now|running|next|upcoming|latest|today|schedule)\b', re.I)


def _wants_one_item(query):
    text = str(query or '')
    if _SCHEDULE_WORDS.search(text):
        return False
    return bool(_ONE_ITEM_WORDS.search(text))


class MechBot(commands.Bot):
    def __init__(self, catalog, store):
        # Ambient channel questions and announcement ingestion both require the privileged
        # Message Content intent; ambient replies remain opt-in and are separately gated.
        intents=discord.Intents.default()
        self.announcement_ingestion_enabled=env_enabled('ANNOUNCEMENT_INGESTION_ENABLED',True)
        self.forum_sync_enabled=env_enabled('FORUM_SYNC_ENABLED',True)
        self.ambient_channel_chat_enabled=env_enabled('MESSAGE_CONTENT_ENABLED',False)
        self.message_content_enabled=self.ambient_channel_chat_enabled or self.announcement_ingestion_enabled
        intents.message_content=self.message_content_enabled
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.catalog=catalog;self.store=store;self.workers=WorkerStore(store);self.payouts=PayoutTracker(store);self.alerts=AlertManager();self._last_payout_note=None
        self.runner=None;self.worker_runner=None;self.payout_runner=None;self.announcement_runner=None;self.conversation_runner=None
        self.forum_archive_runner=None;self.emoji_runner=None;self.command_sync_task=None
        self.worker_setup_lock=asyncio.Lock();self.announcement_access={}
        self.ai_session=None;self.currency_emojis={};self.ambient_last_reply={};self.pending_build_requests={}
        # Presentation only: Mech Arena flavoured emoji + a human voice. Plain text fallbacks
        # are used whenever the upload is unavailable, so replies never depend on it.
        self.emoji_book=EmojiBook(application_id=self.application_id)
        # The understanding pipeline is opt-in per deployment, and off unless a model is
        # configured: two extra calls per question are a cost, not a default.
        self.pipeline_enabled=env_enabled('AI_PIPELINE_ENABLED',True)
        # Fuzzy catalog repair: a typo like "lacwing" must not delete the subject of a
        # sentence. Configurable because a very permissive cutoff can misfire.
        self.fuzzy_names_enabled=env_enabled('FUZZY_NAMES_ENABLED',True)
        try:self.fuzzy_cutoff=float(os.getenv('FUZZY_NAME_CUTOFF',FUZZY_CUTOFF))
        except (TypeError,ValueError):self.fuzzy_cutoff=FUZZY_CUTOFF


    async def setup_hook(self):
        guild=os.getenv('TEST_GUILD_ID','').strip()
        if guild and not guild.isdigit():
            log.error('TEST_GUILD_ID is not a valid Discord snowflake; commands will sync to accessible servers and globally.')
            guild=''
        if os.getenv('AI_API_KEY','').strip() and os.getenv('AI_MODEL','').strip():
            self.ai_session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10,connect=3,sock_read=8))
        self.command_sync_task=asyncio.create_task(self.command_sync_loop(guild))
        self.runner=asyncio.create_task(self.delivery_loop())
        self.worker_runner=asyncio.create_task(self.worker_delivery_loop())
        self.payout_runner=asyncio.create_task(self.payout_loop())
        if self.announcement_ingestion_enabled:
            if getattr(self.store,'supports_announcements',True):
                self.announcement_runner=asyncio.create_task(self.announcement_ingestion_loop())
            else:
                log.warning('Announcement ingestion is enabled, but its private table is not installed; channel reads are paused until migration.')
        if getattr(self.store,'supports_conversations',False):
            self.conversation_runner=asyncio.create_task(self.conversation_cleanup_loop())
        else:
            log.warning('Short-lived conversation memory is disabled until its private Supabase table is installed.')
        if self.forum_sync_enabled:
            self.forum_archive_runner=asyncio.create_task(self.forum_archive_sync_loop())
        else:
            log.info('Automated Plarium Forum scraping is disabled by FORUM_SYNC_ENABLED.')
        self.emoji_runner=asyncio.create_task(self.application_emoji_setup_loop())

    async def sync_guild_commands(self,guild_id):
        target=discord.Object(id=int(guild_id))
        self.tree.clear_commands(guild=target)
        self.tree.copy_global_to(guild=target)
        await self.tree.sync(guild=target)

    async def command_sync_loop(self,guild_id):
        await self.wait_until_ready()
        target_id=int(guild_id) if guild_id else None
        missing_logged=False
        global_synced=False
        synced_guilds=set()
        while not self.is_closed():
            try:
                if not global_synced:
                    await self.tree.sync()
                    global_synced=True
                    log.info('Published global slash commands; per-server copies are synced for faster availability.')
                for guild in list(self.guilds):
                    if guild.id in synced_guilds:continue
                    await self.sync_guild_commands(guild.id)
                    synced_guilds.add(guild.id)
                    log.info('Synced slash commands to accessible server %s.',guild.id)
                if target_id and self.get_guild(target_id) is None:
                    if not missing_logged:
                        log.warning('Configured worker server %s is not accessible yet; worker commands need that server, while other accessible servers have been synced. Retrying.',target_id)
                        missing_logged=True
                    await asyncio.sleep(30)
                    continue
                return
            except discord.HTTPException as e:
                log.warning('Slash-command sync failed (%s); retrying in 60 seconds.',type(e).__name__)
                await asyncio.sleep(60)

    async def on_guild_join(self,guild):
        try:
            await self.sync_guild_commands(guild.id)
            log.info('Synced slash commands to newly accessible server %s.',guild.id)
        except discord.HTTPException as e:
            log.warning('Slash-command sync for newly accessible server failed (%s); restart or retry after permissions are fixed.',type(e).__name__)

    async def application_emoji_setup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                emojis=await self.ensure_application_currency_emojis()
                self.currency_emojis=emojis if len(emojis)==len(APPLICATION_CURRENCY_EMOJIS) else {}
                # The wider Mech Arena emoji set (mech, weapon, pilot, rank-up, GG). Best effort:
                # an empty result only means plain glyphs are used in its place.
                await self.emoji_book.ensure()
                if self.currency_emojis:
                    return
                log.warning('The complete application currency emoji set is not available; PNG attachments will be used while retrying.')
            except Exception as exc:
                self.currency_emojis={}
                log.warning('Application currency emojis were unavailable (%s); PNG attachments will be used while retrying.',type(exc).__name__)
            await asyncio.sleep(3600)

    async def ensure_application_currency_emojis(self):
        token=os.getenv('DISCORD_TOKEN','').strip()
        app_id=self.application_id
        if not token or not app_id:return {}
        base=f'https://discord.com/api/v10/applications/{int(app_id)}/emojis'
        headers={'Authorization':'Bot '+token}
        timeout=aiohttp.ClientTimeout(total=20,connect=4,sock_read=12)
        result={}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(base,headers=headers,allow_redirects=False) as response:
                if response.status!=200:
                    log.warning('Could not list application emojis (HTTP %s); currency image attachments will be used.',response.status)
                    return {}
                body=await response.json(content_type=None)
            existing={str(item.get('name')):item for item in body.get('items',[]) if item.get('name') and item.get('id')}
            for key,name,filename in APPLICATION_CURRENCY_EMOJIS:
                emoji=existing.get(name)
                if emoji is None:
                    path=BOT_ROOT/'data'/filename
                    if not path.is_file():continue
                    image_bytes=path.read_bytes()
                    if len(image_bytes)>256*1024:
                        log.warning('Currency emoji asset %s exceeds Discord’s 256 KiB limit.',filename)
                        continue
                    payload={'name':name,'image':'data:image/png;base64,'+base64.b64encode(image_bytes).decode('ascii')}
                    async with session.post(base,headers=headers,json=payload,allow_redirects=False) as response:
                        if response.status not in (200,201):
                            log.warning('Could not create application currency emoji %s (HTTP %s); image attachments will be used.',name,response.status)
                            continue
                        emoji=await response.json(content_type=None)
                if emoji.get('id'):
                    result[key]=f"<:{name}:{emoji['id']}>"
        if len(result)==len(APPLICATION_CURRENCY_EMOJIS):
            log.info('Application currency emojis are ready for cost replies.')
        return result

    async def conversation_cleanup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:await self.store.delete_expired_conversation_turns()
            except Exception as exc:log.warning('Expired conversation-memory cleanup failed (%s); it will retry.',type(exc).__name__)
            await asyncio.sleep(60)

    async def forum_archive_sync_loop(self):
        """Refresh the last-150 Plarium Forum snapshot without blocking Discord handling."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                count=await refresh_forum_archive()
                log.info('Refreshed %s recent Plarium Forum threads.',count)
                delay=forum_sync_seconds()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Forum scrape failed (%s); the last-good snapshot remains available and will retry.',type(exc).__name__)
                delay=FORUM_SYNC_RETRY_SECONDS
            await asyncio.sleep(delay)

    async def close(self):
        await self.alerts.close()
        tasks=(self.command_sync_task,self.runner,self.worker_runner,self.payout_runner,self.announcement_runner,
               self.conversation_runner,self.forum_archive_runner,self.emoji_runner)
        for task in tasks:
            if task:task.cancel()
        for task in tasks:
            if task:
                try:await task
                except asyncio.CancelledError:pass
        if self.ai_session and not self.ai_session.closed:await self.ai_session.close()
        await self.store.close();self.catalog.close();await super().close()

    def _set_announcement_access(self,channel_id,state):
        previous=self.announcement_access.get(channel_id)
        self.announcement_access[channel_id]=state
        if state=='available' and previous!='available':
            log.info('Announcement source %s is readable; ingestion is active.',channel_id)
        elif state!='available' and previous!=state:
            log.warning('Announcement source %s is not readable (%s); it will be retried.',channel_id,state)

    async def ingest_announcement_channel(self,channel_id):
        if not self.announcement_ingestion_enabled:
            self._set_announcement_access(channel_id,'announcement_ingestion_disabled')
            return 0
        if not self.intents.message_content:
            self._set_announcement_access(channel_id,'message_content_intent_disabled')
            return 0
        if not getattr(self.store,'supports_announcements',True):
            self._set_announcement_access(channel_id,'private_table_not_installed')
            return 0
        stage='channel_lookup'
        try:
            channel=self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not hasattr(channel,'history') or not getattr(channel,'guild',None):
                self._set_announcement_access(channel_id,'not_a_server_message_channel')
                return 0
            member=getattr(channel.guild,'me',None)
            if member and hasattr(channel,'permissions_for'):
                permissions=channel.permissions_for(member)
                if not permissions.view_channel:
                    self._set_announcement_access(channel_id,'view_channel_missing')
                    return 0
                if not permissions.read_message_history:
                    self._set_announcement_access(channel_id,'read_message_history_missing')
                    return 0
            stage='storage_cursor'
            cursor=await self.store.announcement_cursor(channel_id)
            messages={}
            stage='history_read'
            if cursor:
                # Page forward from the stored cursor until a page runs short, so a burst of new
                # announcements is never truncated to one page and nothing in between is skipped.
                after_id=cursor
                for _page in range(ANNOUNCEMENT_BACKFILL_TOTAL_LIMIT//max(1,INCREMENTAL_LIMIT)):
                    page=0;newest=after_id
                    async for message in channel.history(limit=INCREMENTAL_LIMIT,after=discord.Object(id=after_id),
                                                          oldest_first=True):
                        page+=1;message_id=int(message.id);messages[message_id]=message;newest=max(newest,message_id)
                    if page<INCREMENTAL_LIMIT or newest==after_id:break
                    after_id=newest
            else:
                # First read of a channel walks it oldest-first to the end: no announcement is skipped.
                async for message in channel.history(limit=ANNOUNCEMENT_BACKFILL_TOTAL_LIMIT,oldest_first=True):
                    messages[int(message.id)]=message
            # Re-read a small tail each pass so edited announcements refresh without a full scan.
            async for message in channel.history(limit=EDIT_REFRESH_LIMIT,oldest_first=False):
                messages[int(message.id)]=message
            now=int(time.time())
            records=[]
            for message in messages.values():
                content=message_text(message)
                if not content:continue
                created=getattr(message,'created_at',None)
                if not created:continue
                edited=getattr(message,'edited_at',None)
                records.append({
                    'channel_id':str(channel.id),'message_id':str(message.id),
                    'guild_id':str(channel.guild.id),'guild_name':safe_text(channel.guild.name,100),
                    'channel_name':safe_text(channel.name,100),'content':content,
                    'message_url':str(getattr(message,'jump_url',''))[:500],
                    'posted_at':int(created.timestamp()),
                    'edited_at':int(edited.timestamp()) if edited else None,'fetched_at':now,
                })
            stage='storage_upsert'
            await self.store.upsert_announcements(records)
            self._set_announcement_access(channel_id,'available')
            return len(records)
        except discord.Forbidden:
            self._set_announcement_access(channel_id,'permission_denied_'+stage)
        except discord.NotFound:
            self._set_announcement_access(channel_id,'not_found_'+stage)
        except discord.HTTPException as exc:
            self._set_announcement_access(channel_id,
                f'discord_http_{stage}_{type(exc).__name__}_{getattr(exc,"status","unknown")}')
        except Exception as exc:
            self._set_announcement_access(channel_id,f'ingestion_error_{stage}_{type(exc).__name__}')
            log.exception('Announcement ingestion failed during %s for channel %s.',stage,channel_id)
        return 0

    def fact_extraction_enabled(self):
        """The AI pass is on whenever a model is configured, and can be turned off by env."""
        return (bool(os.getenv('AI_API_KEY','').strip()) and bool(os.getenv('AI_MODEL','').strip())
                and env_enabled('ANNOUNCEMENT_AI_EXTRACTION',True))

    async def _announcement_source_rows(self):
        """Every saved announcement in an allowed channel; the dump reads all of them, never a sample."""
        rows,_=await self._saved_announcement_rows(0,ANNOUNCEMENT_FACT_SOURCE_LIMIT,'fact')
        return rows

    def _catalog_left_a_gap(self, profile):
        """True when a catalog lookup came back with nothing on an item the advisor pack models.

        "I don't have confirmed detailed stats for this mech yet" is the worst reply to a player who named
        something we do hold tier, energy, price and ladder data for, so a shrug goes to the advisor before
        it is ever sent. A wrong-but-confident catalog answer is left alone; only an empty one is replaced.
        """
        if not isinstance(profile, dict):
            return False
        if not (profile.get('review') or profile.get('verified_stats')):
            return True
        return len(profile.get('matches') or []) != 1

    async def screenshot_answer(self, text, message, images, base=None):
        """Read a screenshot a member pinged in with, and say what it shows.

        Screenshots are how this bot is actually used: people ping it and drop an image,
        often with no words at all. Before this, the text-only ladder answered those by
        pattern-matching the names it could find - a Deathwalker stat sheet came back as a
        mech review, and an image with no text produced no reply at all.

        Returns the reply, or None when no vision-capable model answered. When the model is
        configured but refuses the image, the reply says so rather than guessing, because a
        confident wrong answer about a screenshot is worse than an honest one.
        """
        if not images:
            return None
        key = os.getenv('AI_API_KEY', '')
        model = os.getenv('AI_MODEL', '')
        if not key or not model:
            return ('I can only read screenshots when a vision model is configured, and none is. '
                    'Tell me in words what the screenshot shows and I will answer that.')
        base = base or os.getenv('AI_BASE_URL', 'https://api.openai.com/v1')
        try:
            reply, ok = await read_screenshot(base, key, model, text, images,
                                              catalog=self.catalog, session=self.ai_session)
        except Exception as exc:
            log.warning('Could not read a pinged screenshot (%s: %s).', type(exc).__name__, exc)
            return ('I could not read that screenshot. If it is a stat sheet, tell me the mech and '
                    'rank; if it is an offer, tell me the item and price; I will answer from there.')
        if ok and reply:
            return reply
        return ('I could not make sense of that screenshot. Tell me what you are looking at - '
                'the mech and rank, or the item and its price - and I will answer.')

    async def pipeline_answer(self, text, message, scope=None):
        """Understand the question with a model, answer it with the real engines, format the result.

        This is the "read, then answer, then lay it out" path. The model does no game
        maths: step 1 only classifies and restates the question, step 2 is the same
        deterministic code that already answers `/upgrade_cost` and the catalog
        lookups, and step 3 may only reword and lay out what step 2 produced —
        `guard_figures` rejects a formatted reply that introduced a number.

        Returns the reply text, or None to let the existing ladder handle the message.
        """
        if not self.pipeline_enabled:
            return None
        base=os.getenv('AI_BASE_URL','https://api.openai.com/v1')
        key=os.getenv('AI_API_KEY',''); model=os.getenv('AI_MODEL','')
        if not key or not model:
            return None

        # Past turns are read only once the message has got this far, so a message the
        # deterministic handlers already dealt with costs nothing.
        history=await self.conversation_history_for(message)
        understood=await understand(base,key,model,text,history=history,catalog=self.catalog,session=self.ai_session)
        if not understood:
            return None
        # Only intents whose answer is arithmetic on verified data go through here.
        # Events, promo codes, forums, reminders and chit-chat keep their own handlers:
        # they have side effects and a model must not own them.
        if understood['intent'] not in ('upgrade_cost','item_stats','compare','meta_advice','builds'):
            return None
        if understood['unclear'] and not (understood['mechs'] or understood['weapons'] or understood['pilots']):
            return None

        emoji=self.currency_emojis
        def cost_answer(u):
            """Deterministic cost for the understood transition, or None.

            Runs the same `game_knowledge_context` parser the free-text path uses,
            from a question rebuilt out of the understood entity and ranks, so a
            question phrased any way at all lands on the same arithmetic.
            """
            names=(u['mechs']+u['weapons']+u['pilots'])[:1]
            if not names:
                return None
            ranks=''
            if u.get('from_rank') and u.get('to_rank'):
                ranks=f" rank {u['from_rank']}"
                if u.get('from_level'):ranks+=f" level {u['from_level']}"
                ranks+=f" to rank {u['to_rank']}"
                if u.get('to_level'):ranks+=f" level {u['to_level']}"
            rebuilt=f"upgrade cost for {names[0]}{ranks}"
            ctx=game_knowledge_context(rebuilt,self.catalog)
            if not ctx.get('upgrade_cost_question'):
                return None
            mod_cost=next((f for f in ctx.get('verified_game_facts',[]) if f.get('type')=='mod_upgrade_cost'),None)
            if mod_cost:
                mod_item=next((f for f in ctx.get('verified_game_facts',[])
                               if f.get('type')=='mod' and str(f.get('rarity','')).casefold()==str(mod_cost.get('rarity','')).casefold()),None)
                if mod_item:
                    return mod_response({'matches':[mod_item],'upgrade_cost':mod_cost})
            cost_fact=next((f for f in ctx.get('verified_game_facts',[]) if f.get('type')=='upgrade_cost'),None)
            if cost_fact and cost_fact.get('cost') and any(int(v or 0) for v in cost_fact['cost'].values()):
                # Same verified figures, said the way a person would say them.
                # Prefer the original currency emoji already uploaded for this app; fall
                # back to the new set, then to plain glyphs.
                em={**self.emoji_book.currency_map(), **{k:v for k,v in (self.currency_emojis or {}).items() if v}}
                transition=(f"rank {cost_fact['from_rank']} L{cost_fact['from_level']} → "
                            f"rank {cost_fact['to_rank']} L{cost_fact['to_level']}")
                return humanise_cost(cost_fact.get('name') or names[0],transition,cost_fact['cost'],em,
                                     no_op=bool(cost_fact.get('no_op')))
            if cost_fact:
                # The row exists but this rarity was never filled in. Say that, and give
                # the nearest transition the table does hold, rather than quoting zero.
                name=cost_fact.get('name') or names[0]
                filled=[]
                for start in (3,4,5):
                    probe=self.catalog.upgrade_cost(name,start,start+1)
                    if probe and probe.get('cost') and any(int(v or 0) for v in probe['cost'].values()):
                        filled.append((start,probe['cost']));break
                hint=''
                if filled:
                    rank,totals=filled[0]
                    pieces=[f"{int(v):,} {k}" for k,v in totals.items() if int(v or 0)]
                    hint=f" The table does hold rank {rank} to {rank+1}: "+', '.join(pieces)+'.'
                transition=(f"rank {u['from_rank']} → {u['to_rank']}" if u.get('from_rank') and u.get('to_rank')
                            else 'that transition')
                return missing_fact(name,transition,nearest=hint.strip() or None)
            return None

        # engine_answer hands these the catalog name it already validated, one at a time.
        def weapon_answer(name):
            if not name:return None
            return weapon_response(self.catalog.weapon(name),None,None)

        def mech_answer(name):
            if not name:return None
            profile=self.catalog.mech_profile(name)
            return mech_response(profile) if profile else None

        def advisor_text(question):
            # engine_answer passes the plain question string, already normalised.
            try:
                verdict=advisor_answer(question or text,[])
            except Exception:
                log.exception('Advisor comparison failed inside the understanding pipeline')
                return None
            return (verdict or {}).get('reply')

        # `engine_answer` is synchronous, so the one async engine (the public builds API)
        # is resolved here first and handed over as a ready string.
        builds_text=''
        if understood['intent']=='builds' and understood['mechs']:
            mech=understood['mechs'][0]
            try:
                payload=await fetch_builds(os.getenv('PORTICO_BASE_URL',BASE),mech)
                builds_text=builds_response(payload,mech=mech,best=True) or ''
            except Exception:
                log.exception('Builds lookup failed inside the understanding pipeline')
                return None
        engine=engine_answer(understood,self.catalog,cost_answer,weapon_answer,mech_answer,
                             advisor_answer=advisor_text,
                             builds_answer=(lambda u: builds_text) if builds_text else None)
        if not engine:
            return None

        # The catalog admitted it holds no stat row for a mech the player named. The advisor has
        # the price ladder and says the gap out loud, and the ladder path already falls back to it
        # here - so this path must too, or the same one-word message gets a shrug when a model is
        # configured and a real answer when it is not.
        if engine.get('source') in ('mech', 'catalog') and understood['mechs']:
            profile = self.catalog.mech_profile(understood['mechs'][0])
            if self._catalog_left_a_gap(profile):
                fallback = await self.advisor_reply(text, scope=scope, force=True)
                if fallback:
                    return fallback

        # Step 3: format. Falls back to the engine's own text on any failure.
        # A verified cost is arithmetic, and `humanise_cost` already writes it as the sentence
        # a person would say. Sending it through the formatter only ever added the chance of a
        # receipt, so for this one intent the engine's sentence is the reply.
        formatted=(None if understood['intent']=='upgrade_cost'
                   else await format_reply(base,key,model,understood,engine['text'],history=history,session=self.ai_session))
        # The formatter is a model, and a model re-lays the answer out however it likes: this
        # path used to send its output straight to Discord, so a cost question came back as a
        # fenced block of label/value rows. The same pass the other AI replies get applies here,
        # so a code block, a pipe table or a lone heading cannot reach the channel.
        reply=naturalize_discord_reply(formatted) if formatted else engine['text']
        return discord_text(reply)

    async def advisor_verdict(self, text, scope=None, force=False):
        """The advisor's whole answer for one sentence - prose plus the envelope - or None.

        Spend advice is arithmetic on the vendored catalog plus the guide post's own buy-or-skip line, so
        it answers in the channel with no model configured and never invents a number. `force` skips the
        wording gate: used when the catalog has already admitted it has no data on a named item. With a
        scope, an answer that still needs something from the player stays pending for one short reply.
        """
        carried = carried_question(scope, text) if scope else None
        question = carried or text
        if not force and not carried and not wants_advisor(question):
            return None
        facts = await self._announcement_fact_rows(('ecr','promo_code','event','balance_change','gear_hub',
                                                    'store_offer','community'), limit=40)
        try:
            verdict = advisor_answer(question, facts)
        except Exception as exc:
            log.warning('Advisor path failed (%s); falling back to the normal answer.', type(exc).__name__)
            return None
        if not verdict or verdict.get('capability') == 'unknown' or not (verdict.get('reply') or '').strip():
            return None
        verdict['question'] = question
        verdict['carried'] = bool(carried)
        if scope:
            remember_question(scope, question, verdict)
        return verdict

    async def advisor_reply(self, text, scope=None, force=False):
        """Just the prose half of `advisor_verdict`, for callers that only need a reply to send."""
        verdict = await self.advisor_verdict(text, scope=scope, force=force)
        return discord_text(verdict['reply']) if verdict else None

    async def _announcement_fact_rows(self,categories,limit=ANNOUNCEMENT_FACT_REPLY_LIMIT):
        """Read curated facts back from the database - this is the answer source after the dump."""
        if not getattr(self.store,'supports_announcement_facts',False):return []
        try:
            return await self.store.announcement_facts(categories=categories,
                channel_ids=tuple(str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS),limit=limit)
        except Exception as exc:
            log.warning('Could not read curated announcement facts (%s); falling back to raw posts.',type(exc).__name__)
            return []

    async def extract_facts_with_ai(self,rows):
        """Run the shared AI extraction pass with this deployment's model configuration."""
        return await ai_extract_facts(rows,base=os.getenv('AI_BASE_URL','https://api.openai.com/v1').strip(),
            key=os.getenv('AI_API_KEY','').strip(),model=os.getenv('AI_MODEL','').strip(),
            batch_size=ANNOUNCEMENT_AI_BATCH_SIZE,max_batches=ANNOUNCEMENT_AI_MAX_BATCHES,
            timeout_seconds=ANNOUNCEMENT_AI_TIMEOUT_SECONDS)

    def _fact_source_signature(self,rows):
        """Stable hash over which messages (and which edits of them) are in the source set."""
        parts=sorted(f"{row.get('message_id')}:{row.get('posted_at')}:{row.get('edited_at') or 0}"
                     for row in rows or [])
        return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:24]

    async def _fact_dump_state(self):
        try:
            rows=await self.store.fetch('SELECT value FROM worker_campaign WHERE key=$1',ANNOUNCEMENT_FACT_STATE_KEY)
            return str(rows[0]['value']) if rows else None
        except Exception:
            return None

    async def _set_fact_dump_state(self,value):
        try:
            await self.store.execute('INSERT INTO worker_campaign(key,value) VALUES($1,$2) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',ANNOUNCEMENT_FACT_STATE_KEY,str(value))
        except Exception as exc:
            log.warning('Could not record the announcement fact dump state (%s).',type(exc).__name__)

    async def refresh_announcement_facts(self,*,use_ai=None,replace=False,rows=None):
        """Turn every saved announcement into curated facts in the database. Returns run stats."""
        if not getattr(self.store,'supports_announcement_facts',False):
            return {'status':'fact_table_missing'}
        source=await self._announcement_source_rows() if rows is None else list(rows or [])
        if not source:
            return {'status':'no_saved_announcements','messages_read':0}
        now=time.time()
        facts,stats=build_facts(source,now=now)
        if use_ai is None:use_ai=self.fact_extraction_enabled()
        ai={'batches':0,'accepted':0,'rejected':0,'error':'skipped'}
        if use_ai:
            result=await self.extract_facts_with_ai(source)
            ai={key:value for key,value in result.items() if key!='facts'}
            if result['facts']:facts=merge_ai_facts(facts,result['facts'],now=now)
        run_id='facts-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        if replace:
            await self.store.clear_announcement_facts(channel_ids=tuple(str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS))
        try:
            written=await self.store.upsert_announcement_facts(facts,run_id=run_id)
        except Exception as exc:
            log.warning('Could not store curated announcement facts (%s).',type(exc).__name__)
            return {**stats,'status':'write_failed','error':type(exc).__name__,'ai':ai}
        stats.update({'status':'ok','facts_written':written,'run_id':run_id,'ai':ai,
                      'signature':self._fact_source_signature(source),
                      'ai_state':'skipped' if not use_ai else ('error' if ai.get('error') else 'ok')})
        log.info('Announcement facts: %s messages read, %s facts written (AI batches %s, %s accepted, %s rejected).',
                 stats.get('messages_read'),written,ai.get('batches'),ai.get('accepted'),ai.get('rejected'))
        return stats

    async def maybe_refresh_announcement_facts(self):
        """Re-dump only when the saved announcements changed, so the loop stays cheap between edits."""
        if not getattr(self.store,'supports_announcement_facts',False):return None
        rows=await self._announcement_source_rows()
        if not rows:return None
        signature=self._fact_source_signature(rows)
        stored=await self._fact_dump_state()
        if stored and stored.startswith(signature) and stored.endswith((':ok',':skipped')):
            return None
        stats=await self.refresh_announcement_facts(rows=rows)
        if stats.get('status')=='ok':
            await self._set_fact_dump_state(f"{stats['signature']}:{stats['ai_state']}")
        return stats

    async def announcement_facts_answer(self,kind,query=None):
        """Answer one announcement question from the fact table; None means 'fall back to raw posts'."""
        now=int(time.time())   # one clock for the whole answer: status, dates and ages must agree
        if kind=='ecr':
            facts=refresh_ecr_status(await self._announcement_fact_rows(('ecr',)),now=now)
            if not facts:return None
            return format_ecr_reply(facts,now=now,include_older=wants_older_ecr(query)) or None
        categories={'promo':('promo_code',),'patch':('balance_change','event','other'),
                    'upcoming':('event','ecr','gear_hub','store_offer','community','balance_change','other')}[kind]
        facts=search_facts(await self._announcement_fact_rows(categories),
                           query if kind in ('promo','patch') else None,
                           () if kind=='promo' else categories)
        if kind=='upcoming':
            facts=[fact for fact in facts if (fact.get('details') or {}).get('stated_dates')
                   or fact.get('category') in ('event','ecr','gear_hub','store_offer')]
        if not facts:return None
        heading={'promo':'🧾 Promo codes in the saved announcements',
                 'patch':'🔧 Patches & balance changes',
                 'upcoming':'📅 What the saved announcements state'}[kind]
        return format_fact_list(facts,heading,limit=6,show_codes=(kind=='promo'),now=now)
    async def announcement_ingestion_loop(self):
        await self.wait_until_ready()
        if not self.intents.message_content:
            for channel_id in ANNOUNCEMENT_CHANNELS:self._set_announcement_access(channel_id,'message_content_intent_disabled')
            return
        while not self.is_closed():
            for channel_id in ANNOUNCEMENT_CHANNELS:
                if self.is_closed():break
                try:await self.ingest_announcement_channel(channel_id)
                except Exception as exc:
                    self._set_announcement_access(channel_id,'ingestion_error_'+type(exc).__name__)
                    log.exception('Announcement ingestion failed for a configured channel; it will retry.')
            # Announcements are the source; the fact table is what the answers read.
            try:await self.maybe_refresh_announcement_facts()
            except Exception:log.exception('Announcement fact dump failed; the next sync will retry it.')
            await asyncio.sleep(ANNOUNCEMENT_SYNC_SECONDS)

    async def _saved_announcement_rows(self, since, limit, operation):
        if not getattr(self.store,'supports_announcements',True):
            return [],'storage_missing'
        try:
            rows=await self.store.recent_announcements(int(since),limit=limit)
            allowed={str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS}
            return [row for row in rows if str(row.get('channel_id') or '') in allowed],None
        except Exception as exc:
            log.warning('Could not read saved %s posts (%s).',operation,type(exc).__name__)
            return [],'read_failed'

    async def upcoming_response(self,query='upcoming events'):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        answer=await self.announcement_facts_answer('upcoming',query)
        if answer:return discord_text(answer)
        rows,error=await self._saved_announcement_rows(time.time()-365*86400,1000,'event')
        result=combined_source_summary(query or 'upcoming events',rows,topic='events')
        if result.startswith('I couldn’t find'):
            if error=='storage_missing':
                return 'I couldn’t find an event update in the saved forum material, and announcement storage is unavailable.'
            if error=='read_failed':
                return 'I couldn’t read saved event announcements and found no matching forum summary. Please try again later.'
            if not rows:
                return format_upcoming(rows,enabled=enabled,access_states=self.announcement_access)
        if error=='storage_missing':
            result+='\n*Announcement storage is unavailable; this summary uses saved forum material only.*'
        elif error=='read_failed':
            result+='\n*Saved announcements could not be read; this summary may be incomplete.*'
        elif not enabled:
            result+='\n*Announcement reading is disabled; this summary may be incomplete.*'
        return result

    def forum_response(self,query=None):
        """Search or list the stored Plarium Forum crawl, separate from event scheduling."""
        return format_forum_request(query)

    async def compound_announcement_reply(self,text):
        """Answer a two-part announcement question in full, or return None.

        Only read-only topics are eligible. Reminders, notes, timezone and loop commands are
        excluded by ``compound`` itself, so this can never turn one message into two side
        effects; those clauses fall through to their own handlers untouched.
        """
        try:
            topics=compound.should_fan_out(text)
            if not topics:return None
            sections=[]
            for topic in topics:
                # The real handler still receives the whole original message, so it applies
                # its own strict gate and its own data path; this only decides how many of
                # them to run and how to label the results.
                if topic=='ecr':
                    sections.append((compound.TOPIC_LABELS['ecr'],await self.ecr_response(text)))
                elif topic=='promo':
                    sections.append((compound.TOPIC_LABELS['promo'],await self.promo_codes_response(text)))
                elif topic=='upcoming':
                    sections.append((compound.TOPIC_LABELS['upcoming'],await self.upcoming_response(text)))
                elif topic=='patch':
                    sections.append((compound.TOPIC_LABELS['patch'],await self.patch_bug_response(text)))
                elif topic=='forum':
                    sections.append((compound.TOPIC_LABELS['forum'],self.forum_response(text)))
            reply=compound.join(sections)
            if not reply:return None
            return discord_text(reply)
        except Exception as exc:
            # A fan-out failure must never swallow the message: fall back to the ordinary
            # single-answer ladder rather than returning an error the user cannot act on.
            log.warning('Compound announcement answer failed (%s); using the single-answer path.',type(exc).__name__)
            return None

    def ecr_shipped_history(self,query=None,ingested_facts=()):
        """Answer an ECR question from the vendored history plus anything ingested since.

        One renderer serves both sources, so a rotation announced on Discord reads exactly
        like a rotation from the tracker file. Live facts are placed first: if an ingested
        rotation is current, it is what the player means.
        """
        try:
            rows=getattr(getattr(self,'advisor_data',None),'ecr',None)
            if not rows:
                from .advisor import load
                rows=load().ecr
            records=[ecr_render.from_tracker_row(row) for row in (rows or ())]
            # Ingested posts get their own block, because a real guide carries strategy and
            # stated figures that a rotation table cannot express. They come first: if a
            # rotation has just been announced, that is what the player is asking about.
            blocks=[]
            for fact in ingested_facts or ():
                try:
                    entries=(fact.get('details') or {}).get('entries') or []
                    if not entries:continue
                    block=ecr_render.render_announcement(entries,
                        title='**'+str(fact.get('title') or 'Crate Rush')+'**',
                        text=ecr_render.fact_source_text(fact))
                    if block:
                        blocks.append(block)
                    else:
                        # Only a post with no renderable block needs a rotation-table row;
                        # otherwise the same post would appear twice in one answer.
                        records.extend(ecr_render.from_fact(fact))
                except Exception:
                    log.warning('An ingested ECR post could not be rendered; skipping it.',exc_info=True)
                    continue
            if not records and not blocks:return None
            # "what happened to Tailsting" is a question about the history, so the digest name
            # wins when there is one. But "current ecr" also resolves to whatever name the word
            # 'current' fuzzy-matches, which answered a question about now with one old item and
            # nothing else. A digest answer only stands when the question is about an item.
            name=ecr_digest_resolve(query,records)
            if name and _wants_one_item(query):
                single=ecr_render.render_one(records,name)
                if single:return single
            history=ecr_render.render(records,title=None) if records else None
            parts=[]
            if blocks:parts.append('\n\n'.join(blocks))
            if history:parts.append(history)
            if not parts:return None
            head=self._ecr_title(ingested_facts)
            return head+'\n\n'+'\n\n---\n\n'.join(parts)
        except Exception as exc:
            log.warning('Shipped ECR history could not be read (%s).',type(exc).__name__)
            return None

    def _ecr_title(self,ingested_facts=()):
        base='**Crate Rush (ECR)**'
        if ingested_facts:
            return base+' · announced rotations plus the recorded history'
        return base+' · from the recorded history — no live announcement has been ingested'

    async def ecr_response(self,query=None):
        answer=await self.announcement_facts_answer('ecr',query)
        if answer:return discord_text(answer)
        text,had_rows=await self._ecr_storage_reply(query)
        if had_rows:return text
        # Nothing has been ingested into the announcement store, but the repository ships a
        # 261-row ECR history. Lead with that and keep the storage diagnostic underneath, so
        # the user gets a real answer and an operator still sees why the live view is empty.
        facts=[]
        try:
            if kind := 'ecr':
                facts=refresh_ecr_status(await self._announcement_fact_rows(('ecr',)),now=int(time.time()))
        except Exception as exc:
            log.warning('Ingested ECR facts could not be read (%s).',type(exc).__name__)
        shipped=self.ecr_shipped_history(query,ingested_facts=facts or ())
        if not shipped:return text
        return f'{shipped}\n\n---\n*No ECR announcement has been ingested, so there is no live '               f'event view to add to the history above.*\n{text}'

    async def _ecr_storage_reply(self,query=None):
        """The original live-announcement ECR answer, plus whether any post was found."""
        channels=', '.join(f'`{channel_id}`' for channel_id in ANNOUNCEMENT_CHANNELS) or 'none configured'
        if not getattr(self.store,'supports_announcements',True):
            return ((f'ECR storage is unavailable: the private announcement table is not installed. '
                     f'Configured channel IDs: {channels}. Apply the announcement-storage migration.'),False)
        disabled_reasons=[]
        if not self.announcement_ingestion_enabled:
            disabled_reasons.append('`ANNOUNCEMENT_INGESTION_ENABLED=false`')
        if not self.intents.message_content:
            disabled_reasons.append('the bot did not request Message Content intent; also enable the privileged intent in the Discord Developer Portal')
        enabled=not disabled_reasons
        try:
            rows=await self.store.recent_announcements(int(time.time())-365*86400,limit=2000)
            allowed={str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS}
            rows=[row for row in rows if str(row.get('channel_id') or '') in allowed]
        except Exception as exc:
            log.warning('Could not read saved ECR event posts (%s).',type(exc).__name__)
            return ((f'ECR storage read failed with `{type(exc).__name__}`. Configured channel IDs: {channels}. '
                     'This is a storage/database failure, not proof of a Discord channel-permission failure; check the bot runtime log.'),False)
        return (format_ecr_events(rows,enabled=enabled,access_states=self.announcement_access,
            configured_channel_ids=ANNOUNCEMENT_CHANNELS,disabled_reasons=disabled_reasons,
            excluded_channel_ids=DISABLED_ANNOUNCEMENT_CHANNELS),bool(rows))

    async def promo_codes_response(self,query='promo codes'):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        answer=await self.announcement_facts_answer('promo',query)
        if answer:return discord_text(answer)
        # The two player-written code posts are in the hash-verified archive already, so a
        # promo question has a real answer with no ingest at all.
        shipped=promo_digest(catalog=self.catalog)
        if shipped:return shipped
        rows,error=await self._saved_announcement_rows(0,2000,'promo-code')
        result=combined_source_summary(query or 'promo codes',rows,topic='promo')
        if error=='storage_missing':
            result+='\n*Announcement storage is unavailable; this summary uses saved forum material only.*'
        elif error=='read_failed':
            result+='\n*Saved announcements could not be read; this lookup may be incomplete.*'
        elif not enabled:
            result+='\n*Announcement reading is disabled; this lookup may be incomplete.*'
        return result

    async def patch_bug_response(self,query):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        answer=await self.announcement_facts_answer('patch',query)
        if answer:return discord_text(answer)
        rows,error=await self._saved_announcement_rows(time.time()-365*86400,2000,'patch/bug')
        result=combined_source_summary(query,rows,topic='issues')
        if error=='storage_missing':
            result+='\n*Announcement storage is unavailable; this summary uses saved forum material only.*'
        elif error=='read_failed':
            result+='\n*Saved announcements could not be read; this lookup may be incomplete.*'
        elif not enabled:
            result+='\n*Announcement reading is disabled; this lookup may be incomplete.*'
        return result

    async def payout_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            # Per-pilot build figures are no longer public, by design: the endpoint
            # exposes totals only. Announce the aggregate once a day instead of
            # polling for something that will never arrive.
            try:
                day,totals=await fetch_totals(os.getenv('PORTICO_BASE_URL',BASE))
                if totals['qualified_today']!=self._last_payout_note:
                    self._last_payout_note=totals['qualified_today']
                    log.info('Payout day %s: %s pilots qualified, %s builds today, %s coins unpaid.',
                             day,totals['qualified_today'],totals['today_builds'],totals['unpaid_coins'])
            except Exception:log.exception('Payout polling failed; will retry')
            await asyncio.sleep(60)

    async def delivery_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for row in await self.store.claim(int(time.time())):
                    try:
                        # A cancellation before send prevents delivery where possible.
                        active=await self.store.fetch("SELECT id FROM reminders WHERE id=$1 AND status='delivering' AND lease_token=$2",row['id'],row['lease_token'])
                        if not active:continue
                        user=self.get_user(int(row['user_id'])) or await self.fetch_user(int(row['user_id']))
                        await user.send('⏰ Reminder: '+safe_text(row['content'],1000),allowed_mentions=discord.AllowedMentions.none())
                        following=next_occurrence(row['kind'],row['due_at'],row['interval_seconds'],row['hour'],row['minute'],row['zone'],int(time.time()))
                        await self.store.complete(row,following)
                    except (discord.Forbidden,discord.NotFound):
                        await self.store.fail(row)
                    except Exception:
                        log.exception('Reminder delivery failed for reminder id %s',row['id'])
                        await self.store.fail(row,'Delivery failed; check bot availability and DMs, then create a new reminder.')
            except Exception:log.exception('Reminder scheduler error')
            await asyncio.sleep(15)

    async def worker_delivery_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for row in await self.workers.claim(int(time.time())):
                    try:
                        if not await self.workers.active(row):continue
                        guild=self.get_guild(GUILD_ID)
                        if not guild:raise RuntimeError('Configured server unavailable')
                        try:member=await guild.fetch_member(int(row['user_id']))
                        except discord.NotFound:
                            await self.workers.abandon_removed(row);continue
                        task=assignment_text(row['user_id'])
                        if not task:
                            await self.workers.abandon_removed(row);continue
                        prefix='Database task reminder' if row['kind']=='daily' else 'Snoozed database task reminder'
                        text=f'⏰ {prefix}: {task}. Ask me to “remind me again after 10 min” or change your daily time.'
                        failures=[]
                        if row['dm_state']=='pending' and await self.workers.active(row):
                            try:
                                await member.send(text,allowed_mentions=discord.AllowedMentions.none())
                                await self.workers.mark(row,'dm','sent')
                            except (discord.Forbidden,discord.NotFound):
                                failures.append('DM unavailable');await self.workers.mark(row,'dm','failed')
                            except discord.HTTPException:
                                failures.append('DM delivery failed');await self.workers.mark(row,'dm','failed')
                        if row['channel_state']=='pending' and await self.workers.active(row):
                            try:
                                channel=guild.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
                                if getattr(channel,'guild',None) is None or channel.guild.id!=GUILD_ID:raise RuntimeError('Wrong channel')
                                await channel.send(f'<@{row["user_id"]}> '+text,allowed_mentions=discord.AllowedMentions(everyone=False,users=[discord.Object(id=int(row['user_id']))],roles=False,replied_user=False))
                                await self.workers.mark(row,'channel','sent')
                            except (discord.HTTPException,RuntimeError):
                                failures.append('Channel delivery failed');await self.workers.mark(row,'channel','failed')
                        # Persist each destination before advancing, so lease recovery avoids
                        # resending already-recorded deliveries. A crash after send but before
                        # recording may still duplicate a message.
                        if row['dm_state']=='failed' and 'DM unavailable' not in failures and 'DM delivery failed' not in failures:failures.append('DM delivery failed')
                        if row['channel_state']=='failed' and 'Channel delivery failed' not in failures:failures.append('Channel delivery failed')
                        await self.workers.finish(row,int(time.time()),'; '.join(failures) or None)
                    except Exception:
                        log.exception('Worker delivery attempt failed for reminder id %s',row['id'])
                        # Lease expiry permits recovery without cancelling future schedules.
            except Exception:log.exception('Worker reminder scheduler error')
            await asyncio.sleep(15)

    async def require_worker_member(self,user_id):
        if not get_worker(user_id):raise ValueError('This database task is not assigned to your account.')
        guild=self.get_guild(GUILD_ID)
        if not guild:raise ValueError('The configured Discord server is unavailable.')
        try:return await guild.fetch_member(int(user_id))
        except discord.NotFound:raise ValueError('Your account is not currently a member of the configured server.')

    async def set_worker_time(self,user_id,clock,zone=None):
        await self.require_worker_member(user_id)
        existing=await self.workers.daily(user_id)
        zone=zone or (existing['zone'] if existing else None)
        if not zone:raise ValueError('Please include your timezone, for example: 8pm Asia/Kolkata.')
        _,due=await self.workers.set_daily(user_id,clock,zone)
        return f'Daily task reminder set for {safe_text(clock,20)} {safe_text(zone,60)}. Next: <t:{due}:F>. I will try your DM and the task channel. {assignment_text(user_id)}'

    async def snooze_worker(self,user_id,seconds):
        await self.require_worker_member(user_id)
        _,due=await self.workers.snooze(user_id,seconds)
        return f'Extra task reminder set for <t:{due}:F>. Your daily time is unchanged; I will try both DM and the task channel.'

    async def create_reminder(self, user_id, spec, content):
        content=content.strip()
        if not content or len(content)>1000:raise ValueError('Reminder text must be 1–1000 characters.')
        if len(await self.store.list_reminders(user_id))>=25:raise ValueError('You have 25 active or failed reminders; cancel some first.')
        # A delay needs no timezone; "8pm" does, and defaulting it to UTC is how a reminder lands at
        # half past one in the player's own night. Ask once instead of storing a wrong schedule.
        getter=getattr(self.store,'timezone_for',None)
        zone=await (getter(user_id) if getter else self.store.get_timezone(user_id))
        set_zone=bool(zone)
        if not zone and needs_wall_clock(spec):
            raise ValueError('That time needs your timezone so it lands at the right hour. Say '
                             '`set my timezone to Asia/Kolkata` (or your own zone) and ask me again.')
        zone=zone or 'UTC'
        parsed=parse_spec(spec,zone)
        rid=await self.store.create_reminder(user_id,content,parsed,zone)
        out=f'Reminder #{rid} set for <t:{parsed[0]}:F> ({safe_text(zone)}). I will send a DM. Make sure your DMs are open.'
        if not set_zone:
            out+=' Set your timezone with `set my timezone to Asia/Kolkata` and timed reminders use your clock.'
        return out

    def currency_icon_files(self):
        files=[]
        for label,filename in CURRENCY_ASSETS:
            path=BOT_ROOT/'data'/filename
            if path.is_file():files.append(discord.File(path,filename=filename,description=label+' icon'))
        return files

    async def reply_message(self,message,text,with_currency_icons=False,mention_author=True,allowed_user_ids=(),suppress_embeds=False):
        kwargs={}
        has_discord_profile_link=bool(re.search(r'https://discord\.com/users/\d{17,20}',str(text),re.I))
        if suppress_embeds or has_discord_profile_link:kwargs['suppress_embeds']=True
        if with_currency_icons and not self.currency_emojis:
            files=self.currency_icon_files()
            if files:kwargs['files']=files
        allowed_ids=[]
        for user_id in allowed_user_ids or ():
            try:user_id=int(user_id)
            except (TypeError,ValueError):continue
            if user_id in IDENTITY_PROFILE_IDS and user_id not in allowed_ids:allowed_ids.append(user_id)
        if allowed_ids:
            allowed=discord.AllowedMentions(everyone=False,
                users=[discord.Object(id=user_id) for user_id in allowed_ids],roles=False,
                replied_user=mention_author)
        else:
            allowed=REPLY_MENTIONS if mention_author else discord.AllowedMentions.none()
        # Long answers are sent whole, as more than one message: cutting at 1,900 characters is how `/help`
        # lost its last third and how a multi-item review lost its price lines.
        chunks=message_chunks(discord_text(text,limit=1900*6))
        await message.reply(chunks[0],mention_author=mention_author,allowed_mentions=allowed,**kwargs)
        for extra in chunks[1:]:
            # The rest of a long answer goes on as plain follow-up messages rather than being cut off.
            channel=getattr(message,'channel',None)
            if channel is None:break
            await channel.send(extra,allowed_mentions=discord.AllowedMentions.none())

    async def conversation_history_for(self,message):
        if not getattr(self.store,'supports_conversations',False):return []
        guild_id=str(message.guild.id) if message.guild else ''
        try:
            return await self.store.conversation_history(message.author.id,guild_id,message.channel.id)
        except Exception as exc:
            log.warning('Could not read short-lived conversation context (%s); continuing without it.',type(exc).__name__)
            return []

    async def conversation_turn_reply(self,message,user_text,assistant_text,with_currency_icons=False,ambient=False):
        await self.reply_message(message,assistant_text,with_currency_icons=with_currency_icons,mention_author=not ambient)
        if getattr(self.store,'supports_conversations',False):
            guild_id=str(message.guild.id) if message.guild else ''
            created=getattr(message,'created_at',None)
            created_at=int(created.timestamp()) if created else None
            try:
                await self.store.add_conversation_turn(message.author.id,guild_id,message.channel.id,
                                                       user_text,assistant_text,created_at=created_at)
            except Exception as exc:
                log.warning('Could not save short-lived conversation context (%s); the reply was still sent.',type(exc).__name__)

    async def private_reply(self,message,text,saved=False,mention_author=True):
        text=discord_text(text)
        if isinstance(message.channel,discord.DMChannel):
            await self.reply_message(message,text,mention_author=mention_author);return
        try:
            await message.author.send(text,allowed_mentions=discord.AllowedMentions.none())
            await self.reply_message(message,'I sent you a private DM.',mention_author=mention_author)
        except discord.Forbidden:
            reply='Saved, but I could not DM you. Please enable DMs; I will still try the task channel.' if saved else 'I could not DM you. Please enable DMs and try again.'
            await self.reply_message(message,reply,mention_author=mention_author)

    def ambient_chat_rate_limited(self,message):
        guild=getattr(message,'guild',None)
        key=(getattr(guild,'id',None),getattr(message.channel,'id',None),getattr(message.author,'id',None))
        now=time.monotonic();previous=self.ambient_last_reply.get(key)
        if previous is not None and now-previous<AMBIENT_CHAT_COOLDOWN_SECONDS:return True
        self.ambient_last_reply[key]=now
        if len(self.ambient_last_reply)>5000:
            self.ambient_last_reply={scope:stamp for scope,stamp in self.ambient_last_reply.items() if now-stamp<3600}
        return False

    async def message_replies_to_bot(self,message):
        reference=getattr(message,'reference',None)
        if not reference or not getattr(reference,'message_id',None):return False
        resolved=getattr(reference,'resolved',None)
        author=getattr(resolved,'author',None)
        if author is not None:return getattr(author,'id',None)==self.user.id
        fetch=getattr(message.channel,'fetch_message',None)
        if not fetch:return False
        try:
            referenced=await fetch(reference.message_id)
            return getattr(getattr(referenced,'author',None),'id',None)==self.user.id
        except (discord.NotFound,discord.Forbidden,discord.HTTPException):
            return False

    async def on_message(self,message):
        if message.author.bot or not self.user:return
        is_dm=isinstance(message.channel,discord.DMChannel)
        in_worker_server=bool(message.guild and message.guild.id==GUILD_ID)
        worker_context=bool(get_worker(message.author.id) and (is_dm or in_worker_server))
        prompt_reply=False
        if worker_context and not is_dm and message.channel.id==CHANNEL_ID and message.reference and message.reference.message_id:
            prompt_reply=str(message.reference.message_id)==await self.workers.get_config('prompt_message_id')
        mentioned=self.user in message.mentions
        reply_to_bot=False
        if not is_dm and not mentioned and not prompt_reply:
            reply_to_bot=await self.message_replies_to_bot(message)
        text=re.sub(r'<@!?'+str(self.user.id)+r'>','',message.content).strip()
        # A misspelled catalog name used to delete the subject of a sentence: "lacwing"
        # matched no mech, so no build request was ever formed and the question fell
        # through to a 1v1 simulator that asked "which mech on each side?". Repair the
        # names first, and remember what was repaired so the reply can say so.
        fuzzy_corrections={}
        if self.fuzzy_names_enabled and text:
            pool=(tuple(getattr(self.catalog,'mech_names',()))+tuple(getattr(self.catalog,'weapon_names',()))
                  +tuple(getattr(self.catalog,'pilot_names',()))+tuple(getattr(self.catalog,'mod_names',())))
            text,fuzzy_corrections=correct_names(text,pool,self.fuzzy_cutoff)
            if fuzzy_corrections:
                log.info('Read %s as catalog names before answering: %s', ', '.join(f'"{k}"' for k in fuzzy_corrections),
                         ', '.join(fuzzy_corrections.values()))
        build_scope=(str(getattr(getattr(message,'guild',None),'id','') or ''),
                    str(getattr(getattr(message,'channel',None),'id','') or ''),
                    str(getattr(getattr(message,'author',None),'id','') or ''))
        now_monotonic=time.monotonic()
        for old_scope,pending in list(self.pending_build_requests.items()):
            if now_monotonic-pending.get('created_at',0)>BUILD_SP_PROMPT_TTL_SECONDS:
                self.pending_build_requests.pop(old_scope,None)
        pending_build=self.pending_build_requests.get(build_scope)
        pending_sp_range=parse_build_sp_range(text,allow_bare=True) if pending_build else None
        pending_build_reply=bool(pending_build and pending_sp_range)
        ambient_question=False
        parsed_reminder=parse_natural_reminder(text)
        directed=is_dm or mentioned or prompt_reply or reply_to_bot
        if pending_build_reply and not directed:
            directed=True
            ambient_question=True
        if not directed:
            # Never jump into another member's reply chain or respond to a message aimed at a user.
            reference=getattr(message,'reference',None)
            if (not self.ambient_channel_chat_enabled or getattr(reference,'message_id',None)
                    or getattr(message,'mentions',None)):
                return
            names=(tuple(getattr(self.catalog,'weapon_names',()))+tuple(getattr(self.catalog,'mech_names',()))
                   +tuple(getattr(self.catalog,'pilot_names',()))+tuple(getattr(self.catalog,'mod_names',())))
            # Only fully specified natural reminder requests may bypass the ambient question gate;
            # the reminder itself is private and never pings the channel author.
            if (not should_answer_unmentioned(text,names) and not parsed_reminder) or self.ambient_chat_rate_limited(message):return
            ambient_question=True
        # A ping that carries only a screenshot is the normal way people use this bot:
        # they ping it and drop an image. There is no text, so the `if not text` gate used
        # to drop the message and the bot said nothing at all.
        images=screenshot_images(message)
        if images and not text:
            text='what does this screenshot show?'
            log.info('Answered a screenshot ping with no text (%s image(s)).',len(images))
        elif not text:
            return
        try:
            identity=bot_identity_response(text)
            if identity:
                await self.reply_message(message,identity,mention_author=not ambient_question,
                                         suppress_embeds=True);return
            if wants_invite_link(text):
                await self.reply_message(message,f'Invite **ARCHΛNGLE** to a server where you can add bots: {INVITE_URL}',mention_author=not ambient_question);return
            if re.fullmatch(r'(?:stop|cancel)\s+(?:(?:my|the)\s+)?(?:(?:ping|dm|direct[- ]message)\s+)?loops?',text,re.I):
                count=self.alerts.stop(message.author.id,message.author.id)
                out=f'Stop requested for {count} of your active loop(s). An in-flight message may still arrive.' if count else 'You have no active ping loops.'
                await self.private_reply(message,out);return
            if worker_context:
                snooze_seconds=parse_worker_snooze(text)
                if snooze_seconds is not None:
                    out=await self.snooze_worker(message.author.id,snooze_seconds)
                    await self.private_reply(message,out,saved=True);return
                worker_time=parse_worker_time(text)
                if worker_time:
                    out=await self.set_worker_time(message.author.id,*worker_time)
                    await self.private_reply(message,out,saved=True);return
                if prompt_reply:
                    await self.private_reply(message,'Reply with a time and timezone, such as `8pm Asia/Kolkata`, or use /worker_time.');return
            if parsed_reminder:
                # Check DM availability before creating, especially for private requests.
                try:await message.author.send('I’ll send your reminder here.',allowed_mentions=discord.AllowedMentions.none())
                except discord.Forbidden:
                    await self.reply_message(message,'Please enable DMs so I can deliver reminders.',mention_author=not ambient_question);return
                out=await self.create_reminder(message.author.id,*parsed_reminder)
                await self.private_reply(message,out,mention_author=not ambient_question);return
            if re.search(r'\bremind\s+me\b',text,re.I):
                await self.private_reply(message,'I can set that. Tell me when and what, in any wording: '
                                         '`remind me in 20 seconds to write notes`, `remind me at 8pm to check the ECR`, '
                                         '`remind me to file the report tomorrow`, `every friday at 9am to post a recap`.');return
            if re.fullmatch(r'(?:help|commands|show\s+help|what(?:\s+features)?\s+can\s+you\s+do|what\s+can\s+you\s+help\s+with)[?!.]*',text,re.I):
                out=help_text(message.author.id,message.guild.id if message.guild else None)
                if ambient_question:await self.reply_message(message,out,mention_author=False)
                else:await self.private_reply(message,out)
                return
            # A message that asks two separate things must not be answered once. This runs
            # before the single-answer gates below, and only fires when at least two distinct
            # read-only topics are actually present; everything else falls through untouched.
            compound_reply=await self.compound_announcement_reply(text)
            if compound_reply:
                await self.reply_message(message,compound_reply,mention_author=not ambient_question);return
            if wants_forum_lookup(text):
                await self.reply_message(message,self.forum_response(text),mention_author=not ambient_question);return
            if wants_promo_codes(text):
                await self.reply_message(message,await self.promo_codes_response(text),mention_author=not ambient_question);return
            if wants_patch_bug_lookup(text):
                await self.reply_message(message,await self.patch_bug_response(text),mention_author=not ambient_question);return
            if wants_ecr_info(text):
                await self.reply_message(message,await self.ecr_response(text),mention_author=not ambient_question);return
            if wants_upcoming(text):
                await self.reply_message(message,await self.upcoming_response(text),mention_author=not ambient_question);return
            if pending_build_reply:
                self.pending_build_requests.pop(build_scope,None)
                try:
                    payload=await fetch_builds(os.getenv('PORTICO_BASE_URL',BASE),pending_build['mech'])
                    parts=builds_response_parts(payload,mech=pending_build['mech'],
                        mode=pending_build.get('mode'),sp_range=pending_sp_range)
                except Exception:
                    log.exception('SP-filtered public builds lookup failed')
                    parts=['The public builds API is unavailable or returned an unexpected response. Please ask again shortly.']
                for index,part in enumerate(parts):
                    await self.reply_message(message,part,mention_author=not ambient_question and index==0,
                                             suppress_embeds=True)
                return
            build_request=natural_builds_request(text,self.catalog.mech_names)
            if build_request:
                if build_request['ambiguous']:
                    out='Which mech do you mean? Please name one mech for the build lookup.'
                elif build_request['unresolved']:
                    out='I couldn’t match that mech to the catalog. Please give me its exact name for a filtered build lookup.'
                elif build_request['needs_mech']:
                    out='Which mech do you want builds for? Name one mech, and I’ll check community submissions at your SP.'
                else:
                    query=build_request['query']
                    sp_range=build_request['sp_range']
                    if query and not build_request['list_requested'] and sp_range is None:
                        self.pending_build_requests[build_scope]={
                            'mech':query,'mode':build_request['mode'],'created_at':time.monotonic()}
                        mode_text=f' for {build_request["mode"]}' if build_request['mode'] else ''
                        out=(f'What SP range are you targeting for {safe_text(query,80)}{mode_text}? '
                             'Reply with one SP value (like `6,500`) or a range (like `6,000–8,000`), and I’ll list all matching community builds.')
                        await self.reply_message(message,out,mention_author=not ambient_question);return
                    try:
                        payload=await fetch_builds(os.getenv('PORTICO_BASE_URL',BASE),query)
                        if sp_range is not None:
                            parts=builds_response_parts(payload,mech=query,mode=build_request['mode'],sp_range=sp_range)
                            out=None
                        else:
                            best=bool(query) and (build_request['best_requested'] or not build_request['list_requested'])
                            out=builds_response(payload,mech=query,mode=build_request['mode'],best=best)
                            parts=None
                    except Exception:
                        log.exception('Natural-language public builds lookup failed')
                        out='The public builds API is unavailable or returned an unexpected response.'
                        parts=None
                    if parts is not None:
                        for index,part in enumerate(parts):
                            await self.reply_message(message,part,mention_author=not ambient_question and index==0,
                                                     suppress_embeds=True)
                    else:
                        await self.reply_message(message,out,mention_author=not ambient_question,
                                                 suppress_embeds=True)
                    return
                await self.reply_message(message,out,mention_author=not ambient_question);return
            expression=parse_natural_calculation(text)
            if expression:
                try:out='Result: '+str(calculate(expression))
                except (ValueError,SyntaxError) as exc:out='Cannot calculate: '+safe_text(exc,200)
                await self.reply_message(message,out,mention_author=not ambient_question);return
            level_note=re.fullmatch(r'(?:well\s+)?(?:mine|my\s+(?:item|weapon|mech))\s+(?:is\s+)?(?:level\s*|l)\s*(\d+)',text,re.I)
            if level_note:
                out=f'Got it: level {int(level_note[1])}. Which item and rank change should I calculate?'
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            if re.fullmatch(r'(?:show|list)\s+(?:my\s+)?reminders',text,re.I):
                rows=await self.store.list_reminders(message.author.id)
                lines=[f"#{r['id']} — <t:{r['due_at']}:F> ({r['status']}) — {safe_text(r['content'],120)}" for r in rows]
                await self.private_reply(message,discord_text('**Your reminders**\n'+'\n'.join(lines)) if lines else 'You have no active or failed reminders.');return
            m=re.fullmatch(r'cancel\s+reminder\s+#?(\d+)',text,re.I)
            if m:
                ok=await self.store.cancel_reminder(message.author.id,int(m[1]))
                await self.private_reply(message,'Reminder cancelled; an in-flight DM may still arrive.' if ok else 'Reminder not found.');return
            if re.fullmatch(r'(?:show|list)\s+(?:my\s+)?notes',text,re.I):
                rows=await self.store.list_notes(message.author.id)
                await self.private_reply(message,discord_text('**Your notes**\n'+'\n'.join(f"#{r['id']} — {safe_text(r['content'],150)}" for r in rows)) if rows else 'You have no notes.');return
            m=re.fullmatch(r'(?:remember\s+that|note)\s+(.+)',text,re.I)
            if m:
                content=m[1].strip()
                if not content or len(content)>1000:raise ValueError('Note must be between 1 and 1000 characters.')
                if len(await self.store.list_notes(message.author.id))>=25:raise ValueError('You have 25 notes; delete one first.')
                # Do not create a private note when the user cannot receive the confirmation.
                if not is_dm:
                    try:await message.author.send('I can save your private note here.',allowed_mentions=discord.AllowedMentions.none())
                    except discord.Forbidden:
                        await self.reply_message(message,'Please enable DMs so I can confirm private notes.');return
                noteid=await self.store.add_note(message.author.id,content)
                await self.private_reply(message,f'Saved note #{noteid}.');return
            m=re.fullmatch(r'delete\s+note\s+#?(\d+)',text,re.I)
            if m:
                ok=await self.store.delete_note(message.author.id,int(m[1]))
                await self.private_reply(message,'Note deleted.' if ok else 'Note not found.');return
            m=re.fullmatch(r'(?:set\s+)?(?:my\s+)?timezone\s+(?:to\s+)?([A-Za-z_+\-/]+)',text,re.I)
            if m:
                tzinfo(m[1]);await self.store.set_timezone(message.author.id,m[1])
                await self.private_reply(message,'Timezone set to '+safe_text(m[1])+'.');return
            # The understanding pipeline runs here, after the cheap deterministic handlers
            # (reminders, notes, timezones, events) have had their turn but before the
            # exact-lookup ladder decides by pattern order. A cost question, however it is
            # phrased, is caught before "the message mentions a mech" can steal it.
            if images:
                # Read the image before the text ladder can answer from catalog data. A
                # stat sheet is not a question about every mech whose name appears in it,
                # and a screenshot with no text has no other path to an answer at all.
                base=os.getenv('AI_BASE_URL','https://api.openai.com/v1')
                shot_answer=await self.screenshot_answer(text,message,images,base=base)
                if shot_answer:
                    await self.conversation_turn_reply(message,text,shot_answer,ambient=ambient_question)
                    return
            pipeline_out=await self.pipeline_answer(text,message,scope=build_scope)
            if pipeline_out:
                # Say what was corrected, so the answer never looks like mind reading.
                prefix=spell_it_out(text,fuzzy_corrections)+'\n' if fuzzy_corrections else ''
                await self.conversation_turn_reply(message,text,prefix+pipeline_out,ambient=ambient_question)
                return
            # Exact catalog lookups are local, so they avoid unnecessary provider round trips.
            weapon_name=direct_item_lookup(text,self.catalog.weapon_names)
            if weapon_name:
                rank_match=re.search(r'\brank\s*(\d+)\b',text,re.I);level_match=re.search(r'\blevel\s*(\d+)\b',text,re.I)
                rank=int(rank_match[1]) if rank_match else None;level=int(level_match[1]) if level_match else None
                profile=self.catalog.weapon(weapon_name,rank,level)
                out=weapon_response(profile,rank,level)
                if self._catalog_left_a_gap(profile):
                    out=await self.advisor_reply(text,scope=build_scope,force=True) or out
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            mech_name=direct_item_lookup(text,self.catalog.mech_names)
            if mech_name:
                rank_match=re.search(r'\b(?:rank|stars?|r)\s*(\d+)\b',text,re.I)
                rank=int(rank_match[1]) if rank_match else None
                profile=self.catalog.mech_profile(mech_name,rank=rank)
                out=mech_response(profile,rank=rank)
                if self._catalog_left_a_gap(profile):
                    out=await self.advisor_reply(text,scope=build_scope,force=True) or out
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            pilot_name=direct_item_lookup(text,getattr(self.catalog,'pilot_names',()))
            if pilot_name and hasattr(self.catalog,'pilot_profile'):
                rank_match=re.search(r'\b(?:rank|stars?|r)\s*(\d+)\b',text,re.I)
                rank=int(rank_match[1]) if rank_match else None
                out=pilot_response(self.catalog.pilot_profile(pilot_name,rank=rank),rank=rank)
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            pilot_mentions=find_mentions(text,getattr(self.catalog,'pilot_names',()))
            advanced_pilot_question=bool(re.search(r'\b(?:compare|comparison|versus|vs\.?|better|stronger|difference|recommend|recommendation|best|which|should|worth|viable|good|use|with|against|implant|mod|build|guide|strategy|help)\b',text,re.I))
            if pilot_mentions and hasattr(self.catalog,'pilot_profile') and not advanced_pilot_question:
                rank_match=re.search(r'\b(?:rank|stars?|r)\s*(\d+)\b',text,re.I)
                rank=int(rank_match[1]) if rank_match else None
                out='\n\n'.join(pilot_response(self.catalog.pilot_profile(name,rank=rank),rank=rank) for name in pilot_mentions[:3])
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            mod_mentions=find_mentions(text,getattr(self.catalog,'mod_names',()))
            advanced_mod_question=bool(re.search(r'\b(?:compare|comparison|versus|vs\.?|better|stronger|difference|recommend|recommendation|best|which|should|worth|viable|good|use|with|against|build|guide|strategy|help)\b',text,re.I))
            if mod_mentions and hasattr(self.catalog,'mod_profile') and not advanced_mod_question:
                rarities=set(re.findall(r'\b(epic|legendary)\b',text,re.I))
                rarity=next(iter(rarities)).casefold() if len(rarities)==1 else None
                mod_name=mod_mentions[0]
                profile=self.catalog.mod_profile(mod_name,rarity)
                level_pair=re.search(r'\b(?:level|lvl|lv|l)\s*(\d+)\s*(?:to|→|->|-)\s*(?:level|lvl|lv|l)?\s*(\d+)\b',text,re.I)
                if level_pair and rarity and re.search(r'\b(?:upgrade|cost|price)\b',text,re.I):
                    profile['upgrade_cost']=self.catalog.mod_upgrade_cost(mod_name,rarity,int(level_pair[1]),int(level_pair[2]))
                out=mod_response(profile)
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            if not pilot_mentions and not mod_mentions:
                verdict=await self.advisor_verdict(text,scope=build_scope)
                if verdict:
                    out=discord_text(verdict['reply'])
                    if verdict.get('carried'):
                        # Say that the short reply was read as an answer, so nothing looks like mind reading.
                        subject=advisor_subject(verdict)
                        out=(f'Reading that as an answer about {subject}: ' if subject
                             else 'Reading that with your earlier question: ')+out
                    await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            conversation_history=await self.conversation_history_for(message)
            announcement_facts=None
            if re.search(r'\becr\b|crate rush|promo|redemption|patch|rebalance|nerf|buff|event|update|gear hub|store',
                         text, re.I):
                # The free-text path answers from the curated fact table too, not from model memory.
                facts=await self._announcement_fact_rows(('ecr','promo_code','event','balance_change','gear_hub',
                                                           'store_offer','community'), limit=40)
                announcement_facts=facts_prompt_bundle(search_facts(facts, text), now=int(time.time())) or None
            reply=await answer_question_with_brain(os.getenv('AI_BASE_URL','https://api.openai.com/v1'),
                os.getenv('AI_API_KEY',''),os.getenv('AI_MODEL',''),text,self.catalog,session=self.ai_session,
                conversation_history=conversation_history,currency_emoji_map=self.currency_emojis,
                announcement_facts=announcement_facts)
            if reply:
                has_costs=bool(re.search(r'\b(?:upgrade costs|cost:)\s+\*\*',reply,re.I))
                await self.conversation_turn_reply(message,text,reply,with_currency_icons=has_costs,ambient=ambient_question);return
            names=find_mentions(text,self.catalog.weapon_names)
            if names:
                m=re.search(r'\brank\s*(\d+)\b',text,re.I);l=re.search(r'\blevel\s*(\d+)\b',text,re.I)
                rank=int(m[1]) if m else None;level=int(l[1]) if l else None
                out='\n\n'.join(weapon_response(self.catalog.weapon(n,rank,level),rank,level) for n in names[:3])
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            names=find_mentions(text,self.catalog.mech_names)
            if names:
                out='\n\n'.join(mech_response(self.catalog.mech_profile(n)) for n in names[:3])
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            if GAME_WORDS.search(text):
                out='I don’t have reviewed information for that game question yet, so I won’t guess. Try `/weapon`, `/mech`, `/builds`, or `/upcoming` for supported lookups.'
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            await self.reply_message(message,'General AI is not configured yet. Use /help to see what I can do.',mention_author=not ambient_question)
        except ValueError as e:
            if ambient_question:await self.reply_message(message,safe_text(e),mention_author=False)
            else:await self.private_reply(message,safe_text(e))
        except Exception:
            log.exception('Message handling failed')
            if ambient_question:await self.reply_message(message,'Sorry, that question failed. Please try again or mention me for help.',mention_author=False)
            else:await self.private_reply(message,'Sorry, that request failed. Please try again or use /help.')


def install_commands(bot):
    tree=bot.tree
    @tree.command(name='advise',description='Ask the gear advisor: is this buy, crate or build worth it?')
    @app_commands.guild_only()
    @app_commands.describe(question='Plain words, e.g. "is Oracle 16 worth 8275 ac" or "I have 4000 ac, what should I buy"')
    async def advise(i:discord.Interaction,question:str):
        # The command is the secondary door: the same engine answers an @-mention with no slash at all,
        # and it shares the pending thread, so answering a follow-up by command still works.
        scope=(str(getattr(i,'guild_id','') or ''),str(getattr(getattr(i,'channel',None),'id','') or ''),
               str(getattr(getattr(i,'user',None),'id','') or ''))
        await i.response.defer(ephemeral=True)
        verdict=await bot.advisor_verdict(question[:900],scope=scope,force=True)
        if not verdict:
            await i.followup.send('Nothing in that sentence is a gear, crate or budget question I can price. '
                                  'Name an item, a crate, or two things you are choosing between.',
                                  ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            return
        reply=verdict['reply']
        if verdict.get('carried'):
            subject=advisor_subject(verdict)
            reply=(f'Reading that as an answer about {subject}: ' if subject
                    else 'Reading that with your earlier question: ')+reply
        await i.followup.send(discord_text(reply),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='help',description='Show bot features and examples')
    async def help_cmd(i:discord.Interaction):
        # The feature list is longer than one Discord message, so it is sent as several rather than
        # truncated: a cut-off /help is how a whole command goes undocumented in the eyes of players.
        chunks=message_chunks(discord_text(help_text(i.user.id,i.guild_id),limit=1900*6))
        await i.response.send_message(chunks[0],ephemeral=True)
        for extra in chunks[1:]:
            await i.followup.send(extra,ephemeral=True)

    @tree.command(name='invite',description='Get the link to add this bot to a server')
    async def invite(i:discord.Interaction):
        await i.response.send_message(f'Invite **ARCHΛNGLE** to a server where you can add bots: {INVITE_URL}',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    async def start_channel_alert(i,member,interval_seconds,duration_seconds,reason):
        if not i.guild:
            await i.response.send_message('Channel pings can only be started in a server.',ephemeral=True);return
        if i.user.id not in OPERATORS:
            await i.response.send_message('Only the two authorized operator IDs may start channel ping loops.',ephemeral=True);return
        if member.bot or not isinstance(i.channel,discord.TextChannel) or not i.guild.me:
            await i.response.send_message('Choose a human member and a server text channel.',ephemeral=True);return
        perms=i.channel.permissions_for(i.guild.me);targetperms=i.channel.permissions_for(member)
        if not (perms.view_channel and perms.send_messages and targetperms.view_channel):
            await i.response.send_message('The bot needs View Channel and Send Messages; the target must be able to view the channel.',ephemeral=True);return
        try:validate_alert(interval_seconds,duration_seconds)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        if not bot.alerts.available(member.id,'channel'):
            await i.response.send_message('A loop is already active for that member and destination, or the five-loop limit has been reached.',ephemeral=True);return
        reason=safe_text(reason.strip() or 'Please check this',180)
        total_messages=1+(duration_seconds-1)//interval_seconds
        allowed=discord.AllowedMentions(everyone=False,users=[discord.Object(id=member.id)],roles=False,replied_user=False)
        await i.response.defer(ephemeral=True)
        try:
            # The first notification is sent immediately. Each message contains exactly one
            # explicit target mention; follow-ups are new channel messages, not silent replies.
            destination=i.channel
            await destination.send(f'{member.mention} 🔔 Ping loop (1/{total_messages}): {reason} — use `/stop_loop` to stop.',allowed_mentions=allowed)
        except (discord.Forbidden,discord.HTTPException):
            await i.followup.send('I could not send the first ping. Check the bot’s View Channel/Send Messages permissions and the target’s channel access; no loop was started.',ephemeral=True)
            return
        try:
            key=bot.alerts.start(member,'channel',destination,interval_seconds,duration_seconds,i.user.id,reason)
        except ValueError as e:
            await i.followup.send('The first ping was sent, but follow-ups were not started: '+str(e),ephemeral=True);return
        await i.followup.send(f'Ping loop `{key}` started. The first ping was sent immediately; up to {total_messages-1} more messages will contain one ping each, every {interval_seconds}s, for at most {duration_seconds}s. Stop with `/stop_loop`.',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='ping_loop',description='Send one immediate ping, then start a bounded channel ping loop')
    @app_commands.guild_only()
    @app_commands.describe(member='Member to ping',interval_seconds='At least 5 seconds',duration_seconds='Maximum 1800 seconds',reason='Short message')
    async def ping_loop(i:discord.Interaction,member:discord.Member,interval_seconds:app_commands.Range[int,5,1800],duration_seconds:app_commands.Range[int,5,1800],reason:str='Please check this'):
        await start_channel_alert(i,member,interval_seconds,duration_seconds,reason)

    @tree.command(name='dm_loop',description='Start a bounded direct-message loop')
    @app_commands.guild_only()
    @app_commands.describe(member='Member to message',interval_seconds='At least 5 seconds',duration_seconds='Maximum 1800 seconds',reason='Short message')
    async def dm_loop(i:discord.Interaction,member:discord.Member,interval_seconds:app_commands.Range[int,5,1800],duration_seconds:app_commands.Range[int,5,1800],reason:str='Please check this'):
        if not i.guild:
            await i.response.send_message('DM loops must be started from a server.',ephemeral=True);return
        if i.user.id not in OPERATORS:
            await i.response.send_message('Only the two authorized operator IDs may start DM loops.',ephemeral=True);return
        if not i.guild or member.bot:
            await i.response.send_message('Choose a human member from a server.',ephemeral=True);return
        try:validate_alert(interval_seconds,duration_seconds)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        if not bot.alerts.available(member.id,'dm'):
            await i.response.send_message('A loop is already active for that member and destination, or the five-loop limit has been reached.',ephemeral=True);return
        reason=safe_text(reason.strip() or 'Please check this',180)
        await i.response.defer(ephemeral=True)
        try:
            await member.send(f'🔔 A direct-message loop was started by an authorized operator: {reason}. Additional messages will arrive every {interval_seconds}s for up to {duration_seconds}s. Use `/stop_loop` to stop it.',allowed_mentions=discord.AllowedMentions.none())
            key=bot.alerts.start(member,'dm',member,interval_seconds,duration_seconds,i.user.id,reason)
            await i.followup.send(f'DM loop `{key}` started. You or an authorized operator can stop it with `/stop_loop`.',ephemeral=True)
        except discord.HTTPException:await i.followup.send('Could not send the initial DM, so the loop did not start. The member may have DMs closed.',ephemeral=True)
        except ValueError as e:await i.followup.send('Loop not started: '+str(e),ephemeral=True)

    @tree.command(name='stop_loop',description='Stop your loops or, if authorized, loops for a member')
    @app_commands.describe(member='Optional member; other users can only be selected by an authorized operator')
    async def stop_loop(i:discord.Interaction,member:discord.User=None):
        if member and member.id!=i.user.id and i.user.id not in OPERATORS:
            await i.response.send_message('You can stop only your own loops.',ephemeral=True);return
        target=member.id if member else i.user.id
        count=bot.alerts.stop(i.user.id,target)
        text=f'Stop requested for {count} channel and DM loop(s) for user `{target}`. An in-flight message may still arrive.' if count else f'No active loops found for user `{target}`.'
        await i.response.send_message(discord_text(text),ephemeral=True)

    @tree.command(name='stop_user_loops',description='Stop all channel and DM loops for one user')
    @app_commands.guild_only()
    @app_commands.describe(member='The user whose active loops should stop')
    async def stop_user_loops(i:discord.Interaction,member:discord.User):
        if not i.guild:
            await i.response.send_message('This operator command must be used in a server.',ephemeral=True);return
        if i.user.id not in OPERATORS:
            await i.response.send_message('Only the two authorized operator IDs may stop loops for another user. Use `/stop_loop` for your own loops.',ephemeral=True);return
        count=bot.alerts.stop(i.user.id,member.id)
        text=f'Stop requested for {count} channel and DM loop(s) for user `{member.id}`. An in-flight message may still arrive.' if count else f'No active loops found for user `{member.id}`.'
        await i.response.send_message(discord_text(text),ephemeral=True)

    @tree.command(name='worker_setup',description='Set up the database worker role and send the initial time request')
    async def worker_setup(i:discord.Interaction):
        if not i.guild or i.guild.id!=GUILD_ID or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.manage_roles:
            await i.response.send_message('Only a server member with Manage Roles can run this in the configured server.',ephemeral=True);return
        await i.response.defer(ephemeral=True)
        async with bot.worker_setup_lock:
            guild=i.guild;me=guild.me;channel=guild.get_channel(CHANNEL_ID)
            if not me or not me.guild_permissions.manage_roles:
                await i.followup.send('The bot needs Manage Roles, and its highest role must be above the worker role.',ephemeral=True);return
            if not isinstance(channel,discord.TextChannel):
                await i.followup.send('The configured task channel is unavailable to the bot.',ephemeral=True);return
            cp=channel.permissions_for(me)
            if not cp.view_channel or not cp.send_messages:
                await i.followup.send('The bot needs View Channel and Send Messages in the configured task channel.',ephemeral=True);return
            roles=[r for r in guild.roles if r.name.casefold()==ROLE_NAME.casefold()]
            if len(roles)>1:
                await i.followup.send('Multiple roles have that name. Please rename the duplicates before setup.',ephemeral=True);return
            try:
                role=roles[0] if roles else await guild.create_role(name=ROLE_NAME,permissions=discord.Permissions.none(),mentionable=True,reason='Database workers setup requested by server manager')
                if role.managed or role.is_default() or me.top_role<=role:
                    await i.followup.send('The worker role is managed, default, or above the bot. Move the bot role above it first.',ephemeral=True);return
                if i.user.id!=guild.owner_id and i.user.top_role<=role:
                    await i.followup.send('Your highest role must be above the worker role to authorize these assignments.',ephemeral=True);return
                dangerous=('administrator','manage_guild','manage_roles','manage_channels','manage_webhooks','manage_messages','kick_members','ban_members','moderate_members','mention_everyone','manage_nicknames','manage_threads','mute_members','deafen_members','move_members')
                if any(getattr(role.permissions,p,False) for p in dangerous):
                    await i.followup.send('The existing worker role has elevated permissions. For safety, remove those permissions or use a separate non-privileged role before setup.',ephemeral=True);return
                added=[];already=[];errors=[]
                for uid in WORKERS:
                    try:
                        member=await guild.fetch_member(int(uid))
                        if role in member.roles:already.append(uid)
                        else:
                            await member.add_roles(role,reason='Database worker assignment requested by server manager');added.append(uid)
                    except (discord.NotFound,discord.Forbidden,discord.HTTPException) as e:errors.append(uid+' ('+type(e).__name__+')')
                await bot.workers.set_config('role_id',role.id)
                if not role.mentionable and not cp.mention_everyone:
                    await i.followup.send(f'Role assigned to {len(added)} members; {len(already)} already had it. Failed: {", ".join(errors) or "none"}. I could not ping the existing role because it is not mentionable. A server manager can make it mentionable, then run setup again.',ephemeral=True);return
                prompt=(f'{role.mention} Please choose a daily time to work on your assigned mech database task. '
                        f'Reply to this message and mention <@{bot.user.id}> with a time and IANA timezone, for example `8pm Asia/Kolkata`, or use `/worker_time`. '
                        'I’ll remind you daily by DM and in this channel. You can say `@bot remind me again after 10 min` '
                        'or `@bot change my reminder time to 9pm`; the extra reminder does not change the daily schedule. '
                        f'Task reference: {REFERENCE_URL}')
                sent=await channel.send(prompt,allowed_mentions=discord.AllowedMentions(everyone=False,users=False,roles=[role],replied_user=False))
                await bot.workers.set_config('prompt_message_id',sent.id)
                await i.followup.send(f'Initial prompt sent. Roles added: {len(added)}; already assigned: {len(already)}; failed: {", ".join(errors) or "none"}. Workers still need to set their own time and timezone.',ephemeral=True)
            except discord.Forbidden:
                await i.followup.send('Discord denied role assignment or channel posting. Check role hierarchy and bot permissions; some changes may already have succeeded.',ephemeral=True)
            except discord.HTTPException:
                await i.followup.send('Discord could not finish setup. Some role assignments may have succeeded; check and retry.',ephemeral=True)

    @tree.command(name='worker_time',description='Set or change your daily database task reminder')
    @app_commands.describe(time='For example 8pm or 20:30',timezone='IANA timezone, required the first time, e.g. Asia/Kolkata')
    async def worker_time(i:discord.Interaction,time:str,timezone:str=None):
        await i.response.defer(ephemeral=True)
        try:out=await bot.set_worker_time(i.user.id,time,timezone)
        except ValueError as e:out=str(e)
        await i.followup.send(out,ephemeral=True)

    @tree.command(name='worker_snooze',description='Add one extra database task reminder without changing the daily time')
    async def worker_snooze(i:discord.Interaction,minutes:app_commands.Range[int,1,10080]=10):
        await i.response.defer(ephemeral=True)
        try:out=await bot.snooze_worker(i.user.id,minutes*60)
        except ValueError as e:out=str(e)
        await i.followup.send(out,ephemeral=True)

    @tree.command(name='worker_status',description='View your database task and daily reminder status')
    async def worker_status(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            await bot.require_worker_member(i.user.id)
            row=await bot.workers.daily(i.user.id);out=assignment_text(i.user.id)
            if not row:out+='\nNo daily time set. Use /worker_time with a timezone.'
            else:
                out+=f"\nDaily: {row['hour']:02d}:{row['minute']:02d} {safe_text(row['zone'],60)}; status: {row['status']}; next <t:{row['due_at']}:F>."
                if row['last_error']:out+='\nLast delivery issue: '+safe_text(row['last_error'],150)+'. Check DMs and bot channel permissions.'
            await i.followup.send(out,ephemeral=True)
        except ValueError as e:await i.followup.send(str(e),ephemeral=True)

    @tree.command(name='worker_pause',description='Pause your daily database task reminder')
    async def worker_pause(i:discord.Interaction):
        if not get_worker(i.user.id):await i.response.send_message('This task is not assigned to your account.',ephemeral=True);return
        ok=await bot.workers.pause(i.user.id)
        await i.response.send_message('Daily task reminder paused. Existing one-off snoozes are not cancelled. Use /worker_time to resume.' if ok else 'No active daily task reminder found.',ephemeral=True)

    @tree.command(name='weapon',description='Get a concise weapon review and key stats')
    @app_commands.describe(name='Exact name and energy variant, e.g. Dreadshot 10',rank='Rank 1–6',level='Level, if known')
    async def weapon(i:discord.Interaction,name:str,rank:app_commands.Range[int,1,6]=None,level:app_commands.Range[int,1,7]=None):
        await i.response.send_message(weapon_response(bot.catalog.weapon(name,rank,level),rank,level),ephemeral=True)

    @weapon.autocomplete('name')
    async def autocomplete(i:discord.Interaction,current:str):
        choices=[n for n in bot.catalog.weapon_names if current.casefold() in n.casefold()][:25]
        return [app_commands.Choice(name=n,value=n) for n in choices]

    @tree.command(name='upgrade_cost',description='Calculate upgrade costs and optional missing-Blueprint purchase cost')
    @app_commands.describe(item='Exact weapon variant or mech name',from_rank='Current rank',to_rank='Target rank',from_level='Optional current level',to_level='Optional target level',blueprints_owned='Blueprints already owned for this item; defaults to 0')
    async def upgrade_cost(i:discord.Interaction,item:str,from_rank:app_commands.Range[int,1,6],to_rank:app_commands.Range[int,1,6],from_level:app_commands.Range[int,1,7]=None,to_level:app_commands.Range[int,1,7]=None,blueprints_owned:app_commands.Range[int,0,1000000]=0):
        if to_rank<from_rank:
            await i.response.send_message('Target rank cannot be lower than the current rank.',ephemeral=True);return
        if (from_level is None)!=(to_level is None):
            await i.response.send_message('Provide both levels, or leave both blank for the calculator’s max-level-to-next-rank transition.',ephemeral=True);return
        if to_rank==from_rank and from_level is None:
            await i.response.send_message('For a same-rank calculation, provide both the current and target levels.',ephemeral=True);return
        if to_rank==from_rank and to_level<from_level:
            await i.response.send_message('The target level cannot be lower than the current level within the same rank.',ephemeral=True);return
        result=bot.catalog.upgrade_cost(item.strip(),from_rank,to_rank,from_level,to_level)
        if not result:
            await i.response.send_message('I could not resolve a confirmed cost for that exact item, variant, and transition. I won’t substitute another variant or guess.',ephemeral=True);return
        out=upgrade_cost_response(result['item'],result,bot.currency_emojis,blueprints_owned=blueprints_owned)
        files=[] if bot.currency_emojis else bot.currency_icon_files()
        kwargs={'ephemeral':True,'allowed_mentions':discord.AllowedMentions.none()}
        if files:kwargs['files']=files
        await i.response.send_message(discord_text(out),**kwargs)

    @upgrade_cost.autocomplete('item')
    async def upgrade_cost_autocomplete(i:discord.Interaction,current:str):
        names=sorted(set(bot.catalog.weapon_names+bot.catalog.mech_names+getattr(bot.catalog,'pilot_names',[])),key=str.casefold)
        return [app_commands.Choice(name=n,value=n) for n in names if current.casefold() in n.casefold()][:25]

    @tree.command(name='mech',description='Get a mech review and user-confirmed rank stats')
    @app_commands.describe(name='Mech name',rank='Optional screenshot-stat rank, 1–7')
    async def mech(i:discord.Interaction,name:str,rank:app_commands.Range[int,1,7]=None):
        await i.response.send_message(mech_response(bot.catalog.mech_profile(name,rank=rank),rank=rank),ephemeral=True)

    @mech.autocomplete('name')
    async def mech_autocomplete(i:discord.Interaction,current:str):
        return [app_commands.Choice(name=n,value=n) for n in bot.catalog.mech_names if current.casefold() in n.casefold()][:25]

    @tree.command(name='pilot',description='Look up a pilot, bonus stats, and rank progression')
    @app_commands.describe(name='Pilot name',rank='Optional screenshot-stat rank, 1–7')
    async def pilot(i:discord.Interaction,name:str,rank:app_commands.Range[int,1,7]=None):
        await i.response.send_message(pilot_response(bot.catalog.pilot_profile(name,rank=rank),rank=rank),ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())

    @pilot.autocomplete('name')
    async def pilot_autocomplete(i:discord.Interaction,current:str):
        return [app_commands.Choice(name=n,value=n) for n in bot.catalog.pilot_names
                if current.casefold() in n.casefold()][:25]

    @tree.command(name='mod',description='Look up mod purchase and level-up costs')
    @app_commands.describe(name='Mod name and rarity',from_level='Optional current level',to_level='Optional target level')
    async def mod(i:discord.Interaction,name:str,from_level:app_commands.Range[int,1,6]=None,
                  to_level:app_commands.Range[int,1,6]=None):
        if (from_level is None)!=(to_level is None):
            await i.response.send_message('Provide both levels for an upgrade-cost calculation, or leave both blank for the full mod cost table.',ephemeral=True);return
        profile=bot.catalog.mod_profile(name)
        matches=profile.get('matches',[])
        if from_level is not None:
            if to_level<from_level:
                await i.response.send_message('The target level cannot be lower than the current level.',ephemeral=True);return
            replies=[]
            for item in matches:
                cost=bot.catalog.mod_upgrade_cost(item.get('mod_label'),item.get('rarity'),from_level,to_level)
                if cost:replies.append(mod_response({'matches':[item],'upgrade_cost':cost}))
            if not replies:
                await i.response.send_message('I could not resolve a confirmed cost for that exact mod, rarity, and level transition.',ephemeral=True);return
            out='\n\n'.join(replies)
        else:
            out=mod_response(profile)
        await i.response.send_message(discord_text(out),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @mod.autocomplete('name')
    async def mod_autocomplete(i:discord.Interaction,current:str):
        choices=getattr(bot.catalog,'mod_choices',[])
        return [app_commands.Choice(name=n,value=n) for n in choices if current.casefold() in n.casefold()][:25]

    @tree.command(name='timezone',description='Set your timezone for reminder scheduling')
    async def set_timezone(i:discord.Interaction,zone:str):
        try:tzinfo(zone)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        await bot.store.set_timezone(i.user.id,zone)
        await i.response.send_message('Timezone set to '+safe_text(zone)+'.',ephemeral=True)

    @tree.command(name='remind',description='Create a private DM reminder')
    @app_commands.describe(when='in 20 seconds or 20s; in an hour; at 8pm; tonight; tomorrow at 9am; every 2 hours; every friday at 9am; daily at 8pm; or YYYY-MM-DD HH:MM',message='What to remind you about')
    async def remind(i:discord.Interaction,when:str,message:str):
        await i.response.defer(ephemeral=True)
        try:
            # Verify a DM can currently be delivered before committing the reminder.
            await i.user.send('I’ll send your reminder here.',allowed_mentions=discord.AllowedMentions.none())
            result=await bot.create_reminder(i.user.id,when,message)
        except discord.Forbidden:result='Please enable DMs before creating a reminder.'
        except ValueError as e:result=str(e)
        await i.followup.send(result,ephemeral=True)

    @tree.command(name='reminders',description='List your upcoming or failed reminders privately')
    async def reminders(i:discord.Interaction):
        rows=await bot.store.list_reminders(i.user.id)
        lines=[f"#{x['id']} — <t:{x['due_at']}:F> ({safe_text(x['zone'],60)}; {x['kind']}; {x['status']}) — {safe_text(x['content'],100)}" for x in rows]
        await i.response.send_message(('Your reminders:\n'+'\n'.join(lines))[:1900] if lines else 'You have no active or failed reminders.',ephemeral=True)

    @tree.command(name='cancel',description='Cancel one of your reminders by ID')
    async def cancel(i:discord.Interaction,reminder_id:int):
        ok=await bot.store.cancel_reminder(i.user.id,reminder_id)
        await i.response.send_message('Reminder cancelled. An already in-flight DM may still arrive.' if ok else 'Reminder not found among your active reminders.',ephemeral=True)

    @tree.command(name='note',description='Manage your private notes')
    @app_commands.choices(action=[app_commands.Choice(name='add',value='add'),app_commands.Choice(name='list',value='list'),app_commands.Choice(name='delete',value='delete')])
    async def note(i:discord.Interaction,action:app_commands.Choice[str],text:str=None,note_id:int=None):
        if action.value=='add':
            if not text or not text.strip() or len(text)>1000:out='Supply note text between 1 and 1000 characters.'
            elif len(await bot.store.list_notes(i.user.id))>=25:out='You have 25 notes; delete one first.'
            else:out=f"Saved note #{await bot.store.add_note(i.user.id,text.strip())}."
        elif action.value=='delete':
            out='Provide note_id.' if note_id is None else ('Note deleted.' if await bot.store.delete_note(i.user.id,note_id) else 'Note not found.')
        else:
            rows=await bot.store.list_notes(i.user.id)
            out='\n'.join(f"#{x['id']} — {safe_text(x['content'],150)}" for x in rows)[:1900] if rows else 'You have no notes.'
        await i.response.send_message(out,ephemeral=True)

    @tree.command(name='calc',description='Calculate simple arithmetic')
    async def calc(i:discord.Interaction,expression:str):
        try:out='Result: '+str(calculate(expression))
        except (ValueError,SyntaxError) as e:out='Cannot calculate: '+safe_text(e,200)
        await i.response.send_message(out,ephemeral=True)

    @tree.command(name='builds',description='Filter community mech builds by squad power')
    @app_commands.describe(mech='Mech to search; omit for recent submissions',
                           sp_range='Your SP value or range, e.g. 6500 or 6000-8000')
    async def builds(i:discord.Interaction,mech:str=None,sp_range:str=None):
        requested_sp=parse_build_sp_range(sp_range,allow_bare=True) if sp_range else None
        if sp_range and requested_sp is None:
            await i.response.send_message('Give one SP value or a range, such as `6,500` or `6,000–8,000`.',ephemeral=True);return
        if mech and requested_sp is None:
            await i.response.send_message(f'What SP range are you targeting for {safe_text(mech,80)}? Re-run `/builds` with one SP value (like `6,500`) or a range (like `6,000–8,000`).',ephemeral=True);return
        if requested_sp is not None and not mech:
            await i.response.send_message('Choose a mech too so I can filter its builds by SP.',ephemeral=True);return
        await i.response.defer(ephemeral=True)
        try:
            payload=await fetch_builds(os.getenv('PORTICO_BASE_URL',BASE),mech)
            if requested_sp is not None:
                parts=builds_response_parts(payload,mech=mech,sp_range=requested_sp)
                for part in parts:
                    await i.followup.send(part,ephemeral=True,allowed_mentions=discord.AllowedMentions.none(),
                                          suppress_embeds=True)
            else:
                out=builds_response(payload,mech=mech,best=False)
                await i.followup.send(out,ephemeral=True,suppress_embeds=True)
        except Exception:
            log.exception('Public builds lookup failed')
            await i.followup.send('The public builds API is unavailable or returned an unexpected response.',ephemeral=True)

    @tree.command(name='forum',description='Search saved recent Plarium Forum threads; not an event schedule')
    @app_commands.describe(query='Optional topic, item, or phrase to search in saved forum posts')
    async def forum(i:discord.Interaction,query:str=None):
        await i.response.send_message(discord_text(bot.forum_response(query)),ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='upcoming',description='Summarize relevant saved event posts and separate event dates from post dates')
    async def upcoming(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        await i.followup.send(discord_text(await bot.upcoming_response()),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='ecr',description='Show the latest Event Crate Rush from the saved announcement facts')
    @app_commands.describe(previous='Also list the earlier ECR posts that were replaced')
    async def ecr(i:discord.Interaction,previous:bool=False):
        await i.response.defer(ephemeral=True)
        await i.followup.send(discord_text(await bot.ecr_response('previous ecrs' if previous else None)),
                              ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='facts_status',description='Show how many curated announcement facts are stored')
    async def facts_status(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        if not getattr(bot.store,'supports_announcement_facts',False):
            await i.followup.send('The curated fact table is not installed yet. Apply `migrations/20261123_announcement_facts.sql`.',
                                  ephemeral=True);return
        stats=await bot.store.announcement_fact_stats()
        state=await bot._fact_dump_state()
        if not stats:
            await i.followup.send('No curated announcement facts are stored yet. Run /facts_refresh after the next announcement sync.',
                                  ephemeral=True);return
        lines=['**Curated announcement facts**']
        total=0
        for row in stats:
            count=int(row.get('total') or 0); total+=count
            latest=row.get('latest')
            lines.append(f"• `{row['category']}` · {count} · by {row.get('extractor') or 'unknown'}"
                         + (f" · newest {time.strftime('%Y-%m-%d', time.gmtime(int(latest)))}" if latest else ''))
        ai_state=(state or '').rsplit(':',1)[-1] if state else 'never'
        lines.append(f'*{total} facts total · last dump status: `{ai_state}` · '
                     + ('AI extraction is configured' if bot.fact_extraction_enabled() else 'AI extraction is off'))
        await i.followup.send(discord_text('\n'.join(lines)),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='facts_refresh',description='Re-read every saved announcement and rebuild the curated fact table')
    @app_commands.describe(full='Clear the stored facts first instead of updating them in place',
                           ai='Also send the posts through the configured AI model for extraction')
    async def facts_refresh(i:discord.Interaction,full:bool=False,ai:bool=True):
        member=i.user if isinstance(i.user,discord.Member) else None
        allowed=bool(member and i.guild and (member.guild_permissions.manage_server or i.guild.owner_id==member.id))
        if not i.guild or not allowed:
            await i.response.send_message('Only a server manager can rebuild the announcement fact table.',ephemeral=True);return
        await i.response.defer(ephemeral=True,thinking=True)
        if not getattr(bot.store,'supports_announcement_facts',False):
            await i.followup.send('The curated fact table is not installed yet. Apply `migrations/20261123_announcement_facts.sql` first.',
                                  ephemeral=True);return
        stats=await bot.refresh_announcement_facts(use_ai=bool(ai),replace=bool(full))
        state=stats.get('status')
        if state!='ok':
            await i.followup.send(discord_text('Fact dump did not run: `'+str(state)+'`.'
                +(' Reason: '+str(stats.get('error')) if stats.get('error') else '')),ephemeral=True);return
        ai_stats=stats.get('ai') or {}
        await i.followup.send(discord_text('\n'.join([
            '**Announcement fact dump complete**',
            f"• Messages read: **{stats.get('messages_read')}** across the allowed announcement channels",
            f"• Facts written: **{stats.get('facts_written')}** · run `{stats.get('run_id')}`",
            f"• Attachment-only messages merged into their post: **{stats.get('stub_messages')}**"
             + (f" · unmatched stubs noted: **{stats.get('orphans')}**" if stats.get('orphans') else ''),
            f"• AI extraction: **{ai_stats.get('accepted',0)} accepted**, {ai_stats.get('rejected',0)} rejected "
             f"over {ai_stats.get('batches',0)} batch(es)"
             + (f" · error `{ai_stats.get('error')}`" if ai_stats.get('error') and ai_stats.get('error')!='skipped' else ''),
        ])),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='promo_codes',description='Show explicitly labeled promo-code mentions from announcements')
    async def promo_codes(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        await i.followup.send(discord_text(await bot.promo_codes_response()),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='recent',description='Read recent posts in a channel you can access')
    @app_commands.describe(channel='A server text channel',limit='1 to 10 recent messages')
    async def recent(i:discord.Interaction,channel:discord.TextChannel,limit:app_commands.Range[int,1,10]=5):
        if not i.guild or not isinstance(i.user,discord.Member) or channel.guild.id != i.guild.id:await i.response.send_message('This command works only for text channels in this server.',ephemeral=True);return
        userperm=channel.permissions_for(i.user);me=i.guild.me;botperm=channel.permissions_for(me) if me else None
        if not userperm.view_channel or not userperm.read_message_history or not botperm or not botperm.view_channel or not botperm.read_message_history:
            await i.response.send_message('Both you and the bot need access to view this channel and its history.',ephemeral=True);return
        await i.response.defer(ephemeral=True)
        try:
            lines=[]
            async for msg in channel.history(limit=limit):
                content=safe_text(msg.content or '[No readable text; may contain attachments or an embed]',180)
                lines.append(f'• <t:{int(msg.created_at.timestamp())}:d> {content} — {msg.jump_url}')
            await i.followup.send(('Recent accessible posts:\n'+'\n'.join(lines))[:1900] if lines else 'No messages found.',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:await i.followup.send('I cannot read that channel history.',ephemeral=True)

    @tree.error
    async def command_error(i:discord.Interaction,error:app_commands.AppCommandError):
        log.error('Slash command failed: %s',type(error).__name__)
        if not i.response.is_done():await i.response.send_message('That command failed. Please try again.',ephemeral=True)
        else:await i.followup.send('That command failed. Please try again.',ephemeral=True)


async def main():
    load_dotenv()
    token=os.getenv('DISCORD_TOKEN','').strip()
    if not token:raise RuntimeError('Set DISCORD_TOKEN in your environment; do not paste it into chat.')
    dsn=os.getenv('DATABASE_URL','').strip()
    local=os.getenv('ALLOW_LOCAL_STORAGE','').lower() in ('1','true','yes')
    if dsn:
        store=await Store.postgres(dsn,auto_migrate=os.getenv('AUTO_MIGRATE','').lower() in ('1','true','yes'),ssl_required=os.getenv('DATABASE_SSL','require').lower()!='disable')
    elif local:
        store=Store(sqlite_path=os.getenv('LOCAL_STORAGE_PATH','./data/local_users.sqlite3'))
        log.warning('Local SQLite selected; Railway container storage is not durable.')
    else:raise RuntimeError('Set DATABASE_URL for durable storage, or explicitly ALLOW_LOCAL_STORAGE=1 for development only.')
    try:
        catalog=Catalog(initialize_snapshot())
        if getattr(store,'supports_verified_stat_overlay',False):
            try:
                overlay_rows=await store.verified_stat_overlay()
                loaded=catalog.load_verified_stat_overlay(overlay_rows)
                if loaded:
                    log.info('Loaded %s user-confirmed pilot/mech screenshot-stat cells from the latest reviewed overlay.',loaded)
                else:
                    log.error('Supabase screenshot-stat overlay returned no complete, validated user-confirmed cells.')
            except Exception as exc:
                log.exception('Could not load the user-confirmed pilot/mech screenshot-stat overlay (%s).',type(exc).__name__)
        else:
            log.error('The private Supabase screenshot-stat overlay tables are not installed or unavailable to this runtime role.')
        bot=MechBot(catalog,store);install_commands(bot)
    except Exception:
        await store.close();raise
    async with bot:await bot.start(token)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(main())
