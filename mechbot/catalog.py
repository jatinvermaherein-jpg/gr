"""Read-only snapshot. Never promote OCR results to verified facts here."""
import gzip
import os
from pathlib import Path
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / 'data' / 'mech_bot_staging.sqlite3.gz'


def initialize_snapshot():
    # A reviewer can supply a revised database through a Railway volume, but it must
    # have the same schema; the bundled snapshot is read-only and never verifies data.
    override = os.getenv('GAME_DATABASE_PATH')
    if override:
        p = Path(override)
        if not p.is_file():
            raise RuntimeError('GAME_DATABASE_PATH does not exist')
        return p
    dest = Path(os.getenv('SNAPSHOT_PATH', '/tmp/mech_bot_game_snapshot.sqlite3'))
    # Always unpack on startup, so a new deploy cannot keep an old cached snapshot.
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix('.tmp')
    with gzip.open(ARCHIVE, 'rb') as source, open(temp, 'wb') as target:
        shutil.copyfileobj(source, target)
    temp.replace(dest)
    return dest


class Catalog:
    def __init__(self, path):
        self.db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.weapon_names = [r['name'] for r in self.db.execute('SELECT name FROM weapons')]
        self.mech_names = [r['name'] for r in self.db.execute('SELECT name FROM mechs')]

    def close(self):
        self.db.close()

    def search(self, name, kind='weapon', limit=10):
        table = 'weapons' if kind == 'weapon' else 'mechs'
        needle = name.strip().casefold()
        if not needle:
            return []
        # Local lookup only; no SQL string interpolation of input.
        rows = self.db.execute(f'SELECT * FROM {table} ORDER BY name').fetchall()
        exact = [r for r in rows if r['name'].casefold() == needle]
        found = exact or [r for r in rows if needle in r['name'].casefold()]
        return [dict(r) for r in found[:limit]]

    def weapon(self, name, rank=None, level=None):
        results = self.search(name, 'weapon')
        if len(results) != 1:
            return {'matches': results, 'verified_stats': [], 'candidate_count': 0}
        item = results[0]
        # This view is the only source for numeric game-stat answers.
        sql = 'SELECT stat_name,rank,level,star_color,value,source_path,source_sha256,reviewed_by,reviewed_at FROM bot_verified_weapon_stats WHERE weapon=?'
        params = [item['name']]
        if rank is not None:
            sql += ' AND rank=?'
            params.append(rank)
        if level is not None:
            sql += ' AND level=?'
            params.append(level)
        sql += ' ORDER BY rank,level,stat_name'
        stats = [dict(r) for r in self.db.execute(sql, params)]
        n = self.db.execute('SELECT count(*) FROM weapon_stat_candidates x JOIN screenshots s ON s.id=x.screenshot_id WHERE s.weapon_id=? AND x.status=\'unverified\'', (item['id'],)).fetchone()[0]
        return {'matches': results, 'verified_stats': stats, 'candidate_count': n}

    def mech(self, name):
        return self.search(name, 'mech')
