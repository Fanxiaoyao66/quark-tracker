#!/usr/bin/env python3
"""Runs INSIDE the quark-auto-save container; reuses its Quark client + saved cookie.

Copy this file into the container's config volume (mounted at /app/config) so it is
reachable at /app/config/qsync_api.py, then call via `docker exec`.

Actions:
  tree <root>...        list saved roots  -> {root: {exists, shows:[{name, seasons:[{name, files}]}]}}
  urls <fid>...         -> {"cookie": <account cookie>, "urls": {fid: download_url}}
  probe <share_url>     walk a SHARE link -> {pwd_id, title_guess, folders:[...]}
  addtask <json>        upsert a task into quark_config.json (match by taskname)
  deltask <taskname>    remove a task
  listtasks             list current tasks
  subsfor <url> <fid>   simplified-Chinese subs inside ONE video folder -> [{ep,lang,ext,url,cookie}]
  subscan <url>         simplified-Chinese subs ANYWHERE in the share -> [{season,ep,lang,ext,url,cookie}]
  movieget <url> <fid>  main movie file + sub from a 剧场版 folder -> {video, sub}

NOTE: quark_auto_save prints transient errors (e.g. SSL EOF) to stdout. We redirect
stdout to stderr so ONLY the final clean JSON reaches the real stdout channel.
"""
import json, sys, time, re, os
sys.path.insert(0, "/app")
import quark_auto_save as q

_REAL_OUT = sys.stdout
sys.stdout = sys.stderr  # keep library SSL/error prints off our JSON channel


def emit(obj):
    _REAL_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")


CONF = os.environ.get("QUARK_CONFIG", "/app/config/quark_config.json")
VIDEO = re.compile(r'\.(mkv|mp4|avi|mov|m4v|ts)$', re.I)
ARCHIVE = re.compile(r'\.(exe|rar|7z|zip)$', re.I)
PWTXT = re.compile(r'(解压密码|密码|password|pass).*\.txt$', re.I)
PW_NAME = re.compile(r'(?:解压密码|密码|password)[：:\s]*([A-Za-z0-9@#._\-/]+)', re.I)
NOISE = re.compile(r'\b(1080p?|720p?|2160p?|480p?|10bit|8bit|x?264|x?265|hevc|avc|aac|flac|web-?rip|web-?dl|bdrip|baha|srtx?\d?)\b', re.I)
EP_PATTERNS = [r'[Ss]\d{1,2}[Ee](\d{1,3})', r'-\s*(\d{1,3})\s*(?:\[|\(|v\d|$|\.)',
               r'(?<![\dxXvV])(\d{1,3})\s*(?:\[|\(|OVA|OAD|END|Fin|话|集)',
               r'\[(\d{1,3})\]', r'第\s*(\d{1,3})\s*[话集話]', r'[Ee][Pp]?\s*(\d{1,3})']


SEASON_DECOY = re.compile(r'[Ss]eason\s*\d{1,2}', re.I)   # 抹掉"Season N"季号诱饵,免被当成集数(如 Season 2 [03] 误判 ep=2)


def parse_ep(name):
    base = SEASON_DECOY.sub(' ', NOISE.sub(' ', os.path.splitext(name)[0]))
    for pat in EP_PATTERNS:
        m = re.search(pat, base)
        if m:
            return int(m.group(1))
    return None


def parse_ep_range(name):
    base = SEASON_DECOY.sub(' ', NOISE.sub(' ', os.path.splitext(name)[0]))
    m = re.search(r'\[(\d{1,3})\s*-\s*(\d{1,3})', base)
    if m:
        return int(m.group(1)), int(m.group(2))
    e = parse_ep(name)
    if e is not None:
        return e, e
    return None


def get_acc():
    with open(CONF, encoding='utf-8') as stream:
        cfg = json.load(stream)
    acc = q.Quark(cfg["cookie"][0], 0)
    # Bound this helper's API calls without editing the upstream container image.
    request = acc._send_request
    def bounded_request(*args, **kwargs):
        kwargs.setdefault('timeout', (8, 30))
        return request(*args, **kwargs)
    acc._send_request = bounded_request
    acc.init()
    return acc, cfg["cookie"][0]


def retry(fn, tries=6, delay=2):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(delay)
    raise last


def ls(acc, fid):
    def _():
        r = acc.ls_dir(fid)
        if isinstance(r, dict) and r.get("code") == 0:
            return r["data"]["list"]
        raise RuntimeError("ls_dir!=0")
    return retry(_)


def fid_of(acc, path):
    def _():
        r = acc.get_fids([path])
        if not r:                    # 抖动时 get_fids 静默返回空(不抛异常), retry 兜不到 -> 显式抛出触发重试
            raise RuntimeError("get_fids empty: %s" % path)
        return r
    try:
        return retry(_, tries=3, delay=1)[0]["fid"]
    except Exception:
        return None                  # 连着 3 次都空 -> 认为该路径确实不存在


def share_detail(acc, pwd_id, stoken, fid):
    def _():
        r = acc.get_detail(pwd_id, stoken, fid)
        if isinstance(r, dict) and r.get("code") == 0:
            return r["data"]["list"]
        raise RuntimeError("detail!=0")
    return retry(_)


def _read_share_txt(acc, cookie, pwd_id, stoken, fid, token):
    # save a share txt into a temp account folder, download, return its text, cleanup
    try:
        acc.mkdir("/__pwtmp")
        r = retry(lambda: acc.get_fids(["/__pwtmp"]))
        tf = r[0]["fid"] if r else None
        if not tf:
            return ""
        retry(lambda: acc.save_file([fid], [token], tf, pwd_id, stoken))
        time.sleep(2)
        lst = retry(lambda: acc.ls_dir(tf)).get("data", {}).get("list", [])
        out = ""
        import urllib.request
        for it in lst:
            if it["file_name"].lower().endswith(".txt"):
                rr, tck = acc.download([it["fid"]])
                u = rr["data"][0]["download_url"]
                ck = cookie + ("; " + tck if tck else "")
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Cookie": ck})
                out = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        if lst:
            acc.delete([it["fid"] for it in lst])
        acc.delete([tf])
        return out
    except Exception:
        return ""

def write_cfg(cfg):
    tmp = CONF + ".tmp"
    json.dump(cfg, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, CONF)


def main():
    action = sys.argv[1]
    if action == "tree":
        acc, _ = get_acc(); out = {}
        for root in sys.argv[2:]:
            rfid = fid_of(acc, root)
            if not rfid:
                miss = False                     # 父目录列得出来且里面没有它 -> 确实不存在,不是抖动
                try:
                    parent, _, leaf = root.rpartition("/")
                    pfid = fid_of(acc, parent) if parent else None
                    if pfid:
                        miss = leaf not in {x["file_name"] for x in ls(acc, pfid) if x.get("dir")}
                except Exception:
                    miss = False
                out[root] = {"exists": False, "missing": miss, "shows": []}; continue
            shows = []
            for show in ls(acc, rfid):
                if not show.get("dir"):
                    continue
                seasons = []
                for sea in ls(acc, show["fid"]):
                    if not sea.get("dir"):
                        continue
                    files = [{"name": f["file_name"], "fid": f["fid"], "size": f["size"]}
                             for f in ls(acc, sea["fid"]) if not f.get("dir")]
                    seasons.append({"name": sea["file_name"], "files": files})
                shows.append({"name": show["file_name"], "seasons": seasons})
            out[root] = {"exists": True, "shows": shows}
        emit(out)
    elif action == "urls":
        acc, cookie = get_acc(); res = {}; dlck = {}
        for fid in sys.argv[2:]:
            def _u(f=fid):
                r, tck = acc.download([f])
                return r["data"][0]["download_url"], tck
            url, tck = retry(_u)
            res[fid] = url; dlck[fid] = tck
        emit({"cookie": cookie, "urls": res, "dlcookies": dlck})
    elif action == "probe":
        acc, cookie = get_acc(); url = sys.argv[2]
        pwd_id, passcode, pdir_fid, _ = acc.extract_url(url)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({"error": "stoken", "message": st.get("message")}); return
        stoken = st["data"]["stoken"]
        folders = []; pw_names = []; pw_txts = []; scan_errors = []; visited = set()

        def walk(fid, path, depth):
            if depth > 32 or len(visited) >= 10000:
                scan_errors.append({"path": path, "error": "scan_limit"}); return
            if fid in visited: return
            visited.add(fid)
            try: items = share_detail(acc, pwd_id, stoken, fid)
            except Exception as exc:
                scan_errors.append({"path": path, "error": str(exc)[:160]}); return
            vids = [it for it in items if not it.get("dir") and VIDEO.search(it["file_name"])]
            arcs = [it for it in items if not it.get("dir") and ARCHIVE.search(it["file_name"])]
            pws = [it for it in items if not it.get("dir") and PWTXT.search(it["file_name"])]
            subs = [it for it in items if it.get("dir")]
            for it in subs:                                    # 密码常藏在诱饵空文件夹名里：解压密码：xxx
                m = PW_NAME.search(it["file_name"])
                if m:
                    pw_names.append(m.group(1))
            for it in pws:
                pw_txts.append((it["fid"], it.get("share_fid_token")))
            if vids or arcs:
                aeps = set()
                for av in arcs:
                    r = parse_ep_range(av["file_name"])
                    if r:
                        for e in range(r[0], r[1] + 1):
                            aeps.add(e)
                veps = {e for e in (parse_ep(v["file_name"]) for v in vids) if e is not None}
                eps = sorted(veps | aeps)
                folders.append({"path": path or "/", "fid": fid,
                                "kind": ("video" if vids else "archive"),
                                "video_count": len(vids), "archive_count": len(arcs),
                                "has_password": bool(pws),
                                "total_size": sum(it.get("size", 0) for it in (vids + arcs)),
                                "archives": [a["file_name"] for a in arcs],
                                "files": [{"fid": v["fid"], "name": v["file_name"], "size": v.get("size", 0),
                                           "kind": "video" if VIDEO.search(v["file_name"]) else "archive"} for v in vids + arcs],
                                "episodes": eps, "ep_min": (eps[0] if eps else None),
                                "ep_max": (eps[-1] if eps else None),
                                "sample": (vids[0]["file_name"] if vids else arcs[0]["file_name"])})
            for d in subs:
                walk(d["fid"], (path + "/" + d["file_name"]).lstrip("/"), depth + 1)

        top = share_detail(acc, pwd_id, stoken, pdir_fid)
        top_dirs = [it for it in top if it.get("dir")]
        title_guess = top_dirs[0]["file_name"] if (
            len(top_dirs) == 1 and not any(VIDEO.search(it["file_name"]) for it in top if not it.get("dir"))) else None
        walk(pdir_fid, "", 0)
        passwords = []                                         # 文件夹名候选优先(免下载)，否则读密码txt
        for p in pw_names:
            if p not in passwords:
                passwords.append(p)
        if not passwords and pw_txts and "--readonly" not in sys.argv:
            txt = _read_share_txt(acc, cookie, pwd_id, stoken, pw_txts[0][0], pw_txts[0][1])
            for m in PW_NAME.finditer(txt or ""):
                if m.group(1) not in passwords:
                    passwords.append(m.group(1))
        emit({"pwd_id": pwd_id, "title_guess": title_guess, "folders": folders, "passwords": passwords, "scan_complete": not scan_errors, "scan_errors": scan_errors})
    elif action == "addtask":
        task = json.loads(sys.argv[2]); cfg = json.load(open(CONF))
        tl = cfg.setdefault("tasklist", [])
        for i, t in enumerate(tl):
            if t.get("taskname") == task["taskname"]:
                tl[i] = {**t, **task}; break
        else:
            tl.append(task)
        write_cfg(cfg)
        emit({"ok": True, "tasks": len(tl), "taskname": task["taskname"]})
    elif action == "deltask":
        name = sys.argv[2]; cfg = json.load(open(CONF))
        before = len(cfg.get("tasklist", []))
        cfg["tasklist"] = [t for t in cfg.get("tasklist", []) if t.get("taskname") != name]
        write_cfg(cfg)
        emit({"ok": True, "removed": before - len(cfg["tasklist"])})
    elif action == "listtasks":
        cfg = json.load(open(CONF))
        emit([{"taskname": t.get("taskname"), "savepath": t.get("savepath"),
               "pattern": t.get("pattern"), "enddate": t.get("enddate", ""),
               "shareurl": t.get("shareurl", "")} for t in cfg.get("tasklist", [])])
    elif action == "lsfiles":
        # 返回账号某路径下的文件清单；调用方必须确认选中版本的具体文件
        # 已经转入，不能因目录里残留旧文件就误报转存成功。
        acc, _ = get_acc()
        fid = fid_of(acc, sys.argv[2])
        if not fid:
            emit({"count": 0, "files": []})
        else:
            try:
                files = [x for x in ls(acc, fid) if not x.get("dir")]
                emit({"count": len(files), "files": [
                    {"fid": x.get("fid"), "name": x.get("file_name"), "size": x.get("size", 0)}
                    for x in files]})
            except Exception as e:
                emit({"count": 0, "files": [], "error": str(e)[:80]})
    elif action == "subsfor":
        # subsfor <share_url> <video_folder_fid>: 该文件夹(及子目录)里的简体外挂字幕→save到/__sub→返回[{ep,lang,ext,url,cookie}]
        acc, cookie = get_acc(); surl = sys.argv[2]; folder = sys.argv[3]
        pwd_id, passcode, _, _ = acc.extract_url(surl)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({"subs": []}); return
        stoken = st["data"]["stoken"]
        SUB = re.compile(r'\.(ass|srt|ssa|sub|vtt)$', re.I)
        found = []
        def gather(fid, depth):
            if depth > 2:
                return
            for it in share_detail(acc, pwd_id, stoken, fid):
                if it.get("dir"):
                    gather(it["fid"], depth + 1)
                elif SUB.search(it["file_name"]):
                    found.append((it["fid"], it.get("share_fid_token"), it["file_name"]))
        gather(folder, 0)
        def lang(nm):
            low = nm.lower()
            if (".sc." in low) or ("简" in nm) or ("chs" in low) or ("[gb]" in low) or ("_sc" in low) or (".chs." in low): return "sc"
            if (".tc." in low) or ("繁" in nm) or ("cht" in low) or ("big5" in low): return "tc"
            return "x"
        rank = {"sc": 3, "x": 2, "tc": 1}
        byep = {}
        for fid, tok, nm in found:
            ep = parse_ep(nm)
            if ep is None:
                continue
            l = lang(nm); cur = byep.get(ep)
            if (not cur) or rank[l] > rank[cur[2]]:
                byep[ep] = (fid, tok, l, nm)
        if not byep:
            emit({"subs": []}); return
        acc.mkdir("/__sub"); tf = fid_of(acc, "/__sub")
        retry(lambda: acc.save_file([v[0] for v in byep.values()], [v[1] for v in byep.values()], tf, pwd_id, stoken))
        time.sleep(3)
        saved = {it["file_name"]: it for it in ls(acc, tf) if not it.get("dir")}
        out = []
        for ep, (fid, tok, l, nm) in byep.items():
            s = saved.get(nm)
            if not s:
                continue
            try:
                r, tck = acc.download([s["fid"]])
                out.append({"ep": ep, "lang": l, "ext": nm.rsplit(".", 1)[-1].lower(),
                            "url": r["data"][0]["download_url"], "cookie": cookie + ("; " + tck if tck else "")})
            except Exception:
                pass
        emit({"subs": out})
    elif action == "subscan":
        # subscan <share_url>: 扫整个分享(任意层级)的所有外挂字幕→按(季,集)去重选简体→save到/__sub→返回[{season,ep,lang,ext,url,cookie}]
        # 与 subsfor 区别：subsfor 只扫单个视频文件夹;subscan 扫全分享(给"补字幕"用,字幕常单独放顶层文件夹)
        acc, cookie = get_acc(); surl = sys.argv[2]
        pwd_id, passcode, pdir_fid, _ = acc.extract_url(surl)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({"subs": []}); return
        stoken = st["data"]["stoken"]
        SUB = re.compile(r'\.(ass|srt|ssa|sub|vtt)$', re.I)
        SEASON_AR = re.compile(r'\bS(\d{1,2})\b|[Ss]eason\s*(\d{1,2})|第\s*(\d{1,2})\s*[季期]')
        SEASON_CN = re.compile(r'第\s*([一二三四五六七八九十]+)\s*[季期]')   # 中文数字季号:第一季/第二季
        CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        found = []
        def gather(fid, path, depth):
            if depth > 4:
                return
            for it in share_detail(acc, pwd_id, stoken, fid):
                nm = it["file_name"]
                if it.get("dir"):
                    gather(it["fid"], (path + "/" + nm).lstrip("/"), depth + 1)
                elif SUB.search(nm):
                    found.append((it["fid"], it.get("share_fid_token"), nm, path))
        gather(pdir_fid, "", 0)
        def lang(nm):
            low = nm.lower()
            if (".sc." in low) or ("简" in nm) or ("chs" in low) or ("[gb]" in low) or ("_sc" in low) or (".chs." in low): return "sc"
            if (".tc." in low) or ("繁" in nm) or ("cht" in low) or ("big5" in low): return "tc"
            return "x"
        def season_of(s):
            s = s or ""
            m = SEASON_AR.search(s)
            if m:
                for g in m.groups():
                    if g: return int(g)
            m = SEASON_CN.search(s)
            if m and m.group(1) in CN_NUM:
                return CN_NUM[m.group(1)]
            return None
        rank = {"sc": 3, "x": 2, "tc": 1}
        bykey = {}
        for fid, tok, nm, path in found:
            ep = parse_ep(nm)
            if ep is None:
                continue
            sea = season_of(nm)
            if sea is None: sea = season_of(path)
            key = (sea, ep)
            l = lang(nm); cur = bykey.get(key)
            if (not cur) or rank[l] > rank[cur[2]]:
                bykey[key] = (fid, tok, l, nm, sea)
        if not bykey:
            emit({"subs": []}); return
        acc.mkdir("/__sub"); tf = fid_of(acc, "/__sub")
        try:                                        # 清掉上次残留,避免同名旧文件干扰
            old = [x["fid"] for x in ls(acc, tf) if not x.get("dir")]
            if old: acc.delete(old)
        except Exception:
            pass
        _allv = list(bykey.values())
        for _ci in range(0, len(_allv), 10):       # 分批转存(每批10):大合集分享一次性 save_file 易部分失败丢字幕(KonoSuba S2 只回1条的根因)
            _ch = _allv[_ci:_ci + 10]
            try:
                retry(lambda c=_ch: acc.save_file([v[0] for v in c], [v[1] for v in c], tf, pwd_id, stoken))
            except Exception:
                pass
            time.sleep(1)
        time.sleep(2)
        saved = {it["file_name"]: it for it in ls(acc, tf) if not it.get("dir")}
        out = []
        for (sea, ep), (fid, tok, l, nm, season) in bykey.items():
            s = saved.get(nm)
            if not s:
                continue
            try:
                r, tck = retry(lambda sf=s["fid"]: acc.download([sf]))   # 逐条下载重试,SSL EOF 抖动不丢字幕
                out.append({"season": season, "ep": ep, "lang": l, "ext": nm.rsplit(".", 1)[-1].lower(),
                            "url": r["data"][0]["download_url"], "cookie": cookie + ("; " + tck if tck else "")})
            except Exception:
                pass
        emit({"subs": out})
    elif action == "subarch":
        # subarch <share_url>: 找"字幕语义文件夹"(外挂字幕/字幕/Subs)里的字幕压缩档→save到/__suba→返回[{name,path,season,url,cookie}]
        # 补 subscan 盲区:外挂字幕常被打包成 .zip/.rar/.7z 放在字幕文件夹里,subscan(只认散装.ass)抓不到
        acc, cookie = get_acc(); surl = sys.argv[2]
        pwd_id, passcode, pdir_fid, _ = acc.extract_url(surl)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({"archives": []}); return
        stoken = st["data"]["stoken"]
        SUBDIR = re.compile(r'字幕|外挂|subtitle|subs', re.I)
        FONT = re.compile(r'字体|font|子集化|subset', re.I)
        SEA = re.compile(r'\bS(\d{1,2})\b|[Ss]eason\s*(\d{1,2})|第\s*([0-9一二三四五六七八九十]+)\s*[季期]')
        CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        def sea_of(s):
            for m in SEA.finditer(s or ""):
                for g in m.groups():
                    if g:
                        return int(g) if str(g).isdigit() else CN.get(g)
            return None
        found = []
        def gather(fid, path, depth):
            if depth > 5:
                return
            for it in share_detail(acc, pwd_id, stoken, fid):
                nm = it["file_name"]; np = (path + "/" + nm).lstrip("/")
                if it.get("dir"):
                    gather(it["fid"], np, depth + 1)
                elif ARCHIVE.search(nm) and SUBDIR.search(path or "") and not FONT.search(nm):
                    found.append((it["fid"], it.get("share_fid_token"), nm, path))
        gather(pdir_fid, "", 0)
        if not found:
            emit({"archives": []}); return
        acc.mkdir("/__suba"); tf = fid_of(acc, "/__suba")
        try:
            old = [x["fid"] for x in ls(acc, tf) if not x.get("dir")]
            if old: acc.delete(old)
        except Exception:
            pass
        retry(lambda: acc.save_file([f[0] for f in found], [f[1] for f in found], tf, pwd_id, stoken))
        time.sleep(3)
        saved = {it["file_name"]: it for it in ls(acc, tf) if not it.get("dir")}
        out = []
        for fid, tok, nm, path in found:
            s = saved.get(nm)
            if not s:
                continue
            try:
                r, tck = retry(lambda sf=s["fid"]: acc.download([sf]))
                out.append({"name": nm, "path": path, "season": sea_of(path) or sea_of(nm),
                            "url": r["data"][0]["download_url"],
                            "cookie": cookie + ("; " + tck if tck else "")})
            except Exception:
                pass
        emit({"archives": out})
    elif action == "movieget":
        # movieget <share_url> <movie_folder_fid>: 剧场版文件夹的主影片(最大,非SP/Menu/CM/NC)+简体字幕→save→直链
        acc, cookie = get_acc(); surl = sys.argv[2]; folder = sys.argv[3]
        pwd_id, passcode, _, _ = acc.extract_url(surl)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({}); return
        stoken = st["data"]["stoken"]
        SUB = re.compile(r'\.(ass|srt|ssa|sub|vtt)$', re.I)
        EXTRA = re.compile(r'\[(SP|Menu|CM|PV|NC(ED|OP)|Preview)', re.I)
        vids = []; subs = []
        def gather(fid, depth):
            if depth > 2:
                return
            for it in share_detail(acc, pwd_id, stoken, fid):
                nm = it["file_name"]
                if it.get("dir"):
                    gather(it["fid"], depth + 1)
                elif VIDEO.search(nm) and not EXTRA.search(nm):
                    vids.append(it)
                elif SUB.search(nm):
                    subs.append(it)
        gather(folder, 0)
        if not vids:
            emit({}); return
        mv = max(vids, key=lambda it: it.get("size", 0))
        # 简体字幕优先
        def sclang(nm):
            low = nm.lower()
            return (".sc." in low) or ("简" in nm) or ("chs" in low) or ("[gb]" in low) or ("_sc" in low)
        sub = next((s for s in subs if sclang(s["file_name"])), (subs[0] if subs else None))
        savefids = [mv["fid"]] + ([sub["fid"]] if sub else [])
        savetoks = [mv.get("share_fid_token")] + ([sub.get("share_fid_token")] if sub else [])
        acc.mkdir("/__mov"); tf = fid_of(acc, "/__mov")
        retry(lambda: acc.save_file(savefids, savetoks, tf, pwd_id, stoken))
        time.sleep(3)
        saved = {it["file_name"]: it for it in ls(acc, tf) if not it.get("dir")}
        res = {}
        sv = saved.get(mv["file_name"])
        if sv:
            try:
                r, tck = acc.download([sv["fid"]]); res["video"] = {"name": mv["file_name"], "url": r["data"][0]["download_url"], "cookie": cookie + ("; " + tck if tck else "")}
            except Exception:
                pass
        if sub:
            ss = saved.get(sub["file_name"])
            if ss:
                try:
                    r, tck = acc.download([ss["fid"]]); res["sub"] = {"ext": sub["file_name"].rsplit(".", 1)[-1].lower(), "url": r["data"][0]["download_url"], "cookie": cookie + ("; " + tck if tck else "")}
                except Exception:
                    pass
        emit(res)


if __name__ == "__main__":
    main()
