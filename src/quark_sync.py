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
SPECIAL_RE = re.compile(r'(NC(ED|OP)|\bMenu\b|\bPreview\b|\bClean(ED|OP)|预告|菜单|特典|Secret\s*Video|\bCM\d|\bPV\d|/Extras?/|/EXTRA/|/SPs?/|ScreenShot)', re.I)


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
    p = Path(local_season)
    if not p.exists():
        return False
    tag = "s%02de%02d" % (season, ep)
    return any(f.is_file() and not f.name.endswith(".part") and tag in f.name.lower() for f in p.iterdir())


def download(url, cookie, dest, dlcookie=""):
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


def handle_archive(cat, name, libdir, snum, local_season, season_files, archives, new_by_show):
    archives = [af for af in archives if not SPECIAL_RE.search(af["name"])]
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


def main():
    log("=== quark-tracker sync%s ===" % (" (DRY)" if DRY else ""))
    ov_all = overrides()
    new_by_show = {}
    for cat, conf in CFG["categories"].items():
        root, libroot = conf["quark_root"], conf["library"]
        try:
            tree = api("tree", root)
        except Exception as e:
            err("scanning %s failed: %s" % (cat, str(e)[:50])); continue
        info = tree.get(root, {})
        if not info.get("exists"):
            continue
        for show in info["shows"]:
            name = show["name"]
            libdir = os.path.join(libroot, name)
            for sea in show["seasons"]:
                snum = season_num(sea["name"])
                if snum is None:
                    log("SKIP non-Season:", name, "/", sea["name"]); continue
                local_season = os.path.join(libdir, sea["name"])
                want = []
                archives = []
                for f in sea["files"]:
                    if SPECIAL_RE.search(f["name"]):
                        continue
                    if ARCHIVE_RE.search(f["name"]):
                        archives.append(f); continue
                    if not VIDEO_RE.search(f["name"]):
                        continue
                    ep = parse_ep(f["name"])
                    if ep is None:
                        log("EP_PARSE_FAIL:", name, "|", f["name"]); continue
                    if local_has(local_season, snum, ep):
                        continue
                    want.append((ep, f))
                if want:
                    want.sort()
                    log("PLAN", name, sea["name"], "new:", ["E%02d" % e for e, _ in want])
                    if not DRY:
                        os.makedirs(local_season, exist_ok=True)
                        for ep, f in want:
                            try:
                                resp = api("urls", f["fid"])
                                cookie, url = resp["cookie"], resp["urls"][f["fid"]]
                                dlcookie = resp.get("dlcookies", {}).get(f["fid"], "")
                            except Exception as e:
                                err("%s S%02dE%02d: URL failed: %s" % (name, snum, ep, str(e)[:40])); continue
                            ext = os.path.splitext(f["name"])[1].lower()
                            dest = os.path.join(local_season, "%s - S%02dE%02d%s" % (name, snum, ep, ext))
                            log("DOWNLOAD", name, "S%02dE%02d" % (snum, ep), "%.0fMB" % (f["size"] / 1048576))
                            t0 = time.time()
                            if download(url, cookie, dest, dlcookie):
                                if OWNER:
                                    subprocess.run(["chown", OWNER, dest], capture_output=True)
                                rec = new_by_show.setdefault((cat, name, libdir), {"eps": [], "bytes": 0, "secs": 0.0})
                                rec["eps"].append((snum, ep)); rec["bytes"] += f.get("size", 0); rec["secs"] += time.time() - t0
                            else:
                                err("%s S%02dE%02d: download failed" % (name, snum, ep))
                if archives:
                    log("ARC_PLAN", name, sea["name"], [a["name"] for a in archives])
                    if not DRY:
                        handle_archive(cat, name, libdir, snum, local_season, sea["files"], archives, new_by_show)
    if DRY:
        log("=== dry-run end ==="); return
    for (cat, name, libdir), rec in new_by_show.items():
        rename_show(libdir, name, ov_all.get(name, {}))
        if OWNER:
            subprocess.run(["chown", "-R", OWNER, libdir], capture_output=True)
        notify.send(CFG.get("notify", []), build_msg(cat, name, libdir, rec), log=log)
        log("NOTIFIED", name, fmt_eps(rec["eps"]))
    if not new_by_show:
        log("no new episodes")
    if ERRORS:  # anything that went wrong gets ONE summary notification
        notify.send(CFG.get("notify", []),
                    "❌ quark-tracker | %d error(s) this run:\n%s" % (
                        len(ERRORS), "\n".join("· " + e for e in ERRORS[:10])), log=log)
    log("=== sync done ===")


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
