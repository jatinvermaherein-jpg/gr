"""Discord bot entrypoint. Requires DISCORD_TOKEN and persistent PostgreSQL in production."""
import asyncio
import ast
import logging
import operator
import os
import re
import time
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from .catalog import Catalog, initialize_snapshot
from .store import Store
from .timeparse import parse_spec, parse_natural_reminder, next_occurrence, tzinfo
from .assistant import GAME_WORDS, find_mentions, weapon_response, mech_response, fetch_builds, builds_response, general_ai, safe_text
from .workers import WorkerStore, get_worker, assignment_text, parse_worker_time, parse_worker_snooze, WORKERS, GUILD_ID, CHANNEL_ID, ROLE_NAME, REFERENCE_URL
from .payouts import PayoutTracker, fetch_snapshot, RECIPIENT, BASE
from .emergency import EmergencyManager, OPERATORS, validate as validate_emergency

log=logging.getLogger('mechbot')
HELP='''**Mech Arena bot**
• `/weapon name rank level` — catalog identity and reviewed stats only.
• `/mech name` — mech identity from the staged catalog.
• `/builds` — builds from the public website, if available.
• `/recent` — recent posts from a channel you can read.
• `/timezone` — set your IANA timezone; default is UTC.
• `/remind`, `/reminders`, `/cancel` — private DM reminders; recurring supported.
• `/note` — private notes, list or delete.
• `/worker_time`, `/worker_status`, `/worker_snooze`, `/worker_pause` — assigned database worker reminders.
• `/worker_setup` — server manager assigns the worker role and sends the initial prompt.
• `/emergency_channel`, `/emergency_dm`, `/emergency_stop` and `/emergency_stop_user` — bounded emergency alerts.
• `/calc` — simple arithmetic.
You can also mention me: `@bot remind me in 10 minutes to check the event`, `@bot what is Dreadshot 10?`, or ask a general question if an AI provider is configured.
Screenshot stats are unverified and are never presented as verified facts.'''


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
        intents=discord.Intents.default();intents.message_content=True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.catalog=catalog;self.store=store;self.workers=WorkerStore(store);self.payouts=PayoutTracker(store);self.emergency=EmergencyManager();self.runner=None;self.worker_runner=None;self.payout_runner=None;self.worker_setup_lock=asyncio.Lock()

    async def setup_hook(self):
        guild=os.getenv('TEST_GUILD_ID','').strip()
        if guild:
            target=discord.Object(id=int(guild));self.tree.copy_global_to(guild=target);await self.tree.sync(guild=target)
            log.info('Synced slash commands to configured test guild.')
        else:
            await self.tree.sync();log.info('Synced global slash commands; propagation can take time.')
        self.runner=asyncio.create_task(self.delivery_loop())
        self.worker_runner=asyncio.create_task(self.worker_delivery_loop())
        self.payout_runner=asyncio.create_task(self.payout_loop())

    async def close(self):
        await self.emergency.close()
        for task in (self.runner,self.worker_runner,self.payout_runner):
            if task:task.cancel()
        for task in (self.runner,self.worker_runner,self.payout_runner):
            if task:
                try:await task
                except asyncio.CancelledError:pass
        await self.store.close();self.catalog.close();await super().close()

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

    async def private_reply(self,message,text,saved=False):
        if isinstance(message.channel,discord.DMChannel):
            await message.channel.send(text,allowed_mentions=discord.AllowedMentions.none());return
        try:
            await message.author.send(text,allowed_mentions=discord.AllowedMentions.none())
            await message.reply('I sent you a private DM.',mention_author=False,allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            reply='Saved, but I could not DM you. Please enable DMs; I will still try the task channel.' if saved else 'I could not DM you. Please enable DMs and try again.'
            await message.reply(reply,mention_author=False,allowed_mentions=discord.AllowedMentions.none())

    async def on_message(self,message):
        if message.author.bot or not self.user:return
        is_dm=isinstance(message.channel,discord.DMChannel)
        in_worker_server=bool(message.guild and message.guild.id==GUILD_ID)
        worker_context=bool(get_worker(message.author.id) and (is_dm or in_worker_server))
        prompt_reply=False
        if worker_context and not is_dm and message.channel.id==CHANNEL_ID and message.reference and message.reference.message_id:
            prompt_reply=str(message.reference.message_id)==await self.workers.get_config('prompt_message_id')
        if not is_dm and self.user not in message.mentions and not prompt_reply:return
        text=re.sub(r'<@!?'+str(self.user.id)+r'>','',message.content).strip()
        if not text:return
        try:
            if re.fullmatch(r'(?:stop|cancel)\s+(?:my\s+)?emergency(?:\s+alerts?)?',text,re.I):
                count=self.emergency.stop(message.author.id,message.author.id)
                await self.private_reply(message,f'Stop requested for {count} emergency alert(s). An in-flight message may still arrive.');return
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
            parsed=parse_natural_reminder(text)
            if parsed:
                # Check DM availability before creating, especially for private requests.
                try:await message.author.send('I’ll send your reminder here.',allowed_mentions=discord.AllowedMentions.none())
                except discord.Forbidden:
                    await message.reply('Please enable DMs so I can deliver reminders.',mention_author=False,allowed_mentions=discord.AllowedMentions.none());return
                out=await self.create_reminder(message.author.id,*parsed)
                await self.private_reply(message,out);return
            if re.match(r'^remind\s+me\b',text,re.I):
                await self.private_reply(message,'Please use “remind me in 10 minutes to …”, “remind me every 2 hours to …”, or “remind me every day at 8pm to …”. Set /timezone first for local times.');return
            if text.lower() in ('help','commands','what can you do?'):
                await self.private_reply(message,HELP);return
            if re.fullmatch(r'(?:show|list)\s+(?:my\s+)?reminders',text,re.I):
                rows=await self.store.list_reminders(message.author.id)
                lines=[f"#{r['id']} — <t:{r['due_at']}:F> ({r['status']}) — {safe_text(r['content'],120)}" for r in rows]
                await self.private_reply(message,('Your reminders:\n'+'\n'.join(lines))[:1900] if lines else 'You have no active or failed reminders.');return
            m=re.fullmatch(r'cancel\s+reminder\s+#?(\d+)',text,re.I)
            if m:
                ok=await self.store.cancel_reminder(message.author.id,int(m[1]))
                await self.private_reply(message,'Reminder cancelled; an in-flight DM may still arrive.' if ok else 'Reminder not found.');return
            if re.fullmatch(r'(?:show|list)\s+(?:my\s+)?notes',text,re.I):
                rows=await self.store.list_notes(message.author.id)
                await self.private_reply(message,('\n'.join(f"#{r['id']} — {safe_text(r['content'],150)}" for r in rows))[:1900] if rows else 'You have no notes.');return
            m=re.fullmatch(r'(?:remember\s+that|note)\s+(.+)',text,re.I)
            if m:
                content=m[1].strip()
                if not content or len(content)>1000:raise ValueError('Note must be between 1 and 1000 characters.')
                if len(await self.store.list_notes(message.author.id))>=25:raise ValueError('You have 25 notes; delete one first.')
                # Do not create a private note when the user cannot receive the confirmation.
                if not is_dm:
                    try:await message.author.send('I can save your private note here.',allowed_mentions=discord.AllowedMentions.none())
                    except discord.Forbidden:
                        await message.reply('Please enable DMs so I can confirm private notes.',mention_author=False,allowed_mentions=discord.AllowedMentions.none());return
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
            names=find_mentions(text,self.catalog.weapon_names)
            if names:
                m=re.search(r'\brank\s*(\d+)\b',text,re.I);l=re.search(r'\blevel\s*(\d+)\b',text,re.I)
                rank=int(m[1]) if m else None;level=int(l[1]) if l else None
                out='\n\n'.join(weapon_response(self.catalog.weapon(n,rank,level),rank,level) for n in names[:3])
                await message.reply(out[:1900],mention_author=False,allowed_mentions=discord.AllowedMentions.none());return
            names=find_mentions(text,self.catalog.mech_names)
            if names:
                out='\n\n'.join(mech_response(self.catalog.mech(n)) for n in names[:3])
                await message.reply(out[:1900],mention_author=False,allowed_mentions=discord.AllowedMentions.none());return
            if GAME_WORDS.search(text):
                await message.reply('For Mech Arena help, use `/weapon`, `/mech`, or `/builds`, and include an exact item name. I won’t invent stats or loadouts.',mention_author=False,allowed_mentions=discord.AllowedMentions.none());return
            reply=await general_ai(os.getenv('AI_BASE_URL','https://api.openai.com/v1'),os.getenv('AI_API_KEY',''),os.getenv('AI_MODEL',''),text)
            if reply and (find_mentions(reply,self.catalog.weapon_names) or find_mentions(reply,self.catalog.mech_names)):
                reply='For Mech Arena facts, use /weapon or /mech so I can use sources rather than guessing.'
            await message.reply(reply or 'General AI is not configured yet. Use /help for the bot’s available features.',mention_author=False,allowed_mentions=discord.AllowedMentions.none())
        except ValueError as e:await self.private_reply(message,safe_text(e))
        except Exception:
            log.exception('Mention handling failed')
            await self.private_reply(message,'Sorry, that request failed. Please try again or use /help.')


def install_commands(bot):
    tree=bot.tree
    @tree.command(name='help',description='Show bot features and examples')
    async def help_cmd(i:discord.Interaction):await i.response.send_message(HELP,ephemeral=True)

    @tree.command(name='emergency_channel',description='Start bounded emergency pings in this channel')
    @app_commands.describe(member='Member to alert',interval_seconds='At least 5 seconds',duration_seconds='Maximum 1800 seconds',reason='Short emergency message')
    async def emergency_channel(i:discord.Interaction,member:discord.Member,interval_seconds:app_commands.Range[int,5,1800],duration_seconds:app_commands.Range[int,5,1800],reason:str='Please check this emergency'):
        if i.user.id not in OPERATORS or not i.guild or i.guild.id!=GUILD_ID:
            await i.response.send_message('Only the two authorized IDs may start alerts in the configured server.',ephemeral=True);return
        if member.bot or not isinstance(i.channel,discord.TextChannel) or not i.guild.me:
            await i.response.send_message('Choose a human member and a server text channel.',ephemeral=True);return
        perms=i.channel.permissions_for(i.guild.me);targetperms=i.channel.permissions_for(member)
        if not (perms.view_channel and perms.send_messages and perms.read_message_history and targetperms.view_channel):
            await i.response.send_message('The bot needs channel access, Send Messages and Read History; the target must be able to view the channel.',ephemeral=True);return
        try:validate_emergency(interval_seconds,duration_seconds)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        if not bot.emergency.available(member.id,'channel'):
            await i.response.send_message('An alert is already active for that member and mode, or the five-alert limit has been reached.',ephemeral=True);return
        reason=safe_text(reason.strip() or 'Please check this emergency',180)
        await i.response.send_message(f'🚨 Emergency alert queued for user `{member.id}`: every {interval_seconds}s for up to {duration_seconds}s. Stop with /emergency_stop.',allowed_mentions=discord.AllowedMentions.none())
        try:
            anchor=await i.original_response()
            bot.emergency.start(member,'channel',anchor,interval_seconds,duration_seconds,i.user.id,reason)
        except ValueError as e:await i.followup.send('Alert not started: '+str(e),ephemeral=True)

    @tree.command(name='emergency_dm',description='Start bounded emergency alerts by direct message')
    @app_commands.describe(member='Member to alert',interval_seconds='At least 5 seconds',duration_seconds='Maximum 1800 seconds',reason='Short emergency message')
    async def emergency_dm(i:discord.Interaction,member:discord.Member,interval_seconds:app_commands.Range[int,5,1800],duration_seconds:app_commands.Range[int,5,1800],reason:str='Please check this emergency'):
        if i.user.id not in OPERATORS or not i.guild or i.guild.id!=GUILD_ID:
            await i.response.send_message('Only the two authorized IDs may start alerts in the configured server.',ephemeral=True);return
        if member.bot:
            await i.response.send_message('Choose a human member.',ephemeral=True);return
        try:validate_emergency(interval_seconds,duration_seconds)
        except ValueError as e:await i.response.send_message(str(e),ephemeral=True);return
        if not bot.emergency.available(member.id,'dm'):
            await i.response.send_message('An alert is already active for that member and mode, or the five-alert limit has been reached.',ephemeral=True);return
        reason=safe_text(reason.strip() or 'Please check this emergency',180)
        await i.response.defer(ephemeral=True)
        try:
            anchor=await member.send(f'🚨 An emergency alert was started by a server operator: {reason}. Repeated replies will arrive every {interval_seconds}s for up to {duration_seconds}s. Send `stop emergency` in this DM or use /emergency_stop to stop.',allowed_mentions=discord.AllowedMentions.none())
            key=bot.emergency.start(member,'dm',anchor,interval_seconds,duration_seconds,i.user.id,reason)
            await i.followup.send(f'DM alert {key} started. The target or an authorized operator can use /emergency_stop.',ephemeral=True)
        except discord.HTTPException:await i.followup.send('Could not send the initial DM. The alert was not started; the member may have DMs closed.',ephemeral=True)
        except ValueError as e:await i.followup.send('Alert not started: '+str(e),ephemeral=True)

    @tree.command(name='emergency_stop',description='Stop your emergency alerts or, if authorized, alerts for a member')
    @app_commands.describe(member='Optional member to stop; only the authorized operators may stop others')
    async def emergency_stop(i:discord.Interaction,member:discord.User=None):
        if member and member.id!=i.user.id and i.user.id not in OPERATORS:
            await i.response.send_message('You can only stop alerts directed at yourself.',ephemeral=True);return
        target=member.id if member else i.user.id
        count=bot.emergency.stop(i.user.id,target)
        await i.response.send_message(f'Stop requested for {count} active channel and DM alert(s) for user `{target}`. An in-flight message may still arrive.',ephemeral=True)

    @tree.command(name='emergency_stop_user',description='Stop all channel and DM emergency alerts for one user')
    @app_commands.describe(member='The user whose ongoing alerts should stop')
    async def emergency_stop_user(i:discord.Interaction,member:discord.User):
        if i.user.id not in OPERATORS:
            await i.response.send_message('Only the two authorized operators may stop alerts for another user. Use /emergency_stop for your own alerts.',ephemeral=True);return
        count=bot.emergency.stop(i.user.id,member.id)
        await i.response.send_message(f'Stop requested for {count} channel and DM alert(s) for user `{member.id}`. An in-flight message may still arrive.',ephemeral=True)

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
                        'Reply to this message with a time and IANA timezone, for example `8pm Asia/Kolkata`, or use `/worker_time`. '
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

    @tree.command(name='weapon',description='Look up a weapon variant and reviewed stats')
    @app_commands.describe(name='Exact name and energy variant, e.g. Dreadshot 10',rank='Rank 1–6',level='Level, if known')
    async def weapon(i:discord.Interaction,name:str,rank:app_commands.Range[int,1,6]=None,level:app_commands.Range[int,1,7]=None):
        await i.response.send_message(weapon_response(bot.catalog.weapon(name,rank,level),rank,level),ephemeral=True)

    @weapon.autocomplete('name')
    async def autocomplete(i:discord.Interaction,current:str):
        choices=[n for n in bot.catalog.weapon_names if current.casefold() in n.casefold()][:25]
        return [app_commands.Choice(name=n,value=n) for n in choices]

    @tree.command(name='mech',description='Look up a mech in the staged catalog')
    async def mech(i:discord.Interaction,name:str):
        await i.response.send_message(mech_response(bot.catalog.mech(name)),ephemeral=True)

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
    @app_commands.describe(when='in 10 minutes; every 2 hours; daily at 8pm; or YYYY-MM-DD HH:MM',message='What to remind you about')
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

    @tree.command(name='builds',description='Retrieve builds from the public website API')
    async def builds(i:discord.Interaction,mech:str=None):
        await i.response.defer(ephemeral=True)
        try:out=builds_response(await fetch_builds(os.getenv('PORTICO_BASE_URL',BASE),mech))
        except Exception:log.exception('Public builds lookup failed');out='The public builds API is unavailable or returned an unexpected response.'
        await i.followup.send(out,ephemeral=True)

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
