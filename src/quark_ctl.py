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

Config: $QUARK_TRACKER_CONFIG or ./config.json
"""
import json, os, subprocess, sys, time, urllib.request, urllib.parse, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


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
        subprocess.run([sys.executable, str(HERE / "quark_sync.py")])
    else:
        print("unknown cmd: " + cmd)


main()
