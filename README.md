<p align="center">
  <img src="assets/logo.svg" alt="quark-tracker" width="480">
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">中文</a>
</p>

# quark-tracker

**Auto-download newly-aired episodes from Quark (夸克网盘) share links to your NAS, in Infuse/Plex/Jellyfin-friendly naming — with TMDB episode titles, smart dedup, multi-threaded downloads, optional notifications, and an agent-driven "just send a link" mode.**

<img width="1791" height="1566" alt="image" src="https://github.com/user-attachments/assets/2116ff66-a3cb-4843-8cb8-16606014ada8" />

<img width="1866" height="1326" alt="57e67868-9af1-4eb3-858c-776b28805f74" src="https://github.com/user-attachments/assets/4cf60f84-c742-4059-8086-59b815e89ab1" />



> 🇨🇳 **夸克网盘自动追番 / 追剧下载到 NAS**：分享链接 → 自动转存 → **aria2c 全速下载**到飞牛 fnOS / 群晖等 NAS → **TMDB 改名** → Infuse / Emby / Jellyfin，支持连载自动监控、完结判断、微信 / QQ 通知。详见 **[中文文档](README.zh-CN.md)**。
>
> **关键词**：夸克网盘 · 夸克自动下载 · 自动追番 · 追剧 · NAS · 飞牛 fnOS · Infuse 刮削 · TMDB · aria2 加速 · quark-auto-save 搭档

It is a companion to [**quark-auto-save**](https://github.com/Cp0204/quark-auto-save) (which watches share links and *transfers* new episodes into your own Quark account). quark-tracker adds the missing half: it **actually downloads** those episodes to local disk, **renames** them with TMDB metadata, **dedupes** against what you already have, and can be **driven by an LLM agent** to decide — from a single share link — whether a show is finished or still airing, and to set up monitoring with an automatic end date.

```
Quark share link (keeps updating)
  │  ① quark-auto-save (Docker)   monitor share → transfer new eps into your Quark account
  ▼
your Quark account  /<root>/<Show>/Season NN/
  │  ② quark-tracker  quark_sync.py (cron)   list via Quark API → dedup by SxxExx → aria2c download new only
  ▼
local NAS library  /media/<Show>/Season NN/<Show> - SxxExx - Episode Name.ext
  │  ③ TMDB rename (bundled) + pluggable notify (webhook / command)
  ▼
  Infuse / Plex / Jellyfin
```

Downloads go straight through the Quark download API (cookie + signed URL) using **aria2c multi-connection**, so they are full-speed and unaffected by any FUSE/WebDAV mount caching.

## Why

quark-auto-save is great at cloud-to-cloud transfer + renaming inside Quark. But if you want **real local files** on your NAS (archive, no dependence on a share staying alive, rock-solid scraping), you need a downloader that pulls only the *new* episodes and merges them into an existing library — fast. That's quark-tracker.

> **Why aria2c matters:** Quark throttles a *single* connection to ~100 KB/s for free accounts. With 16 parallel connections aria2c reached **~40–67 MB/s** in testing (**hundreds of times faster**). A 1.5 GB episode drops from *hours* to *under a minute*.

## Features

- **Download only what's new** — dedup by `SxxExx` against the local library; episodes you already have are skipped.
- **Fast** — aria2c multi-connection bypasses Quark's per-connection throttle (falls back to `curl`).
- **TMDB renaming** — bundled standalone renamer → `Show - S02E10 - Episode Name.ext`. No external agent required.
- **Multi-category** — map any labels (anime / TV / …) to a Quark staging root and a local library path.
- **Pluggable notifications** — `webhook` (ServerChan / Bark / custom) or `command` (run any local push script), or none.
- **Agent-driven mode** (optional) — one background `autodl --link <url>` call runs the whole pipeline: detect seasons (multi-season packs handled season by season), pick the best release (coverage → quality → size → usable simplified-Chinese subs), ask TMDB whether the show has finished, transfer (verified + retried), download, rename, notify — so an LLM agent replies instantly instead of timing its turn out on a multi-GB download. Manual subcommands (`probe / tmdb / addtask / sync / …`) remain for diagnostics.
- **Password-protected archives** — releases disguised as `.exe`/`.rar`/`.7z`/`.zip` are downloaded, the unpack password is parsed from the share (decoy folder names / password txt, with variant guessing), extracted with `7z`, and the episodes filed into the library.
- **External subtitles** — `subs --link <url>` (also run automatically after `autodl`) sweeps a share for external subs (loose or zipped), prefers simplified Chinese, and names them `<video name>.zh.ass` next to the matching episodes.
- **Theatrical movies** — optional: a 剧场版 folder inside a TV share is detected and downloaded into a separate movie library.
- **Zero Python dependencies** — standard library + `aria2c`/`curl` + `docker` only.

## Requirements

- A Linux host / NAS with **Docker** (this guide uses **fnOS / 飞牛**, which is Debian-based — any Linux works).
- **[quark-auto-save](https://github.com/Cp0204/quark-auto-save)** running (a `compose.yaml` is included), logged into your Quark account.
- A free **[TMDB](https://www.themoviedb.org/settings/api) API key** for episode-title renaming.
- `python3`, `docker`, and **`aria2c`** (strongly recommended) on the host.

---

## 🤖 One-command AI deploy

Have an AI agent with shell/SSH access to your NAS (Claude Code, OpenClaw, Cursor, …) set it all up. Paste this prompt:

```text
Deploy "quark-tracker" (https://github.com/Fanxiaoyao66/quark-tracker) on my Linux NAS, end-to-end:
1. Install aria2c (apt/opkg/etc).
2. git clone the repo to /opt/quark-tracker.
3. docker compose up -d the bundled quark-auto-save, then tell me to log into Quark in its WebUI (:5005).
4. Copy src/qsync_api.py into the quark-auto-save config volume (so it lands at /app/config/qsync_api.py).
5. Create /opt/quark-tracker/config.json from config.example.json.
6. Add the hourly root cron job (with flock) from the README.
7. Run quark_sync.py --dry-run to verify, and show me the output.
First ask me for: my media-library paths, my TMDB API key, and how I want notifications
(a webhook URL, or a shell command). Then do it and report exactly what you changed.
```

Review what the agent proposes before granting it root — see the agent-mode notes in Step 7.

---

## Tutorial — deploy on a NAS (example: fnOS / 飞牛)

The steps below use a 飞牛 fnOS NAS as a concrete example. Adapt the paths to your system.

### Step 0 — Prepare the host

1. **Enable SSH** on fnOS: Settings → Terminal/SSH, turn on SSH. SSH in.
2. **Install aria2c** (fnOS is Debian):
   ```bash
   sudo apt update && sudo apt install -y aria2
   ```
3. Note your **media library** paths, e.g. on fnOS they look like:
   ```
   /vol1/1000/动漫     # anime
   /vol1/1000/剧集     # TV
   ```
   Point your player (Infuse / 飞牛影视 / Emby / Jellyfin) at these folders.

### Step 1 — Deploy quark-auto-save

```bash
git clone https://github.com/Fanxiaoyao66/quark-tracker.git
cd quark-tracker
docker compose up -d            # starts quark-auto-save on :5005
```
Open `http://<nas-ip>:5005` (default `admin` / `admin123` — **change it**), and **log into your Quark account** (paste the cookie, or scan the QR if your version supports it).

> **fnOS note:** fnOS can *natively mount* Quark (it shows up under `/vol02/...` via an rclone-backed WebDAV gateway). **quark-tracker does NOT use that mount** — the gateway caches directory listings, so freshly-transferred files don't appear in time. We talk to the Quark API directly instead, which is always fresh and downloads at full speed.

### Step 2 — Install the Quark API helper into the container

`qsync_api.py` must run *inside* the quark-auto-save container (it reuses its Quark client + cookie). Copy it into the config volume:

```bash
cp src/qsync_api.py ./quark-config/qsync_api.py     # ./quark-config is mounted to /app/config
```

### Step 3 — Configure quark-tracker

```bash
sudo mkdir -p /opt/quark-tracker
sudo cp -r src /opt/quark-tracker/
sudo cp config.example.json /opt/quark-tracker/config.json
sudo nano /opt/quark-tracker/config.json
```
A minimal fnOS config:
```json
{
  "container": "quark-auto-save",
  "categories": {
    "动漫": { "quark_root": "/追剧/动漫", "library": "/vol1/1000/动漫" },
    "剧集": { "quark_root": "/追剧/剧集", "library": "/vol1/1000/剧集" }
  },
  "owner": "",
  "download": { "connections": 16 },
  "tmdb": { "api_key": "YOUR_TMDB_KEY", "language": "zh-CN", "enddate_buffer_days": 14 },
  "rename": { "enabled": true },
  "notify": [],
  "overrides_file": "overrides.json"
}
```
- `quark_root` is a folder **in your Quark account** where quark-auto-save will save each category (create `/追剧/动漫` etc. — it's auto-created on first transfer).
- `library` is the **local NAS folder** to download into.
- Scripts find the config via `$QUARK_TRACKER_CONFIG`, or `config.json` next to the repo, or `/opt/quark-tracker/config.json`.

### Step 4 — Add a show

In the quark-auto-save WebUI (`:5005`), create a task:
- **Share link**: paste the Quark share. For a multi-subtitle-group share, point it at one group's subfolder.
- **Save path**: `/追剧/动漫/<ShowName>/Season 0X` (anime) or `/追剧/剧集/<ShowName>/Season 0X` (TV). Use the **same `<ShowName>` as your existing library folder** so new episodes merge in.
- **Pattern**: e.g. `\.mkv$` to only grab video files.

### Step 5 — Test

```bash
# preview what WOULD download (no changes)
sudo QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json python3 /opt/quark-tracker/src/quark_sync.py --dry-run

# do it for real: download new eps + TMDB rename + notify
sudo QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json python3 /opt/quark-tracker/src/quark_sync.py
```
Result on disk:
```
/vol1/1000/动漫/<ShowName>/Season 01/<ShowName> - S01E01 - Episode Name.mkv
```

### Step 6 — Schedule it (cron)

quark-auto-save transfers on its own schedule (default 08/18/20). Run quark-tracker hourly to pick up and download whatever is new:
```cron
# sudo crontab -e   (runs as root)
17 * * * * QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json /usr/bin/flock -n /tmp/quark-tracker.sync.lock /usr/bin/python3 /opt/quark-tracker/src/quark_sync.py >> /opt/quark-tracker/cron.log 2>&1
```

### Step 7 — (Optional) "just send a link" agent mode

Let an LLM agent (e.g. OpenClaw, or any tool-runner) drive everything from a raw link. Grant the agent's user permission to run only the control script:
```sudoers
# /etc/sudoers.d/quark-tracker   (validate with: visudo -cf)
youragentuser ALL=(root) NOPASSWD: /opt/quark-tracker/src/quark_ctl.py
```
Point your agent at [`integrations/openclaw/SKILL.md`](integrations/openclaw/SKILL.md) (works as a template for any agent). Then you just send a Quark link; the agent fires one background `autodl` call and replies instantly, while seasons land in the library with their own notifications. The agent turn never blocks on the download.

---

## Configuration

| Key | Meaning |
|---|---|
| `container` | quark-auto-save container name |
| `categories.<label>.quark_root` | folder in your Quark account where quark-auto-save saves this category |
| `categories.<label>.library` | local directory to download this category into |
| `owner` | optional `user:group` to chown downloads (needs root); empty = leave as-is |
| `video_exts` | recognized video extensions |
| `download.connections` | aria2c parallel connections (default 16) |
| `tmdb.api_key` / `tmdb.bearer_token` / `tmdb.env_file` | TMDB credentials (any one) |
| `tmdb.language` | metadata language (e.g. `zh-CN`, `en-US`) |
| `tmdb.enddate_buffer_days` | days added after the finale for the monitoring end date |
| `rename.enabled` | run the TMDB renamer (false = keep raw `SxxExx` names) |
| `notify` | list of sinks (`command` / `webhook`); empty = no notifications |
| `overrides_file` | per-show `{ "<local folder>": {"tmdb_id": N, "query": "..."} }` for names TMDB can't find |

### Notifications

```json
"notify": [
  { "type": "webhook", "url": "https://sctapi.ftqq.com/<key>.send", "json_field": "text" },
  { "type": "command", "cmd": ["/path/to/push.sh", "--message", "{msg}"] }
]
```
`{msg}` is replaced with the message; a `command` with no `{msg}` placeholder receives the message on stdin.

## Agent CLI (quark_ctl.py)

| Subcommand | Purpose |
|---|---|
| `probe <share_url>` | share structure + parsed episodes (JSON) |
| `tmdb --query Q [--id N] [--season S] [--buffer 14]` | status / completed / air dates / `suggested_enddate` |
| `addtask --name N --url U --savepath P [--enddate D]` | create a quark-auto-save monitoring task |
| `deltask --name N` | remove a task |
| `override --name N --tmdb-id ID [--query Q]` | register a TMDB mapping for renaming |
| `transfer` | run quark-auto-save transfer now |
| `sync` | download new + TMDB rename + notify (shares a lock with the cron run) |
| `autodl --link U [--name N] [--category C]` | **the whole pipeline in one background call**: probe → per-season best release → TMDB judgement → verified transfer → sync → subs → movie |
| `subs --link U [--name N] [--category C]` | add external simplified-Chinese subs to an already-downloaded show (no video re-download) |

## How dedup & naming work

- Episode numbers are parsed from the share filename (handles `S2 - 10`, `[10]`, `第10话`, `EP10`, `S02E10`, …).
- An episode is downloaded only if no completed local file in `Season NN/` already contains its `SxxExx` tag.
- It is saved as `Show - SxxExx.ext`, then the TMDB renamer turns it into `Show - SxxExx - Episode Name.ext`.
- So an already-grabbed back-catalog (named with `SxxExx`) is left untouched; only genuinely new episodes are fetched.

## Notes & caveats

- **Cookie expiry** — the Quark cookie lasts weeks; when transfers start failing, re-login in the quark-auto-save WebUI.
- **fnOS native Quark mount** — not used (its WebDAV gateway caches); quark-tracker uses the Quark API directly.
- **Multi subtitle-group shares** — point the task at one group's subfolder for consistency; the agent mode does this automatically.
- **Name TMDB can't find** — add an `overrides` entry (or let the agent register one) so renaming still gets episode titles.
- This downloads other people's share content into your own account first, then to your disk — same pattern as quark-auto-save. Use responsibly and only for content you're entitled to.

## Credits

- Built on top of [**Cp0204/quark-auto-save**](https://github.com/Cp0204/quark-auto-save).
- Metadata from [**The Movie Database (TMDB)**](https://www.themoviedb.org/). This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

**AGPL-3.0** — see [LICENSE](LICENSE).

quark-tracker links [quark-auto-save](https://github.com/Cp0204/quark-auto-save) at runtime (`qsync_api.py` imports its `quark_auto_save` module), so it inherits the same AGPL-3.0 copyleft. Note AGPL §13: if you run a modified version as a network-accessible service, you must offer its users the corresponding source.
