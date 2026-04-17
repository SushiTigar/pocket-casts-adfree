# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
loosely tracks [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
