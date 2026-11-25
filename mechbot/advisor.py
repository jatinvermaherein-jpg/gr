"""Deterministic advisor engine: compare, invest, plan, model a 1v1, explain, and read the ECR.

This is the part of the advisor that is allowed to know things. Everything it returns comes from the
hash-pinned files in `data/advisor/` (the ARCHANGLE advisor build pack) plus the curated announcement
facts the bot already keeps, and every row carries the trust tier the pack gave it. The style layer in
`mechbot/advising.py` and the language model are only allowed to *phrase* what these functions return:
no number, tier, cost, date or verdict may enter an answer from anywhere else. That is the pack's
P1 rule, and it is enforced by code in `advising.py`, not by a polite request.

Three judgement calls worth knowing when reading this file:

* **Ambiguity is a result.** An unresolved or contested name returns `status='ambiguous'` with the
  candidates, and the caller asks a question. It is never silently guessed, because a guessed item is
  how an advisor talks a player out of 3,000 A-coins.
* **Missing data lowers confidence, it does not fill in.** No confirmed stat curve means the ranking
  says so and ranks on cost efficiency and tier, rather than inventing a power number.
* **Disagreement is shown, not resolved.** The community tier list says Oracle is tier A; the weekly
  ECR guide in the announcement channel says it is trash and to skip it. Both are true of different
  things (raw power, and value for currency in this week's offer), so `compare` and `explain` surface
  both and say which one drove the verdict.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data' / 'advisor'

CURRENCIES = ('credits', 'acoins', 'blueprints', 'mod_parts')
CURRENCY_WORDS = {'credits': 'credits', 'credit': 'credits', 'cr': 'credits', 'acoins': 'acoins',
                  'a-coins': 'acoins', 'a coins': 'acoins', 'ac': 'acoins', 'blueprints': 'blueprints',
                  'blueprint': 'blueprints', 'bp': 'blueprints', 'mod parts': 'mod_parts',
                  'mod_parts': 'mod_parts'}
TIER_SCORE = {'S': 1.00, 'A': 0.85, 'B': 0.70, 'C': 0.55, 'D': 0.40}
TIER_MISSING = 0.50
PROFILES = {
    'balanced': {'tier': 0.30, 'role': 0.20, 'cost': 0.20, 'meta': 0.10, 'stats': 0.20},
    'budget': {'tier': 0.20, 'role': 0.15, 'cost': 0.45, 'meta': 0.10, 'stats': 0.10},
    'f2p': {'tier': 0.15, 'role': 0.15, 'cost': 0.40, 'meta': 0.15, 'stats': 0.15},
    'tryhard': {'tier': 0.45, 'role': 0.20, 'cost': 0.05, 'meta': 0.10, 'stats': 0.20},
    'ecr': {'tier': 0.25, 'role': 0.15, 'cost': 0.35, 'meta': 0.25, 'stats': 0.00},
}
WEAPON_CQ = ('cq', 'barrage', 'heavy')
WEAPON_LONG = ('sniper', 'beam', 'artillery')
WEAPON_MID = ('assault',)
WEAPON_GUIDED = ('guided', 'missile')
PACK_STALE_DAYS = 30
MAX_ANSWER = 1800

# The pack's correction table: these are not fuzzy-match problems, they are known data facts.
EXCLUDED_ITEMS = {'arc torrent 8': 'Arc Torrent 8 does not exist. The catalog has Arc Torrent 6, 10 and 12.'}
SEPARATE_ITEMS = {'dreadshot 8': 'Dreadshot 8 and Dreadshot 10 are separate items, both user-confirmed.'}


def now_epoch(at=None):
    return float(at) if isinstance(at, (int, float)) else time.time()


def normalise(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip().casefold()
    text = re.sub(r'[^a-z0-9]+', ' ', text).strip()
    return re.sub(r'^(?:the|a|an) ', '', text)


def to_number(value):
    """Parse the messy display strings the catalog uses ('~1,299', '67,600', '+8%', '14.5s', '46–225m')."""
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r'-?\d[\d,]*(?:\.\d+)?', str(value or '').replace(' ', ' '))
    if not match:
        return None
    return float(match.group(0).replace(',', ''))


def parse_range(value):
    match = re.search(r'(-?\d[\d,]*(?:\.\d+)?)\s*[–\-~to]+\s*(-?\d[\d,]*(?:\.\d+)?)', str(value or ''))
    if not match:
        single = to_number(value)
        return (None, single) if single is not None else (None, None)
    return to_number(match.group(1)), to_number(match.group(2))


def amount_text(currency, amount):
    if amount in (None, ''):
        return f'no listed {label_currency(currency)} price'
    return f'{int(amount):,} {label_currency(currency)}'


def label_currency(currency):
    return {'credits': 'Credits', 'acoins': 'A-coins', 'blueprints': 'blueprints',
            'mod_parts': 'mod parts'}.get(currency, str(currency))


def parse_budget(text):
    """Read a budget out of natural text: '4000 ac, 300k credits and 120 blueprints'."""
    body = str(text or '').lower().replace(',', ' ')
    out, seen = {}, set()
    for match in re.finditer(r'(\d+(?:\.\d+)?\s*[km]?)\s*(a-? ?coins?|ac|credits?|cr|blueprints?|bps?|mod parts?)', body):
        raw, word = match.group(1), match.group(2)
        multiplier = 1000 if raw.rstrip().endswith('k') else 1000000 if raw.rstrip().endswith('m') else 1
        value = int(float(re.sub(r'[^0-9.]', '', raw)) * multiplier)
        currency = CURRENCY_WORDS.get(word.replace('- ', ' ').replace('a coins', 'acoins'), CURRENCY_WORDS.get(word, None))
        if not currency:
            for key, alias in (('acoins', ('ac', 'a coin', 'a-coin')), ('credits', ('cr', 'credit')),
                               ('blueprints', ('bp', 'blueprint')), ('mod_parts', ('mod part',))):
                if word.startswith(alias):
                    currency = key
                    break
        if currency:
            out[currency] = out.get(currency, 0) + value
            seen.add(match.span())
    leftovers = re.sub(r'(\d+(?:\.\d+)?\s*[km]?)\s*(?:a-? ?coins?|ac|credits?|cr|blueprints?|bps?|mod parts?)', ' ', body)
    for match in re.finditer(r'(?:budget|spend|have)\D{0,12}(\d{3,9})\s*(?:a-? ?coins|credits|blueprints)?', leftovers):
        if 'credit' in match.group(0):
            out['credits'] = out.get('credits', 0) + int(match.group(1))
        elif 'blueprint' in match.group(0):
            out['blueprints'] = out.get('blueprints', 0) + int(match.group(1))
    return {key: value for key, value in out.items() if value > 0}


def parse_dates(value):
    try:
        return date.fromisoformat(str(value))
    except Exception:
        parts = re.findall(r'\d+', str(value or ''))
        if len(parts) >= 3:
            try:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                return None
    return None


class AdvisorData:
    """The vendored pack, indexed once and cached."""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else DATA_DIR
        self.manifest = self._json('MANIFEST.json', {})
        self.weapons = self._json('weapon_catalog_tiered.json', [])
        self.mechs = self._json('mech_catalog_tiered.json', [])
        self.store = self._json('real_gear_catalogue_299.json', [])
        self.ladder = self._json('real_upgrade_costs.json', [])
        self.ecr = self._json('ecr_history_261.json', [])
        self.families = self._json('weapon_families_251.json', [])
        self.pilots = self._json('pilots_36.json', [])
        self.mods = self._json('mods_14.json', [])
        self.legendary_skins = self._json('legendary_skins.json', [])
        self.documents = self._json('documents_index_150.json', [])
        # that file is [ {skin: votes}, 'as-of timestamp' ], not a plain map
        likes = self._json('skin_likes.json', {})
        if isinstance(likes, list):
            self.skin_likes = next((row for row in likes if isinstance(row, dict)), {})
            self.skin_votes_asof = next((row for row in likes if isinstance(row, str)), None)
        else:
            self.skin_likes, self.skin_votes_asof = likes, None
        self.cells = self._cells()
        self._indexes = {}

    def _json(self, name, default):
        path = self.directory / name
        try:
            return json.loads(path.read_text())
        except Exception:
            return default

    def _cells(self):
        """The screenshot table, keyed entity -> rank -> label.

        Rows come in two flavours: absolute numbers (`13500`) and per-rank bonuses (`+4%`), and the
        `unit` column is the only thing that tells them apart. Both are kept - the second kind is the
        rank curve, which is what a return-on-investment question actually needs.
        """
        out = {}
        path = self.directory / 'verified_stat_cells_3520.csv'
        try:
            import csv
            with path.open(newline='') as handle:
                for row in csv.DictReader(handle):
                    entity = normalise(row.get('entity'))
                    if not entity:
                        continue
                    value = str(row.get('value') or '').strip().strip(chr(39))
                    out.setdefault(entity, {}).setdefault(str(row.get('rank')).strip(), {})[
                        str(row.get('stat_label')).strip()] = {'value': value,
                                                                'unit': str(row.get('unit') or '').strip().casefold(),
                                                                'status': str(row.get('status') or '').strip().casefold(),
                                                                'number': to_number(value)}
        except Exception:
            return {}
        return out

    # -- indexes -----------------------------------------------------------------------------
    def _index(self, key, build):
        if key not in self._indexes:
            self._indexes[key] = build()
        return self._indexes[key]

    def trust_for(self, name):
        """The trust tier the pack assigned to one file, or None when it is not in the manifest.

        The manifest is the only place these labels come from, so a re-import that changes a file's
        standing shows up as a changed string in an answer rather than as a quiet upgrade in confidence.
        """
        return (self.manifest.get('trust_tiers') or {}).get(name)

    @property
    def ladder_classes(self):
        def build():
            found = set()
            for step in self.ladder:
                for name, value in step.items():
                    if isinstance(value, dict):
                        found.add(name)
            return found
        return self._index('ladder_classes', build)

    @property
    def store_by_name(self):
        def build():
            out = {}
            for row in self.store:
                name = normalise(row.get('list'))
                if name:
                    out.setdefault(name, row)
            return out
        return self._index('store_by_name', build)

    @property
    def weapon_reviews(self):
        def build():
            return {normalise(row.get('name')): row for row in self.weapons if row.get('name')}
        return self._index('weapon_reviews', build)

    @property
    def mech_reviews(self):
        def build():
            return {normalise(row.get('name')): row for row in self.mechs if row.get('name')}
        return self._index('mech_reviews', build)

    @property
    def family_variants(self):
        def build():
            out = {}
            for row in self.families:
                family = normalise(row.get('family'))
                if not family:
                    continue
                out.setdefault(family, []).append(row)
            for rows in out.values():
                rows.sort(key=lambda row: row.get('variant_number') or 0)
            return out
        return self._index('family_variants', build)

    @property
    def pilot_names(self):
        return {normalise(row.get('name')): row for row in self.pilots if row.get('name')}

    @property
    def mod_names(self):
        return {normalise(row.get('mod_name')): row for row in self.mods if row.get('mod_name')}

    @property
    def ecr_by_name(self):
        def build():
            out = {}
            for row in self.ecr:
                for name in {normalise(row.get('name')), normalise(re.sub(r'\s*\d+$', '', str(row.get('name') or '')))}:
                    if name and name != '?':
                        out.setdefault(name, []).append(row)
            return out
        return self._index('ecr_by_name', build)

    @property
    def max_cost(self):
        """Worst price in the same class and currency, so cost_norm is comparable inside a group."""
        def build():
            out = {}
            for row in self.store:
                cost = row.get('cost') or {}
                currency, amount = cost.get('currency'), to_number(cost.get('amount'))
                if amount is None or currency in (None, 'free', 'Referral', 'Paid'):
                    continue
                bucket = (row.get('type'), currency)
                out[bucket] = max(out.get(bucket, 0.0), amount)
            return out
        return self._index('max_cost', build)

    @property
    def mech_scale(self):
        def build():
            hp = [to_number(row.get('hp')) for row in self.mechs]
            power = [to_number(row.get('power')) for row in self.mechs]
            speed = [to_number(row.get('speed')) for row in self.mechs]
            return {'hp': max([value for value in hp if value] or [1]),
                    'power': max([value for value in power if value] or [1100]),
                    'speed': max([value for value in speed if value] or [40])}
        return self._index('mech_scale', build)

    def trusted_document_words(self):
        def build():
            words = set()
            for row in self.documents:
                if row.get('trust') not in ('user_designated_trusted', 'user_confirmed'):
                    continue
                words.add(normalise(row.get('title')))
            return words
        return self._index('docs', build)

    # -- provenance --------------------------------------------------------------------------
    def build_date(self):
        return parse_dates(self.manifest.get('pack_build_date'))

    def staleness_days(self, at=None):
        built = self.build_date()
        if not built:
            return None
        today = datetime.fromtimestamp(now_epoch(at), timezone.utc).date()
        return (today - built).days

    def staleness_caveat(self, at=None):
        days = self.staleness_days(at)
        if days is None:
            return f'the advisor data files carry no build date, so treat every number as unverified'
        if days > PACK_STALE_DAYS:
            return (f'the advisor catalog was built {days} days ago ({self.manifest.get("pack_build_date")}); '
                    'a patch can move tiers, prices and stats since then')
        return ''


_CACHE = {}


def load(directory=None):
    key = str(directory or DATA_DIR)
    stamp = time.time()
    if key not in _CACHE or _CACHE[key][0] < stamp - 300:
        _CACHE[key] = (stamp, AdvisorData(directory))
    return _CACHE[key][1]


FACTS_PROVIDER = None


def set_facts_provider(provider):
    """Hand the engine a way to read the curated announcement facts.

    The vendored pack is static data, and what to do with *this week's* crate only exists in the facts
    table, which the bot owns. The engine keeps working with nothing wired up - an empty list means the
    buy or skip line is simply absent - so scoring stays testable offline and never reaches into a
    database on its own.
    """
    global FACTS_PROVIDER
    FACTS_PROVIDER = provider


def _facts(facts):
    if facts is not None:
        return facts
    if FACTS_PROVIDER is None:
        return []
    try:
        return list(FACTS_PROVIDER() or [])
    except Exception:
        return []


# ---------------------------------------------------------------------------------------------
# entity resolution
# ---------------------------------------------------------------------------------------------

def _candidate_names(data):
    return data._index('names', lambda: _build_candidate_names(data))


def _build_candidate_names(data):
    names = {}
    for name in data.weapon_reviews:
        names[name] = ('weapon', None)
    for family, rows in data.family_variants.items():
        for row in rows:
            names[normalise(row.get('name'))] = ('weapon', row)
        names.setdefault(family, ('weapon_family', None))
    for name in data.mech_reviews:
        names.setdefault(name, ('mech', None))
    for name, row in data.store_by_name.items():
        names.setdefault(name, (row.get('type'), None))
    for name in data.pilot_names:
        names.setdefault(name, ('pilot', None))
    for name in data.mod_names:
        names.setdefault(name, ('mod', None))
    return names


def _fuzzy_allowed(query, name):
    """Whether a fuzzy comparison between these two strings can be trusted at all.

    SequenceMatcher rates 'save' and 'savant' 0.86 apart, which is how a bot ends up recommending a
    mech nobody asked about. Short words are not names, and a phrase has to be roughly the length of
    what it claims to match.
    """
    if len(query) < 6:
        return False
    shorter, longer = sorted((len(query), len(name)))
    return shorter / longer >= 0.7




LEAD_IN = re.compile(r'^(?:is|are|was|were|do|does|did|can|should|would|tell me about|what about|how about|'
                     r'compare|which|what|who|my|i|the|a|an)[\s,]+', re.I)


def _strip_lead_in(text):
    """Drop the sentence around a name, so '@bot is oracle 16 worth it' still resolves.

    Only leading filler comes off, and only while what is left still looks like a name. Trimming the
    other end would risk cutting a variant number, which is the part that changes the answer.
    """
    body = str(text or '').strip(' ?!.,:;')
    for _ in range(3):
        trimmed = LEAD_IN.sub('', body).strip(' ?!.,:;')
        if trimmed == body or not trimmed:
            break
        body = trimmed
    return body or str(text or '').strip()




def mention_issues(text, data=None):
    """Named items in the sentence that the catalog refuses, with the variants that do exist.

    A player asking about Arc Torrent 8 has to hear that it is not a thing before anything else is said,
    and a fuzzy near-miss is not a licence to guess - so this only reports a phrase when a real family
    name sits right in front of a number the catalog does not carry.
    """
    data = data or load()
    words = normalise(text).split()
    out, seen = [], set()
    for index, token in enumerate(words):
        if not re.fullmatch(r'\d{1,2}', token):
            continue
        variant = int(token)
        for size in (3, 2, 1):
            start = index - size
            if start < 0:
                continue
            family = ' '.join(words[start:index])
            rows = data.family_variants.get(family) or []
            if not rows or family not in _candidate_names(data) or family in seen:
                continue
            known = sorted({int(row.get('variant_number') or 0) for row in rows if row.get('variant_number')})
            if not known or variant in known:
                continue
            seen.add(family)
            label = family.title()
            out.append({'phrase': f'{label} {variant}', 'status': 'refused', 'family': family,
                        'note': (f'{label} {variant} is not in the catalog'
                                 + (f'; it comes in {", ".join(str(value) for value in known)}' if known else '')),
                        'candidates': [f'{label} {value}' for value in known]})
            break
    return out


def resolve(text, data=None, strict=False):
    """Turn whatever the user typed into one catalog item, or into a question.

    `strict` is what the prose reader uses when it is guessing at a name inside a sentence: exact, alias
    and family-variant matching only, no fuzzy and no trimming words off the end. A typo deserves a
    question, not a confident recommendation about the wrong mech.

    Matching runs exact, then alias, then family and variant, then fuzzy, and stops there: a top score
    below 0.85, or two candidates within 0.1 of each other, comes back `ambiguous` with the options
    attached. A wrong item is a worse answer than a question.
    """
    data = data or load()
    raw = str(text or '').strip()
    key = normalise(_strip_lead_in(raw))
    result = {'input': raw, 'status': 'unknown', 'item': None, 'candidates': [], 'notes': []}
    if not key:
        result['status'] = 'missing'
        return result
    if key in EXCLUDED_ITEMS:
        result.update(status='refused', reason='not_in_catalog', notes=[EXCLUDED_ITEMS[key]])
        family = normalise(re.sub(r'\s*\d+$', '', key))
        result['candidates'] = [row['name'] for row in data.family_variants.get(family, [])]
        return result

    names = _candidate_names(data)
    kind, family_row = names.get(key, (None, None))
    if kind is None and not strict:
        # 'savant 10 the better buy' is the name plus a clause: peel words off the end until a known
        # name shows up, but only when exactly one name fits, so a guess never becomes a match
        words = key.split(' ')
        hits = []
        for stop in range(len(words) - 1, 0, -1):
            trial = ' '.join(words[:stop])
            if trial in names:
                hits = [(trial, names[trial])]
                break
        if len(hits) == 1:
            key, (kind, family_row) = hits[0]
            result['notes'].append(f'read "{raw}" as {key} with the rest of the sentence ignored')
    if kind is None:
        match = re.match(r'^(.*?)\s+(\d{1,2})$', key)
        if match:
            family, variant = normalise(match.group(1)), int(match.group(2))
            rows = data.family_variants.get(family, [])
            hit = next((row for row in rows if (row.get('variant_number') or 0) == variant), None)
            if hit:
                kind, family_row = 'weapon', hit
                key = normalise(hit.get('name'))
            elif family in data.weapon_reviews or family in data.mech_reviews:
                known = sorted({row.get('variant_number') for row in rows})
                result.update(status='refused', reason='variant_does_not_exist',
                              notes=[f'{match.group(1).title()} {variant} is not in the catalog'
                                     + (f'; it comes in {", ".join(str(value) for value in known)}' if known else '')],
                              candidates=[f'{match.group(1).title()} {value}' for value in known])
                return result
    if kind is None and not strict:
        scored = sorted(((SequenceMatcher(None, key, name).ratio(), name) for name in names
                         if _fuzzy_allowed(key, name)), reverse=True)[:4]
        if scored and scored[0][0] >= 0.6 and len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
            # two names are equally close, so the sentence is a question, not a lookup
            result.update(status='ambiguous', candidates=[name for _score, name in scored[:3]])
            result['notes'].append(f'"{raw}" is about as close to {", ".join(name for _score, name in scored[:2])} '
                                   'as it is to anything else in the catalog')
            return result
        if not scored or scored[0][0] < 0.85:
            if scored and scored[0][0] >= 0.6:
                result.update(status='ambiguous', candidates=[name for _score, name in scored[:3]])
                result['notes'].append(f'the closest catalog names to "{raw}" are only a partial match')
            return result
        kind, family_row = names[scored[0][1]]
        key = scored[0][1]
        result['notes'].append(f'matched "{raw}" to "{scored[0][1]}" by fuzzy name similarity, not an exact name')

    if kind is None:
        result['status'] = 'unknown'
        return result
    item = _build_item(key, kind, family_row, data)
    if kind == 'weapon_family':
        variants = [row.get('variant_number') for row in data.family_variants.get(key, [])]
        result['notes'].append(f'{item["name"]} is a family; it ships in energy {"/".join(str(v) for v in variants) if variants else "unknown"}')
    result['item'] = item
    result['notes'].extend(item['flags'])
    result['status'] = 'ok'
    return result


def _build_item(key, kind, family_row, data):
    """One catalog row assembled from the store list, the family table and the tiered review.

    Reviews are keyed by family (the tier list reviews `Oracle`, not `Oracle 16`) while costs are keyed
    by variant, so a variant takes its price from itself and its tier from the family. Nothing is
    invented when either side is missing: the caller sees `tier_missing` or `not_purchasable`.
    """
    store = data.store_by_name.get(key) or {}
    name = store.get('list') or (family_row or {}).get('name') or key
    family = normalise(re.sub(r'\s*\d+$', '', normalise(name)))
    is_mech = kind in ('mech', 'mech_family')
    reviews = data.mech_reviews if is_mech else data.weapon_reviews
    review = reviews.get(key) or reviews.get(family) or {}
    review_key = key if reviews.get(key) else family
    variant = (family_row or {}).get('variant_number') or store.get('energy')
    energy = to_number((family_row or {}).get('calculator_energy_field') or store.get('energy') or variant)
    rarity = str(store.get('rarity') or review.get('rarity') or '').casefold()
    item = {
        'name': name.title() if name == name.lower() or name == name.upper() else name,
        'key': key, 'kind': 'mech' if is_mech else 'weapon', 'family': family, 'variant': variant,
        'energy': energy, 'rarity': rarity, 'tier': str(review.get('tier') or '').strip().upper() or None,
        'review': review, 'review_key': review_key if review else None, 'store': store,
        'category': review.get('category') or (family_row or {}).get('database_category'),
        'role': review.get('role'), 'identity': (family_row or {}).get('identity_status') or 'source_reported',
        'tier_source': 'community' if review.get('tier') else None, 'flags': [],
    }
    if review and item['tier'] and review_key != key:
        item['flags'].append(f'the tier comes from the {review.get("name")} family review, not from the '
                             f'{item["name"]} variant')
    if family_row and variant and energy and int(variant) != int(energy):
        item['flags'].append(f'the catalog records {item["name"]} at energy {int(energy)} although the name says '
                             f'{int(variant)}; the user-confirmed entry is preferred')
    if not review.get('tier'):
        item['flags'].append('no community tier for this item')
        item['tier_missing'] = True
    if store:
        item['trust'] = 'user-confirmed (store listing)'
    return item


# ---------------------------------------------------------------------------------------------
# prices, ladders, ECR history
# ---------------------------------------------------------------------------------------------

def ladder_class(item, data=None):
    data = data or load()
    rarity = str(item.get('rarity') or '').casefold().replace('-', '_')
    if not rarity:
        return None
    if item['kind'] == 'mech':
        candidate = f'{rarity}_mech'
    else:
        energy = item.get('energy') or item.get('variant')
        candidate = f'{rarity}_weapon_{int(energy)}' if energy else None
    if candidate and candidate in data.ladder_classes:
        return candidate
    if item['kind'] != 'mech' and rarity:
        energies = sorted({int(match.group(1)) for name in data.ladder_classes
                           for match in [re.match(rf'^{re.escape(rarity)}_weapon_(\d+)$', name)] if match})
        if energies:
            energy = item.get('energy') or item.get('variant') or 0
            nearest = min(energies, key=lambda value: abs(value - int(energy or 0)))
            return f'{rarity}_weapon_{nearest}'
    return None


def step_index(star, level):
    return max(0, min(41, (int(star or 1) - 1) * 6 + (int(level or 1) - 1)))


def cost_from(item, rank=1, data=None, level=1):
    """Cost to take an item from a rank (and optional level) to max, by walking the real 42-step ladder.

    The ladder is 7 stars of 6 levels, so a rank is a star and the sum starts at that step. Callers
    that only know the rank get rank 1 by default, and the answer states the assumption.
    """
    data = data or load()
    cls = ladder_class(item, data)
    if not cls:
        return {'status': 'unavailable', 'reason': f'no upgrade ladder for {item.get("rarity") or "unknown rarity"} {item["kind"]}'}
    start = step_index(rank, level)
    total = {currency: 0 for currency in ('credits', 'acoins', 'blueprints')}
    for step in data.ladder[start:]:
        cell = step.get(cls) or {}
        for currency in total:
            total[currency] += int(cell.get(currency) or 0)
    # one rank is six ladder levels; `next_step` is what the very next upgrade actually costs
    here = data.ladder[start] if start < len(data.ladder) else {}
    cell = here.get(cls) or {}
    nxt = {currency: int(cell.get(currency) or 0) for currency in ('credits', 'acoins', 'blueprints')}
    nxt = {key: value for key, value in nxt.items() if value}
    return {'status': 'ok', 'class': cls, 'from_step': start + 1, 'from_rank': int(rank or 1),
            'steps': len(data.ladder) - start, 'next_step': nxt, **total,
            'trust': 'user-confirmed (upgrade ladder)'}


def store_price(item, data=None):
    data = data or load()
    row = data.store_by_name.get(item['key']) or data.store_by_name.get(item['family']) or {}
    cost = row.get('cost') or {}
    currency = str(cost.get('currency') or '').casefold()
    amount = to_number(cost.get('amount'))
    if currency in ('a-coins', 'acoins', 'a coins'):
        currency = 'acoins'
    elif currency.startswith('credit'):
        currency = 'credits'
    if amount is None or currency not in ('acoins', 'credits'):
        return {'status': 'not_purchasable', 'currency': cost.get('currency'), 'raw': currency or 'none'}
    return {'status': 'ok', 'currency': currency, 'amount': int(amount), 'energy': row.get('energy'),
            'trust': 'user-confirmed (store listing)'}


def ecr_history(item, data=None):
    data = data or load()
    rows = list(data.ecr_by_name.get(item['key'], [])) or list(data.ecr_by_name.get(item['family'], []))
    if not rows:
        return {'status': 'unavailable', 'appearances': 0}
    dated = [row for row in rows if parse_dates(row.get('end')) or parse_dates(row.get('start'))]
    latest = max(dated, key=lambda row: parse_dates(row.get('end')) or parse_dates(row.get('start'))) if dated else rows[-1]
    last_end = parse_dates(latest.get('end'))
    days_ago = None
    if last_end:
        days_ago = (datetime.now(timezone.utc).date() - last_end).days
    cps = str(latest.get('cps') or '')
    match = re.match(r'^(\d[\d,]*)\s*(ac|cr)\b', cps)
    parsed = None
    if match:
        parsed = {'amount': int(match.group(1).replace(',', '')),
                  'currency': 'acoins' if match.group(2) == 'ac' else 'credits'}
    return {'status': 'ok', 'appearances': len(rows), 'latest_start': str(latest.get('start') or ''),
            'latest_end': str(latest.get('end') or ''), 'days_since_end': days_ago,
            'items': to_number(latest.get('items')), 'rank': latest.get('rank'),
            'skewed': bool(latest.get('skewed')), 'cps_text': cps or None, 'cps': parsed,
            'chances': latest.get('chances'), 'trust': 'community'}


def ecr_value(item, data=None):
    """How the last ECR price for this item compared with the Gear Hub price, when both are known."""
    history = ecr_history(item, data)
    price = store_price(item, data)
    if history.get('status') != 'ok' or price.get('status') != 'ok':
        return {'status': 'unavailable', 'reason': 'no ECR history or no store price for this item'}
    cps = history.get('cps')
    if not cps or cps['currency'] != price['currency']:
        return {'status': 'partial', 'ecr': cps, 'store': price,
                'reason': 'the ECR cost and the store price are in different currencies, so no percentage is computed'}
    if not price['amount']:
        return {'status': 'unavailable', 'reason': 'store price is zero'}
    saving = (price['amount'] - cps['amount']) / price['amount']
    return {'status': 'ok', 'ecr': cps, 'store': price, 'discount_pct': round(saving * 100, 1),
            'cheaper_in': 'ECR' if saving > 0 else 'Gear Hub'}


def skin_relevance(item, data=None):
    """Popularity of a mech's legendary skins, from the vote tally the pack shipped.

    Counts are a snapshot (see `asof`) of one community poll, so this is a taste signal with an expiry
    date, not a measure of how good a mech is.
    """
    data = data or load()
    if item['kind'] != 'mech':
        return {'likes': 0, 'legendary_skins': 0, 'asof': data.skin_votes_asof}
    target = normalise(item['name'])
    names = [row.get('Skin name') for row in data.legendary_skins if normalise(row.get('Mech name')) == target]
    likes = []
    for name in names:
        value = data.skin_likes.get(name)
        if isinstance(value, (int, float)):
            likes.append(int(value))
    return {'likes': max(likes or [0]), 'legendary_skins': len(names), 'top_skins': names[:3],
            'asof': data.skin_votes_asof}


def _label_keys(rows, rank, label_part):
    """Match a stat label case-insensitively, exact before substring.

    The screenshots use 'HP' on newer sheets and 'Mech HP' on older ones for the same row, so the
    comparison has to tolerate both without letting 'Mech HP' swallow 'Mech Healing Speed'.
    """
    wanted = str(label_part).casefold().strip()
    available = [str(key) for key in (rows.get(str(rank)) or {})]
    folded = {key.casefold(): key for key in available}
    if wanted in folded:
        return [folded[wanted]]
    if wanted == 'hp':
        return [folded['mech hp']] if 'mech hp' in folded else []
    if wanted == 'mech hp':
        return [folded['mech hp']] if 'mech hp' in folded else ([folded['hp']] if 'hp' in folded else [])
    return [key for key in available if key.casefold() == wanted]


def verified_stat(item, label_part, rank=1, data=None):
    """One user-confirmed stat cell, with the status the reviewer gave it.

    A blank or missing row returns None rather than a zero: the guide ships percentage rows for some
    mechs and absolute rows for others, and reading the wrong kind as the other would be a made-up
    number wearing the bot's voice.
    """
    data = data or load()
    rows = data.cells.get(normalise(item['name'])) or data.cells.get(item['family']) or {}
    if not rows:
        return None
    wanted = int(rank or 1)
    ranks = sorted((int(value) for value in rows if str(value).isdigit()))
    if not ranks:
        return None
    for candidate in sorted(ranks, key=lambda value: abs(value - wanted)):
        key = str(candidate)
        for label in _label_keys(rows, key, label_part):
            cell = rows[key].get(label) or {}
            if cell.get('number') is None:
                continue
            if label_part.casefold() in ('hp', 'mech hp') and cell.get('unit') == 'percentage':
                continue        # a +13% bonus is not an HP figure
            return {'label': label, 'value': cell['value'], 'number': cell['number'], 'rank': candidate,
                    'unit': cell.get('unit'), 'status': cell.get('status'),
                    'trust': 'user-confirmed (screenshot)'}
    return None


def percentage_curve(item, label_part, data=None):
    """The rank-to-rank growth the screenshots record as percentages, when they record it at all."""
    data = data or load()
    rows = data.cells.get(normalise(item['name'])) or data.cells.get(item['family']) or {}
    found = {}
    for key, labels in rows.items():
        if not str(key).isdigit():
            continue
        for label in _label_keys(rows, key, label_part):
            cell = labels.get(label) or {}
            if cell.get('number') is not None:
                found[int(key)] = (cell['number'], cell.get('unit'))
    if not found:
        return None
    low = min(found)
    high = max(found)
    if found[low][1] != 'percentage' or high == low:
        return None
    return {'label': label_part, 'from_rank': low, 'to_rank': high,
            'from_pct': found[low][0], 'to_pct': found[high][0],
            'gain_pct': round(found[high][0] - found[low][0], 2), 'trust': 'user-confirmed (screenshot)'}


# ---------------------------------------------------------------------------------------------
# the scoring spine
# ---------------------------------------------------------------------------------------------

def tier_score(item):
    tier = str(item.get('tier') or '').upper()
    if tier in TIER_SCORE:
        return TIER_SCORE[tier], False
    return TIER_MISSING, True


MECH_ROLE_FIT = {
    'cq': {'attacker': 1.0, 'scout': 0.9, 'tank': 0.6, 'support': 0.5},
    'close': {'attacker': 1.0, 'scout': 0.9, 'tank': 0.7, 'support': 0.4},
    'sniper': {'scout': 1.0, 'attacker': 0.9, 'tank': 0.5, 'support': 0.5},
    'long': {'scout': 1.0, 'attacker': 0.9, 'tank': 0.5, 'support': 0.5},
    'support': {'support': 1.0, 'tank': 0.6, 'scout': 0.45, 'attacker': 0.4},
    'scout': {'scout': 1.0, 'attacker': 0.8, 'support': 0.6, 'tank': 0.4},
    'tank': {'tank': 1.0, 'support': 0.7, 'attacker': 0.6, 'scout': 0.4},
    'attacker': {'attacker': 1.0, 'tank': 0.7, 'scout': 0.6, 'support': 0.4},
}


def role_fit(item, ctx):
    """How well an item suits the play style the question implies, or neutral when it says nothing.

    A mech's role is the catalog's own word for it, so a tank main asking about aegis gets a real
    answer instead of a shrug; an unrecognized role scores neutral rather than guessing at a penalty.
    """
    role = str((ctx or {}).get('role') or '').casefold()
    if role in ('', 'any'):
        return 0.5, 'role not given, scored neutral'
    if item['kind'] == 'mech':
        mech_role = str(item.get('role') or '').casefold()
        table = MECH_ROLE_FIT.get(role)
        if not table:
            return 0.5, f'role {role} is not modelled'
        if mech_role in table:
            return table[mech_role], f'mech role {mech_role}'
        return 0.4, f'mech role {mech_role or "unknown"} for a {role} ask'
    category = str(item.get('category') or '').casefold()
    table = {'cq': (WEAPON_CQ, 1.0, WEAPON_MID, 0.7), 'close': (WEAPON_CQ, 1.0, WEAPON_MID, 0.7),
             'sniper': (WEAPON_LONG, 1.0, WEAPON_GUIDED, 0.7), 'long': (WEAPON_LONG, 1.0, WEAPON_GUIDED, 0.7),
             'scout': (WEAPON_LONG, 1.0, WEAPON_MID, 0.7), 'support': (('support',), 1.0, (), 0.5),
             'tank': (WEAPON_CQ + ('heavy',), 1.0, WEAPON_MID, 0.8),
             'attacker': (WEAPON_CQ + WEAPON_MID, 1.0, WEAPON_LONG, 0.6)}
    good, good_value, mid, mid_value = table.get(role, ((), 0.5, (), 0.5))
    if category in good:
        return good_value, f'{category} weapon for a {role} build'
    if category in mid:
        return mid_value, f'{category} weapon is workable for {role}'
    return 0.3, f'{category or "unknown-category"} weapon against a {role} ask'


def cost_efficiency(item, ctx=None, data=None):
    data = data or load()
    price = store_price(item, data)
    proxy = power_proxy(item, data)
    if proxy is None:
        return None, {'reason': 'no power rating for this item'}
    if price.get('status') != 'ok':
        return proxy, {'not_purchasable': True, 'reason': 'not a priced store item'}
    ceiling = data.max_cost.get((item['kind'], price['currency']))
    if not ceiling:
        return proxy, {'reason': 'no cost ceiling for comparison'}
    return proxy * (1 - min(1.0, price['amount'] / ceiling)), {'cost_norm': round(price['amount'] / ceiling, 3)}


def power_proxy(item, data=None):
    data = data or load()
    review = item.get('review') or {}
    if item['kind'] == 'mech':
        power = to_number(review.get('power'))
        return None if power is None else min(1.0, power / data.mech_scale['power'])
    rating = to_number(review.get('dmgPerEnergyRating'))
    if rating is not None:
        return max(0.0, min(1.0, rating / 5))
    fire = to_number(review.get('fireRate'))
    magazine = to_number(review.get('magazine'))
    reload_speed = to_number(review.get('reloadSpeed'))
    if fire and magazine and reload_speed:
        cycle = magazine / max(fire, 1) + reload_speed / 60
        return max(0.0, min(1.0, (magazine / cycle) / 12)) if cycle else None
    return None


def meta_score(item, data=None, at=None):
    data = data or load()
    score, parts = 0.0, []
    history = ecr_history(item, data)
    if history.get('status') == 'ok':
        days = history.get('days_since_end')
        if days is not None and days <= 90:
            score += 0.40
            parts.append(f'in an ECR {days} days ago')
        else:
            score += 0.20
            parts.append(f'{history["appearances"]} ECR appearances in the community history')
    skins = skin_relevance(item, data)
    if skins['likes'] >= 150:
        score += 0.20
        parts.append(f'legendary skin with {skins["likes"]} likes')
    name = normalise(item['name'])
    family = item['family']
    for title in data.trusted_document_words():
        if name and (name in title or family in title):
            score += 0.10
            parts.append('mentioned in a trusted archived document (undated)')
            break
    return min(1.0, round(score, 3)), parts


def stat_score(item, rank=1, data=None):
    """Verified stats only. No curve, no score: the caller reports the gap rather than inventing one."""
    data = data or load()
    scale = data.mech_scale
    if item['kind'] == 'mech':
        parts, used = [], []
        for label, ceiling in (('HP', scale['hp']), ('Speed', scale['speed']), ('Power', scale['power'])):
            cell = verified_stat(item, label, rank, data)
            if cell and cell['number'] is not None and cell.get('unit') != 'percentage':
                parts.append(min(1.0, cell['number'] / ceiling))
                used.append(f'{label} {cell["value"]}@rank {cell["rank"]}')
        if parts:
            return sum(parts) / len(parts), {'basis': 'user-confirmed cells', 'cells': used}
        review = item.get('review') or {}
        hp, power = to_number(review.get('hp')), to_number(review.get('power'))
        if hp and power:
            return (min(1.0, hp / 300000) + min(1.0, power / scale['power'])) / 2, {
                'basis': 'community catalog values, not the confirmed cells'}
        return None, {'reason': 'no stat row for this mech'}
    review = item.get('review') or {}
    rating = to_number(review.get('dmgPerEnergyRating'))
    if rating is None:
        return None, {'reason': 'no confirmed weapon stat cells and no community rating'}
    return max(0.0, min(1.0, rating / 5)), {'basis': 'community rating (dmg per energy)', 'cells': []}


def score(item, ctx=None, profile='balanced', data=None):
    data = data or load()
    weights = dict(PROFILES.get(profile, PROFILES['balanced']))
    tier, tier_missing = tier_score(item)
    fit, fit_why = role_fit(item, ctx)
    efficiency, cost_detail = cost_efficiency(item, ctx, data)
    meta, meta_why = meta_score(item, data)
    stats, stat_detail = stat_score(item, (ctx or {}).get('rank') or 1, data)
    parts = {'tier': tier, 'role': fit, 'cost': efficiency, 'meta': meta, 'stats': stats}
    flags = list(item.get('flags') or [])
    if tier_missing:
        flags.append('tier_missing')
    if efficiency is None:
        flags.append('cost_efficiency_unavailable')
    if stats is None:
        flags.append('no_stat_basis')
    live = {name: value for name, value in parts.items() if value is not None}
    total_weight = sum(weights[name] for name in live)
    total = sum(weights[name] * value for name, value in live.items()) / total_weight if total_weight else None
    if len(live) != len(parts):
        flags.append('weights_renormalised_over_' + '_'.join(sorted(set(parts) - set(live))))
    return {'total': None if total is None else round(total, 3), 'parts': {k: (None if v is None else round(v, 3))
            for k, v in parts.items()}, 'weights': {name: weights[name] for name in live},
            'profile': profile, 'flags': flags, 'why': {'role': fit_why, 'meta': meta_why, 'cost': cost_detail,
                                                         'stats': stat_detail}}


# ---------------------------------------------------------------------------------------------
# the weekly guide, read from the curated announcement facts
# ---------------------------------------------------------------------------------------------

def guide_advice(item, facts=None, data=None):
    """What the announcement channel's own guide said about this item, if it mentioned it."""
    data = data or load()
    target = normalise(item['name'])
    family = item['family']
    found = []
    for fact in facts or []:
        if fact.get('category') != 'ecr':
            continue
        for entry in ((fact.get('details') or {}).get('entries') or []):
            label = normalise(entry.get('label'))
            if not label or (family not in label and target not in label):
                continue
            found.append({'item': entry.get('label'), 'advice': entry.get('advice'),
                          'note': entry.get('note'), 'date': str(fact.get('title') or ''),
                          'posted_at': fact.get('posted_at'), 'message_id': fact.get('message_id'),
                          'url': fact.get('message_url')})
    verdicts = [hit['advice'] for hit in found if hit.get('advice')]
    disagrees = bool(verdicts) and str(item.get('tier')) in ('S', 'A') and 'skip' in verdicts
    return {'mentions': found[:3], 'verdict': verdicts[0] if verdicts else None,
            'disagrees_with_tier': disagrees,
            'why': ('the tier list rates raw power, the ECR guide rates value for currency in that week'
                    if disagrees else None)}


# ---------------------------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------------------------

ROLE_WORDS = (
    ('cq', r'\b(?:cq|close[- ]?quarters?|close ?range|brawl|rush(?:er|ing)?|shotgun|smg)\b'),
    ('sniper', r'\b(?:sniper|long[- ]?range|beam|artillery|guided|missile)\b'),
    ('support', r'\b(?:support|heal(?:ing|s)?|shield)\b'),
    ('tank', r'\b(?:tank|tanky|bruiser|durable|front ?line)\b'),
    ('scout', r'\b(?:scout|flank(?:er|ing)?|hit and ?run)\b'),
    ('attacker', r'\b(?:attacker|assault|aggressive)\b'),
)
MAIN_MECH_ROLES = ('scout', 'tank', 'support', 'attacker', 'assault')
# "rank" and its shorthand, with an optional copula, so "my rank is 3" reads the same as "rank 3".
RANK_BODY = r'(?:rank|stars?|r)[ \s=]*(?:is[ \s]+|=[ \s]*)?([1-7])(?!\d)'
RANK_ANY = re.compile(r'\b' + RANK_BODY, re.I)
# "worth upgrading to rank 5" is a target, and the same digits in "i am rank 4" are where the player
# actually sits. Reading one as the other prices the wrong stretch of the ladder, so the two shapes are
# matched apart rather than by whichever number turns up first.
RANK_TARGET = re.compile(r'\b(?:to|until|max(?:ed)?(?: up)? to|up to|-{1,2}>)\s*' + RANK_BODY, re.I)
RANK_MINE = re.compile(r"\b(?:i['’]?m|i am|im|my(?:self)?|mine|current(?:ly)?|already|still|sitting"
                       r"|have(?: it)?|on|at)\b[^.?;]{0,14}?\b" + RANK_BODY, re.I)


def _spans(matches):
    return [(match.start(), match.end()) for match in matches]


def _own_rank(body):
    """The rank the player says they are sitting at, or None.

    A number attached to a goal is not a starting point: `cost_from` has to start where the item is, not
    where the player hopes it will be. Any sentence that names two ranks without saying which is which
    yields no rank at all, and the engine answers from rank 1 while saying so.
    """
    if not body:
        return None
    targets = _spans(RANK_TARGET.finditer(body))

    def aimed(start, end):
        return any(start >= low and end <= high for low, high in targets)

    mine = RANK_MINE.search(body)
    if mine and not aimed(mine.start(1), mine.end(1)):
        return int(mine.group(1))
    plain = [match for match in RANK_ANY.finditer(body) if not aimed(match.start(), match.end())]
    if len(plain) == 1:
        return int(plain[0].group(1))
    return None


KIND_WORDS = (('mech', r'\bmechs?\b'), ('weapon', r'\bweapons?\b|\bguns?\b|\bcannon|railgun|chain ?gun'))


def _ctx(text=None, data=None, **extra):
    """Everything a prose question implies but never states as a flag.

    The player only ever types a sentence, so the engine has to read role, goal, budget, rank and
    ownership out of it. Anything it cannot read stays None, which is what keeps the follow-up
    questions and the assumption list honest.
    """
    ctx = {'role': None, 'goal': None, 'budget': parse_budget(text) if text else {}, 'owned': [],
           'engagement': None, 'rank': 1, 'kind': None, 'level': None, 'real_money': False, 'main': None}
    body = str(text or '').casefold()
    if body:
        for role, pattern in ROLE_WORDS:
            if re.search(pattern, body):
                ctx['role'] = role
                break
        main = re.search(r'\bmain(?:s|ing)?\s+([a-z][a-z -]{2,15}?)(?:[,.?!\s](?:i|my|what|how|best|should|for|to)|[,.?!]|$)',
                         body)
        if main:
            word = normalise(main.group(1)).split(' ')[0]
            if word in MAIN_MECH_ROLES:
                ctx['main'] = word
                ctx['role'] = 'attacker' if word == 'assault' else word
        if re.search(r'\b(?:f2p|free ?to ?play|no money|low ?budget|on a budget|bang for (?:the )?buck|'
                     r'best value|cheap(?:est)?|new ?player|low ?level|starting out|catch(?:ing)? ?up)\b', body):
            ctx['goal'] = 'f2p_catch_up'
        elif re.search(r'\b(?:compete|competitive|ranked|tryhard|clan ?war|squad ?war|top ladder|'
                       r'best possible|max meta)\b', body):
            ctx['goal'] = 'compete'
        elif re.search(r'\b(?:max(?:e|ing)? (?:it|one|my)|one ?and ?done|all in|fully upgrade)\b', body):
            ctx['goal'] = 'max_one'
        if re.search(r'\bclose[- ]?(?:range|quarters?)\b|\bcq fight\b', body):
            ctx['engagement'] = 'close'
        elif re.search(r'\blong[- ]?range\b', body):
            ctx['engagement'] = 'long'
        elif re.search(r'\bmid[- ]?range\b', body):
            ctx['engagement'] = 'mid'
        rank = _own_rank(body)
        if rank:
            ctx['rank'] = rank
        kinds = {name for name, pattern in KIND_WORDS if re.search(pattern, body)}
        ctx['kind'] = kinds.pop() if len(kinds) == 1 else None
        ctx['real_money'] = bool(re.search(r'\$\s?\d|usd|bucks|real money|web ?store|crystal|discount', body))
        ctx['owned'] = _owned_names(text, data)
    ctx.update({key: value for key, value in extra.items() if value is not None})
    return ctx


def _owned_names(text, data):
    """Item names the player claims to already have, resolved through the same matcher as everything else."""
    if not data or not text:
        return []
    clause = re.search(r'\bi\s+(?:own|have|got|run|play|use|main)(?:s|ed|ing)?\b([^?!.]*)', str(text), re.I)
    if not clause:
        return []
    return _mention_names(clause.group(1), data)


def profile_for(ctx, question=''):
    """Which weighting set the sentence asks for, without the player naming one.

    'bang for buck' is the budget profile, 'ranked' is tryhard, a crate question is the ECR profile.
    A budget number on its own only nudges the profile when nothing else is stated, because a player
    with 400 A-coins can still be asking what is genuinely best.
    """
    goal = str((ctx or {}).get('goal') or '')
    body = str(question or '').casefold()
    if goal == 'compete':
        return 'tryhard'
    if goal == 'f2p_catch_up':
        return 'budget'
    if re.search(r'\becr\b|\bcrates?\b', body) and not re.search(r'\bstat|dps\b', body):
        return 'ecr'
    if goal == 'max_one':
        return 'balanced'
    if (ctx or {}).get('budget'):
        return 'budget'
    return 'balanced'


# Keys that are bookkeeping rather than something a player reads. An id, a timestamp or a resolved
# pointer must not widen what the reply guard considers safe to say, or the guard becomes decorative.
OPAQUE_KEYS = ('resolved', 'url', 'message_url', 'message_id', 'posted_at', 'channel_id', 'guild_id', 'fact_id',
               'key', 'order', 'source', 'input', 'item', 'after', 'weights', 'provenance', 'ecr_history')


def _numbers(result):
    out = set()

    def walk(node):
        if isinstance(node, dict):
            for name, value in node.items():
                if name in OPAQUE_KEYS:
                    continue
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            out.update(re.findall(r'\d[\d,]*(?:\.\d+)?%?', node))
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            # every rendering the voice layer might use: exact, :g, and the thousands-separated form
            out.add(str(int(node)) if float(node).is_integer() else repr(round(float(node), 6)))
            out.add(f'{node:g}')
            out.add(str(round(node, 2)))
            if float(node).is_integer() and abs(node) >= 1000:
                out.add(f'{int(node):,}')
    walk(result)
    return out


CAVEAT_STALE = ('the advisor pack this engine reads was built {date}, so treat anything the game changed since '
                'then as unconfirmed.')
CAVEAT_COMMUNITY = ('tiers and dmg-per-energy come from the community catalog, which players maintain - they are '
                    'labels, not developer numbers.')
CAVEAT_SIMULATED = ('the power figure is a modelled estimate from community ratings and store costs, not measured '
                     'in-game damage, so ranks are a ranking, not a scoreboard.')
CAVEAT_CONVENTION = ('stats are read at rank 1 unless you said otherwise, and energy is whatever the catalog row '
                     'carries for that item.')
CAVEAT_DISCOUNT = ('the store list is the in-game price; the web-store discount only applies if you are paying real '
                    'money, which this engine never assumes.')

TRUST_KEYS = ('trust', 'tier_source')
TRUST_LABELS = {'community': 'community (catalog values)', 'user-confirmed': 'user-confirmed (screenshot stat cells)',
                'community (dmg per energy)': 'community (catalog values)'}


def _stamp(kind, **extra):
    result = {'kind': kind, 'engine': 'advisor-v1', 'confidence': 'LOW', 'caveats': [], 'tiers_used': [],
              'assumptions': [], 'data_gaps': [], 'followups': [], 'missing_slots': [], 'numbers': set()}
    result.update(extra)
    return result


def _trusts(result):
    """Every trust label attached to any number in this answer, so a reply can cite its sources."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in TRUST_KEYS and isinstance(value, str) and value:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
    walk({key: value for key, value in result.items() if key != 'resolved'})
    return sorted({TRUST_LABELS.get(value, value) for value in found})


def _finish(result, data=None):
    """Attach sources and required caveats, and freeze the number set a reply may quote."""
    data = data or load()
    trusts = sorted({*(result.get('tiers_used') or []), *_trusts(result)})
    result['tiers_used'] = trusts
    caveats = []
    stale = data.staleness_caveat()
    if stale:
        caveats.append(stale)
    if any('community' in tier for tier in trusts):
        caveats.append(CAVEAT_COMMUNITY)
    if (result['kind'] in ('invest', 'plan', 'sim_1v1')
            or any(('simulat' in tier or 'modelled' in tier) for tier in trusts)):
        caveats.append(CAVEAT_SIMULATED)
    if any(('rank' in assumption or 'energy' in assumption) for assumption in result.get('assumptions') or []):
        caveats.append(CAVEAT_CONVENTION)
    if any('paid' in str(value).casefold() for value in (result.get('numbers'),)) or result.get('real_money'):
        caveats.append(CAVEAT_DISCOUNT)
    seen, unique = set(), []
    for caveat in (*caveats, *(result.get('caveats') or [])):
        key = ' '.join(str(caveat).split())
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    result['caveats'] = unique
    result['numbers'] = sorted(_numbers({key: value for key, value in result.items()
                                          if key not in ('numbers', 'resolved')}))
    return result


def explain(name, facts=None, data=None, ctx=None):
    facts = _facts(facts)
    data = data or load()
    found = resolve(name, data)
    if found['status'] != 'ok':
        return _finish(_clarify_or_fail(found, 'explain', facts, data))
    item = found['item']
    priced = store_price(item, data)
    ladder = cost_from(item, (ctx or {}).get('rank') or 1, data)
    scored = score(item, ctx, profile_for(ctx, name), data)
    result = _stamp('explain', item=item['name'], kind_label=item['kind'], tier=item.get('tier'),
                    rarity=item.get('rarity'), energy=item.get('energy'),
                    # The catalog holds HP, speed, power and the ability for a reviewed mech, and the
                    # answer to "redox" was built without ever reading them: it gave energy and a
                    # weighted score while 227,300 HP sat in the same record.
                    stats=mech_stat_sheet(item), resolved=found,
                    review={key: (item.get('review') or {}).get(key) for key in
                            ('strengths', 'weaknesses', 'reasoning', 'perk', 'optimalRange', 'dmgPerEnergy',
                             'fireRate', 'magazine', 'reloadSpeed', 'ability', 'role', 'category')},
                    energy_levels=(item.get('review') or {}).get('energyLevels'), perk=(item.get('review') or
                                                                                           {}).get('perk'),
                    price=priced, cost_to_max=ladder, scores=scored,
                    ecr=ecr_history(item, data), value=ecr_value(item, data), skins=skin_relevance(item, data),
                    guide=guide_advice(item, facts, data), notes=list(found['notes']))
    result['tiers_used'] = ['community (tier list)' if item.get('tier') else 'tier missing',
                            priced.get('trust') or 'store price unavailable',
                            ladder.get('trust') or 'no ladder match']
    if result['guide'].get('disagrees_with_tier'):
        result['caveats'].append('the weekly guide contradicts the tier list for this item, and both are shown')
    if scored['parts'].get('stats') is None:
        result['data_gaps'].append('no user-confirmed stat row for this item')
    result['verdict'] = _value_verdict(item, scored, priced, ladder, result['guide'], result['value'])
    if item.get('tier') == 'D':
        result['verdict'] = 'not worth maxing, ' + result['verdict'][0].lower() + result['verdict'][1:]
        rival = _better_alternative(item, ctx, data)
        if rival:
            result['alternative'] = rival
    from_cells = str((scored['why'].get('stats') or {}).get('basis') or '') == 'user-confirmed cells'
    if item.get('tier') and priced.get('status') == 'ok' and from_cells:
        result['confidence'] = 'HIGH'
    elif item.get('tier') and priced.get('status') == 'ok':
        result['confidence'] = 'MEDIUM'
    result['what_would_change'] = ('your energy capacity and what you already run' if item['kind'] == 'weapon'
                                   else 'the mech you pilot and whether you fight at its range band')
    return _finish(result, data)


def _better_alternative(item, ctx, data):
    """The cheapest same-kind item the catalog rates higher, for a do-not-invest answer."""
    target = TIER_SCORE.get(str(item.get('tier') or '').upper(), 0.5)
    best, best_cost = None, None
    for key, listing in data.store_by_name.items():
        candidate = _build_item(key, listing.get('type') or 'mech', None, data)
        if candidate['kind'] != item['kind'] or candidate['key'] == item['key']:
            continue
        score_of = score(candidate, ctx, profile_for(ctx or {}), data)
        tier_of = TIER_SCORE.get(str(candidate.get('tier') or '').upper(), 0.5)
        if tier_of <= target or (score_of['total'] or 0) <= (scored_total(item, ctx, data) or 0):
            continue
        priced = store_price(candidate, data)
        if priced.get('status') != 'ok':
            continue
        cost = amount_text(priced['currency'], priced['amount'])
        if best is None or priced['amount'] < (best_cost or 0):
            best, best_cost = {'name': candidate['name'], 'tier': candidate.get('tier'), 'cost': cost}, \
                priced['amount']
    return best


def scored_total(item, ctx, data):
    return score(item, ctx, profile_for(ctx or {}), data)['total']


def mech_stat_sheet(item, rank=1):
    """(label, value) pairs held for this mech, at the rank asked for.

    Two sources, and the answer says which it used: user-confirmed cells come first because they
    carry a rank, and the community review row is the fallback. Nothing is estimated here - a
    label with no value is simply absent.
    """
    if item.get('kind') != 'mech':
        return []
    review = item.get('review') or {}
    sheet = []
    for label, keys, unit in (('HP', ('hp',), ''), ('Speed', ('speed', 'speedKmh'), ' km/h'),
                              ('Power', ('power',), ''), ('Energy', ('energy',), '')):
        for key in keys:
            value = review.get(key)
            if value not in (None, ''):
                sheet.append({'label': label, 'value': str(value).strip(), 'unit': unit,
                              'source': 'community catalog'})
                break
    if not sheet:
        data = None
        sheet = []
    return sheet


def _value_verdict(item, scored, priced, ladder, guide, value):
    parts = []
    tier = item.get('tier')
    if tier:
        parts.append(f'tier {tier} in the community list')
    if guide.get('verdict'):
        parts.append(f'the weekly guide says {ADVICE_PHRASE[guide["verdict"]]}')
    if value.get('status') == 'ok':
        parts.append(f'{"cheaper" if value["discount_pct"] > 0 else "pricier"} in the ECR by {abs(value["discount_pct"]):g}%')
    # The weighted total is not reported. It is a number about our own weights, computed from
    # whatever happened to be available, and a bare "scores 0.405 on the balanced weights" tells a
    # player nothing they can act on. The parts that feed it - tier, the guide's line, the ECR
    # discount - are the parts worth saying.
    # Nothing but our own arithmetic stood behind a rating, so there is no rating to give. What is
    # left is a fact about the data, and it is the fact that matters: no tier, no guide line.
    return '; '.join(parts) or 'no community tier and no ECR appearance on record'


ADVICE_PHRASE = {'skip': 'to skip it', 'worth_it': 'it is worth the spend', 'depends': 'it depends on your build'}


def compare(a, b, question='', facts=None, data=None):
    facts = _facts(facts)
    data = data or load()
    ctx = _ctx(question)
    left, right = resolve(a, data), resolve(b, data)
    if 'ok' not in (left['status'], right['status']):
        broken = next(row for row in (left, right) if row['status'] != 'ok')
        merged = dict(broken, input=f'{a} / {b}',
                      candidates=list(left.get('candidates') or []) + list(right.get('candidates') or []),
                      notes=list(left['notes']) + list(right['notes']))
        return _finish(_clarify_or_fail(merged, 'compare', facts, data))
    profile = profile_for(ctx, f'{a} {b} {question}')
    item_a, item_b = left['item'], right['item']
    score_a, score_b = score(item_a, ctx, profile, data), score(item_b, ctx, profile, data)
    rows = _head_to_head(item_a, item_b, score_a, score_b, data, facts)
    total_a, total_b = score_a['total'], score_b['total']
    if total_a is None or total_b is None:
        verdict, confidence = 'NO DATA', 'LOW'
    elif abs(total_a - total_b) < 0.05:
        verdict, confidence = 'TIE', 'LOW'
    else:
        verdict = 'A' if total_a > total_b else 'B'
        confidence = _confidence(item_a, item_b, score_a, score_b)
    # A component only counts when both sides have one: a missing stat must not read as a zero.
    labels = {'tier': 'tier', 'role': 'role_fit', 'cost': 'cost_efficiency', 'meta': 'meta_relevance',
              'stats': 'stats'}
    deltas = sorted(((component, round(abs(score_a['parts'][component] - score_b['parts'][component]), 3))
                     for component in score_a['parts']
                     if score_a['parts'][component] is not None and score_b['parts'][component] is not None),
                    key=lambda row: -row[1])

    decisive = [{'component': name, 'label': labels.get(name, name), 'gap': gap}
                for name, gap in deltas if gap > 0][:3]
    winner, loser = (item_a, item_b) if verdict == 'A' else (item_b, item_a)
    winner_score, loser_score = (score_a, score_b) if verdict == 'A' else (score_b, score_a)
    counter = None
    for component, delta in deltas:
        if delta and loser_score['parts'][component] > winner_score['parts'][component]:
            counter = (f'{loser["name"]} wins {component.replace("_", " ")}'
                       f' ({loser_score["parts"][component]:g} against {winner_score["parts"][component]:g})')
            break
    if not counter:
        # nothing the loser wins on in the weights: look at the raw numbers instead of pretending the
        # gap is total, and name the loser's one advantage if it has one
        cells = {row['label']: row for row in rows}
        damage = cells.get('dmg per energy') or {}
        try:
            worth = [float(str(damage.get(side)).replace('~', '').replace(',', '')) for side in ('A', 'B')]
        except (TypeError, ValueError):
            worth = [None, None]
        loser_row = ('B' if verdict == 'A' else 'A') if verdict in ('A', 'B') else None
        if all(worth) and loser_row and worth[0] != worth[1] and worth[1 if loser_row == 'B' else 0] > \
                worth[0 if loser_row == 'B' else 1]:
            counter = (f'{(item_b if loser_row == "B" else item_a)["name"]} still leads on dmgPerEnergy '
                       f'({damage.get(loser_row)} against {damage.get("A" if loser_row == "B" else "B")}), '
                       'which is what the rating is built from')
        elif loser_row and rows:
            counter = (f'the gap is small enough that {(item_b if loser_row == "B" else item_a)["name"]} is a '
                       'reasonable pick if it suits your build better')
    guide_a, guide_b = guide_advice(item_a, facts, data), guide_advice(item_b, facts, data)
    result = _stamp('compare', picks=[item_a['name'], item_b['name']], verdict=verdict,
                    scores={'A': score_a, 'B': score_b}, head_to_head=rows, profile=profile,
                    decisive_factors=decisive, counter_argument=counter, confidence=confidence,
                    guide={'A': guide_a, 'B': guide_b}, why=_why(item_a, item_b, score_a, score_b, verdict),
                    assumptions=ctx_assumptions(ctx), notes=(left['notes'] + right['notes']),
                    resolved={'A': item_a, 'B': item_b})
    result['tiers_used'] = sorted({'community (tier list)' if (item_a.get('tier') or item_b.get('tier')) else 'tier missing',
                                   'user-confirmed (store listing)', 'user-confirmed (upgrade ladder)',
                                   'community (ECR history)'})
    for gap in (f'{item_a["name"]}: no stat basis' if score_a['parts']['stats'] is None else None,
                f'{item_b["name"]}: no stat basis' if score_b['parts']['stats'] is None else None):
        if gap:
            result['data_gaps'].append(gap)
    if guide_a.get('disagrees_with_tier') or guide_b.get('disagrees_with_tier'):
        result['caveats'].append('the weekly ECR guide and the community tier list disagree on one of these, '
                                 'so read the verdict as value for currency rather than raw power')
    if (ctx or {}).get('role') and score_a['parts']['role'] != score_b['parts']['role']:
        result['decisive_factors'].insert(0, {'component': 'role', 'label': 'role_fit',
                                              'gap': round(abs(score_a['parts']['role'] - score_b['parts']['role']),
                                                          3), 'for': (item_a if score_a['parts']['role'] >
                                                                     score_b['parts']['role'] else item_b)['name'],
                                              'role': ctx['role']})
    result['what_would_change'] = _compare_swings(item_a, item_b, ctx)
    return _finish(result, data)


def _compare_swings(a, b, ctx):
    bits = []
    if a['kind'] == 'weapon' or b['kind'] == 'weapon':
        bits.append('your energy capacity, since the higher-energy pick only pays off if you can carry it')
    if not ctx.get('role'):
        bits.append('which role you play')
    if ctx.get('budget'):
        bits.append('whether the cheaper pick leaves enough for the upgrade ladder')
    return '; '.join(bits) or 'a stat row you trust more than the community rating'


def _confidence(a, b, score_a, score_b):
    have_stats = all(row['parts'].get('stats') is not None for row in (score_a, score_b))
    tiers = bool(a.get('tier')) and bool(b.get('tier'))
    priced = store_price(a, score_a.get('data'))['status'] == 'ok' and store_price(b, score_b.get('data'))['status'] == 'ok'
    if tiers and have_stats and priced:
        return 'HIGH'
    if tiers and priced:
        return 'MEDIUM'
    return 'LOW'


def _head_to_head(a, b, score_a, score_b, data, facts):
    rows = []

    def cell(item, key):
        return (item.get('review') or {}).get(key)

    for label, getter in (
            ('tier', lambda item: item.get('tier') or 'unrated'),
            ('family', lambda item: item.get('family')),
            ('energy', lambda item: item.get('energy')),
            ('rarity', lambda item: item.get('rarity')),
            ('category', lambda item: item.get('category') or item.get('role')),
            ('dmg per energy', lambda item: cell(item, 'dmgPerEnergy')),
            ('optimal range', lambda item: cell(item, 'optimalRange')),
            ('store price', lambda item: amount_text(item_price(item)['currency'], item_price(item)['amount'])
             if item_price(item)['status'] == 'ok' else 'not priced'),
            ('cost to max', lambda item: _ladder_text(cost_from(item, 1, data))),
            ('last ECR', lambda item: ecr_history(item, data).get('latest_end') or 'not in the history'),
            ('guide advice', lambda item: ADVICE_PHRASE.get((guide_advice(item, facts, data) or {}).get('verdict'),
                                                            'the guide did not mention it')),
            ('score', lambda item: None)):
        left = getter(a) if label != 'score' else score_a['total']
        right = getter(b) if label != 'score' else score_b['total']
        if left in (None, '') and right in (None, ''):
            continue
        rows.append({'label': label, 'A': left if left not in (None, '') else 'not in data',
                     'B': right if right not in (None, '') else 'not in data'})
    return rows


def item_price(item, data=None):
    return store_price(item, data or load())


def _ladder_text(ladder):
    if ladder.get('status') != 'ok':
        return 'no ladder row'
    return ' '.join(f'{ladder[key]:,} {label_currency(key)}' for key in ('credits', 'acoins', 'blueprints')
                    if ladder.get(key)) or 'free to finish'


def _why(a, b, score_a, score_b, verdict):
    if verdict in ('TIE', 'NO DATA'):
        return 'the gap between them is inside the noise of what the data can measure'
    winner, loser, winner_score = (a, b, score_a) if verdict == 'A' else (b, a, score_b)
    loser_score = score_b if verdict == 'A' else score_a
    strong = [name for name, value in winner_score['parts'].items()
               if value is not None and loser_score['parts'].get(name) is not None
               and value - loser_score['parts'][name] > 0.08]
    parts = [f'{winner["name"]} leads on {", ".join(name.replace("_", " ") for name in strong)}'
             if strong else f'{winner["name"]} scores higher overall']
    review = winner.get('review') or {}
    if review.get('strengths'):
        parts.append(review['strengths'].strip('.'))
    return '. '.join(parts) + '.'


def ctx_assumptions(ctx):
    out = []
    if not ctx.get('role'):
        out.append('role not given, so nothing was scored for a specific playstyle')
    if not ctx.get('budget'):
        out.append('no budget given, so cost was scored against the item class rather than your pocket')
    return out


def _clarify_or_fail(found, kind, facts, data):
    result = _stamp(kind, verdict='ASK' if found['status'] in ('ambiguous', 'missing') else 'REFUSED',
                    status=found['status'], candidates=found.get('candidates') or [],
                    notes=found.get('notes') or [], resolved=None)
    if found['status'] == 'refused':
        result['data_gaps'].extend(found.get('notes') or [])
        result['caveats'].append('the item was refused because the catalog says it does not exist')
        return result
    slots = ['items'] if kind == 'compare' else ['item']
    result['missing_slots'] = slots
    result['followups'] = ['Which two are you choosing between? Name them as the game does, for example '
                           'Oracle 16 and Savant 10.' if kind == 'compare' else
                           'Which item do you mean? The closest names in the catalog are: '
                           + ', '.join(str(candidate).title() for candidate in (found.get('candidates') or [])[:3])]
    return result


def rank_investments(question='', facts=None, data=None, limit=5):
    """C2: what to spend on, ranked, with the price of each answer and a do-not-invest list beside it.

    Candidates come from the real store list, so every row is something a player can actually buy.
    Items already owned are re-priced as the next ladder step rather than a second purchase, a crate
    that is live right now is shown as the cheaper route into the same item, and anything the guide
    says to skip is moved into `do_not_invest` with the catalog's own reason - a recommendation that
    only lists good buys is a shop window, not advice.
    """
    data = data or load()
    facts = _facts(facts)
    ctx = _ctx(question, data)
    budget = ctx['budget']
    owned = {normalise(name) for name in ctx['owned']}
    profile = profile_for(ctx, question)
    candidates, skipped = [], []
    for key, listing in data.store_by_name.items():
        item = _build_item(key, listing.get('type') or 'mech', None, data)
        if item['kind'] not in ('mech', 'weapon') or (ctx['kind'] and item['kind'] != ctx['kind']):
            continue
        scored = score(item, ctx, profile, data)
        priced = store_price(item, data)
        ladder = cost_from(item, ctx['rank'], data)
        step = ladder.get('next_step') or {}
        already = key in owned or any(normalise(name) == item['family'] for name in ctx['owned'])
        gain = _power_gain(item, data)
        cost = step if (already and step) else _total_cost(priced, ladder)
        row = {'item': item, 'name': item['name'], 'kind': item['kind'], 'tier': item.get('tier'),
               'score': scored['total'], 'weights': scored['weights'], 'profile': profile,
               'action': 'upgrade a rank' if already else 'buy',
               'owned': already, 'buy': priced, 'cost_to_max': ladder, 'total_cost': cost,
               'power_gain': gain['gain'], 'power_gain_basis': gain['basis'], 'gain_group': gain['group'],
               'roi': _roi(gain['gain'], cost), 'roi_basis': ('verified growth curve' if gain['gain'] is not None
                                                              else 'cost efficiency only, no confirmed stat curve'),
               'ecr': ecr_value(item, data), 'ecr_history': ecr_history(item, data),
               'guide': guide_advice(item, facts, data)}
        row['fit'] = _cost_check(cost, budget)
        row['affordable'] = row['fit']['affordable']
        row['skipped_reason'] = _skip_reason(row)
        (skipped if row['skipped_reason'] else candidates).append(row)
    # do_not_invest quotes the review, so it runs before the item payload is dropped

    # a bonus-percentage gain and a full-ladder gain are not the same unit, so they are never added up
    groups = {row['gain_group'] for row in candidates if row['gain_group']}
    use_roi = bool(candidates) and len(groups) == 1 and all(row['roi'] is not None for row in candidates)

    def key_of(row):
        return (-(row['roi'] or 0) if use_roi else -(row['score'] or 0))

    # a pick that needs currencies the player never mentioned is a worse pick at equal merit
    candidates.sort(key=lambda row: (not row['affordable'], key_of(row), -(row['score'] or 0),
                                     len(row['fit']['unstated'])))
    stretch = [row for row in candidates if not row['affordable']][:2]
    ranked = [dict(row, roi_comparable=bool(use_roi)) for row in candidates if row['affordable']][:limit]
    missing = []
    if not budget:
        missing.append('budget')
    if not ctx['role']:
        missing.append('role')
    missing = missing[:2]
    result = _stamp('invest', ranked=ranked, do_not_invest=_do_not_invest(skipped, data, facts),
                    stretch=stretch, profile=profile, budget=budget, confidence='MEDIUM' if ranked else 'LOW',
                    assumptions=ctx_assumptions(ctx) + ([f'ranked on the {profile} weights'] if ranked else []),
                    goal=ctx.get('goal'),
                    roi_basis=('percent growth per currency spent, measured the same way for every row'
                               if use_roi else 'weighted score, because the growth figures are not all '
                                               'measured the same way'),
                    provisional=bool(missing) and bool(ranked), missing_slots=(missing if ranked else []),
                    followups=[FOLLOWUP_TEXT[slot] for slot in missing] if ranked else [],
                    next_milestone=_next_milestone(candidates, remaining(budget, ranked)) if budget else None)
    if not budget and not ctx['role'] and not ctx['goal'] and not ctx['owned'] and not ctx['kind']:
        # nothing to go on, so the answer is two questions and no ranking
        result = _stamp('invest', verdict='ASK', ranked=[], stretch=[], do_not_invest=[], profile=profile,
                        budget=budget, confidence='LOW', missing_slots=['budget', 'role'],
                        followups=[FOLLOWUP_TEXT['budget'], FOLLOWUP_TEXT['role']],
                        assumptions=['no budget, no role and no item named, so there is nothing to rank yet'])
        return _finish(result, data)
    if not ranked:
        result['missing_slots'] = ['budget'] if not budget else ['goal']
        result['followups'] = [FOLLOWUP_TEXT[slot] for slot in result['missing_slots']]
        result['verdict'] = 'ASK'
    else:
        result['verdict'] = ranked[0]['name']
        if not budget:
            result['data_gaps'].append('nothing was filtered by affordability because no budget was given')
        elif all(row['fit']['unstated'] for row in ranked):
            missing = sorted({key for row in ranked for key in row['fit']['unstated']})
            result['assumptions'].append('you named ' + ', '.join(sorted(budget)) + ' only, so every price below '
                                         'also needs ' + ' and '.join(label_currency(key) for key in missing) +
                                         ' you did not mention')
        if not use_roi:
            result['data_gaps'].append('the growth figures in this shortlist are not all measured the same way, '
                                       'so the order is the weighted score and the gains are context')
        if stretch:
            result['data_gaps'].append(f'{len(stretch)} pricier option(s) sit outside the budget and are listed '
                                       'separately')
    if ranked and ranked[0].get('ecr', {}).get('cheaper_in') == 'ECR':
        result['data_gaps'].append(f"{ranked[0]['name']} is cheaper out of a crate than at the shelf price, so "
                                   'timing matters as much as the pick')
    result['tiers_used'] = ['community (tier list)', 'community (weekly ECR guide wording)',
                            'user-confirmed (store listing)', 'user-confirmed (upgrade ladder)',
                            'user-confirmed (screenshot stat cells)']
    # the review prose is engine input, not answer material: dropping it keeps the number guard honest
    for row in (*result['ranked'], *result['stretch']):
        row.pop('item', None)
    return _finish(result, data)


def remaining(budget, ranked):
    out = dict(budget or {})
    for row in ranked[:1]:
        for currency, amount in (row['total_cost'] or {}).items():
            if amount:
                out[currency] = max(0, out.get(currency, 0) - amount)
    return out


def _next_milestone(candidates, leftover):
    """The cheapest thing the budget does not quite reach, and by how much."""
    best = None
    for row in candidates:
        cost = row['total_cost'] or {}
        need = {currency: cost[currency] - leftover.get(currency, 0) for currency in cost
                if cost[currency] > leftover.get(currency, 0)}
        if not need:
            continue
        distance = sum(need.values())
        if best is None or distance < best['distance']:
            best = {'name': row['name'], 'action': row['action'], 'needs': need, 'distance': distance,
                    'cost': cost}
    if not best:
        return None
    best.pop('distance')
    return best


def _power_gain(item, data):
    """Percent growth over the ladder, from the confirmed stat rows only.

    Damage is what a rank-up actually buys you, so mechs are read off Power, then HP, and a weapon
    with no confirmed curve gets None rather than an invented one. Every gain is a percentage, which
    is the one unit in which a sniper mech and a shotgun are comparable.
    """
    for label in ('Power', 'Mech HP', 'HP'):
        curve = percentage_curve(item, label, data)
        if curve and curve['gain_pct']:
            basis = (f"user-confirmed {curve['label']} bonus curve, rank {curve['from_rank']} to "
                     f"{curve['to_rank']} (+{curve['gain_pct']:g}%)")
            return {'gain': abs(float(curve['gain_pct'])), 'basis': basis, 'group': 'bonus-curve'}
    low = verified_stat(item, 'Power', 1, data)
    high = verified_stat(item, 'Power', 7, data)
    if not (low and high) or (low.get('unit') == 'percentage' or high.get('unit') == 'percentage'):
        low, high = verified_stat(item, 'HP', 1, data), verified_stat(item, 'HP', 7, data)
    if low and high and low['number'] and high['number'] > low['number'] \
            and low.get('unit') != 'percentage':
        pct = round((high['number'] - low['number']) / low['number'] * 100.0, 1)
        return {'gain': pct, 'group': 'full-ladder',
                'basis': (f"user-confirmed {low['label']} {low['value']} at rank 1 to {high['value']} at "
                          f"rank {high['rank']}")}
    return {'gain': None, 'group': None,
            'basis': 'no confirmed growth curve for this item, so it is ranked on cost alone'}


def _total_cost(priced, ladder):
    out = {key: 0 for key in CURRENCIES}
    if priced.get('status') == 'ok':
        out[priced['currency']] += priced['amount']
    if ladder.get('status') == 'ok':
        for key in ('credits', 'acoins', 'blueprints'):
            out[key] += int(ladder.get(key) or 0)
    return out


def _fits(budget, total):
    return all(total.get(key, 0) <= budget.get(key, 0) for key in CURRENCIES if total.get(key, 0))


def _roi(gain, total):
    if gain is None:
        return None
    spend = sum(total.get(key, 0) for key in ('credits', 'blueprints')) + 400 * total.get('acoins', 0)
    if spend <= 0:
        return None
    return round(gain * 1000000 / spend, 4)


UNPLANNED_CURRENCIES = {'paid': 'real money', 'referral': 'referrals', 'free': 'nothing', 'none': 'nothing'}


def _weak(row):
    """True when this item should not be in a spend answer at all."""
    return _skip_reason(row) is not None


def _skip_reason(row):
    """Why an item is on the do-not-invest list, in one clause and in the data's own terms."""
    tier = str(row.get('tier') or '')
    review = (row['item'].get('review') or {})
    reasoning = str(review.get('reasoning') or '').casefold()
    if (row.get('guide') or {}).get('verdict') == 'skip' and tier not in ('S', 'A'):
        return "the guide in the announcement says skip this one"
    buy = row.get('buy') or {}
    if buy.get('status') != 'ok':
        raw = str(buy.get('raw') or 'none').casefold()
        label = UNPLANNED_CURRENCIES.get(raw)
        if raw == 'paid':
            return 'it costs real money, not credits or A-coins, so it is not a budget answer'
        if label and label != 'nothing':
            return f'it is only obtainable with {label}, which is not a spend you can plan around'
        return 'it is not priced on the store list, so there is nothing to compare'
    if tier == 'D':
        line = ('tier D in the community list: ' + reasoning).strip(': ')
        return line[:1].upper() + line[1:]
    if tier == 'C' and any(word in (str(review.get('weaknesses') or '') + ' ' + reasoning)
                           for word in ('worst', 'outclassed', 'lowest')):
        return 'tier C in the community list, and the review puts it at the bottom of its category'
    return None


def _cost_check(cost, budget):
    """Whether the currencies the player named cover this, and what they left out.

    Saying "4000 ac" is not a declaration of zero Credits, so an unmet currency the player never
    mentions is reported as a gap to fill rather than a reason to refuse the item.
    """
    if not budget:
        return {'affordable': True, 'short': {}, 'unstated': {}}
    short, unstated = {}, {}
    for key, amount in (cost or {}).items():
        if not amount:
            continue
        if key in budget:
            if amount > budget[key]:
                short[key] = amount - budget[key]
        else:
            unstated[key] = amount
    return {'affordable': not short, 'short': short, 'unstated': unstated}


def _do_not_invest(skipped, data, facts):
    """The other half of an invest answer: what to leave alone, with the reason the data gives."""
    out = []
    for row in sorted(skipped, key=lambda item: -(item['score'] or 0))[:3]:
        review = (row['item'].get('review') or {})
        if row.get('skipped_reason'):
            out.append({'name': row['name'], 'tier': row.get('tier'), 'reason': row['skipped_reason'],
                        'weakness': str(review.get('weaknesses') or '')[:180] or None,
                        'guide': (row.get('guide') or {}).get('verdict')})
            continue
        reason = str(review.get('reasoning') or '').strip() or (
            f'tier {row["tier"]} in the community list' if row.get('tier') else 'no tier and no confirmed stat row')
        out.append({'name': row['name'], 'tier': row.get('tier'),
                    'reason': (row.get('skipped_reason') or reason)[:220],
                    'weakness': str(review.get('weaknesses') or '')[:180] or None,
                    'guide': (row.get('guide') or {}).get('verdict')})
    return out


def _ladder_steps(row, data, rank=1):
    """The rungs of one item's ladder, as per-step costs, starting where the player says they are."""
    item = row['item']
    cls = ladder_class(item, data)
    if not cls:
        return []
    start = step_index(rank, 1)
    out = []
    for step in data.ladder[start:]:
        cell = step.get(cls) or {}
        cost = {currency: int(cell.get(currency) or 0) for currency in ('credits', 'acoins', 'blueprints')}
        cost = {key: value for key, value in cost.items() if value}
        if cost:
            out.append(cost)
    return out


def _affordable_ranks(steps, spare):
    """Complete ranks the spare budget covers, and the rungs that make them up.

    Only whole ranks are taken: six loose rungs are one rank, and a plan that quotes rungs reads like a
    spreadsheet rather than advice.
    """
    taken, ranks = [], 0
    while len(steps) >= 6:
        block, steps = steps[:6], steps[6:]
        cost = {}
        for step in block:
            for currency, amount in step.items():
                cost[currency] = cost.get(currency, 0) + amount
        if not _fits(spare, cost):
            break
        for currency, amount in cost.items():
            spare[currency] -= amount
        taken.extend(block)
        ranks += 1
    return ranks, taken


def plan_budget(question='', facts=None, data=None):
    """C3: spend a stated budget greedily, one real action at a time.

    Each step is either buying something off the store list or the next rung of an item's ladder, and
    every one of them is charged against a ledger of the currencies you actually named. The order is
    chosen by what a unit of your scarcest currency buys, not by what is coolest, and the leftovers are
    reported so the player can see the plan is finite rather than a nudge to spend more.
    """
    data = data or load()
    facts = _facts(facts)
    ctx = _ctx(question, data)
    budget = dict(ctx['budget'])
    if not budget:
        result = _stamp('plan', verdict='ASK', missing_slots=['budget', 'goal'], confidence='LOW',
                        followups=[FOLLOWUP_TEXT['budget'], FOLLOWUP_TEXT['goal']],
                        plan=[], spent={key: 0 for key in CURRENCIES}, leftover=dict(budget),
                        assumptions=['a plan needs a number to spend'])
        return _finish(result, data)
    owned = {normalise(name) for name in ctx['owned']}
    profile = profile_for(ctx, question)
    remaining = {key: int(budget.get(key) or 0) for key in CURRENCIES}
    actions, rejected = [], []
    for key, listing in data.store_by_name.items():
        item = _build_item(key, listing.get('type') or 'mech', None, data)
        if item['kind'] not in ('mech', 'weapon') or (ctx['kind'] and item['kind'] != ctx['kind']):
            continue
        scored = score(item, ctx, profile, data)
        gain = _power_gain(item, data)
        ladder = cost_from(item, ctx['rank'], data)
        buy = store_price(item, data)
        already = key in owned or any(normalise(name) == item['family'] for name in ctx['owned'])
        reason = _skip_reason({'item': item, 'tier': item.get('tier'), 'buy': buy,
                               'guide': guide_advice(item, facts, data)})
        if reason:
            rejected.append({'name': item['name'], 'tier': item.get('tier'), 'reason': reason,
                             'score': scored['total']})
            continue
        actions.append({'name': item['name'], 'item': item, 'kind': item['kind'], 'tier': item.get('tier'),  # noqa
                        'cost': ({buy['currency']: int(buy['amount'])} if buy.get('status') == 'ok' else {}),
                        'gain': gain['gain'], 'gain_basis': gain['basis'], 'gain_group': gain['group'],
                        'score': scored['total'], 'owned': already, 'buy': buy})
    # Each turn of the loop is one decision: for every item, the best action the remaining budget
    # actually covers (a purchase, then as many complete ranks as still fit). A rung at a time reads as
    # noise and never spends the budget, so the unit here is a rank, the way a player counts it.
    ladder_steps = {row['name']: _ladder_steps(row, data, ctx['rank']) for row in actions}
    # Merit decides the order, the budget decides how far each pick goes. A pure value-per-currency
    # greedy pass spends the whole budget on the cheapest rung on earth, which is arithmetic, not advice
    # - and the weighted score already contains cost efficiency, so the ordering is not ignoring price.
    actions.sort(key=lambda row: -(row['score'] or 0))
    plan, spent = [], {key: 0 for key in CURRENCIES}
    progress = {}       # name -> rungs already paid for, so nothing is bought or ranked twice
    purchased_family = set()
    for row in actions:
        if len(plan) >= 5:
            break
        name = row['name']
        # six A-coins short of a second Cryo Launcher is not a better plan than one of them
        if row['item']['family'] in purchased_family and not row['owned']:
            continue
        steps = ladder_steps.get(name) or []
        done = progress.get(name, 0)
        priced = row['buy'].get('status') == 'ok'
        price = {} if (row['owned'] or done or not priced) else dict(row['cost'])
        if price and not _fits(remaining, price):
            continue
        spare = dict(remaining)
        for currency, amount in price.items():
            spare[currency] -= amount
        ranks, taken = _affordable_ranks(steps[done:], spare)
        if not price and not ranks:
            continue
        total = dict(price)
        for step in taken:
            for currency, amount in step.items():
                total[currency] = total.get(currency, 0) + amount
        if not any(total.values()):
            continue
        for currency, amount in total.items():
            remaining[currency] -= amount
            spent[currency] = spent.get(currency, 0) + amount
        start_rank = 1 + done // 6
        to_rank = min(7, start_rank + ranks)
        progress[name] = done + max(6, ranks * 6)
        bought = bool(price) and not row['owned']
        if bought:
            purchased_family.add(row['item']['family'])
        label = ('buy' + (f' and rank to {to_rank}' if ranks else '')) if bought else (
            f'rank up to {to_rank}' if ranks else 'hold')
        plan.append({'action': label, 'name': name, 'kind': row['kind'], 'tier': row['tier'],
                     'cost': {currency: amount for currency, amount in total.items() if amount},
                     'cost_basis': ('purchase price' + (f' plus {ranks} rank(s)' if ranks else '') if bought
                                    else f'{ranks} rank(s) from the upgrade ladder'),
                     'from_rank': start_rank, 'to_rank': to_rank, 'gain': row['gain'],
                     'gain_basis': row['gain_basis'], 'gain_group': row['gain_group'], 'score': row['score'],
                     'owned': row['owned'], 'buy': row['buy'], 'after': dict(remaining)})
    unspent_picks = len({row['name'] for row in actions
                         if row['name'] not in progress and _fits(remaining, row['cost'])})
    unmentioned = [key for key in ('credits', 'acoins', 'blueprints') if not budget.get(key)]
    nearest = None
    for row in actions:
        if row['name'] in progress:
            continue
        need = {currency: amount - remaining.get(currency, 0) for currency, amount in row['cost'].items()
                if amount > remaining.get(currency, 0)}
        if not need:
            continue
        gap = sum(need.values())
        if nearest is None or gap < nearest['gap']:
            nearest = {'name': row['name'], 'action': 'buy', 'needs': need, 'gap': gap}
    if nearest:
        nearest.pop('gap')
    # how much of each currency you named survived the plan, which is the honest measure of tightness
    scarcity = {key: round(max(0, remaining.get(key, 0)) / float(budget[key]), 3) for key in CURRENCIES
                if budget.get(key)}

    result = _stamp('plan', verdict=plan[0]['name'], plan=plan, spent=spent, leftover=remaining, budget=budget,
                    next_milestone=nearest, do_not_buy=sorted(rejected, key=lambda row: -(row['score'] or 0))[:3],
                    profile=profile, confidence='MEDIUM', binding=', '.join(label_currency(key) for key, value in remaining.items()
                                                           if budget.get(key) and value <= 0) or 'none',
                    currency_scarcity={key: round(value, 3) for key, value in scarcity.items()},
                    assumptions=ctx_assumptions(ctx) + [f'ordered on the {profile} weights'],
                    goal=ctx.get('goal'))
    if unmentioned:
        result['assumptions'].append('you did not name ' + ' and '.join(label_currency(key) for key in unmentioned)
                                     + ', so the ladder stops wherever the currencies you did name run out')
    if unspent_picks:
        result['data_gaps'].append(f'the plan stops at {len(plan)} picks; {unspent_picks} other item(s) were '
                                   'affordable but scored lower')
    for row in plan:
        if row.get('gain') is None:
            result['data_gaps'].append(f"{row['name']}: no confirmed growth curve, so its place in the order is "
                                       'the weighted score')
            break
    result['caveats'].append('the order is a greedy read of listed costs, not a proof that no other sequence '
                             'is stronger')
    result['tiers_used'] = ['user-confirmed (store listing)', 'user-confirmed (upgrade ladder)',
                            'community (tier list)', 'user-confirmed (screenshot stat cells)',
                            'community (weekly ECR guide wording)']
    return _finish(result, data)


# The distance a "close / mid / long" question really means, in metres, so the catalog's own range
# bands can be compared against it instead of a category label.
BAND_DISTANCE = {'close': 15.0, 'mid': 70.0, 'long': 200.0}


def simulate_1v1(a_text, b_text, engagement='mid', data=None):
    """A loadout comparison that says out loud that it is an estimate.

    There is no damage formula to copy and the pack has no fire rates, so nothing here is a
    simulation of the game: damage is the community per-energy rating, HP is the screenshot row when
    one exists, and the range band tilts each weapon separately instead of slapping one multiplier on
    a whole build. The output is a ranking of who is better positioned to win on paper, plus the two
    things a player would have to change for that to flip.
    """
    data = data or load()
    left, right = _loadout(a_text, data), _loadout(b_text, data)
    result = _stamp('1v1', loadouts={'A': _loadout_summary(left), 'B': _loadout_summary(right)},
                    engagement=engagement, modelled_estimate=True, confidence='LOW',
                    caveats=['this is a modelled estimate from listed stats, not the game engine; '
                             'positioning, aim and team pressure decide real fights'])
    if not (left.get('mech') and right.get('mech')):
        result['verdict'] = 'ASK'
        result['missing_slots'] = ['mech']
        result['followups'] = ['Which mech on each side? I need both to say anything useful.']
        return _finish(result, data)
    hp_a, hp_detail_a = _hp(left['mech'], data)
    hp_b, hp_detail_b = _hp(right['mech'], data)
    if not (hp_a and hp_b):
        result['verdict'] = 'NO DATA'
        result['data_gaps'].append('one side has no HP row in the data, so there is nothing to divide')
        return _finish(result, data)
    band = engagement if engagement in ('close', 'mid', 'long') else 'mid'
    outcome = _fight(left, right, hp_a, hp_b, band)
    if outcome is None:
        # no weapon damage on either side: compare the mechs themselves and say so
        rating_a, rating_b = _raw_rating(left), _raw_rating(right)
        if not (rating_a and rating_b):
            result['verdict'] = 'NO DATA'
            result['data_gaps'].append('neither loadout has a damage figure in the catalog, and no mech-only '
                                       'power row either')
            return _finish(result, data)
        power_a = rating_a * hp_a
        power_b = rating_b * hp_b
        chance_a = power_a / (power_a + power_b)
        result.update({'dps': None, 'ttk': None, 'hp': {'A': hp_a, 'B': hp_b},
                       'win_chance': round(chance_a, 3), 'win_pct': round(chance_a * 100),
                       'power': {'A': rating_a, 'B': rating_b},
                       'decisive_factor': f'no weapons to model, so this is listed power times HP only, which '
                                          f'{"favours " + (left["mech"]["name"] if chance_a > 0.5 else right["mech"]["name"])}'
                                          if abs(power_a - power_b) / max(power_a, power_b) > 0.05 else
                                          'almost even on listed power and HP',
                       'hp_basis': {'A': hp_detail_a, 'B': hp_detail_b},
                       'what_would_change': 'the weapons you actually run, since they are not in this question'})
        result['confidence'] = 'LOW'
        result['verdict'] = 'A' if chance_a > 0.58 else 'B' if chance_a < 0.42 else 'TIE'
        return _finish(result, data)
    ttk_a, ttk_b, alpha_a, alpha_b, detail_a, detail_b = outcome
    result['assumptions'].append('sustained damage, read as damage per energy times energy over the listed '
                                 'magazine and reload time')
    result['band_distance_m'] = BAND_DISTANCE.get(band, BAND_DISTANCE['mid'])
    chance_a = ttk_b / (ttk_a + ttk_b)
    result.update({'ttk': {'A': round(ttk_a, 2), 'B': round(ttk_b, 2)},
                   'dps': {'A': round(alpha_a, 1), 'B': round(alpha_b, 1)}, 'hp': {'A': hp_a, 'B': hp_b},
                   'win_chance': round(chance_a, 3),
                   'decisive_factor': ('time to kill, which favours '
                                       + (left['mech']['name'] if chance_a > 0.5 else right['mech']['name']))
                   if abs(ttk_a - ttk_b) > 0.5 else 'almost even on paper, so the fight goes to positioning',
                   'damage_basis': {'A': detail_a, 'B': detail_b}, 'hp_basis': {'A': hp_detail_a,
                                                                                'B': hp_detail_b}})
    swing = {}
    for other in ('close', 'mid', 'long'):
        alt = _fight(left, right, hp_a, hp_b, other)
        if alt:
            swing[other] = round(alt[1] / (alt[0] + alt[1]), 3) if (alt[0] + alt[1]) else 0.5
    # display-ready percentages live here, so a reply never does its own arithmetic
    result['win_pct'] = round(chance_a * 100)
    result['win_pct_by_band'] = {name: round(value * 100) for name, value in swing.items()}
    result['sensitivity'] = swing
    flipped = [name for name, value in swing.items()
               if (value > 0.5) != (chance_a > 0.5) and abs(value - chance_a) > 0.03]
    result['verdict'] = 'A' if chance_a > 0.58 else 'B' if chance_a < 0.42 else 'TIE'
    confirmed = all(str(value.get('trust') or '').startswith('user-confirmed')
                    for value in (hp_detail_a, hp_detail_b))
    result['confidence'] = 'MEDIUM' if confirmed else 'LOW'
    result['what_would_change'] = (f'fighting at {", ".join(flipped)} range, where the model says the other side '
                                   'comes out in front' if flipped else
                                   'the weapons you run, since the mech stats are already what the sheet gives')
    return _finish(result, data)


def _fight(left, right, hp_a, hp_b, band):
    """Time to kill both ways at one engagement band. None when a side has no usable damage figure."""
    alpha_a, detail_a = _dps(left, band)
    alpha_b, detail_b = _dps(right, band)
    if not (alpha_a and alpha_b):
        return None
    return (hp_b / alpha_a, hp_a / alpha_b, alpha_a, alpha_b, detail_a, detail_b)


def _loadout_summary(loadout):
    return {'mech': (loadout['mech'] or {}).get('name'), 'role': (loadout['mech'] or {}).get('role'),
            'weapons': [weapon['name'] for weapon in loadout['weapons']],
            'pilot': (loadout['pilot'] or {}).get('name')}


def _loadout(text, data):
    out = {'text': str(text or ''), 'mech': None, 'weapons': [], 'pilot': None}
    for name in _mention_names(text, data):
        found = resolve(name, data)
        if found['status'] != 'ok':
            continue
        item = found['item']
        if item['kind'] == 'mech' and out['mech'] is None:
            out['mech'] = item
        elif item['kind'] == 'weapon' and len(out['weapons']) < 2:
            out['weapons'].append(item)
        elif item['kind'] == 'pilot' and out['pilot'] is None:
            out['pilot'] = item
    return out


def _raw_rating(loadout):
    """The mech's own listed power, used when a question names no weapons."""
    return to_number((loadout['mech'].get('review') or {}).get('power'))


def _weapon_cycle(weapon):
    """Damage and cycle time for one weapon at the energy the catalog gives it.

    DPS here is arithmetic on four listed numbers - damage per energy, energy level, magazine size and
    the reload - which is the only honest way to get a rate out of this pack: it carries no per-bullet
    damage and no travel time. Damage per energy is read as damage per magazine, the way the in-game
    calculator scales it, and the answer says so, because the other reading moves the number by the
    magazine size.
    """
    review = weapon.get('review') or {}
    per_energy = to_number(review.get('dmgPerEnergy'))
    energy = weapon.get('energy') or _max_level(review.get('energyLevels'))
    rpm = to_number(review.get('fireRate'))
    magazine = to_number(review.get('magazine'))
    reload_time = to_number(review.get('reloadSpeed')) or 0.0
    if not (per_energy and energy and rpm and magazine):
        return None
    cycle = max(0.1, magazine / (rpm / 60.0)) + reload_time
    return {'damage': round(per_energy * energy, 1), 'cycle': round(cycle, 2),
            'dps': per_energy * energy / cycle, 'energy': energy, 'magazine': magazine, 'rpm': rpm,
            'reload': reload_time}


def _max_level(value):
    numbers = [float(found) for found in re.findall(r'\d+(?:\.\d+)?', str(value or ''))]
    return max(numbers) if numbers else None


def _band_tilt(weapon, band):
    """How much of a weapon's damage the range band in the question leaves on the table."""
    review = weapon.get('review') or {}
    low, high = parse_range(review.get('optimalRange'))
    _, cap = parse_range(review.get('maxRange'))
    distance = BAND_DISTANCE.get(band or 'mid', BAND_DISTANCE['mid'])
    if high is None:
        return 1.0, 'no range figures to place it'
    if low is not None and low <= distance <= high:
        return 1.0, f'{distance:g} m is inside the {low:g}-{high:g} m optimal band'
    if cap and distance > cap:
        return 0.7, f'{distance:g} m is past the {cap:g} m maximum'
    return 0.9, f'{distance:g} m is outside the optimal band but under the {cap or high:g} m maximum'


def _dps(loadout, band='mid'):
    """Sustained damage of the whole loadout at one distance, from the catalog's own numbers."""
    total, parts = 0.0, []
    for weapon in loadout['weapons']:
        cycle = _weapon_cycle(weapon)
        if not cycle:
            parts.append(f'{weapon["name"]}: not enough listed numbers for a rate')
            continue
        tilt, why = _band_tilt(weapon, band)
        total += cycle['dps'] * tilt
        parts.append(f'{weapon["name"]}: {int(round(cycle["damage"])):,} damage per magazine at energy '
                     f'{int(cycle["energy"])} over a {cycle["cycle"]:g} s cycle'
                     + (f', {why}' if tilt != 1.0 else ''))
    if not total:
        return 0.0, {'basis': parts, 'trust': None, 'note': 'no weapon damage figures'}
    return total, {'basis': parts, 'trust': 'community (catalog values)', 'band': band,
                   'derivation': 'damage per energy x energy / (magazine time + reload), tilted by range'}


def _hp(item, data):
    cell = verified_stat(item, 'HP', 1, data)
    if cell and cell['number']:
        return cell['number'], {'trust': 'user-confirmed (screenshot stat cells)',
                                'shown': f'{item["name"]} HP {cell["value"]} at rank {cell["rank"]}',
                                'label': cell['label'], 'rank': cell['rank'], 'status': cell.get('status')}
    hp = to_number((item.get('review') or {}).get('hp'))
    if hp:
        return hp, {'trust': 'community (catalog sheet)',
                    'shown': f'{item["name"]} HP {item["review"].get("hp")} from the community catalog'}
    return None, {'trust': None, 'shown': 'no HP row for this mech'}


def _label_query(label):
    """Turn an announcement heading into something the resolver can read.

    Guide posts write "ORACLE 16 (R4)" and "PILOT - CHIMERA": the rank note is not part of the name and
    the category prefix is decoration, so both come off before matching. What is left has to survive
    the same resolver a player's typing goes through, which keeps "not in the catalog" honest.
    """
    text = str(label or '')
    text = re.sub(r'\(\s*(?:r|rank)\s*\d+[^)]*\)', ' ', text, flags=re.I)
    text = re.sub(r'^\s*(pilot|mech|weapon|gear)\s*[-–:]\s*', '', text, flags=re.I)
    text = re.sub(r'[^A-Za-z0-9 ]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def ecr_advice(name=None, facts=None, question='', data=None):
    """Read the current ECR offer from the announcement facts, and price each item against the Gear Hub."""
    data = data or load()
    facts = _facts(facts)
    ecr_facts = [fact for fact in facts if fact.get('category') == 'ecr'
                 and ((fact.get('details') or {}).get('entries') or [])]
    ecr_facts.sort(key=lambda fact: (str(fact.get('status') or '') != 'current', -int(fact.get('posted_at') or 0)))
    live = [fact for fact in ecr_facts if str(fact.get('status') or 'current') == 'current']
    picks = []
    for fact in ecr_facts[:2]:
        for entry in ((fact.get('details') or {}).get('entries') or []):
            if entry.get('section'):
                continue
            label = entry.get('label')
            found = resolve(_label_query(label), data)
            item = found['item'] if found['status'] == 'ok' else None
            value = ecr_value(item, data) if item else {'status': 'unavailable'}
            price = store_price(item, data) if item else {'status': 'unavailable'}
            ladder = cost_from(item, 1, data) if item else {'status': 'unavailable'}
            verdict = entry.get('advice')
            picks.append({'announced': label, 'resolved': (item or {}).get('name'), 'advice': verdict,
                          'note': entry.get('note'), 'ecr_status': fact.get('status'),
                          'tier': (item or {}).get('tier'),
                          'guide_line': (item or {}).get('review', {}).get('reasoning'),
                          'store_price': price, 'ecr_value': value, 'cost_to_max': ladder,
                          'score': score(item, _ctx(question), 'ecr', data)['total'] if item else None,
                          'posted_at': fact.get('posted_at'), 'url': fact.get('message_url'),
                          'source': str(fact.get('title') or '')})
    if not picks:
        result = _stamp('ecr', verdict='NO DATA',
                        data_gaps=['no ECR guide post in the saved announcements, so there is nothing to price'],
                        followups=['Which item are you deciding on? I can price it against the Gear Hub without '
                                   'the ECR.'])
        return _finish(result, data)
    worth = [pick for pick in picks if pick['advice'] == 'worth_it']
    skip = [pick for pick in picks if pick['advice'] == 'skip']
    verdict = ('skip' if len(skip) > len(worth) else 'consider' if worth else 'the post gives no verdict')
    if not live:
        # a finished crate is never presented as running; the guide line still travels with it
        verdict = f"no ECR is running, the last one ({ecr_facts[0].get('title') or 'untitled'}) is over"
    result = _stamp('ecr', verdict=verdict, picks=picks[:6], current_posts=len(live),
                    confidence='MEDIUM' if any(pick['resolved'] for pick in picks) else 'LOW',
                    caveats=['buy or skip is the guide\'s own wording, priced against the Gear Hub; the bot did not '
                             'decide it'] + ([f'the offer quoted here is from a post that is no longer running, so '
                                              f'treat the price as history'] if not live else []),
                    freshness='current' if live else 'finished',
                    assumptions=[pick['announced'] + ' is not in the catalog, so it is quoted without a price'
                                 for pick in picks if not pick['resolved']][:2])
    result['tiers_used'] = sorted({'community (weekly ECR guide wording)', 'user-confirmed (store listing)',
                                   'community (ECR history)'})
    result['what_would_change'] = ('the guide calling the offer cheaper than the Gear Hub, or your energy capacity'
                                   if not worth else 'a note that the item is already yours')
    return _finish(result, data)


def slots_in(question, data=None):
    """What the question already supplies, and what it must ask for. Two questions maximum."""
    data = data or load()
    text = str(question or '')
    ctx = _ctx(text, data)
    return {'items': _mention_names(text, data), 'budget': ctx['budget'], 'owned': ctx['owned'],
            'role': ctx['role'], 'goal': ctx['goal'], 'engagement': ctx['engagement'], 'kind': ctx['kind'],
            'rank': ctx['rank'], 'real_money': ctx['real_money']}


MENTION_STOP = {'i', 'a', 'an', 'the', 'is', 'are', 'do', 'does', 'should', 'what', 'which', 'who', 'my',
                'me', 'it', 'this', 'that', 'for', 'to', 'of', 'in', 'on', 'and', 'or', 'but', 'with',
                'buy', 'get', 'worth', 'best', 'can', 'am', 'be', 'vs', 'than', 'or', 'so', 'if', 'as',
                'at', 'have', 'has', 'got', 'will', 'would', 'could', 'ac', 'acoins', 'a', 'coins', 'coin',
                'credit', 'credits', 'blueprint', 'blueprints', 'bp', 'ecr', 'crate', 'crates', 'rank',
                'level', 'energy', 'worth', 'pull', 'save', 'main', 'mains', 'close', 'long', 'mid', 'range',
                'buying', 'spend', 'spending', 'invest', 'upgrade', 'upgrades', 'better', 'good', 'bad'}


def _singular_forms(token):
    """How one plural word could be spelled if it names a catalog item.

    The pack lists `Storm Rack`, the channel says `storm racks`, and a plural is exactly how the
    sentence gets typed. These are the only folds attempted, and each still has to pass the strict
    resolver, so a word that merely looks like a pluralised name does not quietly become one.
    """
    if len(token) <= 4:
        return []
    forms = []
    if token.endswith('ies'):
        forms.append(token[:-3] + 'y')
    elif token.endswith(('ches', 'shes', 'xes', 'zes', 'sses')):
        forms.append(token[:-2])
    elif token.endswith('s') and not token.endswith(('ss', 'us', 'is')):
        forms.append(token[:-1])
    return [form for form in forms if len(form) >= 4 and form != token]


def _phrase_variants(tokens):
    """The phrase as typed, then the same phrase with its plural words folded to singular stems."""
    phrase = ' '.join(tokens)
    yield phrase
    parts = list(tokens)
    for index, part in enumerate(parts):
        if re.fullmatch(r'\d{1,2}', part):
            continue
        folded = _singular_forms(part)
        if folded:
            parts[index] = folded[0]
    folded_phrase = ' '.join(parts)
    if folded_phrase != phrase:
        yield folded_phrase


def _mention_names(text, data):
    """Every catalog item a sentence mentions, whatever capitalisation the player happened to use.

    Players type in lowercase and they pluralise, so this walks the words of the sentence instead of
    leaning on capitals to find proper nouns, and it folds plurals onto the catalog's singular names.
    Only a phrase the resolver accepts outright counts: an ambiguous remainder is a question to ask,
    not a name to build a plan around.
    """
    words = normalise(text).split()
    if not words or not data:
        return []
    taken, hits = [False] * len(words), []
    for length in (4, 3, 2, 1):
        for start in range(len(words) - length + 1):
            if any(taken[start:start + length]):
                continue
            tokens = words[start:start + length]
            if all(re.fullmatch(r'\d{1,2}', part) for part in tokens) or any(part in MENTION_STOP
                                                                            for part in tokens):
                continue
            for phrase in _phrase_variants(tokens):
                found = resolve(phrase, data, strict=not (length == 1 and len(phrase) >= 6))
                if found['status'] != 'ok' or not found['item']:
                    continue
                for index in range(start, start + length):
                    taken[index] = True
                hits.append({'name': found['item']['name'], 'key': found['item']['key'], 'order': start})
                break
    hits.sort(key=lambda row: row['order'])
    seen, ordered = set(), []
    for hit in hits:
        if hit['key'] in seen:
            continue
        seen.add(hit['key'])
        ordered.append(hit['name'])
    return ordered


def gear_mentions(text, data=None):
    """The mech and weapon names in a sentence; pilots and mods are somebody else's feature.

    Keeping the advisor's scope to gear is what stops a pilot comparison from being answered as if it
    were a loadout question, and it is cheaper to filter here than to un-answer a wrong capability later.
    """
    data = data or load()
    out = []
    for name in _mention_names(text, data):
        found = resolve(name, data, strict=True)
        if found['status'] == 'ok' and (found['item'] or {}).get('kind') in ('mech', 'weapon'):
            out.append(name)
    return out


FOLLOWUP_TEXT = {
    'items': 'Which two are you choosing between? Name them the way the game does, for example Oracle 16 and Savant 10.',
    'item': 'Which item do you mean?',
    'budget': "What can you spend? Credits, A-coins and blueprints, roughly is fine.",
    'owned': 'What do you already have? It changes the answer a lot.',
    'role': 'Do you play close quarters, long range or support?',
    'goal': 'What are you optimising for: competing, catching up free to play, or maxing one thing?',
    'engagement': 'Where do you expect the fight to happen: close, mid or long range?',
}
DEFAULTS = {'role': ('any', 'flexible on role'), 'goal': ('compete', 'competitive play'),
            'owned': ([], 'you own none of them'), 'rank': (1, 'rank 1'),
            'engagement': ('mid', 'a mid-range fight')}


def apply_defaults(result, data=None):
    """Round three: stop asking, use the documented defaults and say so. A bot that nags is a bot that is muted."""
    for slot in list(result.get('missing_slots') or []):
        if slot in DEFAULTS:
            value, wording = DEFAULTS[slot]
            result.setdefault('assumptions', []).append(f'assuming {wording}')
    result['missing_slots'] = [slot for slot in (result.get('missing_slots') or []) if slot not in DEFAULTS]
    if not result['missing_slots'] and result.get('verdict') in ('ASK', None):
        result['followups'] = []
    return result
