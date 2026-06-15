#!/usr/bin/env python3
"""quark-tracker: download new episodes that quark-auto-save transferred into your
Quark account, dedup against your local library, TMDB-rename, and notify.

Auto-discovers every <category.quark_root>/<show>/Season NN/ folder, downloads
episodes not already present locally (matched by SxxExx), names them
'<show> - SxxExx.<ext>', runs the bundled TMDB renamer to add episode titles, then
sends notifications. Run hourly from cron; safe to re-run (idempotent).

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
               r'\[(\d{1,3})\]', r'第\s*(\d{1,3})\s*[话集話]', r'[Ee][Pp]?\s*(\d{1,3})']
NOISE = re.compile(r'\b(1080p?|720p?|2160p?|480p?|10bit|8bit|x?264|x?265|hevc|avc|aac|flac|web-?rip|web-?dl|bdrip|baha|srtx?\d?)\b', re.I)
SEASON_RE = re.compile(r'Season\s+(\d{1,3})$', re.I)


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


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line)
    if not DRY:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


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
    base = NOISE.sub(' ', os.path.splitext(name)[0])
    for pat in EP_PATTERNS:
        m = re.search(pat, base)
        if m:
            return int(m.group(1))
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


def download(url, cookie, dest):
    """Multi-connection download via aria2c (bypasses Quark's per-connection throttle);
    falls back to single-stream curl if aria2c is not installed."""
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
    rr = subprocess.run(cmd, capture_output=True, text=True)
    log((rr.stdout or "").strip()[-600:])
    if rr.returncode:
        log("RENAME_ERR", (rr.stderr or "")[-300:])


def main():
    log("=== quark-tracker sync%s ===" % (" (DRY)" if DRY else ""))
    ov_all = overrides()
    new_by_show = {}
    for cat, conf in CFG["categories"].items():
        root, libroot = conf["quark_root"], conf["library"]
        try:
            tree = api("tree", root)
        except Exception as e:
            log("TREE_ERR", cat, e); continue
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
                for f in sea["files"]:
                    if not VIDEO_RE.search(f["name"]):
                        continue
                    ep = parse_ep(f["name"])
                    if ep is None:
                        log("EP_PARSE_FAIL:", name, "|", f["name"]); continue
                    if local_has(local_season, snum, ep):
                        continue
                    want.append((ep, f))
                if not want:
                    continue
                want.sort()
                log("PLAN", name, sea["name"], "new:", ["E%02d" % e for e, _ in want])
                if DRY:
                    continue
                os.makedirs(local_season, exist_ok=True)
                for ep, f in want:
                    try:
                        resp = api("urls", f["fid"])
                        cookie, url = resp["cookie"], resp["urls"][f["fid"]]
                    except Exception as e:
                        log("URL_ERR", name, ep, e); continue
                    ext = os.path.splitext(f["name"])[1].lower()
                    dest = os.path.join(local_season, "%s - S%02dE%02d%s" % (name, snum, ep, ext))
                    log("DOWNLOAD", name, "S%02dE%02d" % (snum, ep), "%.0fMB" % (f["size"] / 1048576))
                    if download(url, cookie, dest):
                        if OWNER:
                            subprocess.run(["chown", OWNER, dest], capture_output=True)
                        new_by_show.setdefault((cat, name, libdir), []).append("S%02dE%02d" % (snum, ep))
                    else:
                        log("DOWNLOAD_FAIL", name, ep)
    if DRY:
        log("=== dry-run end ==="); return
    for (cat, name, libdir), eps in new_by_show.items():
        rename_show(libdir, name, ov_all.get(name, {}))
        if OWNER:
            subprocess.run(["chown", "-R", OWNER, libdir], capture_output=True)
        msg = "📺 %s\n《%s》\nNew: %s" % (cat, name, " ".join(sorted(eps)))
        notify.send(CFG.get("notify", []), msg, log=log)
        log("NOTIFIED", name, eps)
    if not new_by_show:
        log("no new episodes")
    log("=== sync done ===")


main()
