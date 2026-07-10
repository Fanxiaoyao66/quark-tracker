---
name: quark-auto-tracker
description: When the user sends a Quark share link (pan.quark.cn/s/...) and wants to download / track a show, use this. One background `autodl` call handles everything — season detection, best-release pick, TMDB finished-or-airing judgement, transfer, download, rename, notify — and the agent replies instantly instead of blocking its turn on a long download.
---

# Quark auto-tracker (agent skill)

When the user sends a Quark share link (`pan.quark.cn/s/`) and wants it downloaded / tracked,
drive the **quark-tracker** tool.

> Adjust the path below to your install location.

```
sudo /opt/quark-tracker/src/quark_ctl.py <subcommand> ...
```

## The one command you normally need

```
setsid sudo /opt/quark-tracker/src/quark_ctl.py autodl --link "<share_url>" \
    [--name "<Show>"] [--category "<label>"] \
    </dev/null >> /opt/quark-tracker/autodl.log 2>&1 &
```

Then **reply to the user immediately** ("started downloading X, you'll get a notification
per season when it lands") and **end your turn**. `autodl` runs the whole pipeline in the
background and sends its own notifications:

probe → detect seasons (multi-season packs are processed season by season) → pick the best
release per season (episode coverage → quality → size → usable simplified-Chinese subs) →
TMDB finished/airing judgement (finished: download once and delete the task; airing: keep a
monitoring task with enddate = finale + buffer) → verified transfer (retried) → download +
extract archives + TMDB rename + notify → external subtitles → theatrical movie (optional).

Rules:
- **Never** run `transfer`/`sync` synchronously inside your turn for a fresh download — a
  multi-GB download outlives an agent turn and the turn times out. That is exactly what
  `autodl` in the background is for.
- `--name` is optional (falls back to the catalog lookup, then the share's own title); pass
  it when the user already told you the proper show name. If the show exists locally, use
  the exact existing folder name so new episodes merge in.
- `--category` picks the target library from config `categories` (defaults to
  `default_category`).
- Password-protected archives (.exe/.rar/.7z/.zip) are handled automatically (password
  parsed from the share; variants tried with 7z).

## Adding subtitles to an already-downloaded show

When the user says a downloaded show has no (or wrong-language) subtitles:

```
setsid sudo /opt/quark-tracker/src/quark_ctl.py subs --link "<share_url>" \
    [--name "<Show>"] </dev/null >> /opt/quark-tracker/autodl.log 2>&1 &
```

Scans the whole share (including zipped subtitle packs) for external subs, prefers
simplified Chinese, pairs them by season/episode with the library files and names them
`<video name>.zh.ass` so media servers pick them up. Never overwrites existing subs, never
re-downloads video.

## Manual subcommands (diagnostics / special cases)

- `probe <url>` → `title_guess` + `folders[]` (each: `path/fid/kind/video_count/archive_count/episodes/ep_min/ep_max/total_size/sample`) + `passwords[]`
- `tmdb (--query "name" | --id N) [--season S] [--year Y] [--buffer 14]` → `status/completed/finale_air_date/next_episode_to_air/suggested_enddate/episodes[]`
- `addtask --name "Show" --url "<share>#/list/share/<fid>-x" --savepath "<quark_root>/Show/Season 0X" [--pattern "regex"] [--enddate YYYY-MM-DD]`
  (a past `--enddate` is clamped to today+3 — quark-auto-save silently skips expired tasks)
- `deltask --name "Show"` · `listtasks` · `override --name "<local folder>" --tmdb-id N`
- `transfer` (run quark-auto-save now) · `sync` (download + rename + notify; shares a lock
  with the cron sync so parallel runs can't corrupt each other's .part files)

Use these for a *specific single episode* (set `--pattern` to match just that file, then
`transfer` + `sync` + `deltask`) or when the user asks what is being tracked.

## Reporting

`autodl` notifies per season by itself. If the user asks for status later, check
`listtasks` and the tail of `autodl.log` / `quark_sync.log`.

These are other people's share links: only transfer into your own account, then download.
