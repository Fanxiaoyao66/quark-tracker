---
name: quark-auto-tracker
description: When the user sends a Quark share link (pan.quark.cn/s/...) and wants to download / track a show, use this. Auto-detects the share contents, uses TMDB to judge whether the show has finished airing, downloads to the media library in Infuse/Plex-friendly naming, adds a monitoring task with an end date for still-airing shows, and notifies. Handles brand-new shows, a new season of an existing show, or a specific episode.
---

# Quark auto-tracker (agent skill)

When the user sends a Quark share link (`pan.quark.cn/s/`) and wants it downloaded / tracked,
drive the **quark-tracker** tool. Run fully automatically and report when done.

> Adjust the path below to your install location.

```
sudo /opt/quark-tracker/src/quark_ctl.py <subcommand> ...
```

Subcommands:
- `probe <url>` → `title_guess` + `folders[]` (each: `path/fid/video_count/episodes/ep_min/ep_max/sample`)
- `tmdb (--query "name" | --id N) [--season S] [--year Y] [--buffer 14]` → `status/completed/finale_air_date/next_episode_to_air/suggested_enddate/episodes[]`
- `addtask --name "Show" --url "<share>#/list/share/<fid>-x" --savepath "<quark_root>/Show/Season 0X" [--pattern "regex"] [--enddate YYYY-MM-DD]`
- `deltask --name "Show"`
- `override --name "<local folder name>" --tmdb-id N [--query "original title"]`
- `transfer` → transfer the share into your Quark account now
- `sync` → download new episodes + TMDB rename + notify

## Workflow

1. **Probe** the link. If a season has multiple subtitle-group subfolders, auto-pick the best
   (prefer soft-subbed / most complete `ep_max` / mkv). Note which you chose for the report.
2. **Identify on TMDB** with `tmdb` (reconcile the Chinese/fansub name → TMDB; use the original /
   romaji title or `--id` if a Chinese query fails). Decide the library category (e.g. anime vs TV).
3. **Pick the library folder name + season.** If the show already exists locally, reuse the exact
   existing folder name so new episodes merge in (check the library dir). savepath =
   `<category quark_root>/<Show>/Season 0X`. If TMDB can't find the Chinese name, register an
   `override` so renaming gets episode titles.
4. **Branch on `tmdb.completed`:**
   - **Finished** → download all, no monitoring: `addtask` (no enddate) → `transfer` → `sync` → `deltask`.
   - **Still airing** → download what's out + monitor with an end date:
     `addtask --enddate <suggested_enddate>` → `transfer` → `sync` (keep the task; it auto-stops at enddate).
   - **Specific episode** → set `--pattern` to match just that episode, then `transfer` → `sync` → `deltask`.
5. Build the subtitle-group sub-share URL as `<share_url>#/list/share/<folder_fid>-x`.
6. **Report:** show name, category, finished/airing, group chosen, episodes downloaded, monitoring +
   enddate (if any). `sync` sends the configured notifications automatically.

## Rules
- Downloading, renaming (`Show - SxxExx - Episode Name.ext`) and notifying are all done by `sync`;
  it skips episodes already in the library.
- The monitoring end date comes from `tmdb.suggested_enddate` (finale + buffer days).
- Don't invent episode numbers; verify the title/season against TMDB.
- These are other people's share links: only transfer into your own account, then download.
