# October 2026 pipeline update

The public pipeline now includes the download ledger, full-share audit, explicit
season protection, and recurring-episode matching used by the NAS deployment.

## Upgrade

1. Back up source, private config and state. Preserve the download ledger.
2. Update **all** files in `src/`, including `runtime.py`, `link_audit.py`, and
   `download_guard.py`. Copy `src/qsync_api.py` into the container config volume.
3. Existing configuration remains supported. Optional additions: `state_dir`,
   `download_policy_file`, `ai`, and `download.lowest_speed_limit`. Relative state
   paths resolve beside `config.json`. See `config.example.json`.
4. Update agent instructions, run `python3 -m unittest discover -s tests -v`, then
   your usual `--dry-run` sync before enabling scheduled downloads.

Existing transfer tasks are **not automatically rewritten**. A continuing task
whose regex enumerates complete old filenames cannot match future episodes.
Inspect its selected release, then vary only its episode-number portion; retain
show, release group, season and format restrictions. Do not clear the ledger or
restart a full download just to repair monitoring.

## Audit and season intent

```sh
python3 src/quark_ctl.py audit --link '<share>' --name 'Example Show' --season 2 --no-ai
python3 src/quark_ctl.py autodl --link '<share>' --name 'Example Show' --season 2
```

`audit` is read-only, including password-text probing. It scans up to 32 directory
levels / 10,000 directories and reports incomplete scans. Explicit file season
markers take precedence over TMDB season layout. Missing requested seasons,
unknown groups, specials and incomplete scans cannot silently become season 1.

`autodl` preserves season intent and reuses existing library aliases when
unambiguous. Specific-season requests do not process movies or other-season
subtitles. Without an explicit season it skips seasons already containing videos;
use an explicit season to fill a partial season. Sync still deduplicates episodes.
Completeness is claimed only when TMDB episode metadata supports it.

Recurring patterns vary recognized episode tokens while retaining the surrounding
release name. Transfer completion separately checks the original audited file
inventory: an unrelated old file cannot count as success. Unknown naming remains
exact and emits `tracking_exact_only`; re-audit when the source changes. Archive
password-text sidecars remain transferable.

## State and cancellation

`download_ledger.json` records **account-side Quark fids**, independently of local
names, including processed archives. Different fids remain different objects: this
is not content-hash deduplication. Existing libraries still use local-video checks;
historical ledger records are not fabricated. Sidecars/partial files do not count
as completed videos. Keep the ledger, caches, audit reports and policies private.

Copy `download_policy.example.json` to the configured private policy path to pause
a show or block specified seasons. Checks run during planning and before downloads.
Policy changes stop subsequent work, not an already-running aria2 transfer.

## Optional AI and reliability

AI is disabled by default. Configure `ai.command` as an executable argument array:
its wrapper reads UTF-8 `{prompt_file}` and emits a JSON object. `{session_id}` is
unique per call. No shell is used. Direct JSON and OpenClaw result envelopes are
supported. The temporary prompt is private; run the adapter as the same user.

Rules retain explicit season evidence. AI can resolve ambiguous groups, select a
same-episode release and check suspicious new-season numbering. Without usable AI,
ambiguous audit groups stay unassigned, duplicate releases use size fallback, and
season checks retain the claimed season. Relevant filenames are sent in full.

Slow downloads retry with fresh links, disabling the speed check on the final
attempt so a slow CDN can finish. Public single-connection fallback remains.
The helper sets 8s connect / 30s read timeouts for its client; it does not patch the
upstream scheduler or change host IPv6. Start notifications include approximate
size/time; completed downloads update speed history. CDN speed is not guaranteed.
