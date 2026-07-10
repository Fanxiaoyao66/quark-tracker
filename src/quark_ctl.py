#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quark-tracker control entrypoint — meant to be driven by an LLM agent or by hand.

Subcommands:
  probe <share_url>                 share structure + parsed episodes (JSON)
  tmdb (--query Q | --id N) [--season S] [--year Y] [--buffer N] [--lang L]
                                    status / completed / air dates / suggested_enddate (JSON)
  addtask --name N --url U --savepath P [--pattern PAT] [--enddate YYYY-MM-DD]
  deltask --name N
  listtasks
  override --name N --tmdb-id ID [--query Q]    register a TMDB mapping for renaming
  transfer                          run quark-auto-save transfer now
  sync                              download new episodes + TMDB rename + notify
  autodl --link <url> [--name N] [--category C]
                                    the whole pipeline in ONE call, designed to run in the
                                    BACKGROUND (agents: `setsid ... &` and reply instantly):
                                    probe -> detect seasons -> pick the best release per
                                    season -> addtask -> transfer (verified, retried) ->
                                    sync -> notify per season; archives (.exe/.rar/.7z) and
                                    theatrical movies handled automatically
  subs --link <url> [--name N] [--category C]
                                    add external simplified-Chinese subtitles to an
                                    ALREADY-DOWNLOADED show (no video re-download)

Config: $QUARK_TRACKER_CONFIG or ./config.json (see config.example.json)
"""
import json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.parse, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify  # noqa: E402


def load_config():
    p = os.environ.get("QUARK_TRACKER_CONFIG")
    cands = [p] if p else []
    cands += [str(HERE.parent / "config.json"), str(HERE / "config.json"), "/opt/quark-tracker/config.json"]
    for c in cands:
        if c and os.path.exists(c):
            cfg = json.load(open(c, encoding="utf-8"))
            cfg["__dir__"] = os.path.dirname(os.path.abspath(c))
            return cfg
    raise SystemExit("config.json not found (set QUARK_TRACKER_CONFIG)")


CFG = load_config()
CONTAINER = CFG.get("container", "quark-auto-save")
API_IN = CFG.get("api_in_container", "/app/config/qsync_api.py")
QUARK_MAIN = CFG.get("quark_main_in_container", "/app/quark_auto_save.py")
QUARK_CONF = CFG.get("quark_conf_in_container", "/app/config/quark_config.json")
TMDB = CFG.get("tmdb", {})
OWNER = CFG.get("owner", "")
MOVIE_LIB = CFG.get("movie_library", "")
SYNC = str(HERE / "quark_sync.py")
SYNC_LOCK = "/tmp/quark-tracker.sync.lock"
UA_DL = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
VIDEXT = tuple(e.lower().lstrip(".") for e in CFG.get("video_exts", ["mkv", "mp4", "avi", "mov", "m4v", "ts"]))
SUBEXT = ("ass", "srt", "ssa", "sub", "vtt")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def parse_flags(a):
    d, i = {}, 0
    while i < len(a):
        if a[i].startswith("--"):
            k = a[i][2:]
            if i + 1 < len(a) and not a[i + 1].startswith("--"):
                d[k] = a[i + 1]; i += 2
            else:
                d[k] = True; i += 1
        else:
            i += 1
    return d


def dexec(args):
    r = subprocess.run(["docker", "exec", CONTAINER, "python3", API_IN] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("container api error: " + (r.stderr or "")[-600:])
    return r.stdout


def push(msg):
    notify.send(CFG.get("notify", []), msg)


def _chown(path, recursive=False):
    if OWNER:
        subprocess.run(["chown"] + (["-R"] if recursive else []) + [OWNER, path], capture_output=True)


def category(label=None):
    cats = CFG.get("categories", {})
    if not cats:
        raise SystemExit("config has no categories")
    label = label or CFG.get("default_category") or next(iter(cats))
    if label not in cats:
        raise SystemExit("unknown category %r (config has: %s)" % (label, ", ".join(cats)))
    return cats[label]


def run_sync():
    # share one lock with the cron sync so an agent-triggered run can't race it
    # (two syncs on the same .part file force aria2c down to a stuck single stream)
    cmd = [sys.executable, SYNC]
    if shutil.which("flock"):
        cmd = ["flock", SYNC_LOCK] + cmd
    subprocess.run(cmd)


def tmdb_creds():
    key, bearer = TMDB.get("api_key"), TMDB.get("bearer_token")
    if not (key or bearer) and TMDB.get("env_file") and os.path.exists(TMDB["env_file"]):
        for line in open(TMDB["env_file"], encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); v = v.strip().strip('"').strip("'")
                if k.strip() == "TMDB_API_KEY":
                    key = key or v
                if k.strip() == "TMDB_BEARER_TOKEN":
                    bearer = bearer or v
    return key, bearer


def tmdb_get(path, **params):
    key, bearer = tmdb_creds()
    if key:
        params["api_key"] = key
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
        except Exception as e:
            last = e; time.sleep(2)
    raise last


def cmd_tmdb(a):
    args = parse_flags(a); lang = args.get("lang", TMDB.get("language", "zh-CN"))
    tid = args.get("id")
    if not tid:
        p = {"query": args.get("query", ""), "language": lang}
        if args.get("year"):
            p["first_air_date_year"] = args["year"]
        results = tmdb_get("/search/tv", **p).get("results", [])
        if not results:
            out({"found": False, "query": args.get("query")}); return
        tid = results[0]["id"]
    info = tmdb_get("/tv/%s" % tid, language=lang)
    status = info.get("status")
    next_ep = info.get("next_episode_to_air")
    season = int(args.get("season") or (info.get("number_of_seasons") or 1))
    # next_episode_to_air is show-level: while another season is airing it must not
    # mark THIS season as ongoing, or long-finished seasons get a past enddate and
    # quark-auto-save silently skips their transfer task ("out of run window")
    if next_ep and int(next_ep.get("season_number") or 0) != season:
        next_ep = None
    eps = tmdb_get("/tv/%s/season/%s" % (tid, season), language=lang).get("episodes", [])
    today = datetime.date.today()

    def dd(s):
        try:
            return datetime.date.fromisoformat(s)
        except Exception:
            return None
    aired = [e for e in eps if dd(e.get("air_date")) and dd(e["air_date"]) <= today]
    future = [e for e in eps if dd(e.get("air_date")) and dd(e["air_date"]) > today]
    last_air = max([dd(e["air_date"]) for e in aired if dd(e.get("air_date"))], default=None)
    finale = max([dd(e["air_date"]) for e in eps if dd(e.get("air_date"))], default=None)
    buf = int(args.get("buffer") or TMDB.get("enddate_buffer_days", 14))
    completed = status in ("Ended", "Canceled") or (
        next_ep is None and not future and eps and len(aired) == len(eps))
    enddate = None
    if not completed:
        if finale and finale >= today:
            base = finale
        elif len(eps) > len(aired) and last_air:
            base = last_air + datetime.timedelta(days=7 * (len(eps) - len(aired)))
        elif last_air:
            base = last_air + datetime.timedelta(days=28)
        else:
            base = today + datetime.timedelta(days=60)
        enddate = (base + datetime.timedelta(days=buf)).isoformat()
        if datetime.date.fromisoformat(enddate) < today:
            # computed enddate already in the past = this season finished long ago
            completed, enddate = True, None
    out({"found": True, "id": tid, "name": info.get("name"), "original_name": info.get("original_name"),
         "status": status, "completed": completed, "in_production": info.get("in_production"),
         "season": season, "season_episode_count": len(eps), "aired": len(aired),
         "last_air_date": last_air.isoformat() if last_air else None,
         "finale_air_date": finale.isoformat() if finale else None,
         "next_episode_to_air": (next_ep and {"ep": next_ep.get("episode_number"), "air_date": next_ep.get("air_date")}),
         "suggested_enddate": enddate,
         "episodes": [{"ep": e.get("episode_number"), "air_date": e.get("air_date"), "name": e.get("name")} for e in eps]})


def restart():
    subprocess.run(["docker", "restart", CONTAINER], capture_output=True, text=True)


def cmd_addtask(a):
    args = parse_flags(a)
    task = {"taskname": args["name"], "shareurl": args["url"], "savepath": args["savepath"],
            "pattern": args.get("pattern", r"\.(mkv|mp4)$"), "replace": ""}
    if args.get("enddate"):
        ed = args["enddate"]
        try:
            if datetime.date.fromisoformat(ed) < datetime.date.today():
                # a past enddate makes quark-auto-save skip the task silently forever
                ed = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        except Exception:
            pass
        task["enddate"] = ed
    sys.stdout.write(dexec(["addtask", json.dumps(task, ensure_ascii=False)])); restart()


def cmd_deltask(a):
    args = parse_flags(a)
    sys.stdout.write(dexec(["deltask", args["name"]])); restart()


def cmd_override(a):
    args = parse_flags(a)
    f = (CFG.get("rename", {}).get("overrides_file") or CFG.get("overrides_file") or "overrides.json")
    if not os.path.isabs(f):
        f = os.path.join(CFG["__dir__"], f)
    ov = json.load(open(f, encoding="utf-8")) if os.path.exists(f) else {}
    entry = ov.get(args["name"], {})
    if args.get("tmdb-id"):
        entry["tmdb_id"] = int(args["tmdb-id"])
    if args.get("query"):
        entry["query"] = args["query"]
    ov[args["name"]] = entry
    json.dump(ov, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    out({"ok": True, "name": args["name"], "override": entry})


# ---------------------------------------------------------------- autodl helpers

def _fblob(f):
    return ((f.get("path") or "") + " " + (f.get("sample") or "") + " " + " ".join(f.get("archives") or [])).lower()


def _qscore(b):
    # picture-quality score (resolution first, then encode niceties)
    q = 0
    if ("2160" in b) or ("4k" in b): q += 8
    elif "1080" in b: q += 4
    elif "720" in b: q += 1
    if ("超分" in b) or ("upscale" in b): q += 3
    if ("10bit" in b) or ("10-bit" in b) or ("main10" in b) or ("hi10" in b): q += 3
    if ("hevc" in b) or ("x265" in b) or ("h265" in b): q += 2
    if "flac" in b: q += 1
    return q


def _sub_ok(b):
    # subtitle usability: simplified-Chinese available or soft subs = good (2);
    # hardsubbed WITHOUT simplified Chinese = unusable, pass (0); unknown = ok (1)
    has_simp = any(k in b for k in ("简体", "简日", "简繁", "简中", "chs", "_sc", ".sc", "[简", "【简", "gb"))
    soft = ("内封" in b) or ("外挂" in b) or ("softsub" in b)
    if has_simp or soft:
        return 2
    embed = ("内嵌" in b) or ("硬字" in b) or ("hardsub" in b)
    if embed and not has_simp:
        return 0
    return 1


def _select_folder(folders):
    vids = [f for f in folders if f.get("kind") == "video"]
    arcs = [f for f in folders if f.get("kind") == "archive"]
    if vids:
        # release priority: episode coverage (baseline — never pick a half pack) ->
        # picture quality -> larger files -> subtitle usability -> starts at ep 1.
        # Drop hardsubbed-without-simplified releases first (unless that's all there is).
        ok = [f for f in vids if _sub_ok(_fblob(f)) > 0]
        pool = ok if ok else vids

        def vscore(f):
            b = _fblob(f); eps = f.get("episodes") or []
            return (len(eps), _qscore(b), f.get("total_size") or 0, _sub_ok(b), 1 if f.get("ep_min") == 1 else 0)
        return ("video", sorted(pool, key=vscore, reverse=True)[0])
    if arcs:
        # archive releases: quality first, then size
        def ascore(f):
            b = _fblob(f)
            return (1 if f.get("has_password") else 0, _qscore(b), f.get("total_size") or 0, f.get("ep_max") or 0, -(f.get("archive_count") or 99))
        return ("archive", sorted(arcs, key=ascore, reverse=True)[0])
    return (None, None)


def _catalog_path():
    f = CFG.get("catalog_file", "")
    if not f:
        return None
    return f if os.path.isabs(f) else os.path.join(CFG["__dir__"], f)


def _catalog_entry(url):
    # optional: look a share link up in an anime-taste style catalog (name + link [+ tmdb_id])
    cp = _catalog_path()
    if not (url and cp and os.path.exists(cp)):
        return None
    base = url.split("#")[0].rstrip("/")
    try:
        db = json.load(open(cp, encoding="utf-8"))
    except Exception:
        return None
    for e in db:
        q = (e.get("link") or e.get("quark") or "").split("#")[0].rstrip("/")
        if q and q == base:
            return e
    return None


def _name_by_link(url):
    e = _catalog_entry(url)
    return e.get("name") if e else None


def _tmdb_search_id(name):
    try:
        res = tmdb_get("/search/tv", query=name, language=TMDB.get("language", "zh-CN")).get("results", [])
        return res[0]["id"] if res else None
    except Exception:
        return None


def _tmdb_status(tid, season, buffer=None):
    # -> {completed, enddate}: completed = Ended/Canceled or this season fully aired;
    # otherwise enddate = last/finale air date + buffer
    buffer = buffer or int(TMDB.get("enddate_buffer_days", 14))
    lang = TMDB.get("language", "zh-CN")
    try:
        info = tmdb_get("/tv/%s" % tid, language=lang)
        status = info.get("status"); next_ep = info.get("next_episode_to_air")
        season = int(season or (info.get("number_of_seasons") or 1))
        if next_ep and int(next_ep.get("season_number") or 0) != season:
            next_ep = None  # season-scope filter, same as cmd_tmdb
        eps = tmdb_get("/tv/%s/season/%s" % (tid, season), language=lang).get("episodes", [])
        today = datetime.date.today()

        def dd(s):
            try:
                return datetime.date.fromisoformat(s)
            except Exception:
                return None
        aired = [e for e in eps if dd(e.get("air_date")) and dd(e["air_date"]) <= today]
        future = [e for e in eps if dd(e.get("air_date")) and dd(e["air_date"]) > today]
        last_air = max([dd(e["air_date"]) for e in aired if dd(e.get("air_date"))], default=None)
        finale = max([dd(e["air_date"]) for e in eps if dd(e.get("air_date"))], default=None)
        completed = status in ("Ended", "Canceled") or (
            next_ep is None and not future and eps and len(aired) == len(eps))
        enddate = None
        if not completed:
            if finale and finale >= today:
                base = finale
            elif len(eps) > len(aired) and last_air:
                base = last_air + datetime.timedelta(days=7 * (len(eps) - len(aired)))
            elif last_air:
                base = last_air + datetime.timedelta(days=28)
            else:
                base = today + datetime.timedelta(days=60)
            enddate = (base + datetime.timedelta(days=buffer)).isoformat()
            if datetime.date.fromisoformat(enddate) < today:
                return {"completed": True, "enddate": None}
        return {"completed": completed, "enddate": enddate}
    except Exception:
        return {"completed": None, "enddate": None}


def _cn2num(s):
    m = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if str(s).isdigit():
        return int(s)
    return m.get(s)


# multi-season packs must land in the BASE show folder (seasons go to Season NN
# subdirs); strip trailing season suffixes like 第三季 / Season 3 / S4 from the name
_SEASON_SUFFIX = re.compile(r'\s*(?:第\s*[0-9一二三四五六七八九十百零两]+\s*[季期部]|Season\s*\d{1,2}|\bS\d{1,2})\s*$', re.I)


def _strip_season(name):
    if not name:
        return name
    n = _SEASON_SUFFIX.sub('', name).strip()
    return n or name


def _season_of(folder):
    # season detection: leaf folder name first (S1 / Season 1 / 第一季); when the leaf
    # has no marker, walk UP the parent components and accept one only if it contains
    # EXACTLY one season marker (protects against "S1+S2+S3+S4" batch-pack names).
    # Real-world case: season lives in the parent ("03.第三季/[fansub group]") while the
    # leaf is just the fansub group name — leaf-only detection silently dropped it.
    # Bonus/subtitle-ish subfolders don't inherit a season. None = "unknown"
    # (single-season mode defaults to 1; multi-season mode drops the folder).
    parts = [p for p in (folder.get("path") or "").split("/") if p]
    if not parts:
        return None
    leaf = parts[-1]
    for pat in (r'\bS(\d{1,2})\b', r'[Ss]eason\s*(\d{1,2})'):
        mm = re.search(pat, leaf)
        if mm:
            return int(mm.group(1))
    mm = re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*[季期]', leaf)
    if mm:
        n = _cn2num(mm.group(1))
        if n:
            return n
    if re.search(r'^(SPs?|EXTRAS?|Menus?|Scans?|CDs?|Fonts?|Subs?|NC(OP|ED))\b|外挂字幕|特典|映像', leaf, re.I):
        return None
    for comp in reversed(parts[:-1]):
        found = []
        for pat in (r'\bS(\d{1,2})\b', r'[Ss]eason\s*(\d{1,2})'):
            for mm in re.finditer(pat, comp):
                v = int(mm.group(1))
                if v not in found:
                    found.append(v)
        for mm in re.finditer(r'第\s*([0-9一二三四五六七八九十]+)\s*[季期]', comp):
            v = _cn2num(mm.group(1))
            if v and v not in found:
                found.append(v)
        if len(found) == 1:
            return found[0]
    return None


def _acct_season_files(savepath):
    try:
        return json.loads(dexec(["lsfiles", savepath])).get("count", 0)
    except Exception:
        return 0


def _write_pw(name, passwords):
    if not passwords:
        return
    pf = CFG.get("archive_passwords_file", "arc_passwords.json")
    if not os.path.isabs(pf):
        pf = os.path.join(CFG["__dir__"], pf)
    cur = {}
    if os.path.exists(pf):
        try:
            cur = json.load(open(pf, encoding="utf-8"))
        except Exception:
            cur = {}
    cur[name] = passwords
    try:
        json.dump(cur, open(pf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def _dl_season(cat, name, link, folders, passwords, snum, multi):
    # one season: pick the best release -> addtask -> transfer (verified, retried against
    # a race where a season is skipped) -> sync (download/extract/rename/notify)
    kind, folder = _select_folder(folders)
    if not folder:
        push("❌ %s · Season %02d: nothing downloadable found." % (name, snum)); return
    savepath = "%s/%s/Season %02d" % (cat["quark_root"].rstrip("/"), name, snum)
    url = "%s#/list/share/%s-x" % (link, folder["fid"])
    pattern = r"\.(mkv|mp4|avi|mov|m4v|ts|exe|rar|7z|zip)$|(解压密码|密码|password).*\.txt$"
    is_arc = (folder.get("archive_count") or 0) > 0
    pure_arc = is_arc and (folder.get("video_count") or 0) == 0
    if is_arc:
        _write_pw(name, passwords)
    # completion + enddate: finished (or archive-only) -> delete the task when done;
    # still airing -> enddate = finale + buffer (auto-stops); unknown -> 90-day fallback
    completed = pure_arc; enddate = None
    if not pure_arc:
        tid = (_catalog_entry(link) or {}).get("tmdb_id") or _tmdb_search_id(name)
        info = _tmdb_status(tid, snum) if tid else {"completed": None, "enddate": None}
        if info.get("completed") is True:
            completed = True
        elif info.get("completed") is False:
            enddate = info.get("enddate")
        else:
            enddate = (datetime.date.today() + datetime.timedelta(days=90)).isoformat()
    tn = name + ((" S%d" % snum) if multi else "")
    task = {"taskname": tn, "shareurl": url, "savepath": savepath, "pattern": pattern, "replace": ""}
    if enddate and not completed:
        task["enddate"] = enddate
    try:
        dexec(["addtask", json.dumps(task, ensure_ascii=False)])
    except Exception as e:
        push("❌ %s · Season %02d: creating the task failed: %s" % (name, snum, str(e)[:70])); return
    ok = False   # after transfer, verify the season actually has files in the account; retry if not
    for _ in range(10):
        subprocess.run(["docker", "exec", CONTAINER, "python3", QUARK_MAIN, QUARK_CONF], capture_output=True, text=True)
        if _acct_season_files(savepath) > 0:
            ok = True; break
        time.sleep(20)
    if not ok:
        push("❌ %s · Season %02d: transfer into the Quark account failed after 10 tries; the scheduled task will keep retrying." % (name, snum)); return
    push("✅ %s · Season %02d transferred, downloading to the library…" % (name, snum))
    run_sync()
    if completed:   # finished shows (and archive packs): download once, don't keep monitoring
        try:
            dexec(["deltask", tn])
        except Exception:
            pass
    if not pure_arc:
        try:
            ns = _fetch_subs(cat, name, link, folder["fid"], snum)
            if ns:
                push("📝 %s · Season %02d: matched %d external simplified-Chinese subtitle(s)" % (name, snum, ns))
        except Exception:
            pass


def _fetch_subs(cat, name, link, folder_fid, snum):
    # external subs from the chosen video folder -> pair with the (TMDB-renamed) library
    # episodes -> save as "<video name>.zh.<ext>". Existing subs are never overwritten.
    try:
        r = json.loads(dexec(["subsfor", link.split("#")[0], folder_fid]))
    except Exception:
        return 0
    subs = r.get("subs") or []
    libdir = os.path.join(cat["library"], name, "Season %02d" % snum)
    if not subs or not os.path.isdir(libdir):
        return 0
    try:
        files = os.listdir(libdir)
    except Exception:
        return 0
    placed = 0
    for s in subs:
        ep = s.get("ep")
        if not ep:
            continue
        tag = "s%02de%02d" % (snum, ep)
        vid = next((fn for fn in files if (tag in fn.lower()) and (fn.rsplit(".", 1)[-1].lower() in VIDEXT)), None)
        if not vid:
            continue
        stem = vid.rsplit(".", 1)[0]
        ext = s.get("ext") if s.get("ext") in SUBEXT else "ass"
        dest = os.path.join(libdir, "%s.zh.%s" % (stem, ext))
        if os.path.exists(dest):
            placed += 1; continue
        url = s.get("url")
        if not url:
            continue
        rc = subprocess.run(["curl", "-fsSL", "--retry", "3", "-H", "Cookie: " + s.get("cookie", ""),
                             "-H", "User-Agent: " + UA_DL, "-H", "Referer: https://pan.quark.cn/",
                             "-o", dest, url]).returncode
        if rc == 0 and os.path.exists(dest):
            _chown(dest); placed += 1
    return placed


def _movie_name_year(raw):
    nm = re.sub(r'\[[^\]]*\]|【[^】]*】', '', raw or '')
    nm = re.sub(r'(剧场版|劇場版|the movie|gekijou(ban)?|movie)', '', nm, flags=re.I)
    nm = re.sub(r'\b(1080p|720p|2160p|4k|bdrip|hevc|x265|x264|10bit|flac|web-?dl|avc|aac)\b', '', nm, flags=re.I)
    nm = nm.strip(" -_·　\t")
    year = None
    try:
        res = tmdb_get("/search/movie", query=nm, language=TMDB.get("language", "zh-CN")).get("results", [])
        if res:
            d = res[0].get("release_date") or ""
            year = d[:4] if len(d) >= 4 else None
            if res[0].get("title"):
                nm = res[0]["title"]
    except Exception:
        pass
    return nm, year


def _fetch_movie(name, link, folders):
    # detect a theatrical-movie folder in the share -> download the main file + a
    # simplified-Chinese sub into <movie_library>/<Title (year)>/. Skipped when
    # movie_library is not configured or the movie already exists.
    if not MOVIE_LIB:
        return 0
    try:
        MOV = r'剧场版|劇場版|劇場|the\s*movie|gekijou'   # deliberately narrow — a bare "movie"/"ZERO" hits TV packs like Re:Zero
        cand = [f for f in folders if (f.get("video_count") or 0) > 0 and re.search(MOV, f.get("path") or "", re.I)]
        if not cand:
            return 0
        f = sorted(cand, key=lambda x: (_qscore(_fblob(x)), x.get("total_size") or 0), reverse=True)[0]
        seg = next((p for p in (f.get("path") or "").split("/") if re.search(MOV, p, re.I)), name)
        mname, year = _movie_name_year(seg)
        folder = "%s (%s)" % (mname or name, year) if year else (mname or name)
        dest_dir = os.path.join(MOVIE_LIB, folder)
        if os.path.isdir(dest_dir) and any(fn.rsplit(".", 1)[-1].lower() in VIDEXT for fn in os.listdir(dest_dir)):
            return 1
        r = json.loads(dexec(["movieget", link.split("#")[0], f["fid"]]))
        v = r.get("video")
        if not v or not v.get("url"):
            return 0
        os.makedirs(dest_dir, exist_ok=True)
        mp = os.path.join(dest_dir, "%s.mkv" % folder); part = mp + ".part"
        rc = subprocess.run(["aria2c", "-x16", "-s16", "-k1M", "--file-allocation=none", "--max-tries=5",
                             "--retry-wait=3", "-c", "--console-log-level=warn", "--summary-interval=0",
                             "--header=Cookie: " + v.get("cookie", ""), "--header=User-Agent: " + UA_DL,
                             "--referer=https://pan.quark.cn/", "-d", dest_dir, "-o", os.path.basename(part), v["url"]]).returncode
        if rc == 0 and os.path.exists(part):
            os.replace(part, mp)
        sub = r.get("sub")
        if sub and sub.get("url") and os.path.exists(mp):
            sdest = os.path.join(dest_dir, "%s.zh.%s" % (folder, sub.get("ext", "ass")))
            subprocess.run(["curl", "-fsSL", "--retry", "3", "-H", "Cookie: " + sub.get("cookie", ""),
                            "-H", "User-Agent: " + UA_DL, "-H", "Referer: https://pan.quark.cn/", "-o", sdest, sub["url"]])
        _chown(dest_dir, recursive=True)
        return 1 if os.path.exists(mp) else 0
    except Exception:
        return 0


def _has_existing_sub(sd, stem):
    # does this episode already have ANY subtitle (bare <stem>.ext or tagged <stem>.zh.ext)?
    # release-bundled bare .ass files count too — never double-add
    try:
        for fn in os.listdir(sd):
            if fn.startswith(stem + ".") and fn.rsplit(".", 1)[-1].lower() in SUBEXT:
                return True
    except Exception:
        pass
    return False


def _resolve_libdir(cat, name, link):
    # show name -> library folder. Try <library>/<name> directly; else fuzzy-match via the
    # catalog names (the local folder may differ, e.g. renamed from English to Chinese)
    lib = cat["library"]
    direct = os.path.join(lib, name)
    if os.path.isdir(direct):
        return direct
    e = _catalog_entry(link) or {}
    cands = {c for c in (name, e.get("name"), e.get("original_name")) if c}
    try:
        dirs = [d for d in os.listdir(lib) if os.path.isdir(os.path.join(lib, d))]
    except Exception:
        return direct
    low = {d.lower(): d for d in dirs}
    for c in cands:                     # case-insensitive exact
        if c.lower() in low:
            return os.path.join(lib, low[c.lower()])
    for d in dirs:                      # substring both ways, >=2 chars to avoid noise
        for c in cands:
            if len(c) >= 2 and (c in d or d in c):
                return os.path.join(lib, d)
    return direct


def _sub_ep(fn):
    b = re.sub(r'\b(1080p?|720p?|2160p?|10bit|8bit|x?264|x?265|hevc|Ma10p|flac|aac|BDRip|web-?dl|web-?rip)\b', ' ', fn.rsplit('.', 1)[0], flags=re.I)
    for pat in (r'\[(\d{1,3})(?:v\d+)?\]', r'[Ss]\d{1,2}[Ee](\d{1,3})', r'-\s*(\d{1,3})\b', r'第\s*(\d{1,3})\s*[话集話]'):
        m = re.search(pat, b)
        if m:
            return int(m.group(1))
    return None


_SUBSEA_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _sub_season(fn):
    m = re.search(r'第\s*([一二三四五六七八九十])\s*季', fn)
    if m:
        return _SUBSEA_CN.get(m.group(1))
    m = re.search(r'\bS(\d{1,2})\b', fn)
    if m:
        return int(m.group(1))
    return None


def _place_sub_archives(link, index, only_season):
    # subtitle ARCHIVES (.7z/.zip packs inside subtitle folders) -> download -> 7z-extract
    # -> pair by (season, ep) with library episodes; existing subs skipped
    if not shutil.which("7z"):
        return 0
    arcs = []
    for _ in range(4):
        try:
            arcs = json.loads(dexec(["subarch", link.split("#")[0]])).get("archives") or []
        except Exception:
            arcs = []
        if arcs:
            break
        time.sleep(2)
    if not arcs:
        return 0
    rk = {"sc": 3, "x": 2, "tc": 1}
    placed = 0
    tmp = "/tmp/qt_suba_%d" % os.getpid()
    subprocess.run(["mkdir", "-p", tmp])
    try:
        for i, av in enumerate(arcs):
            url = av.get("url")
            if not url:
                continue
            ext = (av.get("name") or "").rsplit(".", 1)[-1].lower()
            arcf = os.path.join(tmp, "a%d.%s" % (i, ext if ext in ("7z", "zip", "rar", "exe") else "7z"))
            rc = subprocess.run(["curl", "-fsSL", "--retry", "4", "-H", "Cookie: " + av.get("cookie", ""),
                                 "-H", "User-Agent: " + UA_DL, "-H", "Referer: https://pan.quark.cn/",
                                 "-o", arcf, url]).returncode
            if rc != 0 or not os.path.exists(arcf):
                continue
            exd = os.path.join(tmp, "ex%d" % i)
            subprocess.run(["mkdir", "-p", exd])
            subprocess.run(["7z", "x", "-y", "-o" + exd, arcf], capture_output=True)
            ahint = av.get("season")
            best = {}
            for root, _dd, files in os.walk(exd):
                for fn in files:
                    if fn.rsplit(".", 1)[-1].lower() not in SUBEXT:
                        continue
                    ep = _sub_ep(fn)
                    if ep is None:
                        continue
                    sea = _sub_season(fn) or ahint or only_season
                    if not sea:
                        continue
                    low = fn.lower()
                    lang = "sc" if (".sc." in low or "chs" in low or "简" in fn or "[gb]" in low) else ("tc" if (".tc." in low or "cht" in low or "繁" in fn) else "x")
                    k = (sea, ep); cur = best.get(k)
                    if not cur or rk[lang] > rk[cur[1]]:
                        best[k] = (os.path.join(root, fn), lang)
            for (sea, ep), (srcpath, lang) in best.items():
                v = index.get((sea, ep))
                if not v:
                    continue
                sd, vid = v[0]
                stem = vid.rsplit(".", 1)[0]
                if _has_existing_sub(sd, stem):
                    continue
                sext = srcpath.rsplit(".", 1)[-1].lower()
                if sext not in SUBEXT:
                    sext = "ass"
                dest = os.path.join(sd, "%s.zh.%s" % (stem, sext))
                if subprocess.run(["cp", srcpath, dest]).returncode == 0 and os.path.exists(dest):
                    _chown(dest); placed += 1
    finally:
        subprocess.run(["rm", "-rf", tmp])
    return placed


def _place_subs_from_scan(cat, name, link):
    # scan the WHOLE share for external subs -> pair by (season, ep) with the TMDB-renamed
    # library episodes (SxxExx) -> save as "<video name>.zh.<ext>"; existing subs skipped.
    # Transient SSL EOFs randomly drop a few entries per scan, so accumulate over multiple
    # rounds and stop after 2 rounds with no news (a share with no subs exits in 2 rounds).
    acc = {}; err = None; stable = 0
    for _ in range(5):
        try:
            got = (json.loads(dexec(["subscan", link.split("#")[0]])).get("subs")) or []
        except Exception as e:
            err = str(e)[:80]; got = []
        before = len(acc)
        for s in got:
            k = (s.get("season"), s.get("ep"))
            if k[1] is not None and k not in acc:
                acc[k] = s
        stable = stable + 1 if len(acc) == before else 0
        if stable >= 2:
            break
        time.sleep(2)
    subs = list(acc.values())
    base = _resolve_libdir(cat, name, link)
    if not os.path.isdir(base):
        return {"placed": 0, "found": len(subs), "reason": "no_libdir", "libdir": base}
    index = {}; seasons = set()                    # {(season, ep): [(season_dir, filename)]}
    for sd_name in os.listdir(base):
        sd = os.path.join(base, sd_name)
        if not os.path.isdir(sd):
            continue
        sm = re.search(r'[Ss]eason\s*(\d{1,2})|\bS(\d{1,2})\b', sd_name)
        snum = int(next((g for g in sm.groups() if g), 1)) if sm else 1
        seasons.add(snum)
        for fn in os.listdir(sd):
            if fn.rsplit(".", 1)[-1].lower() not in VIDEXT:
                continue
            em = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', fn)
            if em:
                index.setdefault((int(em.group(1)), int(em.group(2))), []).append((sd, fn))
    only_season = next(iter(seasons)) if len(seasons) == 1 else None
    placed = 0; missing = 0
    for s in subs:
        ep = s.get("ep")
        if not ep:
            continue
        sea = s.get("season") or only_season       # unmarked season + single-season library -> that season
        vids = index.get((sea, ep)) if sea else None
        if not vids:
            missing += 1; continue
        sd, vid = vids[0]
        stem = vid.rsplit(".", 1)[0]
        if _has_existing_sub(sd, stem):
            placed += 1; continue
        ext = s.get("ext") if s.get("ext") in SUBEXT else "ass"
        dest = os.path.join(sd, "%s.zh.%s" % (stem, ext))
        url = s.get("url")
        if not url:
            continue
        rc = subprocess.run(["curl", "-fsSL", "--retry", "3", "-H", "Cookie: " + s.get("cookie", ""),
                             "-H", "User-Agent: " + UA_DL, "-H", "Referer: https://pan.quark.cn/",
                             "-o", dest, url]).returncode
        if rc == 0 and os.path.exists(dest):
            _chown(dest); placed += 1
    try:    # zipped subtitle packs (subarch) — subscan only sees loose .ass files
        placed += _place_sub_archives(link, index, only_season)
    except Exception:
        pass
    if placed == 0 and not subs:
        if err:
            return {"placed": 0, "error": err}
        return {"placed": 0, "found": 0, "reason": "no_subs"}
    return {"placed": placed, "found": len(subs), "missing": missing, "libdir": base, "seasons": sorted(seasons)}


def _link_items(a, need_name_guess):
    # shared entry parsing for autodl/subs: --link <url> [--name N] [--category C]
    args = parse_flags(a)
    link = args.get("link") if isinstance(args.get("link"), str) else None
    if not link:
        push("⚠️ no --link given."); return None, None
    cat = category(args.get("category") if isinstance(args.get("category"), str) else None)
    name = args.get("name") if isinstance(args.get("name"), str) else None
    if not name:
        name = _name_by_link(link)
    if not name and need_name_guess:
        try:
            name = json.loads(dexec(["probe", link.split("#")[0]])).get("title_guess")
        except Exception:
            name = None
    if not name:
        push("⚠️ could not derive a show name from the link; pass --name."); return None, None
    return cat, {"name": _strip_season(name), "link": link.split("#")[0]}


def cmd_subs(a):
    # add external simplified-Chinese subs to an already-downloaded show (no video re-download)
    cat, it = _link_items(a, need_name_guess=False)
    if not it:
        return
    name, link = it["name"], it["link"]
    res = _place_subs_from_scan(cat, name, link)
    n = res.get("placed", 0)
    if n > 0:
        push("📝 %s: matched %d external subtitle(s) into the library — they show up after the next media-server scan." % (name, n))
    elif res.get("reason") == "no_libdir":
        push("⚠️ %s: not in the library (%s missing). Download the show first, or check the folder name." % (name, res.get("libdir")))
    elif res.get("reason") == "no_subs" or res.get("found", 0) == 0:
        push("ℹ️ %s: this share has no external subtitle files (probably a soft-subbed release that needs none)." % name)
    elif res.get("error"):
        push("❌ %s: adding subtitles failed: %s" % (name, res["error"]))
    else:
        push("ℹ️ %s: found %d subtitle(s) but %d didn't match any library episode — different numbering or a different release." % (name, res.get("found", 0), res.get("missing", 0)))


def cmd_autodl(a):
    # the whole pipeline in one call; meant to be run in the background (agents: setsid + &)
    cat, it = _link_items(a, need_name_guess=True)
    if not it:
        return
    name, link = it["name"], it["link"]
    try:
        probe = json.loads(dexec(["probe", link]))
    except Exception as e:
        push("❌ %s: probing the share failed: %s" % (name, str(e)[:80])); return
    if probe.get("error"):
        push("❌ %s: the share won't open (%s) — the link may be dead." % (name, probe.get("message"))); return
    folders = probe.get("folders", [])
    passwords = probe.get("passwords") or []
    # season detection: >=2 distinct seasons -> per-season processing
    # (each season gets its own verified transfer + sync + notification)
    by_season = {}
    for f in folders:
        sn = _season_of(f)
        if sn:
            by_season.setdefault(sn, []).append(f)
    if len(by_season) >= 2:
        for sn in sorted(by_season):
            _dl_season(cat, name, link, by_season[sn], passwords, sn, True)
    else:
        _dl_season(cat, name, link, folders, passwords, 1, False)
    try:    # theatrical movie -> movie library (only when movie_library is configured)
        if _fetch_movie(name, link, folders):
            push("🎬 %s: theatrical movie downloaded into the movie library" % name)
    except Exception:
        pass
    try:    # subtitle sweep over the whole share as a safety net (idempotent, silent)
        _place_subs_from_scan(cat, name, link)
    except Exception:
        pass
    restart()


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd, a = sys.argv[1], sys.argv[2:]
    if cmd == "probe":
        sys.stdout.write(dexec(["probe", a[0]]))
    elif cmd == "listtasks":
        sys.stdout.write(dexec(["listtasks"]))
    elif cmd == "tmdb":
        cmd_tmdb(a)
    elif cmd == "addtask":
        cmd_addtask(a)
    elif cmd == "deltask":
        cmd_deltask(a)
    elif cmd == "override":
        cmd_override(a)
    elif cmd == "transfer":
        subprocess.run(["docker", "exec", CONTAINER, "python3", QUARK_MAIN, QUARK_CONF])
    elif cmd == "sync":
        run_sync()
    elif cmd == "autodl":
        cmd_autodl(a)
    elif cmd == "subs":
        cmd_subs(a)
    else:
        print("unknown cmd: " + cmd)


main()
