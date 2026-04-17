# Pocket Casts Ad-Free Pipeline

> A self-hosted ad remover that uses your [Pocket Casts](https://pocketcasts.com)
> account as the sync fabric. Built on top of
> [MinusPod](https://github.com/ttlequals0/MinusPod): downloads each episode,
> removes the ads with a local LLM, and puts the clean version back into your
> Pocket Casts Up Next queue on every device.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org)
[![Pocket Casts Plus required](https://img.shields.io/badge/requires-Pocket%20Casts%20Plus-f78166.svg)](https://pocketcasts.com/plus/)

> [!IMPORTANT]
> **This app requires an active [Pocket Casts Plus](https://pocketcasts.com/plus/)
> subscription.** The cleaned `.mp3` files are uploaded back to Pocket Casts as
> *custom files*, which is a Plus-only feature. The free tier will accept your
> login but reject the uploads.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [First-time setup](#first-time-setup)
- [Web UI](#web-ui)
- [CLI](#cli)
- [Configuration reference](#configuration-reference)
- [Architecture](#architecture)
- [MinusPod patches](#minuspod-patches)
- [Troubleshooting](#troubleshooting)
- [Logs](#logs)
- [Tests](#tests)
- [Contributing](#contributing)
- [License & credits](#license--credits)

---

## Why this exists

Premium podcast subscriptions cut ads, but only for a handful of shows you pay
for individually. This project sits between Pocket Casts and your podcasts and
strips ads from **everything you already subscribe to** — using your own GPU,
not someone else's cloud.

The hard parts (transcription, ad detection, audio surgery) come from
[MinusPod](https://github.com/ttlequals0/MinusPod). This repo adds:

- **Pocket Casts integration** — auth, episode listing, custom-file uploads,
  Up Next sync, played/archived state, auto-reconciliation of stale queues.
- **Local-first orchestration** — a Flask dashboard that drives MinusPod from a
  podcast-centric (not feed-centric) view.
- **Portable transcription backends** — use `whisper.cpp` natively on macOS
  (Metal), Linux (CUDA/CPU), or the vendor Docker image on anything else.
- **History & accounting** — every cleaned episode lands in a searchable,
  exportable history with time saved and ad counts.

## How it works

```
                   ┌─────────────────────────────────────────────┐
                   │  Pocket Casts (your subscriptions)          │
                   └───────────────┬────────────────┬────────────┘
                                   │                │
                       1. List     │                │ 6. Upload + sync
                                   ▼                │
                   ┌────────────────────────┐       │
                   │   Dashboard (Flask)    │       │
                   │      this repo         │───────┘
                   └────┬───────────────────┘
                        │ 2. Hand off feed
                        ▼
                   ┌────────────────────────┐
                   │       MinusPod         │
                   │  (port 8000, patched)  │
                   └────┬─────────┬─────────┘
                        │         │
                3. Whisper        4. LLM ad detection
                        │         │
                        ▼         ▼
                ┌──────────┐ ┌──────────┐
                │ whisper  │ │  Ollama  │
                │   .cpp   │ │  qwen3.5 │
                │  :8765   │ │ :11434   │
                └──────────┘ └──────────┘
                        │
                5. FFmpeg cuts the ads, re-embeds metadata
                        │
                        ▼
                  cleaned `.mp3`  →  uploaded to Pocket Casts
```

Whenever Pocket Casts already has its own AI transcript for an episode, the
pipeline uses that and skips Whisper entirely — usually saving 10–30 minutes
per episode.

## Quick start

> Already installed Ollama, whisper.cpp, and MinusPod? You're three commands
> away.

```bash
cp .env.example .env       # then edit and add your Pocket Casts credentials
./start_services.sh        # starts Ollama, Whisper (native Metal), MinusPod
source .env && python3 pocketcasts_adfree.py ui
# Open http://localhost:5050
```

You only need to restart `start_services.sh` when:
- the Ollama model changed,
- whisper.cpp was rebuilt or its model swapped,
- MinusPod source was patched.

Credentials, system prompts, and MinusPod settings hot-reload without a restart.

## First-time setup

### Prerequisites

Required everywhere:

- An active [Pocket Casts Plus](https://pocketcasts.com/plus/) subscription —
  the pipeline uploads cleaned files to Pocket Casts Cloud, which is a
  Plus-only feature.
- Python 3.10+
- `ffmpeg`
- [Ollama](https://ollama.com) (or any OpenAI-compatible LLM endpoint — see
  `OPENAI_BASE_URL` in [Configuration reference](#configuration-reference))
- 16 GB of RAM minimum; 32 GB+ recommended if you want to run the default
  35B-parameter ad-detection model locally. Smaller models (e.g. `llama3.1:8b`)
  work on lighter hardware.
- About 10 GB of disk for vendored models + transcripts.

Platform-specific notes:

| Platform | Transcription backend | Notes |
|----------|-----------------------|-------|
| macOS (Apple Silicon) | `whisper.cpp` native with `-DWHISPER_METAL=ON` | Fastest path; `scripts/setup_whisper.sh` handles it. |
| macOS (Intel) / Linux | `whisper.cpp` native (CPU or CUDA) | Same script; set `WHISPER_CUDA=1` before running if you have an NVIDIA GPU. |
| Windows / other | Docker image | Use the whisper.cpp server Docker container. The Services panel warns when Docker is in use because it's much slower on ARM/Apple. |

Install the toolchain (macOS example — substitute your OS's package manager):

```bash
brew install ffmpeg ollama cmake
```

### 1. Clone

```bash
git clone https://github.com/<your-fork>/pocket-casts-mod.git
cd pocket-casts-mod
```

### 2. Credentials

```bash
cp .env.example .env
$EDITOR .env   # add POCKETCASTS_EMAIL and POCKETCASTS_PASSWORD
```

### 3. Python env + dependencies

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 4. Vendored dependencies (MinusPod + whisper.cpp)

The `MinusPod/` and `whisper.cpp/` checkouts are deliberately **not**
committed; the helper scripts re-create them at known-good commits and apply
the local patches in `patches/`.

```bash
./scripts/setup_minuspod.sh    # clone, pin commit, apply patches/minuspod-local.patch
./scripts/setup_whisper.sh     # clone, build with WHISPER_METAL=ON, fetch model
```

### 5. LLM for ad detection

The pipeline asks an LLM to classify transcript windows. Out of the box it
expects Ollama with `qwen3.5:35b-a3b`; `start_services.sh` derives a tuned
variant named `qwen3.5-addetect` with a 16 K context. Any OpenAI-compatible
endpoint works — point `OPENAI_BASE_URL` at it and set `OPENAI_MODEL` to
whatever model you want.

```bash
# Default setup (local Ollama, requires ~24 GB VRAM/RAM)
brew services start ollama        # or: `systemctl --user start ollama`
ollama pull qwen3.5:35b-a3b

# Lighter alternative (works on ~8 GB RAM, less accurate)
ollama pull llama3.1:8b
echo 'OPENAI_MODEL=llama3.1:8b' >> .env
```

### 6. Launch

```bash
./start_services.sh
source .env && python3 pocketcasts_adfree.py ui
```

Open <http://localhost:5050>.

## Web UI

The dashboard at `http://localhost:5050` has two views:

### Dashboard

- **Stat cards** — Subscriptions, Eligible, Patreon (skipped), Processed
  Episodes. Click any card to filter the list below.
- **In Up Next** — every episode currently queued in Pocket Casts (including
  uploaded custom files), grouped by podcast. Custom files are inline and
  editable: rename, mark played/unplayed, remove from Up Next, delete. The
  dashboard also auto-reconciles stale originals: whenever an ad-free upload
  exists, the original episode is silently removed from Up Next and marked
  played.
- **All Podcasts** — every subscription. Expand a row to see episodes.
  - Episodes are tagged `unplayed` / `in progress` / `played` / `archived` /
    `processed`, with play status pulled directly from your Pocket Casts
    account.
  - Each episode has inline **Queue / Un-queue** and **Mark played /
    Mark unplayed** buttons.
  - Header checkbox (with `indeterminate` state) selects all eligible episodes
    for the podcast.
  - Per-podcast **Reset processed** button if you want to re-process older
    episodes.
- **Auto-refresh** — the list refreshes every ~20 seconds while the tab is
  visible and you haven't selected anything. No manual refresh button needed.
- **Toolbar** — Search, Process Selected, [Services](#services-panel),
  Clean up played Ad-Free files.
- **Floating log panel** — colored real-time log. Auto-expands on any new log
  entry so progress, Whisper/LLM messages, and errors are always visible;
  collapse manually from the header. Skip and Stop appear here when a job is
  running.

### History

Every processed episode, with timestamp, podcast, episode title, ads removed,
and time saved. Sortable, filterable, exportable as CSV.

### Services panel

Click **Services** in the toolbar.

Each row shows: status dot (healthy / running but unhealthy / down), backend
pill (`native` / `docker` / `brew`), pid, port, and a `docs` link that jumps
to the relevant section of this README. The footer has a model picker for
MinusPod ad detection.

| Service | Port | Managed via | Configured by |
|---------|------|-------------|---------------|
| [Ollama](#ollama--llm-provider) | 11434 | `brew services` (preferred) | `OPENAI_MODEL` env, `start_services.sh` |
| [Whisper](#whispercpp--transcription) | 8765 | Native binary or Docker | `scripts/setup_whisper.sh`, models in `whisper.cpp/models/` |
| [MinusPod](#minuspod-patches) | 8000 | Flask under `MinusPod/venv/` | `start_services.sh` (env vars) |
| [Pipeline UI](#web-ui) | 5050 | This repo | `pocketcasts_adfree.py ui` |

The panel won't let you stop the UI itself (it'd kill the panel that's
hosting it).

## CLI

```bash
source .env

# Test the pipeline end-to-end on a single feed
python3 pocketcasts_adfree.py test --rss-url 'https://feeds.simplecast.com/54nAGcIl'

# Process every feed registered in MinusPod
python3 pocketcasts_adfree.py auto

# Filter by podcast name (case-insensitive substring)
python3 pocketcasts_adfree.py auto --filter 'daily'

# Launch the dashboard
python3 pocketcasts_adfree.py ui
```

## Configuration reference

All configuration lives in `.env`. Copy `.env.example` to start, then override
only what you need.

### Required

| Variable | Purpose |
|----------|---------|
| `POCKETCASTS_EMAIL` | Pocket Casts account email. |
| `POCKETCASTS_PASSWORD` | Pocket Casts account password. |

### Ad-cut tuning (optional)

| Variable | Default | Effect |
|----------|---------|--------|
| `AD_START_PAD` | `1.5` | Seconds to extend each ad earlier. |
| `AD_END_PAD` | `2.0` | Seconds to extend each ad later. |
| `AD_END_PAD_TAIL` | `5.0` | Extra padding for ads that end at the very end of the file. Catches musical outros that Whisper truncates. |
| `TAIL_GAP_MIN_SECONDS` | `60` | If Whisper's transcript ends this many seconds before the audio file does, treat the gap as an untranscribed post-roll ad and cut it. |

### MinusPod runtime (optional)

| Variable | Default | Effect |
|----------|---------|--------|
| `OPENAI_MODEL` | `qwen3.5-addetect` | Ollama model used for ad classification. |
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | Where Ollama lives. |
| `WINDOW_SIZE_SECONDS` | `600` | Transcript window size handed to the LLM. |
| `WINDOW_OVERLAP_SECONDS` | `120` | Overlap between consecutive windows. |
| `AD_DETECTION_MAX_TOKENS` | `4096` | Token budget per LLM call. |
| `OLLAMA_NUM_PARALLEL` | `2` | Concurrent Ollama requests. Increase only if you have plenty of VRAM. |

## Architecture

| Component | Path | Port | Role |
|-----------|------|------|------|
| Pipeline orchestrator | `pocketcasts_adfree.py` | — | CLI + Pocket Casts API client + sync engine |
| Web server | `ui_server.py` | 5050 | Flask app exposing the dashboard and REST API |
| Service control plane | `services_manager.py` | — | Start/stop/restart/health for the four backends |
| Templates | `templates/` | — | `index.html`, `readme.html` |
| Static assets | `static/` | — | `css/app.css`, `js/app.js` |
| Tests | `tests.py` | — | `unittest`-based suite |
| MinusPod (vendored) | `MinusPod/` | 8000 | Ad detection + audio processing engine. **Re-cloned via `scripts/setup_minuspod.sh`.** |
| whisper.cpp (vendored) | `whisper.cpp/` | 8765 | Local Metal-accelerated ASR. **Re-cloned via `scripts/setup_whisper.sh`.** |
| Ollama | (system) | 11434 | Local LLM inference. |

### Ollama — LLM provider

Hosts the model that classifies transcript segments as ad / non-ad. Managed
via `brew services start ollama`; the dashboard's Services panel can also
start/stop/restart it. Model is selectable at runtime in the panel footer
(picks any model present in `ollama list`).

### whisper.cpp — transcription

Two backends supported:

- **Native (Metal, recommended)** — built by `scripts/setup_whisper.sh` with
  `-DWHISPER_METAL=ON`. Runs on the GPU, ~10× faster than Docker on Apple
  Silicon.
- **Docker** — provided as a fallback for non-macOS hosts. The Services panel
  warns when this path is in use.

Models live in `whisper.cpp/models/` (`ggml-large-v3-turbo.bin` is preferred
when present).

## MinusPod patches

Local modifications to MinusPod live as a single patch in
[`patches/minuspod-local.patch`](patches/minuspod-local.patch). The pinned
upstream commit is recorded in
[`patches/MINUSPOD_BASE.txt`](patches/MINUSPOD_BASE.txt). To re-apply from a
clean clone:

```bash
./scripts/setup_minuspod.sh
```

Summary of what's patched:

| File | Why we patch it |
|------|-----------------|
| `storage.py`, `database/__init__.py` | Honor `DATA_DIR` env var so we don't write to `/app/data`. |
| `llm_client.py` | Disable Ollama "thinking mode" (`reasoning_effort: none`) for faster responses. |
| `main_app/processing.py` | Wires in the new `detect_tail_gap` heuristic; honors `SKIP_VERIFICATION=true`. |
| `config.py` | Read `WINDOW_SIZE_SECONDS` / `WINDOW_OVERLAP_SECONDS` from the environment. |
| `roll_detector.py` | Tighter pre-roll regexes plus the new `detect_tail_gap` for outros Whisper failed to transcribe. Tunable via `TAIL_GAP_MIN_SECONDS`. |
| `audio_processor.py` | Pad ad boundaries 1.5 s before / 2 s after; tail-of-file ads get 5 s after (`AD_END_PAD_TAIL`). |

See [`patches/README.md`](patches/README.md) for line-by-line detail.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| `No module named httpx` | `source venv/bin/activate && pip install -r requirements.txt` |
| Upload fails with 403 / "subscription required" | Your Pocket Casts account is on the free tier. Custom-file upload is a [Plus](https://pocketcasts.com/plus/) feature. |
| `Could not find RSS for: [name]` | The pipeline resolves feeds via the iTunes Search API. Pass `--rss-url` directly or add the feed manually in MinusPod. |
| MinusPod "Circuit breaker OPEN" | The LLM endpoint failed repeatedly. Check `ollama list` (or your remote endpoint), then `./start_services.sh`. |
| Fans still spinning after a job | The pipeline auto-unloads Ollama. Force it: `curl -s -X POST http://localhost:11434/api/generate -H "Content-Type: application/json" -d '{"model":"<your-model>","keep_alive":"0s"}'` |
| Transcription much slower than expected | You're probably on the Docker whisper image. Switch to the native binary via the Services panel (Metal on macOS, CPU/CUDA elsewhere). |
| Ad still partially in outro | Increase `TAIL_GAP_MIN_SECONDS` (smaller threshold = more aggressive) or `AD_END_PAD_TAIL`. See `patches/README.md`. |
| Custom-file thumbnail stuck on the generic icon | Pocket Casts caches the colour fallback for ~1 minute after upload. The image does eventually render on every device — it's cosmetic only. |

## Logs

| Service | File |
|---------|------|
| MinusPod | `/tmp/minuspod.log` |
| whisper.cpp | `/tmp/whisper-server.log` |
| Pipeline UI | `/tmp/pocketcasts-ui.log` (and the floating log panel) |
| Ollama | `~/Library/Logs/Homebrew/ollama/ollama.log` |

The Services panel can tail any of these inline (`Log` button per row).

## Tests

```bash
source venv/bin/activate
python -m unittest tests -v
```

The suite covers artwork normalization, date validation, state management,
Patreon detection, transcript parsing, skip/stop semantics, upload ordering,
Up Next queue safety, Pocket Casts iOS-parity (`hasCustomImage` / `colour`),
RSS resolution, processed-podcast detection, the failed-episode abort path,
the `services_manager` helpers, and every `/api/*` endpoint.

## Contributing

PRs that make it more portable, improve ad-detection quality, or broaden
platform support are welcome.

When you change anything in `MinusPod/`, regenerate the patch:

```bash
cd MinusPod
git diff > ../patches/minuspod-local.patch
```

Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` for any user-facing
change.

## License & credits

[MIT](LICENSE).

Built on top of:
- [MinusPod](https://github.com/ttlequals0/MinusPod) by ttlequals0 — ad
  detection + audio processing engine.
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) by Georgi Gerganov —
  Metal-accelerated transcription.
- [Ollama](https://ollama.com/) — local LLM inference.

The unofficial Pocket Casts API client is reverse-engineered from public iOS
client traffic; use accordingly.
