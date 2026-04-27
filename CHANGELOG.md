# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
loosely tracks [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **"JSON.parse: unexpected character at line 1 column 1" in dashboard** —
  `/api/subscriptions` and `/api/files` returned Werkzeug's HTML 500 page
  whenever `PocketCastsClient.__init__` raised (most commonly an
  `httpx.HTTPStatusError` on a 401). The frontend's `resp.json()` then
  failed because the body started with `<!doctype html>`. Now:
  - `_login` parses Pocket Casts' JSON error envelope and raises a typed
    `PocketCastsAuthError(status_code, message_id, upstream_message)` so
    the caller knows whether it was `login_account_locked`,
    `login_wrong_password`, etc.
  - A Flask error handler converts `PocketCastsAuthError` into a JSON
    `502 {error, message, message_id, hint}` response.
  - `get_pc()` caches auth failures for 60s so the dashboard's 20s
    auto-refresh doesn't hammer `/user/login` and *extend* the lockout
    (which is what likely triggered the user's `login_account_locked`
    state in the first place).
  - The dashboard renders a dedicated red banner with the human-readable
    hint ("wait ~15 minutes", "fix POCKETCASTS_PASSWORD", etc.) instead
    of the cryptic JSON.parse error.
- **Runaway episode polls** — `download_processed_audio` previously polled
  MinusPod for `max_retries × retry_after` ≈ 8 hours when the backend got
  wedged. A single stuck episode could hold the whole queue hostage (the
  "second queued episode never uploaded" bug). Now bounded by a wallclock
  cap (`EPISODE_MAX_WALLCLOCK_SECONDS`, default 90 min) and a stall
  watchdog (`EPISODE_STALL_THRESHOLD_SECONDS`, default 15 min) that bounces
  whisper-server once and then aborts so the queue can move on.
- **Whisper Metal crashes** — `start_services.sh` and `services_manager.py`
  used to launch `whisper-server --processors $cores --threads $cores`,
  which exceeded Metal's hard 8-command-buffer ceiling on most Apple Silicon
  Macs and triggered `kIOGPUCommandBufferCallbackErrorInnocentVictim`
  panics. We now cap threads at 8 and force `--processors 1` (which is
  also required for correct token timestamps — whisper.cpp #2036).
- **OOM-induced kernel panics** — defaults reduced for systems with ≤ 36 GB
  RAM: `OLLAMA_NUM_PARALLEL=1` (was 2), `OLLAMA_MAX_LOADED_MODELS=1` (Ollama
  default is 3), and `OLLAMA_KEEP_ALIVE=30s` so models evict between
  episodes instead of clinging to ~22 GB of VRAM forever.

### Added
- **Memory preflight warning** — `/api/system/memory` (and the existing
  `/api/services` payload) now report total/available RAM and a
  human-readable warning when free memory dips below 8 GB. The job runner
  injects the same warning into the run log before processing starts so
  users see it before their machine swap-thrashes.
- **README "pick a model" guidance** — explicit table mapping free-RAM
  budget to recommended model. The default README pointed at
  `qwen3.5:35b-a3b` (~22 GB resident) without warning that on 36 GB Macs
  it leaves almost no headroom.
- **Up Next auto-reconcile** — `/api/subscriptions` now silently removes
  originals from Up Next and marks them played whenever their Ad-Free upload
  already exists, fixing the stale "Dec 31, 1969" leftovers users saw after
  interrupted runs.
- **Per-episode Queue / Un-queue / Mark played** controls on every episode in
  *All Podcasts*, backed by two new endpoints (`/api/pc_episode/<uuid>/up_next`
  and `/api/pc_episode/<uuid>/played`).
- **Rich Up Next rows** — regular podcast episodes in the *In Up Next* section
  now show the same metadata and actions as uploaded custom files (status
  pill, publish date, duration, Mark played / Un-queue). Backend enriches
  `/api/subscriptions` with `playingStatus`, `playedUpTo`, and `duration`
  from Pocket Casts' authenticated episode API, eliminating the stale
  "Loading metadata…" placeholder.
- **Pocket Casts play status surfaced in *All Podcasts*** — the dashboard
  merges the public podcast feed (for titles/UUIDs) with the authenticated
  episode status API (for playingStatus/isDeleted) so every row shows
  accurate `unplayed / in-progress / played / archived` state.
- **Dashboard auto-refresh** — subscriptions, files, and Up Next re-fetch
  every 20 s while the tab is visible and the user hasn't selected anything.
- **History page** — `/history` view in the dashboard listing every processed
  episode with timestamps, ad count, and time saved. CSV export included.
- **Per-podcast "Reset processed"** action inline with each expanded podcast,
  replacing the global reset modal.
- **README viewer** — `/readme` route renders `README.md` via the `markdown`
  package; each row in the Services panel now links to the relevant section.
- **Tail-gap ad heuristic** — MinusPod patch (`detect_tail_gap`) flags a
  synthetic post-roll when Whisper drops the last 60+ seconds of an episode,
  catching musical/silent outros that previously slipped through partially cut.
  Tunable via `TAIL_GAP_MIN_SECONDS`.
- **End-of-file post-roll padding** — `AD_END_PAD_TAIL` (default 5 s) gives
  the cutter extra runway on outros where the LLM truncates the ad early.
- **Patches workflow** — `patches/minuspod-local.patch` + `scripts/setup_minuspod.sh`
  / `scripts/setup_whisper.sh` reproduce the vendored `MinusPod/` and
  `whisper.cpp/` checkouts from upstream.
- **`.env.example`, `LICENSE` (MIT), `CHANGELOG.md`** for first public release.

### Changed
- **Processing Log auto-expands** whenever any new log line is emitted so
  progress, errors, and Whisper/LLM status are always visible.
- **PREMIUM chip** on Patreon rows now sits inline at the right edge instead
  of absolute-positioned, so it no longer covers the episode-count number.
- **History page** trimmed to Processed / Episode / Podcast / Ads / Time
  Saved; dropped the noisy Original size / New size / Saved / Pocket Casts
  columns and the misleading Disk Reclaimed stat card.
- **File row "Played" button** renamed to *Mark played* / *Mark unplayed* so
  the action verb is explicit.
- **README** rewritten for a public audience: calls out the Pocket Casts Plus
  requirement up front, broadens prerequisites beyond Apple Silicon, and
  documents the new auto-refresh + auto-reconcile behavior.
- **Dashboard layout** polished: tighter typography, stat cards as a responsive
  grid, header navigation between Dashboard / History.
- **Episode rows** now render Pocket Casts state directly: `unplayed`,
  `in-progress`, `played`, `archived`, or `processed`.
- **Custom Files section** merged into the *In Up Next* group, with full
  per-row edit controls (rename, delete, mark played/unplayed, remove from Up Next).
- **Services panel** shows each service's purpose, README anchor, and a
  next-action hint when a health check fails.
- **Podcast header checkbox** is now a real `<input type="checkbox">` with
  proper `indeterminate` support; clicking it selects/deselects all eligible
  episodes without expanding the row.
- `ui_server.py` split into `templates/index.html`, `templates/readme.html`,
  `static/css/app.css`, and `static/js/app.js`.

### Removed
- **Refresh button** in the toolbar (dashboard auto-refreshes now).
- **IMG NOT READY** pill on custom-file rows — Pocket Casts serves the image
  everywhere within a minute of upload; the pill was misleading.
- Global **Reset Processed** button (replaced by per-podcast action).
- **Fix stuck thumbnails** button (root-cause fixed; no longer needed).
- **MinusPod / Pocket Casts status pills** in the header (functionality lives
  in the Services panel).
- **Unplayed / Played tabs** and **Select unprocessed / none / latest 3 / latest 1**
  buttons from the per-podcast episode list.
- Throwaway exploratory scripts: `test_pc.py`, `test_pc2.py`, `test_pc3.py`,
  `test_pc4.py`, `test_transcript.py`.

### Fixed
- Header-row checkbox no longer triggers row expansion when clicked.
- Partial outro ads (e.g. `Voicemail Dump Truck — DRINK ME.mp3`) where Whisper
  failed to transcribe a long musical tail.
