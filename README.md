# Mech Arena Discord Bot

A community helper bot with weapon/mech lookups, reminders, private notes, calculations, public-build lookup, worker schedules, payout monitoring, and bounded emergency alerts. It is deployed as a separate Railway worker. Local tests cover business logic with SQLite and mocks; they do not replace live Discord, Railway, or PostgreSQL checks.

## Payout reporting

- Polls `https://portico-hq-live.netlify.app/api/mech-arena/public/payouts` about once per minute. Only public aggregate data is read. It monitors all valid Discord IDs reported by the API, not just the nine workers.
- The API reports `today.builds` and a payout day in **Asia/Kolkata**. When the first observed count is 10 or more, the bot DMs **1442058583129722986** with the user's ID, day and total. If it observes a larger count later that day, it sends the new total. Counts are snapshots: changes between polls may be combined. Counts dropping and later recovering to an already-reported high are not re-reported. No payment is executed.
- Persisted high-water marks suppress ordinary repeat notifications across restarts. Pending, previously observed alerts survive a day rollover and retry if the owner's DMs fail. A crash after Discord accepts a DM but before the database confirms it can produce a duplicate on recovery; exactly-once delivery is not guaranteed.
- The endpoint only exposes today's live count in this integration. If the bot or site is down for an entire payout day, it may miss that day. API errors are logged and retried; unexpected response shapes are not treated as completions. Make sure the recipient can receive the bot's DMs.

## Emergency alerts

Only **1444891876376186901** and **1442058583129722986** can start alerts, and only in the configured server. They select a target, interval, duration and optional reason:

- `/emergency_channel member interval_seconds duration_seconds reason` starts in the channel where the command is invoked. A public bot interaction response is the anchor; each subsequent ping is a reply to it and explicitly mentions only the selected member. The bot and recipient must be able to view the channel; bot must be able to send and read history.
- `/emergency_dm member interval_seconds duration_seconds reason` sends an initial bot DM, then replies to that DM. Discord does not allow a DM to reply to a server interaction across channels. DMs notify the recipient without an @mention. If the recipient blocks DMs the alert does not start.
- `/emergency_stop_user member:@user` lets either authorized operator stop **all channel and DM alerts for that selected user** without stopping alerts for other people. `/emergency_stop` stops the caller's own alerts, and authorized operators may also choose a target with `/emergency_stop member:@user`. Targets can send `stop emergency` to the bot by DM to stop their own alerts. One already in-flight message may still arrive.
- To reduce abuse, interval is **at least 5 seconds**, duration is **at most 30 minutes**, and at most **300 repeated pings per alert** are allowed. The initial DM anchor is an additional notification. If the chosen duration and interval would exceed 300 pings, the command is rejected; for example, use a 5-second interval for at most 1,500 seconds (25 minutes). Only one alert per target and destination mode and at most five active alerts overall. No guarantee of exact timing or spam-free delivery: Discord rate limits and permissions apply, and alerts stop on delivery failure. Restarting the bot cancels active emergency alerts; they are deliberately not resumed.

## Nine database workers

`data/workers.json` contains the nine fixed Discord IDs and 47 assigned mechs; Voidghost is S and Biform is excluded. The target server is `1509560571563868160`, target channel `1526008373009715313`.

1. A server manager with Manage Roles runs `/worker_setup` in that server. The bot checks for the role `bot database workers`; creates it if missing, assigns it to the nine members it can find, and posts a role-ping request in the task channel. It reports missing members and assignment failures privately. Re-running can post another prompt. Duplicate same-named roles require manual correction; the bot refuses elevated-permission roles and hierarchy violations. Existing role must be mentionable or the bot must be allowed to mention roles in the channel.
2. Each worker responds to the bot's posted request with e.g. `8pm Asia/Kolkata`, DMs the bot, mentions it, or uses `/worker_time time:8pm timezone:Asia/Kolkata`. The timezone is mandatory initially. Their time and timezone are stored privately; the confirmation includes their assigned group.
3. The daily reminder is attempted in both DM and the linked channel and includes assigned mechs. Failures do not prevent the other destination from being attempted. `/worker_status` privately reports the last delivery issue; next daily occurrences continue. A worker who leaves the server has their daily schedule paused. DST gaps are skipped; ambiguous local times use the earlier occurrence.
4. `/worker_snooze minutes:10` or `@bot remind me again in 10 minutes` adds an extra one-time DM and channel reminder without changing the daily schedule. `/worker_time` changes it; `/worker_pause` pauses only the daily reminder (existing snoozes are not cancelled). If DMs are blocked, a natural-language confirmation may fail, but the bot reports in-channel that the request was saved and still attempts channel delivery. Scheduling is not a guarantee that Discord will deliver a message.

## Run locally

Requires Python 3.10+ (3.11+ recommended). From this folder:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env locally, keeping DISCORD_TOKEN secret.
# For local-only testing use ALLOW_LOCAL_STORAGE=true and leave DATABASE_URL empty.
python -m unittest discover -s tests -v
python -m mechbot.bot
```

In the Discord Developer Portal, create a bot and invite it with the `bot` and `applications.commands` scopes. The bot only reads DMs and messages that explicitly mention it, so it does not request the privileged Message Content Intent. Use `/help` for the slash-command list, `/invite` for the invite URL, or mention the bot and ask for its invite link. Worker prompt replies should mention the bot, or use `/worker_time`. For worker setup it needs Manage Roles, and its highest role must be above the worker role; it also needs View Channel, Send Messages and Read Message History in the task channel and any emergency-alert channels. Invite it only to a server where you have authorization. `TEST_GUILD_ID` syncs commands quickly to that guild; the bot also publishes them globally for other installed servers. If the configured guild is not accessible, global commands are still published while the bot retries the guild sync. Global propagation can take time. Slash commands appear only after the bot is installed and command sync succeeds.

## Railway + Supabase

1. Provision a PostgreSQL database (e.g. Supabase). Use a dedicated least-privilege DB user/connection, TLS, and encrypted secrets. A transaction-pooler connection is supported by disabling statement cache. Create and restrict the private `mech_bot_app` schema/tables using `PG_DDL` in `mechbot/store.py` under a trusted administrator, or set `AUTO_MIGRATE=true` only for a trusted account with schema-create privileges. Restart with `AUTO_MIGRATE=false` after setup. The schema is private application storage and is not the separate draft game-data import. The limited `mech_bot_runtime` connection was verified against the schema; full production reminder and payout workflows still need live Discord testing.
2. Deploy the project as a Railway **worker** using the `Procfile` command `python -m mechbot.bot`. Set `DISCORD_TOKEN`, `DATABASE_URL`, `DATABASE_SSL=require`, and `PORTICO_BASE_URL` as Railway secrets/variables. Optional AI needs `AI_API_KEY` and `AI_MODEL`; `AI_BASE_URL` defaults to `https://api.openai.com/v1` and must provide an OpenAI-compatible `/chat/completions` endpoint. The AI receives the current question plus relevant reviewed catalog facts only—no chat history or private notes. Game answers are blocked when reviewed facts are missing; the model is not allowed to fill gaps from memory. Add the key directly in Railway, never in chat or GitHub.
3. Use one active worker instance. SQLite local fallback does not survive an ephemeral Railway restart without a persistent volume and is unsuitable for production. Polling claims and worker reminders have database leases, but emergency alerts are in-memory and cancel on restart.
4. Confirm permissions, run `/worker_setup`, test with consenting members, and check Railway logs, a small controlled emergency alert, a worker reminder, and a real payout API threshold. Watch DM failures; local tests do not substitute for these live checks.

## Game data and limitations

`data/mech_bot_staging.sqlite3.gz` is the offline catalog; startup makes a private runtime copy and applies the user’s 2026-11-10 confirmation only when both the screenshot-archive hash and all 22,652 candidate-cell values match the reviewed manifest. The source snapshot is not modified. Verified weapon stats are shown only for confirmed screenshot-to-weapon identities; the clipped Voltaic title, other unresolved identities, and missing/cropped cells remain unavailable. Website-derived archive data remains unverified and is not used as confirmed game fact. Weapon energy variants are distinct. `/builds` returns public-site listings with an uncertainty notice. When configured, the AI interprets natural-language questions and answers from reviewed facts; unsupported game questions are answered cautiously rather than guessed.
