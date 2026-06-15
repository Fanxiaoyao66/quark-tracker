#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standalone TMDB episode renamer -> Infuse/Plex/Jellyfin friendly names.

Renames files under `<show-root>/Season NN/` to `Title - SxxExx - Episode Name.ext`,
matching episode titles from TMDB. Episode numbers are inferred from the filename
(`SxxExx`, or a bare number inside a `Season NN` folder).

Stdlib only. Provide TMDB creds via --api-key / --bearer / --env-file or env vars
TMDB_API_KEY / TMDB_BEARER_TOKEN.
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.parse, urllib.request, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

INVALID = r'[\\/:*?"<>|]'


def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(INVALID, ' ', text)).strip()


def load_env_file(path: str) -> Dict[str, str]:
    d: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return d
    for raw in p.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def resolve_creds(args) -> Tuple[Optional[str], Optional[str]]:
    key = args.api_key or os.environ.get('TMDB_API_KEY')
    bearer = args.bearer or os.environ.get('TMDB_BEARER_TOKEN')
    if not (key or bearer) and args.env_file:
        d = load_env_file(args.env_file)
        key = key or d.get('TMDB_API_KEY')
        bearer = bearer or d.get('TMDB_BEARER_TOKEN')
    return key, bearer


class TMDB:
    BASE = 'https://api.themoviedb.org/3'

    def __init__(self, api_key, bearer):
        self.api_key, self.bearer = api_key, bearer

    def _get(self, path, **params):
        if self.api_key:
            params['api_key'] = self.api_key
        url = self.BASE + path + '?' + urllib.parse.urlencode(params)
        headers = {'Accept': 'application/json'}
        if self.bearer:
            headers['Authorization'] = 'Bearer ' + self.bearer
        last = None
        for _ in range(4):
            try:
                req = urllib.request.Request(url, headers=headers)
                return json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
            except Exception as e:
                last = e
                time.sleep(2)
        raise last

    def search(self, query, year=None, language='zh-CN'):
        p = {'query': query, 'language': language}
        if year:
            p['first_air_date_year'] = year
        return self._get('/search/tv', **p).get('results', [])

    def season(self, tv_id, n, language='zh-CN'):
        return self._get('/tv/%s/season/%s' % (tv_id, n), language=language)


EP_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')
NUM_RE = re.compile(r'^(\d{1,3})$')
SEASON_DIR_RE = re.compile(r'Season\s+(\d{1,3})$', re.I)


def infer_episode(path: Path, season_hint: Optional[int]):
    m = EP_RE.search(path.name)
    if m:
        return int(m.group(1)), int(m.group(2))
    if season_hint is not None:
        m = NUM_RE.match(path.stem)
        if m:
            return season_hint, int(m.group(1))
    return None


def collect(show_root: Path, video_exts):
    out = []
    for sdir in sorted(p for p in show_root.iterdir() if p.is_dir()):
        m = SEASON_DIR_RE.match(sdir.name)
        hint = int(m.group(1)) if m else None
        for f in sorted(p for p in sdir.iterdir() if p.is_file()):
            if f.suffix.lower().lstrip('.') not in video_exts:
                continue
            ep = infer_episode(f, hint)
            if ep:
                out.append((ep[0], ep[1], f))
    return out


def main():
    ap = argparse.ArgumentParser(description='Rename TV/anime episodes using TMDB episode titles.')
    ap.add_argument('--show-root', required=True, help='Folder containing Season NN/ subdirs')
    ap.add_argument('--query', help='TMDB search query (defaults to folder name)')
    ap.add_argument('--year', type=int)
    ap.add_argument('--tmdb-id', type=int, help='Skip search, use this TV id')
    ap.add_argument('--show-title', help='Filename prefix (defaults to folder name)')
    ap.add_argument('--language', default='zh-CN')
    ap.add_argument('--video-exts', default='mkv,mp4,avi,mov,m4v,ts')
    ap.add_argument('--api-key'); ap.add_argument('--bearer'); ap.add_argument('--env-file')
    ap.add_argument('--apply', action='store_true', help='Perform renames (default: preview)')
    args = ap.parse_args()

    root = Path(args.show_root)
    if not root.is_dir():
        raise SystemExit('show-root not found: %s' % root)
    exts = [e.strip().lower().lstrip('.') for e in args.video_exts.split(',')]
    show_title = args.show_title or root.name
    key, bearer = resolve_creds(args)
    if not (key or bearer):
        raise SystemExit('No TMDB credentials (use --api-key/--bearer/--env-file or env vars)')
    tmdb = TMDB(key, bearer)

    eps = collect(root, exts)
    if not eps:
        print('No episode files detected under Season NN/.'); return

    tv_id = args.tmdb_id
    if not tv_id:
        results = tmdb.search(args.query or root.name, args.year, args.language)
        if not results:
            raise SystemExit('TMDB: no match for %r (try --query original title or --tmdb-id)' % (args.query or root.name))
        tv_id = results[0]['id']
        print('Matched TV: %s (id=%s)' % (results[0].get('name'), tv_id))

    title_map: Dict[Tuple[int, int], str] = {}
    for s in sorted({e[0] for e in eps}):
        data = tmdb.season(tv_id, s, args.language)
        for e in data.get('episodes', []):
            nm = e.get('name') or ''
            if nm:
                title_map[(s, e['episode_number'])] = clean(nm)

    planned = 0
    for s, e, f in eps:
        title = title_map.get((s, e))
        base = '%s - S%02dE%02d' % (show_title, s, e)
        new = base + (' - %s' % title if title else '') + f.suffix.lower()
        if f.name == new:
            continue
        dst = f.with_name(new)
        print('%s\n  -> %s' % (f.name, new))
        planned += 1
        if args.apply:
            if dst.exists():
                print('  ! target exists, skip'); continue
            f.rename(dst)
    print('Planned changes: %d  Mode: %s' % (planned, 'applied' if args.apply else 'preview'))


if __name__ == '__main__':
    main()
