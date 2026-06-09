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
strips ads from **everything you already subscribe to** — transcription runs
locally on your machine; ad detection can use local Ollama or a cloud LLM API.

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
                │ whisper  │ │   LLM    │
                │   .cpp   │ │ Ollama / │
                │  :8765   │ │   API    │
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

> Already installed whisper.cpp and MinusPod (and Ollama, if you use local LLM)?
> One command.

```bash
cp .env.example .env       # add Pocket Casts credentials + LLM settings
source .env && python3 pocketcasts_adfree.py ui
# Open http://localhost:5050
```

The UI auto-starts Whisper and MinusPod in the background on every launch.
**Ollama is started only when `LLM_PROVIDER=ollama`** (the default); if you
use a cloud LLM API instead, Ollama is skipped entirely. No separate
`./start_services.sh` step is required. MinusPod is also auto-updated from
upstream on each start.

## First-time setup

### Prerequisites

Required everywhere:

- An active [Pocket Casts Plus](https://pocketcasts.com/plus/) subscription —
  the pipeline uploads cleaned files to Pocket Casts Cloud, which is a
  Plus-only feature.
- Python 3.10+
- `ffmpeg`
- An **ad-detection LLM** — either:
  - **[Ollama](https://ollama.com)** running locally (default), or
  - A **cloud / remote API** (OpenRouter, OpenAI, Groq, Together, a self-hosted
    vLLM/LiteLLM endpoint, etc.) — see
    [Choosing an LLM backend](#5-choosing-an-llm-backend)
- 16 GB of RAM minimum for local Whisper transcription; **32 GB+ recommended**
  only if you also run a large local LLM (e.g. the default 35B Ollama model).
  With a cloud LLM, RAM pressure is much lower — you mainly need headroom for
  Whisper.
- About 10 GB of disk for vendored models + transcripts (Whisper weights; Ollama
  models are additional if you run locally).

Platform-specific notes:

| Platform | Transcription backend | Notes |
|----------|-----------------------|-------|
| macOS (Apple Silicon) | `whisper.cpp` native with `-DWHISPER_METAL=ON` | Fastest path; `scripts/setup_whisper.sh` handles it. |
| macOS (Intel) / Linux | `whisper.cpp` native (CPU or CUDA) | Same script; set `WHISPER_CUDA=1` before running if you have an NVIDIA GPU. |
| Windows / other | Docker image | Use the whisper.cpp server Docker container. The Services panel warns when Docker is in use because it's much slower on ARM/Apple. |

Install the toolchain (macOS example — substitute your OS's package manager):

```bash
brew install ffmpeg cmake
# Only if using local Ollama (LLM_PROVIDER=ollama, the default):
brew install ollama
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

### 5. Choosing an LLM backend

Ad detection classifies transcript windows with an LLM — one call per ~8 min
of audio, so a 4-hour episode is ~30 calls. Set `LLM_PROVIDER` in `.env` to
pick how MinusPod reaches that model.

| `LLM_PROVIDER` | When to use | Key variables |
|----------------|-------------|---------------|
| `ollama` *(default)* | Free, private, runs on your GPU/RAM | `OPENAI_MODEL`, optional `OPENAI_BASE_URL` |
| `openrouter` | One API key, 200+ models (Claude, GPT, DeepSeek, …) | `OPENROUTER_API_KEY`, `OPENAI_MODEL` |
| `openai-compatible` | **Any** OpenAI-compatible endpoint | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` |
| `anthropic` | Direct Claude API (no OpenAI shim) | `ANTHROPIC_API_KEY` |

When `LLM_PROVIDER` is anything other than `ollama`, the dashboard **does not
start Ollama** and hides it from the Services panel — only Whisper and
MinusPod need to run locally.

#### Option A — Local Ollama (default)

Out of the box the pipeline expects Ollama with `qwen3.5:35b-a3b`;
`start_services.sh` derives a tuned variant named `qwen3.5-addetect` with a
16 K context.

**Pick a model that fits your machine.** A too-large model is both slow (the
"45 min per episode" complaint) and a hard-crash risk on machines with less
than ~48 GB RAM (model + Whisper + KV cache + your other apps can panic the
kernel).

| Free RAM | Recommended model | Why |
|----------|-------------------|-----|
| ≥ 48 GB | `qwen3.5:35b-a3b` | Best accuracy. MoE so generation is fast. ~22 GB resident. |
| 24-48 GB | `qwen3:14b` | Solid accuracy, ~9 GB resident, ~2× the windows/min. **Default for ≤ 36 GB Macs.** |
| 8-24 GB | `llama3.1:8b` | Acceptable for short shows; misses the occasional native-read sponsor. ~5 GB. |

```bash
# .env — local Ollama (default; LLM_PROVIDER=ollama can be omitted)
export LLM_PROVIDER=ollama
export OPENAI_MODEL=qwen3:14b
export OPENAI_BASE_URL=http://localhost:11434/v1

ollama pull qwen3:14b
```

If you change the model after `start_services.sh` already created the
`qwen3.5-addetect` alias, set `OPENAI_MODEL` explicitly so MinusPod stops
asking for the alias.

#### Option B — Cloud / remote API

Use this when you don't want a multi-GB model resident on your machine.
Transcription still runs locally via whisper.cpp; only ad detection goes to
the API.

**OpenRouter** — one key, many models. Model IDs use the `provider/model`
form from [openrouter.ai/models](https://openrouter.ai/models):

```bash
# .env — OpenRouter
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-v1-your-key-here
export OPENAI_MODEL=deepseek/deepseek-v4-flash

# Optional: pin discounted infrastructure hosts (see the model's Providers tab
# on openrouter.ai for exact slugs). Without this, OpenRouter load-balances at
# blended pricing — often ~3× more than hosts like GMICloud or Novita.
export OPENROUTER_PROVIDER_ORDER=GMICloud,Novita,Alibaba
export OPENROUTER_ALLOW_FALLBACKS=false
# Or auto-pick cheapest: export OPENROUTER_PROVIDER_SORT=price
```

**Any other OpenAI-compatible API** — OpenAI, Groq, Together, Fireworks, a
local MLX/vLLM/LiteLLM proxy, etc. Set `LLM_PROVIDER=openai-compatible` and
point `OPENAI_BASE_URL` at the provider's `/v1` root:

```bash
# .env — OpenAI
export LLM_PROVIDER=openai-compatible
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=sk-your-openai-key
export OPENAI_MODEL=gpt-4o-mini

# .env — Groq (example)
export LLM_PROVIDER=openai-compatible
export OPENAI_BASE_URL=https://api.groq.com/openai/v1
export OPENAI_API_KEY=gsk_your-groq-key
export OPENAI_MODEL=llama-3.3-70b-versatile

# .env — self-hosted or custom proxy (example)
export LLM_PROVIDER=openai-compatible
export OPENAI_BASE_URL=http://localhost:8800/v1
export OPENAI_API_KEY=not-needed
export OPENAI_MODEL=qwen3:14b
```

Restart the UI (or MinusPod from the Services panel) after changing LLM
settings so MinusPod picks up the new environment.

### 6. Launch

```bash
source .env && python3 pocketcasts_adfree.py ui
```

Open <http://localhost:5050>. The UI starts Whisper and MinusPod automatically
(Ollama too, when `LLM_PROVIDER=ollama`) — watch the floating log panel for
progress.
First launch takes ~60 s for MinusPod to initialise; subsequent starts are
faster because services are already running.

> **Manual service control:** `./start_services.sh` is still available if you
> want to pre-warm services before launching the UI, or start them without the
> UI at all (e.g. CLI use). It also handles the `--mlx` flag for MLX-based
> LLM inference.

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
to the relevant section of this README. When `LLM_PROVIDER=ollama`, the footer
also has a model picker for MinusPod ad detection; with a cloud provider,
Ollama is hidden and the active LLM provider is shown instead.

| Service | Port | Managed via | Configured by |
|---------|------|-------------|---------------|
| [Ollama](#llm-backend-ollama-or-api) | 11434 | `brew services` (preferred); **skipped when using a cloud LLM** | `LLM_PROVIDER`, `OPENAI_MODEL` |
| [Whisper](#whispercpp--transcription) | 8765 | Native binary or Docker | `scripts/setup_whisper.sh`, models in `whisper.cpp/models/` |
| [MinusPod](#minuspod-patches) | 8000 | Flask under `MinusPod/venv/` — **auto-updated on every start** | `LLM_PROVIDER` and related vars in `.env` |
| [Pipeline UI](#web-ui) | 5050 | This repo | `python3 pocketcasts_adfree.py ui` |

The panel won't let you stop the UI itself (it'd kill the panel that's
hosting it).

## CLI

```bash
source .env && source venv/bin/activate

# Launch the dashboard (auto-starts all services)
python3 pocketcasts_adfree.py ui

# Test the pipeline end-to-end on a single feed (services must be running)
python3 pocketcasts_adfree.py test --rss-url 'https://feeds.simplecast.com/54nAGcIl'

# Process every feed registered in MinusPod
python3 pocketcasts_adfree.py auto

# Filter by podcast name (case-insensitive substring)
python3 pocketcasts_adfree.py auto --filter 'daily'
```

## Configuration reference

All configuration lives in `.env`. Copy `.env.example` to start, then override
only what you need.

### Required

| Variable | Purpose |
|----------|---------|
| `POCKETCASTS_EMAIL` | Pocket Casts account email. |
| `POCKETCASTS_PASSWORD` | Pocket Casts account password. |

### LLM backend

Set `LLM_PROVIDER` to choose how MinusPod runs ad detection. See
[Choosing an LLM backend](#5-choosing-an-llm-backend) for full examples.

| Variable | Default | Effect |
|----------|---------|--------|
| `LLM_PROVIDER` | `ollama` | `ollama` (local), `openrouter`, `openai-compatible`, or `anthropic`. Non-`ollama` values skip starting Ollama. |
| `OPENAI_MODEL` | `qwen3.5-addetect` | Model name / ID passed to MinusPod. For OpenRouter use `provider/model` slugs; for Ollama use `ollama list` names. |
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | Base URL for Ollama or `openai-compatible` providers (must end in `/v1`). Ignored for `openrouter` and `anthropic`. |
| `OPENAI_API_KEY` | `not-needed` | API key for `openai-compatible` endpoints. Set to your provider's key (OpenAI, Groq, Together, etc.). |
| `OPENROUTER_API_KEY` | — | Required when `LLM_PROVIDER=openrouter`. |
| `OPENROUTER_PROVIDER_ORDER` | — | Comma-separated OpenRouter host slugs to try in order (e.g. `GMICloud,Novita,Alibaba`). Pins discounted providers instead of blended pricing. |
| `OPENROUTER_ALLOW_FALLBACKS` | `false` when order is set | Whether OpenRouter may use hosts outside `OPENROUTER_PROVIDER_ORDER`. |
| `OPENROUTER_PROVIDER_SORT` | — | Auto-rank hosts: `price`, `throughput`, or `latency`. Alternative to an explicit order list. |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic`. |

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
| `WINDOW_SIZE_SECONDS` | `600` | Transcript window size handed to the LLM. |
| `WINDOW_OVERLAP_SECONDS` | `120` | Overlap between consecutive windows. |
| `AD_DETECTION_MAX_TOKENS` | `4096` | Token budget per LLM call. |
| `OLLAMA_NUM_PARALLEL` | `1` | *(Ollama only)* Concurrent requests. Each in-flight slot duplicates the KV cache. Increase only on machines with ≥48 GB free RAM. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | *(Ollama only)* How many models Ollama keeps resident. Bumping this silently doubles memory if MinusPod swaps detection ↔ verification ↔ chapters models. |
| `OLLAMA_KEEP_ALIVE` | `30s` | *(Ollama only)* How long Ollama keeps the model loaded after the last request. Short values quiet the fans between episodes; longer values save the ~30 s reload cost. |
| `EPISODE_MAX_WALLCLOCK_SECONDS` | `5400` (90 min) | Hard cap on a single episode. If exceeded the orchestrator gives up and moves to the next one so the queue stays unblocked. |
| `EPISODE_STALL_THRESHOLD_SECONDS` | `900` (15 min) | If MinusPod's stage doesn't change this long during transcription, restart `whisper-server`. Same threshold twice aborts the episode. |
| `EPISODE_STALL_THRESHOLD_LLM_SECONDS` | `2700` (45 min) | Higher cap during ad detection / verify / review — each LLM window can take many minutes on large models. |
| `LLM_TIMEOUT_LOCAL` | `1200` (20 min) | MinusPod per-window timeout for local Ollama (was 10 min; too tight for `qwen3.5-addetect`). Cloud APIs use MinusPod's shorter default. |

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
| Ollama | (system) | 11434 | Local LLM inference when `LLM_PROVIDER=ollama`. |
| Cloud LLM | (remote) | — | OpenRouter, OpenAI, Groq, Anthropic, or any `openai-compatible` endpoint when `LLM_PROVIDER` is set accordingly. |

### LLM backend — Ollama or API

MinusPod classifies transcript segments as ad / non-ad using whichever backend
`LLM_PROVIDER` selects:

- **`ollama`** — runs on your machine. Managed via `brew services start ollama`;
  the dashboard's Services panel can also start/stop/restart it. Model is
  selectable at runtime in the panel footer (picks any model in `ollama list`).
- **`openrouter`** — routes to any [OpenRouter model](https://openrouter.ai/models)
  via `OPENROUTER_API_KEY` + `OPENAI_MODEL`.
- **`openai-compatible`** — works with **any** provider that speaks the OpenAI
  Chat Completions API: OpenAI, Groq, Together, Fireworks, self-hosted vLLM,
  MLX proxies, etc. Set `OPENAI_BASE_URL` to the provider's `/v1` root and
  `OPENAI_API_KEY` to your key (`not-needed` for local proxies that don't
  require auth).
- **`anthropic`** — direct Claude API via `ANTHROPIC_API_KEY`.

Whisper transcription always runs locally (or via your configured whisper.cpp
backend); cloud LLM providers do not replace transcription.

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
| MinusPod "Circuit breaker OPEN" | The LLM endpoint failed repeatedly. With Ollama, check `ollama list`; with a cloud API, verify `LLM_PROVIDER`, API key, and `OPENAI_MODEL`. The UI auto-restarts MinusPod on next launch. |
| Fans still spinning after a job | *(Ollama only)* The pipeline auto-unloads Ollama. Force it: `curl -s -X POST http://localhost:11434/api/generate -H "Content-Type: application/json" -d '{"model":"<your-model>","keep_alive":"0s"}'` |
| Ollama missing from Services panel | Expected when `LLM_PROVIDER` is not `ollama`. The panel shows which cloud provider is active instead. |
| Cloud LLM works but transcription is slow | Normal — Whisper still runs locally. Cloud LLM only speeds up ad detection, not transcription. |
| `start_services.sh` not needed? | Correct — the UI auto-starts everything. `start_services.sh` is still useful for pre-warming services before the UI, or for `--mlx` mode. |
| MinusPod patch failed to apply after auto-update | A new upstream release changed a file our patch touches. Run `cd MinusPod && git diff > ../patches/minuspod-local.patch` to regenerate after manually resolving. |
| Stuck on "Starting transcription" for a long time | Normal for 2+ hour episodes (many 5–10 min Whisper chunks). Check `/tmp/minuspod.log` for `pass1:transcribing N/M` or `Chunk N complete`. If the log stops mid-chunk for 15+ min, restart Whisper via the Services panel. `start_services.sh` now uses 5-min chunks and skips loudnorm preprocessing. |
| Transcription much slower than expected | You're probably on the Docker whisper image. Switch to the native binary via the Services panel (Metal on macOS, CPU/CUDA elsewhere). |
| One episode takes 30+ minutes | A 4-hour show = ~30 LLM windows. With `qwen3.5:35b-a3b` that's ~30 × 1.5 min = 45 min. Switch to `qwen3:14b` (`echo 'OPENAI_MODEL=qwen3:14b' >> .env`) — same 30 windows, ~3 × faster. |
| Mac kernel panics or hard freezes during a job | The default model is ~22 GB resident. Combined with Whisper Metal buffers (~2 GB), browser, IDE, etc. it can OOM the GPU on a 36 GB machine. The dashboard now shows a memory warning before each job; heed it, switch to `qwen3:14b`, or set `OLLAMA_NUM_PARALLEL=1` (already the default). |
| Whisper crash with `kIOGPUCommandBufferCallbackErrorInnocentVictim` | Metal has a hard 8-command-buffer limit. The launcher now forces `--processors 1 --threads ≤8`; if you customised it, lower those numbers. |
| Aborted on `pass1:detecting:N/M` | Ad detection uses your configured **LLM** (Ollama or API), not Whisper. With local Ollama, large models (`qwen3.5-addetect`) can exceed 10 min per window — use `OPENAI_MODEL=qwen3:14b` on ≤36 GB Macs, or raise `LLM_TIMEOUT_LOCAL`. The stall watchdog restarts Ollama (not Whisper) for detecting stages and waits up to 45 min (`EPISODE_STALL_THRESHOLD_LLM_SECONDS`). With a cloud API, check rate limits and model availability. |
| Queue stalls on one episode forever | Wallclock cap 90 min (`EPISODE_MAX_WALLCLOCK_SECONDS`). Transcription stalls bounce whisper; LLM stalls bounce Ollama. See `EPISODE_STALL_THRESHOLD_*` above. |
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

This patch is automatically reapplied on every startup after a MinusPod
upstream pull, so your local changes survive auto-updates. If a new upstream
release conflicts with the patch, you'll see a warning in the startup log —
resolve manually and regenerate.

## License & credits

[MIT](LICENSE).

Built on top of:
- [MinusPod](https://github.com/ttlequals0/MinusPod) by ttlequals0 — ad
  detection + audio processing engine.
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) by Georgi Gerganov —
  Metal-accelerated transcription.
- [Ollama](https://ollama.com/) — optional local LLM inference (when
  `LLM_PROVIDER=ollama`).

The unofficial Pocket Casts API client is reverse-engineered from public iOS
client traffic; use accordingly.
