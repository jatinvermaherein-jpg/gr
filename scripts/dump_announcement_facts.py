#!/usr/bin/env python3
"""One-time (and repeatable) dump: every saved announcement becomes a curated fact row.

The announcement channel is the *source of record*; after this dump the bot answers ECR, promo-code,
event and patch questions from `announcement_facts` instead of re-reading posts. The dump reads every
saved announcement in the allowed channels - it never samples - merges attachment-only stubs into the
post they belong to, classifies each post into a typed fact, and (when a model is configured) asks the
AI to fill in what the deterministic reader cannot see, keeping only answers that quote the post
verbatim.

Usage:
    python scripts/dump_announcement_facts.py --sqlite data/local_users.sqlite3 [--dry-run] [--json out.json]
    python scripts/dump_announcement_facts.py --postgres "$DATABASE_URL" [--replace] [--ai|--no-ai]
    python scripts/dump_announcement_facts.py --channel 1524319795791659080 --answer ecr

Exit codes: 0 wrote (or would write) facts, 2 nothing to do, 1 configuration/storage failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mechbot.facts import (ai_extract_facts, build_facts, format_announcement_reply, format_ecr_reply,
                           format_fact_list, merge_ai_facts, search_facts)
from mechbot.store import Store

try:                                                    # reuse the bot's channel policy when importable
    from mechbot.bot import ANNOUNCEMENT_CHANNELS as DEFAULT_CHANNELS
except Exception:                                       # pragma: no cover - only without discord installed
    from mechbot.announcements import DEFAULT_ANNOUNCEMENT_CHANNELS as DEFAULT_CHANNELS


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--sqlite', help='path to a SQLite store (default: LOCAL_STORAGE_PATH)')
    source.add_argument('--postgres', help='PostgreSQL/Supabase DSN (default: DATABASE_URL)')
    parser.add_argument('--channel', action='append', type=str, default=[],
                        help='restrict the dump to this channel id (repeatable); default: the bot\'s configured channels')
    parser.add_argument('--limit', type=int, default=2000, help='maximum saved announcements to read (default: all)')
    parser.add_argument('--since', type=int, default=0, help='unix time lower bound (default: every message)')
    parser.add_argument('--replace', action='store_true',
                        help='delete the stored facts for these channels first, instead of updating them in place')
    ai = parser.add_mutually_exclusive_group()
    ai.add_argument('--ai', dest='ai', action='store_true', help='force the AI extraction pass')
    ai.add_argument('--no-ai', dest='ai', action='store_false', help='skip the AI pass and store deterministic facts only')
    parser.set_defaults(ai=None)
    parser.add_argument('--auto-migrate', action='store_true',
                        help='with --postgres, apply the app DDL (creates announcement_facts if the schema is migrating)')
    parser.add_argument('--dry-run', action='store_true', help='report what would be written and write nothing')
    parser.add_argument('--json', help='also write the built facts to this path for review')
    parser.add_argument('--answer', choices=('ecr', 'promo', 'upcoming'),
                        help='preview the answer the bot will give from these facts')
    return parser.parse_args(argv)


def announce(message=''):
    print(message, flush=True)


async def run(args):
    dsn = args.postgres or (os.getenv('DATABASE_URL', '').strip() if not args.sqlite else '')
    store = None
    if dsn:
        store = await Store.postgres(dsn, auto_migrate=args.auto_migrate)
    else:
        path = args.sqlite or os.getenv('LOCAL_STORAGE_PATH', './data/local_users.sqlite3')
        store = Store(sqlite_path=path)
    try:
        if not getattr(store, 'supports_announcements', True):
            announce('ERROR: the announcement table is not available in this store, so there is nothing to dump.')
            return 1
        if not getattr(store, 'supports_announcement_facts', False):
            announce('ERROR: the curated fact table is not installed. Apply migrations/20261123_announcement_facts.sql '
                      '(or pass --auto-migrate with a Postgres DSN).')
            return 1
        channels = tuple(args.channel) if args.channel else tuple(str(value) for value in DEFAULT_CHANNELS)
        announce(f'Reading every saved announcement from {len(channels)} channel(s): {", ".join(channels)}')
        rows = await store.recent_announcements(int(args.since), limit=args.limit)
        rows = [row for row in rows if str(row.get('channel_id') or '') in channels]
        if not rows:
            announce('ERROR: no announcements are saved for these channels yet. Let the bot finish its first '
                      'ingestion pass (or widen --since/--channel), then run the dump again.')
            return 1
        now = time.time()
        facts, stats = build_facts(rows, now=now)
        if stats['orphan_message_ids']:
            announce("Attachment-only messages with no announcement nearby (left unfiled, not invented into a fact): "
                     + ', '.join(stats['orphan_message_ids']))
        announce(f"Read {stats['messages_read']} messages -> {stats['facts_written']} facts "
                 f"({stats['stub_messages']} attachment-only messages merged, {stats['orphans']} unmatched stubs, "
                 f"{stats['noise_messages']} non-announcement messages skipped).")
        ai_state = args.ai
        if ai_state is None:
            ai_state = bool(os.getenv('AI_API_KEY', '').strip() and os.getenv('AI_MODEL', '').strip()) \
                and os.getenv('ANNOUNCEMENT_AI_EXTRACTION', 'true').strip().casefold() not in ('0', 'false', 'no')
        ai_summary = 'skipped'
        if ai_state:
            if not facts:
                announce('Nothing to enrich: no facts were produced from these messages.')
            else:
                announce(f'AI extraction over {len(facts)} posts in batches of 4 (verbatim-quote check on)...')
                result = await ai_extract_facts(rows, base=os.getenv('AI_BASE_URL', 'https://api.openai.com/v1').strip(),
                                               key=os.getenv('AI_API_KEY', '').strip(),
                                               model=os.getenv('AI_MODEL', '').strip(),
                                               system_prompt=None)
                ai_summary = (f"{result['accepted']} accepted, {result['rejected']} rejected, "
                              f"{result['batches']} batch(es)" + (f", error {result['error']}" if result['error'] else ''))
                if result['facts']:
                    facts = merge_ai_facts(facts, result['facts'], now=now)
        announce(f'AI pass: {ai_summary}')
        by_category = Counter(fact['category'] for fact in facts)
        by_status = Counter(f"{fact['category']}/{fact['status']}" for fact in facts if fact['category'] == 'ecr')
        announce('Facts by category: ' + ', '.join(f'{name}={count}' for name, count in by_category.most_common()))
        if by_status:
            announce('ECR fact states: ' + ', '.join(
                f'{key.rsplit("/", 1)[1]}={count}' for key, count in sorted(by_status.items())))
        if args.json:
            payload = [{key: value for key, value in fact.items() if not key.startswith('_')} for fact in facts]
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
            announce(f'Wrote {len(payload)} facts to {args.json}')
        if args.answer:
            categories = {'ecr': ('ecr',), 'promo': ('promo_code',),
                          'upcoming': ('event', 'ecr', 'gear_hub', 'store_offer', 'balance_change', 'community')}[args.answer]
            pool = [fact for fact in facts if fact['category'] in categories]
            preview = (format_ecr_reply(pool, now=now) if args.answer == 'ecr'
                       else format_fact_list(search_facts(pool), 'Preview from the fact table', limit=6,
                                             show_codes=(args.answer == 'promo')))
            announce('\n--- answer preview ---\n' + (preview or 'No saved fact matches that question yet.')
                     + '\n--- end preview ---')
        if args.dry_run:
            announce('Dry run: nothing was written.')
            return 0 if facts else 2
        run_id = 'dump-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        if args.replace:
            removed = await store.clear_announcement_facts(channel_ids=channels)
            announce(f'Cleared {removed} stored fact(s) for these channels.')
        written = await store.upsert_announcement_facts(facts, run_id=run_id)
        announce(f'Wrote {written} curated fact(s) ({run_id}). Answers now read from announcement_facts.')
        return 0 if written else 2
    finally:
        await store.close()


def main(argv=None):
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
