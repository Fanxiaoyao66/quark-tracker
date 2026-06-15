#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
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


CONF = "/app/config/quark_config.json"
VIDEO = re.compile(r'\.(mkv|mp4|avi|mov|m4v|ts)$', re.I)
NOISE = re.compile(r'\b(1080p?|720p?|2160p?|480p?|10bit|8bit|x?264|x?265|hevc|avc|aac|flac|web-?rip|web-?dl|bdrip|baha|srtx?\d?)\b', re.I)
EP_PATTERNS = [r'[Ss]\d{1,2}[Ee](\d{1,3})', r'-\s*(\d{1,3})\s*(?:\[|\(|v\d|$|\.)',
               r'\[(\d{1,3})\]', r'第\s*(\d{1,3})\s*[话集話]', r'[Ee][Pp]?\s*(\d{1,3})']


def parse_ep(name):
    base = NOISE.sub(' ', os.path.splitext(name)[0])
    for pat in EP_PATTERNS:
        m = re.search(pat, base)
        if m:
            return int(m.group(1))
    return None


def get_acc():
    cfg = json.load(open(CONF))
    acc = q.Quark(cfg["cookie"][0], 0)
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
    r = retry(lambda: acc.get_fids([path]))
    return r[0]["fid"] if r else None


def share_detail(acc, pwd_id, stoken, fid):
    def _():
        r = acc.get_detail(pwd_id, stoken, fid)
        if isinstance(r, dict) and r.get("code") == 0:
            return r["data"]["list"]
        raise RuntimeError("detail!=0")
    return retry(_)


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
                out[root] = {"exists": False, "shows": []}; continue
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
        acc, cookie = get_acc(); res = {}
        for fid in sys.argv[2:]:
            res[fid] = retry(lambda f=fid: acc.download([f])[0]["data"][0]["download_url"])
        emit({"cookie": cookie, "urls": res})
    elif action == "probe":
        acc, _ = get_acc(); url = sys.argv[2]
        pwd_id, passcode, pdir_fid, _ = acc.extract_url(url)
        st = retry(lambda: acc.get_stoken(pwd_id, passcode))
        if st.get("status") != 200:
            emit({"error": "stoken", "message": st.get("message")}); return
        stoken = st["data"]["stoken"]
        folders = []

        def walk(fid, path, depth):
            if depth > 3:
                return
            items = share_detail(acc, pwd_id, stoken, fid)
            vids = [it for it in items if not it.get("dir") and VIDEO.search(it["file_name"])]
            subs = [it for it in items if it.get("dir")]
            if vids:
                eps = sorted({e for e in (parse_ep(v["file_name"]) for v in vids) if e is not None})
                folders.append({"path": path or "/", "fid": fid, "video_count": len(vids),
                                "episodes": eps, "ep_min": (eps[0] if eps else None),
                                "ep_max": (eps[-1] if eps else None),
                                "sample": vids[0]["file_name"]})
            for d in subs:
                walk(d["fid"], (path + "/" + d["file_name"]).lstrip("/"), depth + 1)

        top = share_detail(acc, pwd_id, stoken, pdir_fid)
        top_dirs = [it for it in top if it.get("dir")]
        title_guess = top_dirs[0]["file_name"] if (
            len(top_dirs) == 1 and not any(VIDEO.search(it["file_name"]) for it in top if not it.get("dir"))) else None
        walk(pdir_fid, "", 0)
        emit({"pwd_id": pwd_id, "title_guess": title_guess, "folders": folders})
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


main()
