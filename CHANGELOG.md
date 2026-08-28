# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
loosely tracks [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Auto-load `.env` at startup** — `pocketcasts_adfree.py` reads `.env` from
  the repo root when the UI or CLI starts, so `source .env` is no longer
  required before `python3 pocketcasts_adfree.py ui`. Secrets still come from
  `secrets.sh` / Passwords app / `secrets.ps1` only.
- **Verification pass on by default** — `SKIP_VERIFICATION_UNDER_SECONDS` now
  defaults to `0` in quick-setup docs and MinusPod env passthrough (always run
  the second LLM pass; catches mid-roll ads). Set `86400` to skip verification
  and save ~50% LLM cost.
- **Ad detection — LLM cost optimisations panel** — new "Ad detection"
  button in the dashboard toolbar opens a modal that surfaces three
  tunables from MinusPod's stage-tunables system:
  - **Large context window** (`largeWindowSeconds`, default 1200) —
    when the model ID matches a 1M-context pattern (deepseek-v4,
    gemini-2.5-flash, gemini-3-flash, gemini-flash, qwen-long,
    llama-4-, llama-3.1-405b) **and** the episode is longer than 2×
    the base window, the detector uses this larger window so a 60-min
    episode takes 3 windows instead of 6.
  - **Skip verification pass on short episodes**
    (`skipVerificationUnderSeconds`, default 1200) — pass 2 doubles
    LLM cost on every episode for near-zero yield on short ones. Set
    to 0 to disable.
  - **System-prompt caching** (`enablePromptCaching`, default on) —
    annotate the system prompt with OpenRouter's `cache_control:
    ephemeral` marker so the provider can cache it across the ~22
    windows of a long episode. Saves roughly 1.2K tokens per window
    at the cache-read rate (1/4.5× input). The `cached` count is
    logged per request for visibility.
  Backed by `GET /api/minuspod/settings` and
  `PUT /api/minuspod/stage-tunables` proxy endpoints. The PUT proxy
  merges the dashboard's three keys on top of the existing stage
  tunables so other tunables set in MinusPod's own UI (e.g.
  detectionTemperature) are preserved. New patch
  `patches/llm-cost-optimizations.patch` captures the upstream-side
  changes (config, prompts, ad_detector, llm_client, processing, API
  surface, tests) for replay after a MinusPod update.

### Fixed
- **500 from `/api/episodes/<uuid>` when MinusPod is down** — `list_feeds`
  and friends would propagate raw `httpx.ConnectError` out of the view,
  turning "MinusPod isn't running" into a useless 500. The endpoint now
  does an upfront `health()` check and returns a clean 503 with a hint
  ("start it from the Services panel"), with the individual calls also
  wrapped in try/except as defence in depth. Tests:
  `test_api_episodes_returns_503_when_minuspod_down`.
- **Gated Pocket Casts transcript reuse** — when Pocket Casts Plus has
  already transcribed an episode, the pipeline verifies alignment (coverage,
  proportional duration parity, multi-point fuzzy probes) and injects the VTT
  into MinusPod to skip the expensive Whisper pass. Duration delta scales with
  episode length (`max(10s, 3% of duration)` at default coverage). Validation
  harness: `scripts/validate_pc_transcripts.py` (drift-corrected ad overlap +
  simulated gate pass rate).
- **Misleading pre-populated MinusPod transcripts (partial VTT)** — an
  earlier build injected *partial* Pocket Casts WebVTT (~1–2 minutes) which
  caused the detector to delete cold opens. The new gate rejects partial
  transcripts via `PC_TRANSCRIPT_MIN_COVERAGE` (default 97%) before injection.
- **Cryptic "MinusPod is not reachable" from the job runner** — `_process_job`
  used to bail with a generic stack trace if MinusPod was down at job
  start. Now logs a clear "MinusPod is not reachable on startup" with
  a hint to start it from the Services panel.
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
- **Podcast cover art on the dashboard** — every podcast card now shows
  a 48×48 thumbnail sourced from iTunes' Search API (no RSS scraping
  required, no auth, no rate limits in practice), with a letter
  placeholder fallback. Results are cached on disk in
  `podcast_artwork_cache.json` so the dashboard doesn't re-hit iTunes
  on every load. The same artwork is also rendered in the *In Up Next*
  section so unsubscribed podcast covers stay visible there too.
  Broken images fall back to the letter placeholder via inline `onerror`
  rather than leaving a blank 48×48 box.
- **`.env.example` rewrite** — reorganised into REQUIRED / LLM BACKEND
  / OPTIONAL with a 3-step quick-start decision tree and a side-by-side
  comparison of the four LLM backends (Ollama, OpenRouter, OpenAI-
  compatible, Anthropic). Each optional variable now has a "Change if…"
  rationale comment.
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
- **UI declutter** — the top Services Quick Bar is now collapsible
  (chevron toggle, state persisted in `localStorage`) so it stops
  dominating the dashboard when you don't need to look at it. Stat
  cards tightened, history summary matched. All data still present,
  just behind progressive disclosure.
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
