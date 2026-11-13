"""Discord bot entrypoint. Requires DISCORD_TOKEN and persistent PostgreSQL in production."""
import asyncio
import ast
import base64
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
from .timeparse import parse_spec, parse_natural_reminder, next_occurrence, tzinfo
from .assistant import GAME_WORDS, find_mentions, direct_item_lookup, bot_identity_response, IDENTITY_PROFILE_IDS, wants_invite_link, should_answer_unmentioned, parse_natural_calculation, natural_builds_request, parse_build_sp_range, weapon_response, mech_response, fetch_builds, builds_response, builds_response_parts, answer_question_with_brain, upgrade_cost_response, safe_text, discord_text
from .announcements import DEFAULT_ANNOUNCEMENT_CHANNELS, ANNOUNCEMENT_SYNC_SECONDS, INITIAL_BACKFILL_LIMIT, INCREMENTAL_LIMIT, EDIT_REFRESH_LIMIT, message_text, wants_upcoming, format_upcoming, wants_ecr_info, format_ecr_events, wants_promo_codes, format_promo_codes
from .workers import WorkerStore, get_worker, assignment_text, parse_worker_time, parse_worker_snooze, WORKERS, GUILD_ID, CHANNEL_ID, ROLE_NAME, REFERENCE_URL
from .payouts import PayoutTracker, fetch_snapshot, RECIPIENT, BASE
from .alerts import AlertManager, OPERATORS, MAX_POOLS_PER_TARGET, validate as validate_alert

log=logging.getLogger('mechbot')
REPLY_MENTIONS=discord.AllowedMentions(everyone=False,users=False,roles=False,replied_user=True)
AMBIENT_CHAT_COOLDOWN_SECONDS=8
BUILD_SP_PROMPT_TTL_SECONDS=600

def env_enabled(name,default=False):
    value=os.getenv(name)
    if value is None:return default
    return value.strip().casefold() in ('1','true','yes','on')

def configured_announcement_channels():
    raw=os.getenv('ANNOUNCEMENT_CHANNEL_IDS','').replace(';',',')
    values=[]
    for item in raw.split(','):
        if not item.strip():continue
        try:channel_id=int(item.strip())
        except ValueError:
            log.warning('Ignoring an invalid announcement channel ID; no value or secret was logged.')
            continue
        if channel_id>0 and channel_id not in values:values.append(channel_id)
    return tuple(values) or DEFAULT_ANNOUNCEMENT_CHANNELS

ANNOUNCEMENT_CHANNELS=configured_announcement_channels()
# Fine-grained permissions: View Channels, Send Messages, Embed Links, Read History,
# Manage Roles, Use Slash Commands, and Send Messages in Threads. No Administrator.
INVITE_URL='https://discord.com/oauth2/authorize?client_id=1570061533898211408&permissions=277293911040&integration_type=0&scope=bot%20applications.commands'
BOT_ROOT=Path(__file__).resolve().parent.parent
CURRENCY_ASSETS=(('A-Coins','emoji_a_coins.png'),('Credits','emoji_credits.png'),('Blueprints (BP)','emoji_blueprints.png'))
APPLICATION_CURRENCY_EMOJIS=(('acoins','archangle_a_coins_v2','emoji_a_coins.png'),('credits','archangle_credits_v2','emoji_credits.png'),('blueprints','archangle_blueprints_v2','emoji_blueprints.png'))
HELP='''**Mech Arena helper**
Use `/help` anytime to see this command list.
• `/invite` — get the link to add me to a server.
• `/weapon name rank level` — a concise weapon review, key stats, perk and listed purchase price.
• `/upgrade_cost item from_rank to_rank [from_level to_level] [blueprints_owned]` — calculate upgrade costs and price missing Blueprints; defaults to 0 owned.
• `/mech name` — a short mech review with key details and listed purchase price.
• `/builds [mech] [sp_range]` — browse recent builds or filter all mech builds by your SP range.
• `/upcoming` — check dated and recent event posts from the configured announcement channels.
• `/ecr` — check saved Event Crate Rush event updates; ECR event info is separate from promo codes.
• `/promo_codes` — check the latest saved, explicitly labeled promo-code posts; validity is not guaranteed.
• `/recent` — read recent posts in a channel you can access.
• `/timezone` — set your IANA timezone; default is UTC.
• `/remind`, `/reminders`, `/cancel` — private DM reminders; recurring reminders are supported.
• `/note` — add, list or delete private notes.
• `/worker_time`, `/worker_status`, `/worker_snooze`, `/worker_pause` — manage assigned database-task reminders.
• `/worker_setup` — set up the worker role and send the initial time request.
• `/stop_loop` — stop your own active ping loops.
• `/calc` — calculate simple arithmetic; you can also ask “what’s 240 plus 60?” or “calculate 240 / 60”.
• Ask naturally for “latest ECR event” or “latest promo code”—these are separate lookups—plus upgrade costs, builds, event dates, or reminders such as “remind me in 20 minutes to check the event”. Reminder requests must include when and what; they are delivered privately by DM.
• Channel questions — when the server has opted into natural chat, ask normally without a bot mention. Only apparent questions/lookups and explicit, fully specified reminder requests get a reply; ambient replies do not ping the author and have a short per-user cooldown.
You can also mention me: `@bot remind me in 20 seconds to check the event`, `@bot review EM Rifle 10`, `@bot how much to upgrade Redox from rank 4 to 5?`, or `@bot how do I invite you?`. Reviews and upgrade costs use confirmed game data; missing details are never guessed.'''


def help_text(user_id=None,guild_id=None):
    if user_id in OPERATORS and guild_id is not None:
        restricted=('• `/ping_loop member interval_seconds duration_seconds reason` — send an immediate, single-user ping, then bounded follow-up pings.\n'
                    '• `/dm_loop member interval_seconds duration_seconds reason` — start a bounded DM loop.\n'
                    'Each command starts an independent loop pool; a member may have at most three active pools across both destinations.\n'
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


class MechBot(commands.Bot):
    def __init__(self, catalog, store):
        # Ambient channel questions and announcement ingestion both require the privileged
        # Message Content intent; ambient replies remain opt-in and are separately gated.
        intents=discord.Intents.default()
        self.announcement_ingestion_enabled=env_enabled('ANNOUNCEMENT_INGESTION_ENABLED',True)
        self.ambient_channel_chat_enabled=env_enabled('MESSAGE_CONTENT_ENABLED',False)
        self.message_content_enabled=self.ambient_channel_chat_enabled or self.announcement_ingestion_enabled
        intents.message_content=self.message_content_enabled
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.catalog=catalog;self.store=store;self.workers=WorkerStore(store);self.payouts=PayoutTracker(store);self.alerts=AlertManager()
        self.runner=None;self.worker_runner=None;self.payout_runner=None;self.announcement_runner=None;self.conversation_runner=None
        self.emoji_runner=None;self.command_sync_task=None;self.worker_setup_lock=asyncio.Lock();self.announcement_access={}
        self.ai_session=None;self.currency_emojis={};self.ambient_last_reply={};self.pending_build_requests={}

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

    async def close(self):
        await self.alerts.close()
        tasks=(self.command_sync_task,self.runner,self.worker_runner,self.payout_runner,self.announcement_runner,
               self.conversation_runner,self.emoji_runner)
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
        if not self.announcement_ingestion_enabled or not self.intents.message_content:
            self._set_announcement_access(channel_id,'message_content_intent_disabled')
            return 0
        if not getattr(self.store,'supports_announcements',True):
            self._set_announcement_access(channel_id,'private_table_not_installed')
            return 0
        try:
            channel=self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not hasattr(channel,'history') or not getattr(channel,'guild',None):
                self._set_announcement_access(channel_id,'not_a_server_message_channel')
                return 0
            member=getattr(channel.guild,'me',None)
            if member and hasattr(channel,'permissions_for'):
                permissions=channel.permissions_for(member)
                if not permissions.view_channel or not permissions.read_message_history:
                    self._set_announcement_access(channel_id,'view_channel_or_read_history_missing')
                    return 0
            cursor=await self.store.announcement_cursor(channel_id)
            messages={}
            if cursor:
                async for message in channel.history(limit=INCREMENTAL_LIMIT,after=discord.Object(id=cursor),oldest_first=True):
                    messages[int(message.id)]=message
            else:
                async for message in channel.history(limit=INITIAL_BACKFILL_LIMIT,oldest_first=False):
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
            await self.store.upsert_announcements(records)
            self._set_announcement_access(channel_id,'available')
            return len(records)
        except discord.Forbidden:
            self._set_announcement_access(channel_id,'permission_denied')
        except discord.NotFound:
            self._set_announcement_access(channel_id,'not_found_or_not_a_member')
        except discord.HTTPException as exc:
            self._set_announcement_access(channel_id,'discord_http_'+type(exc).__name__)
        return 0

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
            await asyncio.sleep(ANNOUNCEMENT_SYNC_SECONDS)

    async def upcoming_response(self):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        if not getattr(self.store,'supports_announcements',True):
            return 'Announcement storage is not installed yet. The private database migration is required before ingestion can be enabled.'
        try:
            rows=await self.store.recent_announcements(int(time.time())-365*86400,limit=1000)
            allowed={str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS}
            rows=[row for row in rows if str(row.get('channel_id') or '') in allowed]
        except Exception as exc:
            log.warning('Could not read saved announcement records (%s).',type(exc).__name__)
            return 'I can’t read the saved announcements right now. I’ll retry when the private storage is available.'
        return format_upcoming(rows,enabled=enabled,access_states=self.announcement_access)

    async def ecr_response(self):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        if not getattr(self.store,'supports_announcements',True):
            return 'ECR event lookup needs the private announcement-storage migration before it can read saved posts.'
        try:
            rows=await self.store.recent_announcements(int(time.time())-365*86400,limit=2000)
            allowed={str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS}
            rows=[row for row in rows if str(row.get('channel_id') or '') in allowed]
        except Exception as exc:
            log.warning('Could not read saved ECR event posts (%s).',type(exc).__name__)
            return 'I can’t read saved ECR event posts right now. I’ll retry when private storage is available.'
        return format_ecr_events(rows,enabled=enabled,access_states=self.announcement_access)

    async def promo_codes_response(self):
        enabled=(self.announcement_ingestion_enabled and self.intents.message_content
                 and getattr(self.store,'supports_announcements',True))
        if not getattr(self.store,'supports_announcements',True):
            return 'Promo-code lookup needs the private announcement-storage migration before it can read saved posts.'
        try:
            rows=await self.store.recent_announcements(0,limit=2000)
            allowed={str(channel_id) for channel_id in ANNOUNCEMENT_CHANNELS}
            rows=[row for row in rows if str(row.get('channel_id') or '') in allowed]
        except Exception as exc:
            log.warning('Could not read saved promo-code source posts (%s).',type(exc).__name__)
            return 'I can’t read the saved announcement posts right now. I’ll retry when private storage is available.'
        return format_promo_codes(rows,enabled=enabled,access_states=self.announcement_access)

    async def payout_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                day,counts=await fetch_snapshot(os.getenv('PORTICO_BASE_URL',BASE))
                for uid,count in counts.items():await self.payouts.observe(uid,day,count)
            except Exception:log.exception('Payout polling failed; will retry')
            try:
                for row in await self.payouts.pending():
                    uid,day,count=row['user_id'],row['day'],row['observed_count']
                    claim=await self.payouts.claim(uid,day,count)
                    if not claim:continue
                    token,previous=claim
                    try:
                        owner=self.get_user(RECIPIENT) or await self.fetch_user(RECIPIENT)
                        verb='reached the 10-build target' if previous==0 else 'increased their total'
                        await owner.send(f'📊 Build update: Discord user ID `{uid}` {verb}. Total for payout day {day}: **{count} builds** (Asia/Kolkata). Source: public Portico payout API.',allowed_mentions=discord.AllowedMentions.none())
                        await self.payouts.delivered(uid,day,count,token)
                    except Exception:
                        await self.payouts.failed(uid,day,token)
                        log.exception('Could not DM payout update to configured recipient')
            except Exception:log.exception('Payout delivery failed; will retry')
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
        zone=await self.store.get_timezone(user_id)
        parsed=parse_spec(spec,zone)
        rid=await self.store.create_reminder(user_id,content,parsed,zone)
        return f'Reminder #{rid} set for <t:{parsed[0]}:F> ({safe_text(zone)}). I will send a DM. Make sure your DMs are open.'

    def currency_icon_files(self):
        files=[]
        for label,filename in CURRENCY_ASSETS:
            path=BOT_ROOT/'data'/filename
            if path.is_file():files.append(discord.File(path,filename=filename,description=label+' icon'))
        return files

    async def reply_message(self,message,text,with_currency_icons=False,mention_author=True,allowed_user_ids=()):
        kwargs={}
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
        await message.reply(discord_text(text),mention_author=mention_author,allowed_mentions=allowed,**kwargs)

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
            names=tuple(getattr(self.catalog,'weapon_names',()))+tuple(getattr(self.catalog,'mech_names',()))
            # Only fully specified natural reminder requests may bypass the ambient question gate;
            # the reminder itself is private and never pings the channel author.
            if (not should_answer_unmentioned(text,names) and not parsed_reminder) or self.ambient_chat_rate_limited(message):return
            ambient_question=True
        if not text:return
        try:
            identity=bot_identity_response(text)
            if identity:
                identity_user_ids=[int(user_id) for user_id in re.findall(r'<@!?(\d+)>',identity)
                                   if int(user_id) in IDENTITY_PROFILE_IDS]
                await self.reply_message(message,identity,mention_author=not ambient_question,
                                         allowed_user_ids=identity_user_ids);return
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
                await self.private_reply(message,'I can set that. Include when and what, for example: `remind me in 20 seconds to write notes`.');return
            if re.fullmatch(r'(?:help|commands|show\s+help|what(?:\s+features)?\s+can\s+you\s+do|what\s+can\s+you\s+help\s+with)[?!.]*',text,re.I):
                out=help_text(message.author.id,message.guild.id if message.guild else None)
                if ambient_question:await self.reply_message(message,out,mention_author=False)
                else:await self.private_reply(message,out)
                return
            if wants_promo_codes(text):
                await self.reply_message(message,await self.promo_codes_response(),mention_author=not ambient_question);return
            if wants_ecr_info(text):
                await self.reply_message(message,await self.ecr_response(),mention_author=not ambient_question);return
            if wants_upcoming(text):
                await self.reply_message(message,await self.upcoming_response(),mention_author=not ambient_question);return
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
                    await self.reply_message(message,part,mention_author=not ambient_question and index==0)
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
                            await self.reply_message(message,part,mention_author=not ambient_question and index==0)
                    else:
                        await self.reply_message(message,out,mention_author=not ambient_question)
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
            # Exact catalog lookups are local, so they avoid unnecessary provider round trips.
            weapon_name=direct_item_lookup(text,self.catalog.weapon_names)
            if weapon_name:
                rank_match=re.search(r'\brank\s*(\d+)\b',text,re.I);level_match=re.search(r'\blevel\s*(\d+)\b',text,re.I)
                rank=int(rank_match[1]) if rank_match else None;level=int(level_match[1]) if level_match else None
                out=weapon_response(self.catalog.weapon(weapon_name,rank,level),rank,level)
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            mech_name=direct_item_lookup(text,self.catalog.mech_names)
            if mech_name:
                out=mech_response(self.catalog.mech_profile(mech_name))
                await self.conversation_turn_reply(message,text,out,ambient=ambient_question);return
            conversation_history=await self.conversation_history_for(message)
            reply=await answer_question_with_brain(os.getenv('AI_BASE_URL','https://api.openai.com/v1'),
                os.getenv('AI_API_KEY',''),os.getenv('AI_MODEL',''),text,self.catalog,session=self.ai_session,
                conversation_history=conversation_history,currency_emoji_map=self.currency_emojis)
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
    @tree.command(name='help',description='Show bot features and examples')
    async def help_cmd(i:discord.Interaction):await i.response.send_message(help_text(i.user.id,i.guild_id),ephemeral=True)

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
            await i.response.send_message(f'This member already has {MAX_POOLS_PER_TARGET} active loop pools across channel and DM destinations. Stop one before adding another.',ephemeral=True);return
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
            await i.response.send_message(f'This member already has {MAX_POOLS_PER_TARGET} active loop pools across channel and DM destinations. Stop one before adding another.',ephemeral=True);return
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
        names=sorted(set(bot.catalog.weapon_names+bot.catalog.mech_names),key=str.casefold)
        return [app_commands.Choice(name=n,value=n) for n in names if current.casefold() in n.casefold()][:25]

    @tree.command(name='mech',description='Get a short mech review and key details')
    async def mech(i:discord.Interaction,name:str):
        await i.response.send_message(mech_response(bot.catalog.mech_profile(name)),ephemeral=True)

    @mech.autocomplete('name')
    async def mech_autocomplete(i:discord.Interaction,current:str):
        return [app_commands.Choice(name=n,value=n) for n in bot.catalog.mech_names if current.casefold() in n.casefold()][:25]

    @tree.command(name='timezone',description='Set your timezone for reminder scheduling')
    async def set_timezone(i:discord.Interaction,zone:str):
        try:tzinfo(zone)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        await bot.store.set_timezone(i.user.id,zone)
        await i.response.send_message('Timezone set to '+safe_text(zone)+'.',ephemeral=True)

    @tree.command(name='remind',description='Create a private DM reminder')
    @app_commands.describe(when='in 20 seconds or 20s; every 2 hours; daily at 8pm; or YYYY-MM-DD HH:MM',message='What to remind you about')
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
                for part in parts:await i.followup.send(part,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            else:
                out=builds_response(payload,mech=mech,best=False)
                await i.followup.send(out,ephemeral=True)
        except Exception:
            log.exception('Public builds lookup failed')
            await i.followup.send('The public builds API is unavailable or returned an unexpected response.',ephemeral=True)

    @tree.command(name='upcoming',description='Show upcoming events in the configured announcement channels')
    async def upcoming(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        await i.followup.send(discord_text(await bot.upcoming_response()),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name='ecr',description='Show Event Crate Rush event updates, separate from promo codes')
    async def ecr(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        await i.followup.send(discord_text(await bot.ecr_response()),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

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
        catalog=Catalog(initialize_snapshot());bot=MechBot(catalog,store);install_commands(bot)
    except Exception:
        await store.close();raise
    async with bot:await bot.start(token)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(main())
