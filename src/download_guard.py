"""Season intent and cancellation checks shared by planning and disk sync."""
import json
import re
import unicodedata
from pathlib import Path
from link_audit import inventory_units, season_hints

from runtime import load_config, config_path

def policy_path():
    return config_path(load_config(), "download_policy_file", "download_policy.json")
VIDEO = re.compile(r'\.(mkv|mp4|avi|mov|m4v|ts)$', re.I)

def normalize(value):
    return ''.join(c.lower() for c in unicodedata.normalize('NFKC', value or '') if c.isalnum())

def policy_for(name, link='', tmdb_id=None):
    path = Path(policy_path())
    if not path.exists(): return None
    data = json.loads(path.read_text(encoding='utf-8'))
    normalized = normalize(name)
    share = re.search(r'/s/([a-zA-Z0-9]+)', link or '')
    for entry in data.get('shows', []):
        aliases = entry.get('aliases', []) + [entry.get('name', '')]
        if any(normalize(a) and normalize(a) in normalized for a in aliases): return entry
        if share and share.group(1) in entry.get('share_ids', []): return entry
        if tmdb_id and tmdb_id in entry.get('tmdb_ids', []): return entry
    return None

def requested_season(name, explicit=None):
    hints = season_hints(name or '')
    if explicit is not None:
        if isinstance(explicit, bool): raise ValueError('--season requires a positive integer')
        number = int(explicit)
        if number < 1 or (hints and hints != {number}):
            raise ValueError('Requested season conflicts with title or is invalid')
        return number
    if len(hints) > 1: raise ValueError('Multiple seasons named; specify --season')
    return next(iter(hints)) if hints else None

def canonical_name(name, link, library):
    policy = policy_for(name, link)
    if policy: return policy['name']
    aliases = [re.sub(r'\s*(?:第\s*[零一二三四五六七八九十两\d]+\s*[季期]|Season\s*\d+|S\d+)\s*$', '', s.strip(), flags=re.I)
               for s in re.split(r'[/／]', name)]
    root = Path(library)
    matches = [p.name for p in root.iterdir() if p.is_dir() and normalize(p.name) in {normalize(a) for a in aliases}] if root.exists() else []
    if len(matches) > 1: raise ValueError('Multiple existing library folders match; specify exact --name')
    if matches: return matches[0]
    # A display title containing aliases must never become nested library paths.
    result = aliases[0].replace('\\', '／').strip()
    if result in ('', '.', '..'): raise ValueError('Invalid library folder name')
    return result

def owned_seasons(library, name):
    base = Path(library) / name
    if not base.exists(): return []
    owned = []
    for p in base.iterdir():
        match = re.fullmatch(r'Season\s+(\d+)', p.name, re.I)
        if match and p.is_dir() and any(f.is_file() and VIDEO.search(f.name) and f.stat().st_size > 0 for f in p.iterdir()):
            owned.append(int(match.group(1)))
    return sorted(owned)

def ensure_allowed(name, season=None, link=''):
    entry = policy_for(name, link)
    if entry:
        if entry.get('paused'): raise ValueError('Download cancelled/paused: ' + entry['name'])
        if season in entry.get('blocked_seasons', []): raise ValueError('Season already owned and blocked: S%02d' % season)

def validate_task(probe, season, pattern):
    """Direct addtask cannot relabel S1 files as S2, even with a custom pattern."""
    if not probe.get('scan_complete') or probe.get('error'):
        raise ValueError('Share scan incomplete; task not created')
    regex = re.compile(pattern)
    units, _ = inventory_units(probe)
    selected = [(u, f) for u in units for f in u['folder']['files'] if regex.search(f['name'])]
    if not selected: raise ValueError('No verified media matches task pattern')
    if any(u['season'] != season for u, f in selected):
        raise ValueError('Requested season is missing, unknown or conflicts with selected files')
    return len(selected)

def filename_conflict(name, season):
    hints = season_hints(name)
    return bool(hints and hints != {season})
