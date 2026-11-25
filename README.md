# Mech Arena Discord Bot

ARCHΛNGLE is an unofficial Mech Arena community helper for weapon, mech, and verified pilot lookups, polished community-build and announcement/forum summaries, reminders, calculations, and searchable Plarium Forum discussions. It is not affiliated with Plarium.

**Profile description draft (Discord Developer Portal):**
> Mech Arena weapon, mech, and pilot lookups · build summaries · reminders · event updates · community tools.<br>
> Unofficial fan project; not affiliated with Plarium.<br>
> Powered and managed by [https://discord.com/users/1442058583129722986](https://discord.com/users/1442058583129722986) (Nub) · Game database maintained by [https://discord.com/users/1444891876376186901](https://discord.com/users/1444891876376186901) (ᧁꪖꪶꪖ𝓽ꫀ𝘳) · ECR event guides and promo-code posts by [https://discord.com/users/1442422409087942678](https://discord.com/users/1442422409087942678) (MRX). ECR event information is separate from promo codes; contact the relevant person for bot, database, or event questions.

## Commands and replies

- `/help`, `/invite`, `/weapon`, `/mech`, `/pilot`, `/upgrade_cost`, `/builds`, `/recent`, `/upcoming`, `/forum [query]`, `/ecr`, `/promo_codes`, `/timezone`, `/remind`, `/reminders`, `/cancel`, `/note`, `/calc`.
- Assigned database workers also use `/worker_setup`, `/worker_time`, `/worker_status`, `/worker_snooze`, and `/worker_pause`.
- Only Discord IDs **1442058583129722986** and **1444891876376186901** can start `/ping_loop` and `/dm_loop` or use `/stop_user_loops` to stop another member’s loops. These commands work in any server where the bot is installed; `/stop_loop` remains available for users to stop their own loops.
- Public message replies ping only the reply author. Other user, role, `@everyone`, and `@here` mentions are disabled.
- User-facing answers are kept concise and within Discord's 2,000-character limit. Exact catalog lookups and verified upgrade costs are resolved locally instead of waiting on AI; unsupported game questions abstain without an AI round trip.
- `/upgrade_cost` keeps rank-up resources separate from the A-Coins needed to buy missing Blueprints. Its optional `blueprints_owned` parameter defaults to zero; the purchase estimate uses the pinned calculator's per-rarity schedule and is not a live shop offer.
- Natural-language requests cover verified pilot lookups, events, promo codes, patch notes/bugs, forum discussions, builds, game facts, calculations, and reminders. A reminder needs a when and a what, in either order and in ordinary wording — `remind me in 20 minutes to check the ECR`, `remind me to file the report tomorrow`, `remind me at 8pm to check the ECR`, `remind me every friday at 9am to post a recap`, `in an hour`, `tonight at 9` — and weekly recurrence reuses the interval schedule, so no extra column is needed. A clock-based reminder for a player with no stored timezone is refused with a request to set one, because scheduling it as UTC would wake them at the wrong hour; a relative delay needs no timezone and says so once. Sentences that ask *about* reminders, or decline one, are never mistaken for a request to create one. ECR requests use announcements only; broader event and promo-code requests search both announcements and forum posts; patch/bug questions also search both. Source posts are summarized rather than repeated, with channel/source details omitted by default. Publication dates come from the original post and are labeled separately from event dates; a publication date is never substituted for an unstated event date. Pilot answers use only verified `pilot_list` records. Build answers identify the submitter; a plain profile link is included only if a real public profile URL is verified (none is currently exposed by the public API/UI), so no link is guessed. Build replies suppress embeds. Replies longer than one Discord message are sent as several messages on line boundaries (`message_chunks`), and `/help` uses that path, so nothing is silently cut at 2,000 characters or rejected by the API. A mech-specific build lookup asks for the requester’s SP if needed and filters public submissions by overlap with their recorded ranges; game-mode hints also filter matches. Recent/list requests remain an unranked feed, and the public feed has no ratings or votes, so community tags are not a verified meta ranking. `/builds` accepts an optional `sp_range`.

## Bounded ping loops

`/ping_loop`, `/dm_loop`, and `/stop_user_loops` are global guild commands, available wherever the bot is installed. Their runtime allowlist remains IDs `1442058583129722986` and `1444891876376186901`; unauthorized interactions are rejected. To also hide these commands from ordinary members in the picker, a server manager must configure them **in each server** under **Server Settings → Integrations → ARCHΛNGLE → Commands**: deny each command to `@everyone`, then allow the two operator user accounts. A bot token cannot apply these per-user settings. **Picker visibility is not guaranteed until each server manager saves and verifies the overrides.** Discord administrators can override command permissions, so runtime ID checks remain essential and reject non-operator admins.

- `/ping_loop member interval_seconds duration_seconds reason` sends one immediate channel message and then new follow-up messages, explicitly mentioning only the target member. The bot needs View Channel and Send Messages; the target must be able to view the channel.
- `/dm_loop member interval_seconds duration_seconds reason` sends an initial DM followed by separate DM messages. If the target cannot receive the initial DM, the loop does not start.
- `/stop_loop [member]` stops the caller's channel and DM loops; an operator may select another user. `/stop_user_loops member` is restricted to the two operators and also cancels both modes for that target. There is no `/emergency_*` command or `stop emergency` text handler; use `/stop_loop` to stop a loop.
- Safety bounds are a minimum **5-second interval**, maximum **30-minute duration**, maximum **300 repeated pings**, and **five active loops globally**. A 5-second interval can run for at most 25 minutes to stay within the repeat cap. A member may have at most one channel loop and one DM loop active at a time. Delivery failures, cancellation, process restart, and the deadline stop loops; they are not resumed after restart. One in-flight message may still arrive after a stop request.

## Nine database workers

`data/workers.json` contains the nine fixed Discord IDs and their assigned mech groups; Voidghost is S tier and Biform is excluded. The configured worker server is `1509560571563868160`, with task channel `1526008373009715313`.

1. A server manager with Manage Roles runs `/worker_setup` in the configured server. The bot creates `bot database workers` only if that role is missing, assigns it to the nine configured members it can find, and posts the initial time request. It reports missing members and assignment failures privately. Re-running can post another prompt. Duplicate same-named roles, elevated permissions, role hierarchy, and mentionability issues require server-side correction.
2. Each worker sets a daily time and IANA timezone with `/worker_time` or by replying to the prompt (for example, `8pm Asia/Kolkata`). The timezone is required initially. Changes update the daily schedule; `/worker_snooze` adds a one-time reminder without changing it; `/worker_pause` pauses the daily reminder.
3. Daily and snoozed reminders are attempted by DM and in the linked task channel. Failures at one destination do not prevent the other attempt. A worker who leaves the configured server has their daily schedule paused. DST gaps are skipped; ambiguous times use the earlier occurrence.

## Announcement ingestion and upcoming events

The bot is configured to read only these two announcement channels:

- `1524319795791659080` in server `1509560571563868160`
- `1570083896823906364` in server `1479464891709526051`

Channel `1512089401193271539` is excluded even if it appears in `ANNOUNCEMENT_CHANNEL_IDS`.

The background reader retries every 10 minutes. On its first successful access, it backfills the last 100 messages from each configured channel, then fetches new posts and refreshes a short tail for edits. A missing server, channel, or permission is transient: the bot keeps retrying and does not mark the source permanently unavailable. It stores only text, embed titles/descriptions/fields, channel labels, timestamps, and the Discord source link in the private `mech_bot_app.announcements` table. It never follows links embedded in the posts. If `/ecr` finds no update, its response identifies configured channel IDs and the last result for each channel (including permission, Discord HTTP, disabled-ingestion, or storage failures).

`/ecr` and explicit ECR requests show Event Crate Rush announcement updates only; ECR is separate from promo codes. Broader event and promo-code requests combine relevant announcement and forum material. Patch-note and bug questions search both sources. These replies are concise summaries, not copied post bodies; channel names and source links are omitted by default. Promo codes are included only when explicitly labeled in a source post. A code is called expired only when the post says so; otherwise validity is unconfirmed. The bot does not send proactive promo notifications or claim that a code works.

Announcement ingestion is enabled by default in the code; set `ANNOUNCEMENT_INGESTION_ENABLED=false` only to disable it. An existing deployment with that variable explicitly set to `false` must change it to `true`. Enable the privileged **Message Content Intent** in the Discord Developer Portal and grant the bot View Channel and Read Message History in each source channel. On first successful access it reads the newest 100 messages from each configured channel, then fetches updates every 10 minutes and refreshes a short tail for edits. Set `MESSAGE_CONTENT_ENABLED=true` separately if users should be able to ask natural-language questions without mentioning the bot. The bot ignores messages aimed at another member and reply chains to other members, never pings the author for an ambient answer, and rate-limits to one ambient answer per user/channel every eight seconds. Fully specified natural reminder text is allowed; reminders and confirmations are private by DM, while notes and loop-management actions are not executed from ambient channel text. Unclear reminder requests are ignored in ambient chat. DMs and explicit bot mentions work with the ambient intent off. `ANNOUNCEMENT_CHANNEL_IDS`, if explicitly set, overrides the defaults and must include every channel to be read; channel `1512089401193271539` is always excluded. Defaults are `1524319795791659080` and `1570083896823906364`. `/upcoming` combines relevant saved announcement/forum posts. A future event date is labeled as confirmed only when the source states a full year; yearless dates are marked as mentioned but unconfirmed. If no event date is stated, the original publication date is explicitly labeled as the post date. No date or event detail is invented.

## Gear advisor

`mechbot/advisor.py` answers spend questions from the vendored ARCHANGLE Advisor pack in `data/advisor/`
(13 files, hash-pinned in `MANIFEST.json`, regenerated only by `scripts/import_advisor_pack.py`). It ranks
gear on five weighted parts — community tier, role fit, cost efficiency, meta relevance and confirmed
stat cells — and derives, never invents: prices come from the store list, the 42-rung upgrade ladder from
the cost table, growth curves from the user-confirmed screenshot rows, and the buy-or-skip verdict for a
crate from the announcement guide post itself. Every figure carries its trust tier and every answer states
which parts are community numbers rather than developer numbers; `dmgPerEnergy` and tier letters are
labelled as community data, and `04_PRIVATE_UNREDACTED_DO_NOT_SHARE` is never read.

Players do not need a command: `@bot is Oracle 16 worth 8275 ac`, `@bot I have 4000 ac and 300k credits,
what should I buy`, `@bot should I pull this ECR` are routed by `mechbot/advising.py`, which reads role,
goal, budget, rank and ownership out of the sentence, asks at most two questions when something critical
is missing, and uses documented defaults rather than nagging. `/advise <question>` runs the same engine for
anyone who prefers a command. Ambiguous names come back as a question, an item that does not exist is
refused with the variants that do, and a finished ECR is never presented as running.

Names reach the engine however they are typed: plurals fold onto the catalog's singular entries
(`@bot storm racks or charge rockets`, `@bot should I max my Pumas?`), a bare item name is treated as a
question (`@bot voidghost`), a two-item choice qualifies on its shape alone, and a catalog lookup that
returns no review and no verified cells is handed to the advisor before the shrug is ever sent — a
question that names game gear never ends in "send me a screenshot". A bare spend question with no item in
it ("what should I invest in?", "where do I put my credits?") is answered by ranking the store, while a
question about *earning* currency ("how do I get free a-coins fast") is deliberately left to the other
answers — a value ranking is not a reply to it. A stated rank is read as the player's
own position, not as a goal: "is Redox worth upgrading to rank 5?" prices the ladder from rank 1 and
says so, while "i am rank 4" prices rank 4 to max. When the advisor asks a question, the short answer to
it is understood as an answer: "1500 ac" after "what should I invest in?" is merged into that question and
planned, and a reply that does not fill one of the slots the engine named ("hello there") is left for the
normal paths instead of being swallowed.

Two hard limits: `mechbot/advising.py` refuses to ship a number the engine did not produce (every digit in
a reply must appear in the result envelope, or the reply is rebuilt from a numberless template), and
`mechbot/humanize.py` applies the [blader/humanizer](https://github.com/blader/humanizer) pattern list to
*wording only* — its v4.0.0 licence to add a supporting detail is deliberately not taken, and a rewrite
that moves a number or touches a quoted passage is discarded. The advisor works with no AI model configured.

## Plarium Forum snapshot and search

`/forum [query]` and natural-language requests such as `latest forum posts about Surge` search or summarize the stored Plarium Forum crawl. It covers the 150 newest-created threads across English sections and includes original-post text, category, author metadata, forum-displayed dates, and source links. It does not collect replies. Search results are concise community-discussion summaries, not verified game statistics, rankings, or a live event schedule. Forum post dates use the thread-creation date, not last activity or scrape time. Source links and author/channel details are omitted from normal summaries. `/forum` remains separate from ECR lookup; broader event questions can use both announcements and forum posts. The supplied JSON snapshot is not presented as current or upcoming event information.

The bot runs `scripts/scrape_mech_arena.py` at startup and refreshes every 12 hours by default (`FORUM_SYNC_SECONDS` accepts 1–48 hours). A failed scrape preserves the last-good JSON and retries after one hour. A result replaces `data/mech_arena_last_150_threads.json` only when the scraper proves the cutoff condition, has no failed listing pages, and returns exactly 150 unique threads with valid links and creation dates; incomplete crawls preserve the last-good snapshot. The replacement is atomic. Set `FORUM_SYNC_ENABLED=false` to disable scheduled scraping or tune the interval with `FORUM_SYNC_SECONDS`. Scraper dependencies are installed from `requirements.txt`.

## Read-only data API

`mechbot/api.py` serves what the bot has already collected over HTTP, from the same process as the Discord session. It is a read layer and not a second crawler: nothing is re-summarised, so every stored announcement post comes back with its `content` exactly as fetched from Discord, and every forum thread comes back with the crawled `original_post.text`. Anything the bot derived — category, ECR kind, `status`/`state`/`standing`, per-item blocks, the guide's own buy/skip line, rotation cadence — arrives in separately named fields, and each row carries a `source` object pointing at the message or file it came from. The derived judgements are imported from `mechbot.facts` (`refresh_ecr_status`, `_ecr_state`, `_fact_standing`, `dedupe_facts`) rather than recomputed, so the API and the channel answers cannot disagree.

Routes, all under `/api/mech-arena/public`: `/` (a public HTML page — current ECR with the guide's own per-item lines, the announcement originals folded open, dated events, the newest forum threads — and the same counts as JSON for a script), `/view` (that page under the API path), `/health`, `/announcements` and `/announcements/{message_id}`, `/facts`, `/ecr`, `/ecr/current`, `/ecr/history`, `/ecr/rotations`, `/events`, `/forum`, `/forum/meta`, `/stats`, and `/export?dataset=announcements|facts|ecr_history|forum`. Every list route takes `limit`, `offset`, `order`, `since`/`until` (unix seconds), `q`, `fields=a,b` to trim a row, and `format=json|ndjson|csv`; `?fields=contnet` is a 400 that lists the allowed names rather than a quietly empty object. `/ecr/history` and `/ecr/rotations` read the vendored tracker pack verbatim and are never merged with live announcements. `/events` returns a `window` only when the post stated a date naming a year: `Event runs March 1 to March 8` with no year stays `window: null` with a `date_note` saying so, because a timestamp would put a year here that the game never published.

It is off unless a deployment asks for it: `DATA_API_ENABLED=true`, or any `PORT` with `DATA_API_MODE` not `off`. Set `DATA_API_HOST` (default `0.0.0.0`), `DATA_API_PORT` (falls back to `PORT`, then 8080), `DATA_API_MAX_LIMIT` (rows per page and the export ceiling, default 200), `DATA_API_CACHE_SECONDS` (default 30; `ETag` is a revision fingerprint, so 304s resolve in one query and `If-None-Match` on `/health` and `/` is deliberately never answered from cache), `DATA_API_TOKEN` (a bearer key readers must send; `/health` alone answers without it — `/`, `/view` and the JSON index are gated too, because the page is the data in another skin), `DATA_API_ALLOW_ORIGIN` (default `*`, or a comma-separated list to pin a site), and `DATA_API_PUBLIC_HOST` for a proxy that does not forward `Host`. Only `GET` and `OPTIONS` exist — every other method gets 405 with `Allow: GET, OPTIONS` — and no route accepts a table name, so the player-owned rows in the same database (reminders, notes, chat memory, per-user payout rows, stored timezones) cannot be reached or even named by a query. `/data_api` in Discord reports whether the API is on, its URL, and the row counts. Note that `payouts.py` polls the website's `/api/mech-arena/public/payouts` function; that route shares the prefix but is served by the site, not by the bot. `tests/test_data_api.py` pins the originals, the shared ECR derivation, the privacy scan of every response body, and the startup behaviour.

### Hosting it publicly on Railway

The repo is already a Railway service, so pushing enables this with two variables and one click — no new
build step, no separate app.

1. **Existing bot service (simplest).** Set `DATA_API_ENABLED=true`, then **Service → Settings → Networking →
   Generate Domain**. Railway injects `PORT`, `mechbot.bot` starts the API on it inside the same process, and
   the domain serves both `https://<host>/` (the public page) and `https://<host>/api/mech-arena/public/…`
   (the JSON). Nothing else about the bot changes; a port clash is a log line, not a crash.
2. **A second service that only reads (keeps the public URL off the Discord process).** Create another service
   in the same Railway project with Root Directory `mech_arena_bot` and Start Command
   `python -m mechbot.api_server`, give it the same `DATABASE_URL`, then generate a domain for it. The bot
   worker keeps writing; this service never touches Discord, needs no `DISCORD_TOKEN`, and cannot write.
   Health check path: `/health` (it answers without a token, and reports `ok`, the revision and nothing else).
3. **Set `DATA_API_TOKEN` if you want the URL shareable but not wide open.** Readers then send
   `Authorization: Bearer <token>` or `?token=<token>`. The token gates the public page exactly like the
   JSON — it is a read key, not an admin key, and there is no admin: only `GET` and `OPTIONS` exist.
   `DATA_API_ALLOW_ORIGIN` narrows browser access to your site's origin when the site fetches the JSON
   directly.
4. **Do not add a `web:` line to the `Procfile`.** Railway picks `web` as a service's start command, so a
   `web: python -m mechbot.api_server` entry would replace the bot rather than run beside it. Use a second
   service (step 2) for a separate start command, or step 1 for the single-service setup.

`data/mech_arena_last_150_threads.json` and the ECR history pack ship inside the repo, so a fresh service has
the forum crawl and the tracker history immediately; only the announcements come from the database, and an
empty or unmigrated one shows on the page as “Nothing stored yet” instead of a plausible-looking guess.

## Payout reporting

- Polls the public `$ARCHANGLE_BASE_URL/api/mech-arena/public/payouts` endpoint (defaults to the Netlify site) about once per minute. It reads public aggregate data only and monitors valid Discord IDs returned by the API, not just the nine workers.
- When an observed count first reaches 10 builds, or increases later that payout day, the bot DMs **1442058583129722986** with the user ID, day, and total. No payment is executed. Counts dropping and then returning to an already-reported high are not re-reported.
- Persisted high-water marks suppress ordinary repeats across restarts. A crash after Discord accepts a DM but before storage confirms it can still result in a duplicate. If the bot or site is down for a full payout day, the current API may not allow that missed day to be recovered.

## AI, privacy, and latency

- Optional AI uses an OpenAI-compatible `/chat/completions` endpoint configured by `AI_BASE_URL`, `AI_API_KEY`, and `AI_MODEL`. With private Supabase memory enabled, it may also receive up to five recent user/assistant turns scoped to the same user and exact channel or DM. Only the same user’s DMs, explicit bot mentions, direct replies to the bot, and (when `MESSAGE_CONTENT_ENABLED=true`) apparent questions in the exact channel are eligible; other participants’ messages, slash commands, private notes, and reminder data are excluded. Ambient questions are not answered from or written into any other member’s conversation history. Turns are capped at 1,500 characters per side and physically deleted after five hours. No API key should be posted or logged.
- Local exact-name lookups, rank-upgrade prices, and questions without matching confirmed game evidence skip the AI call. Other AI calls reuse a short-lived session and time out after 10 seconds, then fall back to confirmed local facts or an abstention.
- The bot uses one Railway worker instance. Reminders, worker schedules, payout high-water marks, and announcement records use the private Supabase schema; ping loops are in-memory and stop on process restart.

## Currency emojis

On startup, the bot tries to create or reuse three application-owned emojis from the transparent 128×128 assets in `data/emoji_*.png`. **These three — A-Coins, Credits, Blueprints — are the only custom emojis the bot uses.** There are no decorative emoji and no GIFs; the reply is plain text plus these three marks (with plain-text fallbacks if registration fails), so nothing depends on assets loading. The registered names are versioned (`archangle_a_coins_v2`, `archangle_credits_v2`, and `archangle_blueprints_v2`) so refreshed artwork is created instead of silently reusing older `v1` images. `/upgrade_cost` uses the custom emoji codes for A-Coins, Credits, and Blueprints; if registration is unavailable, the reply attaches the same PNG assets. The optimized transparent PNGs are the runtime assets; the original checkerboard-backed references remain separate from the deploy files.

## Supabase and deployment

1. Production storage uses the private `mech_bot_app` schema and a least-privilege `mech_bot_runtime` PostgreSQL login with TLS. Existing Supabase project data is separate and was not wiped or imported. Additive migrations `add_mech_bot_announcements` and `add_short_lived_conversation_memory` have been applied to the `hq bot` Supabase project. The runtime role has only the required table privileges and still has no schema `CREATE` privilege. The checked-in `migrations/` SQL files are the repeatable local records of those changes. Keep `AUTO_MIGRATE=false` for the runtime account.
2. Deploy this project as a separate Railway **worker** with the `Procfile` command `python -m mechbot.bot`. Configure `DISCORD_TOKEN`, `DATABASE_URL`, `DATABASE_SSL=require`, and `ARCHANGLE_BASE_URL` (or the older `PORTICO_BASE_URL` alias) as Railway variables/secrets. Optional AI settings belong in the Railway service configuration; never paste secrets into chat or GitHub.
3. Bot role hierarchy matters for `/worker_setup`: its highest role must be above `bot database workers`. The worker command remains limited to the configured worker server. The bounded-loop commands are global guild commands and work in any server; configure their per-user visibility overrides separately in each server as described above. `/stop_loop` and other public commands also remain globally available, with guild mirrors for faster availability.
4. SQLite local fallback is for development only; an ephemeral Railway filesystem is not durable. Test live Discord delivery and PostgreSQL permissions separately from the local mock-based test suite.

## Game archive and coverage audit

`data/mech_bot_staging.sqlite3.gz` is the read-only offline catalog. Startup copies it to a private runtime path; it never edits the source archive. It now contains 150 source documents, 1,951 structured rows, 151 document versions, 904 flattened entities, 353 public-asset references, 432 image-OCR records, 36 existing image-transcription candidates, and the 251-screenshot weapon dataset with 22,652 candidate cells. The 84 screenshots from `PILOT.zip` and `MA SS.zip`, plus six earlier chat uploads, have raw OCR and confidence/bounding-box records staged; 72 pilot-bonus rows and 451 mech-stat rows are positional OCR candidates. The 90 raw OCR records from the 84 Drive images and six chat uploads are marked user-approved as raw text; this does not independently validate OCR accuracy, screenshot identity, game version/date, or the user-attributed Gladitor label. Pilot lookups use only verified `pilot_list` rows. The raw `PILOT.zip` text is not surfaced as pilot facts, and the 523 derived pilot/mech stat-candidate rows remain `needs_review` and unavailable as verified stats. The review workbook and detailed review artifacts are kept in the local workspace and are not included in the public repository. Drive data is labeled `Gladitor` at the user’s request; that attribution has not been independently verified. The packaged bot archive and the local source staging database passed SQLite integrity checks and retained the existing public-data fingerprint during this import. The importer now records one dynamic ISO-8601 UTC batch timestamp in document, OCR, and ingest-queue audit fields and metadata instead of assigning a hard-coded midnight. The original source ZIPs, review workbook, candidate/approval files, generated overlay reports, and SQL batch payloads are local review artifacts and are excluded from this public repository; the read-only runtime catalog snapshot is included.

All 52 Mech Arena Database documents, 59 captured Plarium forum original posts, and 36 upgrade-calculator documents are present. Their user-confirmed review is pinned to the existing snapshot; new or changed imports stay pending. The 12 existing public structured collections are searchable when their pinned source documents are verified, and verified non-code public documents/posts are eligible for archive retrieval. Approved raw OCR from non-pilot images may be searched for relevant context; `PILOT.zip` OCR is retained for audit but is not surfaced as pilot data. Pilot responses and assistant facts use only verified `pilot_list` entries. Chat-upload excerpts are restricted to relevant game text; conversation messages, usernames, and image-generation prompts are not returned. The two derived pilot/mech candidate collections (523 rows) remain unsearchable until separately reviewed. Gladitor remains user-attributed, not independently verified. Public scripts, calculator client-source maps, and sitemaps remain stored for audit but are not fed into answer context as factual prose.

The separate user-confirmed screenshot values are exposed only when weapon identity is accepted. Cropped or unresolved identities/cells remain gated; `Sm` means 5 m, Arc Torrent 8 is excluded, Dreadshot 8 and 10 remain separate, and the Dreadshot 10 calculator energy mismatch is not silently used. The 342 earlier public-image OCR records remain `needs_review`; the 90 new Drive/chat raw OCR records are user-approved as raw screenshot text, with OCR accuracy not independently rechecked. `PILOT.zip` text and the 72 derived pilot candidates are not exposed as pilot facts; verified `pilot_list` records are the sole pilot lookup source. The 36 existing transcriptions and all 523 derived pilot/mech rows remain unverified candidates and cannot be used as confirmed answer facts. Original image binaries and the two Drive ZIPs remain separate workspace files; they are not embedded in the bot catalog.

Captured scope is not equivalent to complete coverage of every public web/forum item: the forum snapshot contains original posts only (no replies and limited category-page sampling); some calculator media URLs were missing/rate-limited; and older/hidden site pages or future updates may not be captured. The bot must not claim those absences mean there is no information. `/weapon` and `/mech` provide concise reviews, key stats, perks, and listed purchase prices; purchase price is kept separate from upgrade cost. Calculator rank-only steps use the confirmed max-level-to-next-rank transition. Variant mismatches are withheld rather than guessed.

Run local tests with:

```bash
python -m unittest discover -s tests -v
python -m compileall -q mechbot scripts tests
```

The `.github/workflows/ci.yml` workflow runs these checks on pushes to `main`, pull requests, and manual dispatches.
