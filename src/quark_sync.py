#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quark-tracker: download new episodes that quark-auto-save transferred into your
Quark account, dedup against your local library, TMDB-rename, and notify.

Auto-discovers every <category.quark_root>/<show>/Season NN/ folder, downloads
episodes not already present locally (matched by SxxExx), names them
'<show> - SxxExx.<ext>', runs the bundled TMDB renamer to add episode titles, then
sends notifications. Run hourly from cron; safe to re-run (idempotent).

Also handles password-protected archives (.exe/.rar/.7z/.zip — common for adult /
"decoy-extension" releases): reads password candidates from a sideband file written
by quark_ctl autodl, from password txt files next to the archives, or from the
folder name itself; tries variants with 7z until one opens, then extracts the
episodes into the library. Needs p7zip (`7z`) on the host.

Config: $QUARK_TRACKER_CONFIG or ./config.json (see config.example.json).
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch")
EP_PATTERNS = [r'[Ss]\d{1,2}[Ee](\d{1,3})', r'-\s*(\d{1,3})\s*(?:\[|\(|v\d|$|\.)',
               r'(?<![\dxXvV])(\d{1,3})\s*(?:\[|\(|OVA|OAD|END|Fin|话|集)',
               r'\[(\d{1,3})\]', r'第\s*(\d{1,3})\s*[话集話]', r'[Ee][Pp]?\s*(\d{1,3})']
NOISE = re.compile(r'\b(1080p?|720p?|2160p?|480p?|10bit|8bit|x?264|x?265|hevc|avc|aac|flac|web-?rip|web-?dl|bdrip|baha|srtx?\d?)\b', re.I)
# strip "Season N" before episode parsing, or "Season 2 [03]" is misread as ep=2
SEASON_DECOY = re.compile(r'[Ss]eason\s*\d{1,2}', re.I)
SEASON_RE = re.compile(r'Season\s+(\d{1,3})$', re.I)
ARCHIVE_RE = re.compile(r'\.(exe|rar|7z|zip)$', re.I)
PWTXT_RE = re.compile(r'(解压密码|密码|password|pass).*\.txt$', re.I)
PW_TOKEN_RE = re.compile(r'(?:解压密码|密码|password)[：:\s]*([A-Za-z0-9@#._\-/]+)', re.I)
# NCOP/NCED / menus / previews / bonus shorts: not real episodes, would pollute numbering
SPECIAL_RE = re.compile(r'(Mini[\s._-]*(?:Anime|Animation)|迷你动画|小剧场|NC(ED|OP)|\bMenu\b|\bPreview\b|\bClean(ED|OP)|预告|菜单|特典|Secret\s*Video|\bCM\d|\bPV\d|/Extras?/|/EXTRA/|/SPs?/|ScreenShot)', re.I)


from runtime import load_config, state_path, save_json, ask_json


CFG = load_config()
CONTAINER = CFG.get("container", "quark-auto-save")
API_IN = CFG.get("api_in_container", "/app/config/qsync_api.py")
OWNER = CFG.get("owner", "")
EXTS = [e.lower().lstrip(".") for e in CFG.get("video_exts", ["mkv", "mp4", "avi", "mov", "m4v", "ts"])]
VIDEO_RE = re.compile(r'\.(' + '|'.join(EXTS) + r')$', re.I)
RENAME_CFG = CFG.get("rename", {})
TMDB = CFG.get("tmdb", {})
CONN = str(CFG.get("download", {}).get("connections", 16))
LOGFILE = os.path.join(CFG["__dir__"], "quark_sync.log")
DRY = "--dry-run" in sys.argv


def arc_pw_file():
    f = CFG.get("archive_passwords_file", "arc_passwords.json")
    return f if os.path.isabs(f) else os.path.join(CFG["__dir__"], f)


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line)
    if not DRY:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


ERRORS = []


def err(*a):
    # log an error and collect it; main() sends one summary notification at the end
    m = " ".join(str(x) for x in a)
    log("ERR", m)
    ERRORS.append(m)


def overrides():
    f = RENAME_CFG.get("overrides_file") or CFG.get("overrides_file") or "overrides.json"
    if not os.path.isabs(f):
        f = os.path.join(CFG["__dir__"], f)
    return json.load(open(f, encoding="utf-8")) if os.path.exists(f) else {}


def api(*args):
    r = subprocess.run(["docker", "exec", CONTAINER, "python3", API_IN] + list(args),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("api %s failed: %s" % (args[0], (r.stderr or "")[-300:]))
    return json.loads(r.stdout)


def parse_ep(name):
    base = SEASON_DECOY.sub(' ', NOISE.sub(' ', os.path.splitext(name)[0]))
    for pat in EP_PATTERNS:
        m = re.search(pat, base)
        if m:
            return int(m.group(1))
    return None


def parse_ep_range(name):
    # archive names like "...[01-08 Fin]..." (batch) or a bare single episode number
    base = SEASON_DECOY.sub(' ', NOISE.sub(' ', os.path.splitext(name)[0]))
    m = re.search(r'\[(\d{1,3})\s*-\s*(\d{1,3})', base)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    e = parse_ep(name)
    if e is not None:
        return (e, e)
    return None


def season_num(folder):
    m = SEASON_RE.match(folder)
    return int(m.group(1)) if m else None


def local_has(local_season, season, ep):
    p=Path(local_season)
    if not p.exists(): return False
    tag="s%02de%02d"%(season,ep)
    # 必须是真正的视频文件才算"本地已有"——.nfo/.xml/.ass 等元数据/字幕旁车文件也会带着 sXXeYY 标签，
    # 但那不代表正片已经下载；只认旁车文件会把还没下的集数误判成"本地已有"而永久静默跳过(2026-09-07 魔法禁书目录 S03 实例)。
    return any(f.is_file() and VIDEO_RE.search(f.name) and not (f.name.endswith(".part") or f.name.endswith(".part.aria2")) and tag in f.name.lower() for f in p.iterdir())


def download(url, cookie, dest, dlcookie="", slowguard=False):
    """Multi-connection download via aria2c (bypasses Quark's per-connection throttle);
    falls back to single-stream curl if aria2c is not installed.

    Quark validates the temporary `__puus` cookie issued by the /file/download API; the
    signed URL returns HTTP 403 without it. Pass it via `dlcookie` (see qsync_api `urls`)."""
    if dlcookie:
        cookie = cookie + "; " + dlcookie
    d = os.path.dirname(dest)
    part = os.path.basename(dest) + ".part"
    partpath = os.path.join(d, part)
    hdrs = ["--header=Cookie: " + cookie, "--header=User-Agent: " + UA,
            "--referer=https://pan.quark.cn/"]
    if shutil.which("aria2c"):
        cmd = ["aria2c", "-x" + CONN, "-s" + CONN, "-k1M", "--file-allocation=none",
               "--max-tries=5", "--retry-wait=3", "-c", "--console-log-level=warn",
               "--summary-interval=0", "-d", d, "-o", part] + hdrs + [url]
        if slowguard:
            cmd.append("--lowest-speed-limit=" + CFG.get("download", {}).get("lowest_speed_limit", "2M"))
        ok = subprocess.run(cmd).returncode == 0
        if not ok:  # some Quark links reject multi-threaded Range requests; retry single-stream
            for _x in (partpath, partpath + ".aria2"):
                try:
                    os.remove(_x)
                except OSError:
                    pass
            cmd1 = ["aria2c", "-x1", "-s1", "-k1M", "--file-allocation=none",
                    "--max-tries=5", "--retry-wait=3", "-c", "--console-log-level=warn",
                    "--summary-interval=0", "-d", d, "-o", part] + hdrs + [url]
            if slowguard:
                cmd1.append("--lowest-speed-limit=" + CFG.get("download", {}).get("lowest_speed_limit", "2M"))
            ok = subprocess.run(cmd1).returncode == 0
    else:
        hf = "/tmp/.qt_hdr_%d" % os.getpid()
        with open(hf, "w") as fh:
            fh.write("Cookie: " + cookie)
        ok = subprocess.run(["curl", "-fsS", "--retry", "3", "-C", "-", "-o", partpath,
              "-H", "@" + hf, "-H", "User-Agent: " + UA,
              "-H", "Referer: https://pan.quark.cn/", url]).returncode == 0
        try:
            os.remove(hf)
        except OSError:
            pass
    if ok and os.path.exists(partpath):
        os.replace(partpath, dest)
        ctrl = partpath + ".aria2"
        if os.path.exists(ctrl):
            try:
                os.remove(ctrl)
            except OSError:
                pass
        return True
    return False


def _read_pw_from_txt(season_files):
    # if a password txt sits next to the archives in the account folder, download and parse it
    out = []
    for f in season_files:
        if not PWTXT_RE.search(f["name"]):
            continue
        try:
            resp = api("urls", f["fid"])
            url = resp["urls"][f["fid"]]
            cookie = resp["cookie"]
            dlck = resp.get("dlcookies", {}).get(f["fid"], "")
        except Exception:
            continue
        tmp = "/tmp/.qt_pw_%d.txt" % os.getpid()
        if not download(url, cookie, tmp, dlck):
            continue
        try:
            txt = open(tmp, encoding="utf-8", errors="replace").read()
        except Exception:
            txt = ""
        try:
            os.remove(tmp)
        except OSError:
            pass
        for m in PW_TOKEN_RE.finditer(txt):
            out.append(m.group(1))
    return out


def _pw_variants(p):
    # uploaders write decoys like "ruach@66" when the real password is "ruach", or give a
    # whole link where the password is its last segment — derive variants and try them all
    out = [p]
    if "@" in p:
        out.append(p.split("@")[0])
    if "/" in p:
        seg = p.rstrip("/").split("/")[-1]
        if seg:
            out.append(seg)
    return out


def archive_passwords(name, local_season, season_files):
    # candidate sources: (1) sideband file written by quark_ctl autodl (parsed from the share)
    # (2) password txt next to the archives (3) a password embedded in the show/season folder name
    cands = []
    try:
        pf = arc_pw_file()
        if os.path.exists(pf):
            m = json.load(open(pf, encoding="utf-8"))
            v = m.get(name)
            if isinstance(v, list):
                cands += v
            elif isinstance(v, str):
                cands.append(v)
    except Exception:
        pass
    cands += _read_pw_from_txt(season_files)
    for nm in (name, os.path.basename(local_season)):
        mm = PW_TOKEN_RE.search(nm or "")
        if mm:
            cands.append(mm.group(1))
    out = []
    for c in cands:
        for v in _pw_variants(c):
            if v and v not in out:
                out.append(v)
    return out


def handle_archive(cat, name, libdir, snum, local_season, season_files, archives, new_by_show, ledger):
    archives = [af for af in archives if af["fid"] not in ledger and not SPECIAL_RE.search(af["name"])]
    if not archives:
        return
    if not shutil.which("7z"):
        err("%s: archives found but 7z is not installed (p7zip needed), skipped" % name)
        return
    eps = set()
    for af in archives:
        r = parse_ep_range(af["name"])
        if r:
            for e in range(r[0], r[1] + 1):
                eps.add(e)
    if eps and all(local_has(local_season, snum, e) for e in sorted(eps)):
        log("ARC_SKIP all local", name, "S%02d" % snum, sorted(eps))
        return
    pwlist = archive_passwords(name, local_season, season_files)
    if not pwlist:
        err("%s: archives found but no unpack password could be read, skipped" % name)
        return
    log("ARC_PW_CANDIDATES", name, len(pwlist))
    os.makedirs(local_season, exist_ok=True)
    stage = os.path.join(local_season, "_arc")
    try:
        shutil.rmtree(stage)
    except OSError:
        pass
    os.makedirs(stage, exist_ok=True)
    rec = new_by_show.setdefault((cat, name, libdir), {"eps": [], "bytes": 0, "secs": 0.0})
    ok_any = False
    good_pw = None
    pw_failed = False
    for af in archives:
        rng = parse_ep_range(af["name"])
        if rng and all(local_has(local_season, snum, e) for e in range(rng[0], rng[1] + 1)):
            continue  # this pack's episodes are all local already (maybe via the video path)
        try:
            resp = api("urls", af["fid"])
            url = resp["urls"][af["fid"]]
            cookie = resp["cookie"]
            dlck = resp.get("dlcookies", {}).get(af["fid"], "")
        except Exception as e:
            err("%s: archive URL failed: %s" % (name, str(e)[:50]))
            continue
        arcpath = os.path.join(stage, af["name"])
        log("ARC_DOWNLOAD", name, af["name"], "%.0fMB" % (af.get("size", 0) / 1048576))
        t0 = time.time()
        if not download(url, cookie, arcpath, dlck):
            err("%s: archive download failed: %s" % (name, af["name"][:36]))
            continue
        ex = os.path.join(stage, "x")
        try:
            shutil.rmtree(ex)
        except OSError:
            pass
        os.makedirs(ex, exist_ok=True)
        used = None
        rr = None
        trylist = ([good_pw] if good_pw else []) + [p for p in pwlist if p != good_pw]
        for pw in trylist:
            rr = subprocess.run(["7z", "x", "-y", "-p" + pw, "-o" + ex, arcpath],
                                capture_output=True, text=True)
            if rr.returncode == 0:
                used = pw
                good_pw = pw
                break
            try:
                shutil.rmtree(ex)
                os.makedirs(ex, exist_ok=True)
            except OSError:
                pass
        if not used:
            pw_failed = True
            err("%s: extraction failed: %s" % (name, af["name"][:36]))
            log("ARC_7Z_DETAIL", ((rr.stderr or "") + (rr.stdout or ""))[-160:] if rr else "no pw")
            try:
                os.remove(arcpath)
            except OSError:
                pass
            continue
        for root_, _d, files in os.walk(ex):
            for fn in files:
                if not VIDEO_RE.search(fn) or SPECIAL_RE.search(fn):
                    continue
                ep = parse_ep(fn)
                if ep is None:
                    err("%s: extracted video has no parsable episode number: %s" % (name, fn[:36]))
                    continue
                if local_has(local_season, snum, ep):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                dest = os.path.join(local_season, "%s - S%02dE%02d%s" % (name, snum, ep, ext))
                src = os.path.join(root_, fn)
                sz = os.path.getsize(src)
                os.replace(src, dest)
                if OWNER:
                    subprocess.run(["chown", OWNER, dest], capture_output=True)
                rec["eps"].append((snum, ep))
                rec["bytes"] += sz
                ok_any = True
                log("ARC_EXTRACTED", name, "S%02dE%02d" % (snum, ep))
        ledger_mark(ledger, af["fid"], cat, name, snum, -1)
        rec["secs"] += time.time() - t0
        try:
            os.remove(arcpath)
        except OSError:
            pass
        try:
            shutil.rmtree(ex)
        except OSError:
            pass
    try:
        shutil.rmtree(stage)
    except OSError:
        pass
    if not ok_any:
        new_by_show.pop((cat, name, libdir), None)
        if pw_failed:
            err("%s: none of the password candidates opened the archives, skipped" % name)
        else:
            err("%s: archives extracted but contained no usable video, skipped" % name)


def rename_show(libdir, name, ov):
    if not RENAME_CFG.get("enabled", True):
        return
    script = RENAME_CFG.get("script", "tmdb_rename.py")
    if not os.path.isabs(script):
        script = str(HERE / script)
    cmd = [sys.executable, script, "--show-root", libdir, "--apply",
           "--language", TMDB.get("language", "zh-CN"), "--video-exts", ",".join(EXTS)]
    if ov.get("query"):
        cmd += ["--query", ov["query"]]
    if ov.get("tmdb_id"):
        cmd += ["--tmdb-id", str(ov["tmdb_id"])]
    if TMDB.get("env_file"):
        cmd += ["--env-file", TMDB["env_file"]]
    if TMDB.get("api_key"):
        cmd += ["--api-key", TMDB["api_key"]]
    if TMDB.get("bearer_token"):
        cmd += ["--bearer", TMDB["bearer_token"]]
    rr = None
    for _try in range(3):  # transient TMDB/network hiccups leave episodes untitled -> retry
        rr = subprocess.run(cmd, capture_output=True, text=True)
        if rr.returncode == 0:
            break
        log("RENAME_RETRY", name, "try %d/3 failed:" % (_try + 1), (rr.stderr or "")[-160:])
        time.sleep(5)
    log((rr.stdout or "").strip()[-600:])
    if rr.returncode:
        err("%s: TMDB rename failed 3 times, episode titles may be missing" % name)


def fmt_eps(pairs):
    pairs = sorted(set(pairs)); out = []; i = 0
    while i < len(pairs):
        s, e = pairs[i]; j = i
        while j + 1 < len(pairs) and pairs[j + 1] == (s, pairs[j][1] + 1):
            j += 1
        out.append("S%02dE%02d-E%02d" % (s, e, pairs[j][1]) if j > i else "S%02dE%02d" % (s, e))
        i = j + 1
    return ", ".join(out)


def fmt_size(b):
    return "%.1f GB" % (b / 1073741824) if b >= 1073741824 else "%.0f MB" % (b / 1048576)


def build_msg(cat, name, libdir, rec):
    eps = rec["eps"]; b = rec.get("bytes", 0)
    secs = max(rec.get("secs", 0), 0.1)
    spd = b / secs / 1048576
    return ("🎬 quark-tracker | new in library\n"
            "━━━━━━━━━━━━━\n"
            "📺 %s · %s\n"
            "🆕 +%d eps · %s\n"
            "💾 %s · ⚡ %.0f MB/s (aria2c)\n"
            "📂 %s\n"
            "🕒 %s") % (name, cat, len(eps), fmt_eps(eps), fmt_size(b), spd, libdir,
                        time.strftime("%Y-%m-%d %H:%M"))



QUARK_ROOTS = {label: c['quark_root'] for label, c in CFG['categories'].items()}
LIB = {label: c['library'] for label, c in CFG['categories'].items()}
ACTIVE_SHOWS_CACHE = {}
LEDGER_FILE = state_path(CFG, 'download_ledger.json')
AI_DEDUP_CACHE = state_path(CFG, 'ai_dedup_cache.json')
SEASON_VERIFY_CACHE = state_path(CFG, 'season_verify_cache.json')
SPEED_STATS_FILE = state_path(CFG, 'speed_stats.json')
SEASON_HINT_PATTERNS = [r'[Ss](\d{1,2})[Ee]\d{1,3}', r'\bSeason\s*(\d{1,2})\b', r'\bS(\d{1,2})\b(?!\d)']

def push(message):
    if not DRY:
        notify.send(CFG.get('notify', []), message, log=log)

def active_shows(cat, force=False, ttl=300):
    now=time.time()
    ent=ACTIVE_SHOWS_CACHE.get(cat)
    if ent and not force and now-ent["ts"]<ttl:
        return ent["names"]
    root=QUARK_ROOTS[cat]
    try:
        tree=api("tree",root)
    except Exception as e:
        err("刷新 %s 目录失败:%s"%(cat,str(e)[:50]))
        return ent["names"] if ent else set()
    info=tree.get(root,{})
    names={show["name"] for show in info.get("shows",[])} if info.get("exists") else set()
    if not names:                    # 空 = 夸克抖动(fid_of 静默返空), 绝不能当成"所有剧都被移除"
        if info.get("missing"):      # 账号里确实没有这个分类目录 -> 空是真的,别报警
            ACTIVE_SHOWS_CACHE[cat]={"ts":now,"names":names}
            return names
        err("%s 目录树返回空,判为抖动,沿用上次快照"%cat)
        return ent["names"] if ent else set()
    ACTIVE_SHOWS_CACHE[cat]={"ts":now,"names":names}
    return names

def show_active(cat,name,force=False):
    from download_guard import ensure_allowed
    try: ensure_allowed(name)
    except ValueError:
        log('POLICY_SHOW_SKIP', name); return False
    return name in active_shows(cat, force=force)

def filter_stale_errors(errors):
    active_now=set()
    for cat in QUARK_ROOTS:
        active_now.update(active_shows(cat, force=True))
    touched={e.split(" · ",1)[0] for e in errors if " · " in e}
    removed=touched-active_now
    if not removed:
        return errors
    kept=[]
    for e in errors:
        if any(e.startswith(name+" ·") for name in removed):
            log("ERR_SUPPRESS_STALE",e)
            continue
        kept.append(e)
    return kept

def ledger_load():
    return json.load(open(LEDGER_FILE, encoding="utf-8")) if os.path.exists(LEDGER_FILE) else {}

def ledger_save(d):
    if not DRY:
        save_json(LEDGER_FILE, d)

def ledger_mark(ledger,fid,cat,name,snum,ep):
    ledger[fid]={"cat":cat,"show":name,"season":snum,"ep":ep,"ts":time.strftime("%Y-%m-%d %H:%M:%S")}
    ledger_save(ledger)

def archives_all_local(local_season,snum,archives):
    eps=set()
    for af in archives:
        r=parse_ep_range(af["name"])
        if r:
            for e in range(r[0],r[1]+1): eps.add(e)
    return bool(eps) and all(local_has(local_season,snum,e) for e in sorted(eps))

def ai_dedup_cache_load():
    return json.load(open(AI_DEDUP_CACHE, encoding="utf-8")) if os.path.exists(AI_DEDUP_CACHE) else {}

def ai_dedup_cache_save(d):
    if not DRY:
        save_json(AI_DEDUP_CACHE, d)

def ai_pick_best(show,snum,ep,cands):
    key="%s|S%02dE%02d|%s"%(show,snum,ep,"|".join(sorted(c["name"] for c in cands)))
    cache=ai_dedup_cache_load()
    cached=cache.get(key)
    if cached:
        if isinstance(cached,list) and len(cached)==2: return cached[0],cached[1]
        if isinstance(cached,str): return cached,"\u66f4\u4f18\u7248\u672c"   # \u517c\u5bb9\u6539\u7248\u524d\u53ea\u5b58\u6587\u4ef6\u540d\u7684\u65e7\u7f13\u5b58
    lines="\n".join("%d. %s (%.0fMB)"%(i+1,c["name"],c["size"]/1048576) for i,c in enumerate(cands))
    prompt=("\u756a\u5267\u300a%s\u300b S%02dE%02d \u51fa\u73b0%d\u4e2a\u5019\u9009\u6587\u4ef6\uff0c\u90fd\u5bf9\u5e94\u540c\u4e00\u96c6\uff0c\u53ea\u80fd\u4fdd\u7559\u4e00\u4e2a\u7528\u4e8e\u4e0b\u8f7d\u5165\u5e93\u3002\n%s\n\n"
            "\u8bf7\u6309\u4f18\u5148\u7ea7\uff1a1) \u5185\u5bb9\u66f4\u5b8c\u6574/\u66f4\u957f\uff08\u5982 Director's Cut\u3001\u52a0\u957f\u7248\u3001Extended \u4f18\u5148\u4e8e\u666e\u901a\u7535\u89c6\u653e\u9001\u7248\uff0c\u9664\u975e\u660e\u663e\u662f\u9884\u544a/\u82b1\u7d6e/\u9519\u8bef\u6587\u4ef6\uff09 "
            "2) \u753b\u8d28/\u5206\u8fa8\u7387/\u7801\u7387\u66f4\u597d 3) \u7efc\u5408\u53d1\u5e03\u8d28\u91cf\u66f4\u4f18\uff0c\u9009\u51fa\u5e94\u4fdd\u7559\u7684\u552f\u4e00\u7248\u672c\u3002\n"
            "\u53ea\u56de\u590d\u4e25\u683c JSON\uff0c\u4e0d\u8981\u4efb\u4f55\u89e3\u91ca\u6587\u5b57\uff1a{\"keep\":\"<\u5019\u9009\u91cc\u7684\u6587\u4ef6\u540d\u539f\u6587\uff0c\u4e00\u5b57\u4e0d\u5dee>\",\"why\":\"<\u4e0d\u8d85\u8fc710\u4e2a\u6c49\u5b57\uff0c\u8bf4\u660e\u9009\u5b83\u7684\u539f\u56e0\uff0c\u5982 \u52a0\u957f\u5b8c\u6574\u7248/\u753b\u8d28\u66f4\u9ad8>\"}"
            )%(show,snum,ep,len(cands),lines)
    try:
        obj=ask_json(CFG, prompt)
        pick=obj["keep"]; why=str(obj.get("why") or "\u66f4\u4f18\u7248\u672c")[:20]
        names={c["name"] for c in cands}
        if pick not in names: raise RuntimeError("AI pick not in candidates: %r"%pick[:80])
        log("AI_DEDUP",show,"S%02dE%02d"%(snum,ep),"keep:",pick,"why:",why)
    except Exception as e:
        pick=max(cands,key=lambda c:c["size"])["name"]
        why="\u4f53\u79ef\u66f4\u5927(AI\u4e0d\u53ef\u7528\u515c\u5e95)"
        log("AI_DEDUP_FALLBACK(size)",show,"S%02dE%02d"%(snum,ep),"keep:",pick,"reason:",str(e)[:100])
    cache[key]=[pick,why]
    ai_dedup_cache_save(cache)
    return pick,why

def season_verify_cache_load():
    return json.load(open(SEASON_VERIFY_CACHE, encoding="utf-8")) if os.path.exists(SEASON_VERIFY_CACHE) else {}

def season_verify_cache_save(d):
    if not DRY:
        save_json(SEASON_VERIFY_CACHE, d)

def tmdb_get(path,**params):
    import urllib.request,urllib.parse
    key,bearer=tmdb_creds()
    qs=dict(params); qs["language"]="zh-CN"
    if key: qs["api_key"]=key
    url="https://api.themoviedb.org/3"+path+"?"+urllib.parse.urlencode(qs)
    req=urllib.request.Request(url,headers={"Accept":"application/json",**({"Authorization":"Bearer "+bearer} if bearer else {})})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read().decode("utf-8"))

def tmdb_show_seasons(show_name,override):
    key,bearer=tmdb_creds()
    if not key and not bearer: return None
    try:
        tv_id=(override or {}).get("tmdb_id")
        if not tv_id:
            data=tmdb_get("/search/tv",query=(override or {}).get("query") or show_name)
            results=data.get("results") or []
            if not results: return None
            tv_id=results[0]["id"]
        detail=tmdb_get("/tv/%d"%tv_id)
    except Exception:
        return None
    seasons=[{"season_number":s["season_number"],"name":s.get("name") or "","episode_count":s.get("episode_count") or 0,"air_date":s.get("air_date")}
             for s in detail.get("seasons",[]) if s.get("season_number",0)>0]
    return {"tv_id":tv_id,"seasons":seasons}

def filename_season_hint(filenames):
    # 字幕组常直接把季号写进文件名(如 "Toaru Majutsu no Index S1 01")——这是比"数集数猜"或"问AI猜"更硬的证据，
    # 分享方自己写明第几季，不该被"超编→问AI"这种软判断盖过。多数文件(>=80%)标法一致才采信，避免个别文件干扰。
    votes={}
    for fn in filenames:
        base=NOISE.sub(' ', os.path.splitext(fn)[0])
        for pat in SEASON_HINT_PATTERNS:
            m=re.search(pat, base)
            if m:
                n=int(m.group(1)); votes[n]=votes.get(n,0)+1
                break
    if not votes: return None
    total=sum(votes.values())
    best,cnt=max(votes.items(),key=lambda kv:kv[1])
    return best if total>=3 and cnt/total>=0.8 else None

def verify_new_season(show,claimed_num,filenames,override):
    from link_audit import season_hints
    explicit = set().union(*(season_hints(fn) for fn in filenames))
    if explicit == {claimed_num}: return claimed_num
    if explicit and explicit != {claimed_num}:
        raise ValueError('Filename season conflicts with destination; do not relabel or download')
    # 只在"第一次给这季建本地文件夹"时调用一次(见 main() 调用点)；判完缓存，AI不可用/查不到TMDB时兜底=沿用分享方自称的季号(即修复前的老行为，不会更差)
    key="%s|%s"%(show,claimed_num)
    cache=season_verify_cache_load()
    if key in cache: return cache[key]
    info=tmdb_show_seasons(show,override)
    if not info or len(info["seasons"])<=1:
        verified=claimed_num
        cache[key]=verified; season_verify_cache_save(cache); return verified
    seasons=info["seasons"]
    valid_nums={s["season_number"] for s in seasons}
    hint=filename_season_hint(filenames)
    if hint is not None and hint in valid_nums:
        verified=hint                            # 文件名自标季号是最硬的证据，直接信，连AI都不用问
        if verified!=claimed_num:
            log("SEASON_VERIFY_FROM_FILENAME",show,"folder claims S%02d, filenames consistently say S%02d -> trust filename"%(claimed_num,verified))
        cache[key]=verified; season_verify_cache_save(cache); return verified
    # 按解析出的"去重后集数"判断是否超编，别用原始文件数——同一集有 BD/SP 等多版本时原始文件数会是集数的倍数，
    # 拿这种虚高的数字去跟 TMDB 集数比，会把明明没标错的季误判成"超编"进而去问 AI，AI 还可能判错
    # (2026-09-07 魔法禁书目录 S01 实例：24集BD+24集SP=48个文件，48>24 触发AI核实，AI 把 S01 误判成 S03)。
    ep_nums={e for e in (parse_ep(fn) for fn in filenames) if e is not None}
    ep_count=len(ep_nums) if ep_nums else len(filenames)
    matching=next((s for s in seasons if s["season_number"]==claimed_num),None)
    if matching and ep_count<=matching["episode_count"]:
        verified=claimed_num                    # 集数在该季合理范围内，大概率没标错，省一次AI调用
        cache[key]=verified; season_verify_cache_save(cache); return verified
    season_lines="\n".join("第%d季 《%s》 共%d集 %s"%(s["season_number"],s["name"],s["episode_count"],s.get("air_date") or "年份未知") for s in seasons)
    file_lines="\n".join(sorted(filenames))
    prompt=("番剧《%s》的一批文件被归到夸克分享里的\"Season %02d\"文件夹，但分享方常按自己包里的顺序随手编号，不一定是TMDB官方的真实季数。\n"
            "该剧在TMDB的完整季列表：\n%s\n\n这批文件的全部文件名：\n%s\n\n这批文件解析出 %d 个不同的集数(原始文件数%d个，可能含同集多版本)。\n"
            "请判断这批内容实际对应上面哪一季，只回复严格 JSON，不要任何解释文字：{\"season_number\":<整数>}"
            )%(show,claimed_num,season_lines,file_lines,ep_count,len(filenames))
    try:
        got=int(ask_json(CFG, prompt)["season_number"])
        valid={s["season_number"] for s in seasons}
        if got not in valid: raise RuntimeError("AI season %d not in TMDB list"%got)
        verified=got
        log("SEASON_VERIFY",show,"folder claims S%02d -> AI says S%02d"%(claimed_num,verified))
    except Exception as e:
        verified=claimed_num
        log("SEASON_VERIFY_FALLBACK",show,"claimed S%02d, reason:"%claimed_num,str(e)[:100])
    cache[key]=verified
    season_verify_cache_save(cache)
    return verified

def load_speed_samples():
    return json.load(open(SPEED_STATS_FILE, encoding="utf-8")) if os.path.exists(SPEED_STATS_FILE) else []

def save_speed_samples(samples):
    if not DRY:
        save_json(SPEED_STATS_FILE, samples[-60:])

def eta_range_secs(total_bytes):
    samples=load_speed_samples()
    mbps=sorted(s["bytes"]/max(s["secs"],0.1)/1048576 for s in samples if s.get("bytes",0)>2*1048576 and s.get("secs",0)>1)
    if len(mbps)<3:
        lo,hi=8.0,30.0                          # Initial estimate until enough download samples exist
    else:
        lo=max(mbps[max(0,int(len(mbps)*0.25))],1.0)
        hi=max(mbps[min(len(mbps)-1,int(len(mbps)*0.75))],lo+0.1)
    return total_bytes/(hi*1048576), total_bytes/(lo*1048576)   # \u5feb->\u6162\uff0c\u79d2

def fmt_duration(secs):
    if secs<60: return "%d\u79d2"%max(1,int(round(secs)))
    return "%d\u5206\u949f"%max(1,int(round(secs/60.0)))

def fmt_eta(total_bytes):
    if total_bytes<=0: return "-"
    fast_s,slow_s=eta_range_secs(total_bytes)
    if slow_s-fast_s<20: return "\u7ea6"+fmt_duration((fast_s+slow_s)/2)
    return "%s~%s"%(fmt_duration(fast_s),fmt_duration(slow_s))

def build_start_msg(cat,name,plans,ai_notes):
    total_eps=sum(len(p["want"]) for p in plans)
    vid_bytes=sum(f["size"] for p in plans for _,f in p["want"])
    arc_bytes=sum(a.get("size",0) for p in plans for a in p["archives"])
    total_bytes=vid_bytes+arc_bytes
    season_bits=[]
    for p in plans:
        bits=[]
        if p["want"]: bits.append("%d\u96c6"%len(p["want"]))
        if p["archives"]: bits.append("%d\u4e2a\u538b\u7f29\u5305"%len(p["archives"]))
        if bits: season_bits.append("S%02d %s"%(p["snum"],"\u00b7".join(bits)))
    lines=["\U0001F680 quark-tracker\uff5c\u5373\u5c06\u4e0b\u8f7d",
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
           "\U0001F4FA %s \u00b7 %s"%(name,cat),
           "\U0001F4E6 "+(" \u00b7 ".join(season_bits) if season_bits else "-")]
    if total_eps: lines.append("\U0001F195 \u5171 %d \u96c6"%total_eps)
    lines.append("\U0001F4BE %s"%fmt_size(total_bytes))
    for snum,ep,why in ai_notes:
        lines.append("\U0001F916 S%02dE%02d \u2192 %s"%(snum,ep,why))
    lines.append("\u23f1 \u9884\u8ba1 %s\uff08\u6309\u8fd1\u671f\u5e73\u5747\u7f51\u901f\u4f30\u7b97\uff09"%fmt_eta(total_bytes))
    return "\n\n".join(lines)

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

def main():
    from download_guard import ensure_allowed, filename_conflict, owned_seasons
    log("=== quark_sync start%s ==="%(" (DRY)" if DRY else ""))
    new_by_show={}
    speed_samples=[]
    ledger=ledger_load()
    for cat,root in QUARK_ROOTS.items():
        try: tree=api("tree",root)
        except Exception as e: err("扫描 %s 目录失败:%s"%(cat,str(e)[:50])); continue
        info=tree.get(root,{})
        if not info.get("exists") or not info.get("shows"):
            if not info.get("missing"): err("扫描 %s 目录返回空,本轮跳过(疑似夸克抖动)"%cat)
            continue
        for show in info["shows"]:
            name=show["name"]; libdir=os.path.join(LIB[cat],name)
            if not show_active(cat,name):
                log("SHOW_REMOVED_SKIP",name)
                continue
            plans=[]; ai_notes=[]                # 第一遍：只规划(含AI择优)，不下载——这样才能凑齐整部剧的季/集/GB汇总去推"即将下载"
            for sea in show["seasons"]:
                if not show_active(cat,name):
                    log("SHOW_REMOVED_MIDRUN",name,"before",sea["name"])
                    break
                claimed_snum=season_num(sea["name"])
                if claimed_snum is None: log("SKIP non-Season:",name,"/",sea["name"]); continue
                try: ensure_allowed(name, claimed_snum)
                except ValueError:
                    log('POLICY_SEASON_SKIP', name, sea['name']); continue
                conflicts = [f for f in sea['files'] if filename_conflict(f['name'], claimed_snum)]
                for f in conflicts: log('SEASON_FILE_CONFLICT_SKIP', name, sea['name'], f['name'])
                if conflicts:
                    sea = dict(sea, files=[f for f in sea['files'] if f not in conflicts])
                if not sea['files']: continue
                local_season_asis=os.path.join(libdir,sea["name"])
                if os.path.exists(local_season_asis):
                    snum=claimed_snum; local_season=local_season_asis   # 老季，本地已有文件夹，不重新核实
                else:
                    snum=verify_new_season(name,claimed_snum,[f["name"] for f in sea["files"]],overrides().get(name,{}))
                    if snum != claimed_snum and snum in owned_seasons(LIB[cat], name):
                        log('OWNED_SEASON_REMAP_BLOCKED', name, 'S%02d' % claimed_snum, 'S%02d' % snum)
                        continue
                    local_season=os.path.join(libdir,"Season %02d"%snum)   # 全新季，核实一次真实季号后再建目录
                try: ensure_allowed(name, snum)
                except ValueError:
                    log('POLICY_SEASON_SKIP', name, 'S%02d' % snum); continue
                want=[]; archives=[]
                for f in sea["files"]:
                    if SPECIAL_RE.search(f["name"]): continue   # 跳过 NCED/NCOP/菜单/特典/Secret Video
                    if filename_conflict(f['name'], snum):
                        log('SEASON_FILE_CONFLICT_SKIP', name, sea['name'], f['name']); continue
                    if ARCHIVE_RE.search(f["name"]): archives.append(f); continue
                    if not VIDEO_RE.search(f["name"]): continue
                    ep=parse_ep(f["name"])
                    if ep is None: log("EP_PARSE_FAIL:",name,"|",f["name"]); continue
                    if local_has(local_season,snum,ep): continue
                    if f["fid"] in ledger:   # 台账里有=之前下过这个文件，只是本地/账号哪边改过名对不上号了，不是真的新集
                        log("LEDGER_SKIP",name,"S%02dE%02d"%(snum,ep),"(fid 已下载过，跳过重下)")
                        continue
                    want.append((ep,f))
                if want:
                    by_ep={}
                    for ep,f in want: by_ep.setdefault(ep,[]).append(f)
                    deduped=[]
                    for ep,fs in by_ep.items():
                        if len(fs)==1:
                            deduped.append((ep,fs[0]))
                        else:
                            keep,why=ai_pick_best(name,snum,ep,fs)
                            log("DEDUP_COLLISION",name,"S%02dE%02d"%(snum,ep),"candidates:",[c["name"] for c in fs],"keep:",keep,"why:",why)
                            ai_notes.append((snum,ep,why))
                            deduped.append((ep,next(f for f in fs if f["name"]==keep)))
                    want=deduped
                    want.sort(key=lambda x:x[0])
                    log("PLAN",name,sea["name"],"new:",["E%02d"%e for e,_ in want])
                archives=[af for af in archives if af["fid"] not in ledger]   # 台账里有=已处理过，规划阶段就排除，避免误报"即将下载"
                if archives and archives_all_local(local_season,snum,archives):
                    archives=[]   # 集数在本地都已存在(如恶魔高校压缩包早已解压入库)，规划阶段就排除，避免误报"即将下载"
                if archives:
                    log("ARC_PLAN",name,sea["name"],[a["name"] for a in archives])
                if want or archives:
                    plans.append({"snum":snum,"sea":sea,"local_season":local_season,"want":want,"archives":archives})
            if not plans: continue
            start_msg=build_start_msg(cat,name,plans,ai_notes)
            log("START_PLAN",name,start_msg.replace("\n"," | "))
            if not DRY: push(start_msg)
            for p in plans:                      # 第二遍：真正下载/解压
                sea=p["sea"]; snum=p["snum"]; local_season=p["local_season"]; want=p["want"]; archives=p["archives"]
                try: ensure_allowed(name, snum)
                except ValueError:
                    log('POLICY_SEASON_SKIP', name, sea['name']); continue
                if not show_active(cat,name):
                    log("SHOW_REMOVED_MIDRUN",name,"before",sea["name"])
                    break
                if want and not DRY:
                    os.makedirs(local_season,exist_ok=True)
                    for ep,f in want:
                        try: ensure_allowed(name, snum)
                        except ValueError:
                            log('POLICY_SEASON_SKIP', name, sea['name']); break
                        if not show_active(cat,name):
                            log("SHOW_REMOVED_MIDDOWNLOAD",name,"S%02dE%02d"%(snum,ep))
                            break
                        try: _r=api("urls",f["fid"]); url=_r["urls"][f["fid"]]; _dlck=_r.get("dlcookies",{}).get(f["fid"],"")
                        except Exception as e: err("%s · S%02dE%02d 取直链失败:%s"%(name,snum,ep,str(e)[:40])); continue
                        ext=os.path.splitext(f["name"])[1].lower()
                        dest=os.path.join(local_season,"%s - S%02dE%02d%s"%(name,snum,ep,ext))
                        log("DOWNLOAD",name,"S%02dE%02d"%(snum,ep),"%.0fMB"%(f["size"]/1048576))
                        _t0=time.time()
                        _okdl=False
                        for _att in range(3):        # Retry with fresh credentials; CDN reassignment is not guaranteed
                            if _att:
                                time.sleep(3)
                                try: _r=api("urls",f["fid"]); url=_r["urls"][f["fid"]]; _dlck=_r.get("dlcookies",{}).get(f["fid"],"")
                                except Exception: continue
                                log("RETRY_FRESH_LINK",name,"S%02dE%02d"%(snum,ep),"attempt",_att+1)
                            if download(url,_r["cookie"],dest,_dlck,slowguard=(_att<2)): _okdl=True; break
                        if _okdl:
                            if OWNER: subprocess.run(["chown",OWNER,dest])
                            _secs=time.time()-_t0
                            rec=new_by_show.setdefault((cat,name,libdir),{"eps":[],"bytes":0,"secs":0.0})
                            rec["eps"].append((snum,ep)); rec["bytes"]+=f.get("size",0); rec["secs"]+=_secs
                            speed_samples.append({"bytes":f.get("size",0),"secs":_secs})
                            ledger_mark(ledger,f["fid"],cat,name,snum,ep)
                        else: err("%s · S%02dE%02d 下载失败(x16)"%(name,snum,ep))
                if archives and not DRY:
                    if not show_active(cat,name):
                        log("SHOW_REMOVED_BEFORE_ARCHIVE",name,sea["name"])
                        break
                    handle_archive(cat,name,libdir,snum,local_season,sea["files"],archives,new_by_show,ledger)
    if speed_samples: save_speed_samples(load_speed_samples()+speed_samples)
    if DRY: log("=== dry-run end ==="); return
    ov=overrides()
    for (cat,name,libdir),rec in new_by_show.items():
        if not show_active(cat,name, force=True):
            log("SKIP_RENAME_PUSH_REMOVED",name)
            continue
        rename_show(libdir, name, ov.get(name, {}))
        if OWNER: subprocess.run(["chown","-R",OWNER,libdir])
        push(build_msg(cat,name,libdir,rec)); log("PUSHED",name,fmt_eps(rec["eps"]))
    if not new_by_show: log("no new episodes")
    filtered_errors=filter_stale_errors(ERRORS)
    if filtered_errors:                          # 任何一步出错都通知(汇总一条)
        push("❌ quark-tracker｜本次有 %d 处出错：\n\n%s"%(len(filtered_errors),"\n\n".join("· "+e for e in filtered_errors[:10])))
    log("=== quark_sync done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        _tb = traceback.format_exc()
        log("FATAL\n" + _tb)
        try:
            notify.send(CFG.get("notify", []),
                        "❌ quark-tracker crashed: " + ((_tb.strip().splitlines() or [""])[-1])[:200],
                        log=log)
        except Exception:
            pass
        raise
