"""Serve the data API and its public page from their own process, without Discord.

    python -m mechbot.api_server

Railway deploys this repo automatically, so a second service in the same project with this start command
gets a public URL without touching the bot worker: the bot keeps writing, this reads the same database and
answers `GET`s and the human page. It listens on `PORT` (Railway's) or `DATA_API_PORT`, and it is
read-only by construction — the routes in `mechbot/api.py` are the only queries it can make, and `POST`,
`PUT`, `PATCH` and `DELETE` are refused before a handler runs.

Without `DATABASE_URL` it falls back to a local SQLite file, which is a development convenience only: an
ephemeral container's filesystem holds nothing, and the page then says so instead of inventing rows.
"""

import asyncio
import os
import signal
import sys
from types import SimpleNamespace

from .api import DataApi, config as api_config
from .store import Store


def _truthy(name):
    return str(os.getenv(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


async def build_store():
    """The same store the bot uses, opened by a process that only reads from it."""
    dsn = str(os.getenv('DATABASE_URL') or os.getenv('SUPABASE_DB_URL') or '').strip()
    if dsn:
        ssl_required = str(os.getenv('DATABASE_SSL') or 'require').strip().lower() != 'disable'
        store = await Store.postgres(dsn, auto_migrate=_truthy('AUTO_MIGRATE'), ssl_required=ssl_required)
        print('connected to PostgreSQL; announcements and facts read from the private mech_bot_app schema',
              flush=True)
        return store
    path = str(os.getenv('MECH_BOT_SQLITE') or 'data/mech_bot_api_reader.sqlite3').strip()
    if not os.path.exists(path) and not _truthy('ALLOW_LOCAL_STORAGE'):
        raise SystemExit(f'No DATABASE_URL, and {path} does not exist. Set DATABASE_URL, point '
                         'MECH_BOT_SQLITE at an existing file, or set ALLOW_LOCAL_STORAGE=true for a '
                         'development database.')
    print(f'local SQLite at {path} — a container filesystem is not durable, so treat this as development',
          flush=True)
    return Store(sqlite_path=path)


async def main():
    settings = api_config()
    if not settings['enabled']:
        print('The data API is off. Set DATA_API_ENABLED=true (or any PORT) to start it; '
              'DATA_API_TOKEN requires readers to send a bearer key.', file=sys.stderr)
        return 2
    store = await build_store()
    # No runners on purpose: the collector loops live in the bot, and the index says "not started" rather
    # than implying this process keeps the data fresh.
    bot = SimpleNamespace(store=store, announcement_runner=None, forum_archive_runner=None,
                          conversation_runner=None)
    served = DataApi(bot, settings=settings)
    url = await served.start()
    if not url:
        await store.close()
        return 2
    print(f'data API live at {url} — public page at {url.rsplit("/api", 1)[0]}/', flush=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ('SIGTERM', 'SIGINT'):
        try:
            loop.add_signal_handler(getattr(signal, name), stopped.set)
        except (NotImplementedError, AttributeError):
            pass                                        # Windows: Ctrl-C still raises KeyboardInterrupt
    await stopped.wait()
    await served.stop()
    await store.close()
    print('stopped', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
