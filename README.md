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
- [Quick setup: OpenRouter + DeepSeek V4 Flash](#quick-setup-openrouter--deepseek-v4-flash)
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

When Pocket Casts Plus has already transcribed an episode, the pipeline
fetches that VTT, verifies it against the audio (coverage, duration
parity, and multi-point fuzzy probes), and injects it into MinusPod to
**skip the Whisper pass** when verification passes. RSS publisher transcripts
are not injected by default (`PC_TRANSCRIPT_ALLOW_RSS=false`) since those
often come from ad-free master cuts. Set `DISABLE_TRANSCRIPT_SYNC=true` to
skip the entire path and always use local Whisper.

## Quick start

For a **full new install**, start with [First-time setup](#first-time-setup). For
**OpenRouter + DeepSeek V4 Flash** (no local Ollama), use the
[dedicated quick setup](#quick-setup-openrouter--deepseek-v4-flash) instead.

Already installed MinusPod, whisper.cpp, and credentials? Launch the UI:

**macOS / Linux**

```bash
# Step 1 — tunables in .env (not secrets)
cp .env.example .env

# Step 2 — secrets (one-time; see Credentials in First-time setup)
# macOS: Passwords app + secrets.sh | Windows: secrets.ps1 | Linux: plain secrets.sh

# Step 3 — launch (.env tunables are auto-loaded by pocketcasts_adfree.py)
source venv/bin/activate    # or: source .venv/bin/activate
source secrets.sh && python3 pocketcasts_adfree.py ui
# Open http://localhost:5050 (browser prompts for login if UI_AUTH_PASSWORD is set)
```

**Windows (PowerShell)** — see [Windows launch](#step-2b3-launch-the-ui-windows).

The UI auto-starts Whisper and MinusPod in the background on every launch.
**Ollama is started only when `LLM_PROVIDER=ollama`** (the default); if you
use a cloud LLM API instead, Ollama is skipped entirely. No separate
`./start_services.sh` step is required. MinusPod is also auto-updated from
upstream on each start, and the
[LLM cost-optimisation patch](#minuspod-patches) is re-applied on top.

## Quick setup: OpenRouter + DeepSeek V4 Flash

This matches the author's cloud setup: **local Whisper** for transcription,
**OpenRouter** for ad detection with `deepseek/deepseek-v4-flash-0731`, full-
transcript windows (up to 10 hr episodes in one LLM call), verification pass
enabled, and cheapest-host routing. No Ollama or GPU required beyond Whisper.

**You need:** Pocket Casts Plus, an [OpenRouter API key](https://openrouter.ai/keys),
Python 3.10+, `ffmpeg`, and vendored MinusPod + whisper.cpp (complete
[First-time setup](#first-time-setup) steps 1, 3, and 4 if you haven't already).

### Step 1 — Tunables in `.env`

```bash
cp .env.example .env
```

Paste this block into `.env` (secrets go in Passwords app / `secrets.ps1` — not here):

```bash
export DISABLE_TRANSCRIPT_SYNC=true
export LLM_PROVIDER=openrouter
export OPENAI_MODEL=deepseek/deepseek-v4-flash-0731
export OPENROUTER_PROVIDER_SORT=price

# Full transcript in one window (up to ~1 hr); 1M context on this model
export LARGE_WINDOW_SECONDS=3600
# Output budget for the ad list JSON. 8192 truncates on ad-heavy ~1 hr episodes
# (log: "hit max_tokens" / "empty completion"); 16384 is the safe default here.
export AD_DETECTION_MAX_TOKENS=16384
export LARGE_WINDOW_MIN_SECONDS=300
export LARGE_WINDOW_MAX_SECONDS=36000
# Always run verification (catches mid-roll ads; ~2× LLM cost).
# Set 86400 to skip verification on episodes under 24 h (cheaper; may miss mid-rolls).
export SKIP_VERIFICATION_UNDER_SECONDS=0
export ENABLE_PROMPT_CACHING=true
```

Use `deepseek/deepseek-v4-flash-0731` (not the older `deepseek/deepseek-v4-flash`
slug). `OPENROUTER_PROVIDER_SORT=price` auto-picks the cheapest host; to pin hosts
instead, set `OPENROUTER_PROVIDER_ORDER=DeepInfra,StreamLake,GMICloud` and
`OPENROUTER_ALLOW_FALLBACKS=false`.

**Rough cost:** about **$0.01–0.02 per episode** on OpenRouter with verification
(~2 LLM passes). Set `SKIP_VERIFICATION_UNDER_SECONDS=86400` to skip verification
and roughly halve cost (may leave mid-roll ads in place).

### Step 2 — Secrets (pick your OS)

| OS | Where secrets live | Instructions |
|----|-------------------|--------------|
| **macOS** | Passwords app + `secrets.sh` | [Step 2a](#step-2a-macos-passwords-app--secretssh) below |
| **Windows** | `secrets.ps1` | [Step 2b](#step-2b-windows-secretsps1) in First-time setup |
| **Linux** | `secrets.sh` (plain exports) | [Step 2c](#step-2c-linux-secretssh) in First-time setup |

### Step 3 — Launch

**macOS / Linux:**

```bash
source venv/bin/activate   # or: source .venv/bin/activate
source secrets.sh && python3 pocketcasts_adfree.py ui
```

`.env` is read automatically at startup; you only need `source secrets.sh` for
Pocket Casts credentials and API keys.

**Windows (PowerShell):**

```powershell
.\venv\Scripts\Activate.ps1
. .\secrets.ps1
python pocketcasts_adfree.py ui
```

(Optional: `Load-DotEnv .env` before other shell commands — see
[Step 2b.2](#step-2b2-add-the-load-dotenv-helper). Python loads `.env` on its own.)

Open `http://localhost:5050`. Log in with `admin` and your `UI_AUTH_PASSWORD`.

## First-time setup

Follow these steps **in order** on a fresh machine. If you only want OpenRouter +
DeepSeek V4 Flash, you can skip step 5 and use
[Quick setup: OpenRouter](#quick-setup-openrouter--deepseek-v4-flash) for the LLM
`.env` block instead.

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
    [Choosing an LLM backend](#step-5--choosing-an-llm-backend)
- 16 GB of RAM minimum for local Whisper transcription; **32 GB+ recommended**
  only if you also run a large local LLM (e.g. the default 35B Ollama model).
  With a cloud LLM, RAM pressure is much lower — you mainly need headroom for
  Whisper.
- About 10 GB of disk for vendored models + transcripts (Whisper weights; Ollama
  models are additional if you run locally).

#### Platform-specific notes

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

### Step 1 — Clone the repo

No fork required — clone this repository directly:

```bash
git clone https://github.com/SushiTigar/pocket-casts-adfree.git
cd pocket-casts-adfree
```

Want to send pull requests? Fork on GitHub first, then clone your fork instead.

### Step 2 — Credentials

**Secrets** (Pocket Casts login, API keys, dashboard password) must **not** be
committed. **Tunables** (LLM provider, window sizes, cost optimizations) live in
`.env`. Platform-specific secret storage:

| Platform | Secret storage | Launch helper |
|----------|----------------|---------------|
| macOS | Passwords app + `secrets.sh` | `source secrets.sh` (`.env` auto-loaded by Python) |
| Windows | `secrets.ps1` | `. .\secrets.ps1` then `python pocketcasts_adfree.py ui` |
| Linux | `secrets.sh` (plain exports) | `source secrets.sh` (`.env` auto-loaded by Python) |

`secrets.sh`, `secrets.ps1`, and `.env` are gitignored — never commit them.

#### Step 2a — macOS: Passwords app + `secrets.sh`

Credentials live in the **Passwords** app (iCloud Keychain sync). The project
reads **Internet password** items — the same entries you see in Passwords.
Legacy generic Keychain entries from older setups still work as a fallback.

**Step 2a.1 — Add logins (pick one method)**

**Option A — Passwords app (recommended)**

Open **Passwords** (Spotlight → “Passwords”) and add three logins:

| Website | Username | Password |
|---------|----------|----------|
| `http://localhost:5050` | `admin` (or your `UI_AUTH_USER`) | Dashboard login — generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'` |
| `https://pocketcasts.com` | Your Pocket Casts email | Your Pocket Casts password |
| `https://openrouter.ai` | `api-key` | Your OpenRouter key (`sk-or-v1-…`) — only if using OpenRouter |

Use title **Pocket Casts Ad-Free UI** for the localhost entry so it is easy to find.

**Option B — Terminal (`security add-internet-password`)**

```bash
UI_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

security add-internet-password -a admin -s localhost -P 5050 -r http \
  -l "Pocket Casts Ad-Free UI" -w "$UI_PASS" -U

security add-internet-password -a "you@example.com" -s pocketcasts.com -r htps \
  -l "Pocket Casts" -w "your-pocket-casts-password" -U

# Only if using OpenRouter (LLM_PROVIDER=openrouter):
security add-internet-password -a api-key -s openrouter.ai -r htps \
  -l "OpenRouter (Pocket Casts pipeline)" -w "sk-or-v1-your-key-here" -U
```

Retrieve or update later:

```bash
# Dashboard password — or search "Pocket Casts Ad-Free UI" in Passwords
security find-internet-password -s localhost -a admin -P 5050 -w

security add-internet-password -a admin -s localhost -P 5050 -r http \
  -l "Pocket Casts Ad-Free UI" -w "NEW_PASSWORD" -U
```

**Migrating from generic Keychain entries**

If you previously used `add-generic-password` (those items do **not** appear in
Passwords), copy them into Internet-password entries once:

```bash
if security find-generic-password -s ui-auth-password -w >/dev/null 2>&1; then
  security add-internet-password -a admin -s localhost -P 5050 -r http \
    -l "Pocket Casts Ad-Free UI" \
    -w "$(security find-generic-password -s ui-auth-password -w)" -U
fi
if security find-generic-password -s pocketcasts-email -w >/dev/null 2>&1; then
  EMAIL="$(security find-generic-password -s pocketcasts-email -w)"
  PASS="$(security find-generic-password -s pocketcasts-password -w)"
  security add-internet-password -a "$EMAIL" -s pocketcasts.com -r htps \
    -l "Pocket Casts" -w "$PASS" -U
fi
if security find-generic-password -s openrouter-api-key -w >/dev/null 2>&1; then
  security add-internet-password -a api-key -s openrouter.ai -r htps \
    -l "OpenRouter (Pocket Casts pipeline)" \
    -w "$(security find-generic-password -s openrouter-api-key -w)" -U
fi
```

**Step 2a.2 — Create `secrets.sh` (loads Passwords app / Keychain into the shell)**

`secrets.sh` is not committed. Create it in the repo root:

```bash
cat > secrets.sh <<'EOF'
#!/usr/bin/env bash
_ACCOUNT="$(id -un)"
_UI_USER="${UI_AUTH_USER:-admin}"
_kc_internet_pass() { security find-internet-password -s "$1" -a "$2" -w 2>/dev/null || true; }
_kc_internet_pass_server() { security find-internet-password -s "$1" -w 2>/dev/null || true; }
_kc_internet_pass_port() { security find-internet-password -s "$1" -a "$2" -P "$3" -w 2>/dev/null || true; }
_kc_internet_acct() { security find-internet-password -s "$1" 2>/dev/null | awk -F'"' '/"acct"/ { print $4; exit }'; }
_kc_generic() { security find-generic-password -a "$_ACCOUNT" -s "$1" -w 2>/dev/null || true; }
export POCKETCASTS_EMAIL="$(_kc_internet_acct pocketcasts.com)"
[ -z "$POCKETCASTS_EMAIL" ] && export POCKETCASTS_EMAIL="$(_kc_generic pocketcasts-email)"
export POCKETCASTS_PASSWORD="$(_kc_internet_pass_server pocketcasts.com)"
[ -z "$POCKETCASTS_PASSWORD" ] && export POCKETCASTS_PASSWORD="$(_kc_generic pocketcasts-password)"
export OPENROUTER_API_KEY="$(_kc_internet_pass openrouter.ai api-key)"
[ -z "$OPENROUTER_API_KEY" ] && export OPENROUTER_API_KEY="$(_kc_generic openrouter-api-key)"
export UI_AUTH_PASSWORD="$(_kc_internet_pass_port localhost "$_UI_USER" 5050)"
[ -z "$UI_AUTH_PASSWORD" ] && export UI_AUTH_PASSWORD="$(_kc_generic ui-auth-password)"
EOF
chmod +x secrets.sh
```

The UI also reads Passwords app / Keychain automatically on startup if env vars
are unset, but `source secrets.sh` is still recommended so shell scripts and
`start_services.sh` see the same values.

#### Step 2b — Windows: `secrets.ps1`

**Step 2b.1 — Create `secrets.ps1`**

```powershell
# secrets.ps1 — NOT committed
$env:POCKETCASTS_EMAIL = "you@example.com"
$env:POCKETCASTS_PASSWORD = "your-pocket-casts-password"
$env:OPENROUTER_API_KEY = "sk-or-v1-your-key-here"   # if using OpenRouter
# Dashboard login (recommended). Generate: python -c "import secrets; print(secrets.token_urlsafe(24))"
$env:UI_AUTH_PASSWORD = "paste-generated-password-here"
# Optional: $env:UI_AUTH_USER = "admin"
```

**Step 2b.2 — Add the `Load-DotEnv` helper**

Paste into your PowerShell session before launching (or add to your profile):

```powershell
function Load-DotEnv($path) {
  Get-Content $path | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    if ($_ -match '^\s*export\s+([^=]+)=(.*)$') {
      $name = $matches[1].Trim()
      $val = $matches[2].Trim()
      if ($val.Length -ge 2 -and $val[0] -eq $val[-1] -and ($val[0] -eq "'" -or $val[0] -eq '"')) {
        $val = $val.Substring(1, $val.Length - 2)
      }
      Set-Item -Path "env:$name" -Value $val
    }
  }
}
```

**Step 2b.3 — Launch the UI (Windows)**

```powershell
cd pocket-casts-adfree
.\venv\Scripts\Activate.ps1
. .\secrets.ps1
python pocketcasts_adfree.py ui
```

`pocketcasts_adfree.py` loads `.env` from the repo root automatically.
`Load-DotEnv` (below) is only needed if you want tunables in your PowerShell
session before starting Python.

On Windows, Whisper usually runs via **Docker** (see
[Platform-specific notes](#platform-specific-notes)); use the Services panel to
start the Docker whisper backend if a native build is unavailable.

#### Step 2c — Linux: `secrets.sh`

Put plain exports in `secrets.sh` (no Keychain):

```bash
cat > secrets.sh <<'EOF'
#!/usr/bin/env bash
export POCKETCASTS_EMAIL="you@example.com"
export POCKETCASTS_PASSWORD="your-pocket-casts-password"
export OPENROUTER_API_KEY="sk-or-v1-your-key-here"   # if using OpenRouter
export UI_AUTH_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
EOF
chmod +x secrets.sh
```

#### Step 2d — LLM tunables in `.env` (all platforms)

```bash
cp .env.example .env
$EDITOR .env   # LLM_PROVIDER, OPENAI_MODEL, LARGE_WINDOW_SECONDS, etc.
```

Do **not** put `POCKETCASTS_EMAIL`, `POCKETCASTS_PASSWORD`, `OPENROUTER_API_KEY`,
or `UI_AUTH_PASSWORD` in `.env`.

### Step 3 — Python env + dependencies

```bash
python3 -m venv venv && source venv/bin/activate   # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Step 4 — Vendored dependencies (MinusPod + whisper.cpp)

The `MinusPod/` and `whisper.cpp/` checkouts are deliberately **not**
committed; the helper scripts re-create them at known-good commits and apply
the local patches in `patches/`.

```bash
./scripts/setup_minuspod.sh    # clone, pin commit, apply patches/minuspod-local.patch
./scripts/setup_whisper.sh     # clone, build with WHISPER_METAL=ON, fetch model
```

On Windows, use Docker for Whisper (see [Platform-specific notes](#platform-specific-notes)).

### Step 5 — Choosing an LLM backend

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

**OpenRouter** — one key, many models. For the author's tuned setup (`deepseek/
deepseek-v4-flash-0731`, full-transcript windowing), use
[Quick setup: OpenRouter](#quick-setup-openrouter--deepseek-v4-flash) instead of
the shorter example below.

Model IDs use the `provider/model` form from [openrouter.ai/models](https://openrouter.ai/models).
Store `OPENROUTER_API_KEY` in Passwords app / `secrets.ps1` / `secrets.sh` — not in
`.env`. OpenRouter honours `cache_control` for prompt caching when
`ENABLE_PROMPT_CACHING=true`.

```bash
# macOS — store API key in Passwords app (see Step 2a.1)
security add-internet-password -a api-key -s openrouter.ai -r htps \
  -l "OpenRouter (Pocket Casts pipeline)" -w "sk-or-v1-your-key-here" -U

# .env — OpenRouter tunables only
export LLM_PROVIDER=openrouter
export OPENAI_MODEL=deepseek/deepseek-v4-flash-0731

# Cost optimizations for OpenRouter (works on any cloud provider):
export ENABLE_PROMPT_CACHING=true         # OpenRouter honours cache_control markers
export LARGE_WINDOW_SECONDS=3600          # 1hr full-transcript window
export SKIP_VERIFICATION_UNDER_SECONDS=0  # always verify (use 86400 to skip & save ~50% LLM cost)

# Optional: pin discounted infrastructure hosts (see the model's Providers tab
# on openrouter.ai for exact slugs). Without this, OpenRouter load-balances at
# blended pricing — often ~3× more than hosts like GMICloud or Novita.
export OPENROUTER_PROVIDER_ORDER=GMICloud,Novita,Alibaba
export OPENROUTER_ALLOW_FALLBACKS=false
# Or auto-pick cheapest: export OPENROUTER_PROVIDER_SORT=price
```

**Any other OpenAI-compatible API** — OpenAI, DeepSeek direct, Groq, Together,
etc. Set `LLM_PROVIDER=openai-compatible` and put API keys in Passwords app /
`secrets.ps1` (or add a custom entry to `secrets.sh`), not in committed files.
Point `OPENAI_BASE_URL` at the provider's `/v1` root:

```bash
# API keys below are shown for illustration — store them in Passwords app /
# secrets.ps1 / secrets.sh in production, not in .env.

# .env — DeepSeek (cheapest direct, no markup)
export LLM_PROVIDER=openai-compatible
export OPENAI_BASE_URL=https://api.deepseek.com
export OPENAI_API_KEY=sk-your-deepseek-key
export OPENAI_MODEL=deepseek-v4-flash

# Cost optimizations for DeepSeek:
# DeepSeek caches automatically (prefix-matching); cache_control is a no-op.
# The code auto-detects the provider and skips annotations automatically.
# Full-transcript windowing. Default 0 always runs verification (~2 LLM calls).
# Set SKIP_VERIFICATION_UNDER_SECONDS=86400 to skip verification (~1 LLM call; may miss mid-rolls).
export LARGE_WINDOW_SECONDS=3600          # 1hr full-transcript window
export SKIP_VERIFICATION_UNDER_SECONDS=0

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

#### Switching between providers

The code auto-detects your provider and adapts caching behaviour:

| Provider | Caching mechanism | What you need |
|---|---|---|
| **DeepSeek** | Automatic prefix-based. No annotation needed — the code skips `cache_control` automatically. | `LARGE_WINDOW_SECONDS=3600`, `SKIP_VERIFICATION_UNDER_SECONDS=0` (or `86400` to skip verification and save ~50% LLM cost) |
| **OpenRouter** | `cache_control: ephemeral` annotations. The code sends them when `ENABLE_PROMPT_CACHING=true`. | Same cost tunables as DeepSeek plus `ENABLE_PROMPT_CACHING=true` |
| **Anthropic** | `cache_control: ephemeral` annotations (prompt caching). | `ANTHROPIC_API_KEY`, `ENABLE_PROMPT_CACHING=true` |
| **Ollama** | No caching. Annotations are skipped automatically. | Local model, free |

To switch from DeepSeek back to OpenRouter, just change `LLM_PROVIDER`,
`OPENAI_BASE_URL`, and `OPENAI_API_KEY` — `LARGE_WINDOW_SECONDS` and
`SKIP_VERIFICATION_UNDER_SECONDS` apply to all providers and don't need
changing.

### Step 6 — Launch the UI

**macOS / Linux:**

```bash
source venv/bin/activate
source secrets.sh && python3 pocketcasts_adfree.py ui
```

**Windows:** [Step 2b.3](#step-2b3-launch-the-ui-windows).

Open <http://localhost:5050>. If `UI_AUTH_PASSWORD` is set, the browser prompts
for HTTP Basic Auth (default username `admin`, or `UI_AUTH_USER`). The UI starts
Whisper and MinusPod automatically
(Ollama too, when `LLM_PROVIDER=ollama`) — watch the floating log panel for
progress.
First launch takes ~60 s for MinusPod to initialise; subsequent starts are
faster because services are already running.

> **Manual service control:** `./start_services.sh` is still available if you
> want to pre-warm services before launching the UI, or start them without the
> UI at all (e.g. CLI use). It sources `secrets.sh` and `.env` for its own
> shell session. `pocketcasts_adfree.py` loads `.env` automatically when you
> run the UI or CLI either way.
> It also handles the `--mlx` flag for MLX-based LLM inference.

## Web UI

The dashboard at `http://localhost:5050` has two views.

### Dashboard login

When `UI_AUTH_PASSWORD` is set (recommended), every page and API call requires
HTTP Basic Auth. Default username is `admin` (`UI_AUTH_USER` overrides it).
Password is stored in the Passwords app on macOS (`localhost:5050`), or in
`secrets.ps1` on Windows / `secrets.sh` on Linux. The browser caches
credentials after the first successful login, so the existing
`fetch()` calls in the UI work without changes.

From another device on your home Wi‑Fi, use `http://<your-mac-lan-ip>:5050`
(find the IP with `ipconfig getifaddr en0`). The Mac must be awake and on the
same network. For access away from home, use a private mesh VPN such as
[Tailscale](https://tailscale.com) on the Mac and your phone, then open
`http://<tailscale-ip>:5050`.

The UI binds to all interfaces (`0.0.0.0`) so LAN and Tailscale access work;
do not expose port 5050 directly to the public internet without TLS.

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
| [Ollama](#llm-backend--ollama-or-api) | 11434 | `brew services` (preferred); **skipped when using a cloud LLM** | `LLM_PROVIDER`, `OPENAI_MODEL` |
| [Whisper](#whispercpp--transcription) | 8765 | Native binary or Docker | `scripts/setup_whisper.sh`, models in `whisper.cpp/models/` |
| [MinusPod](#minuspod-patches) | 8000 | Flask under `MinusPod/venv/` — **auto-updated on every start** | `LLM_PROVIDER` and related vars in `.env` |
| [Pipeline UI](#web-ui) | 5050 | This repo | `python3 pocketcasts_adfree.py ui` |

The panel won't let you stop the UI itself (it'd kill the panel that's
hosting it).

## CLI

**macOS / Linux:**

```bash
source secrets.sh && source venv/bin/activate

# Launch the dashboard (auto-starts all services; .env loaded automatically)
python3 pocketcasts_adfree.py ui

# Test the pipeline end-to-end on a single feed (services must be running)
python3 pocketcasts_adfree.py test --rss-url 'https://feeds.simplecast.com/54nAGcIl'

# Process every feed registered in MinusPod
python3 pocketcasts_adfree.py auto

# Filter by podcast name (case-insensitive substring)
python3 pocketcasts_adfree.py auto --filter 'daily'
```

**Windows:** load secrets with `. .\secrets.ps1`, then run the same
`python pocketcasts_adfree.py …` commands (`.env` is loaded automatically).

## Configuration reference

Configuration is split between **secrets** (`secrets.sh` / Passwords app on macOS,
`secrets.ps1` on Windows) and **`.env` tunables**. Copy `.env.example` to
`.env` for tunables only.

**`.env` loading:** `pocketcasts_adfree.py` reads `.env` from the repo root at
startup (UI and CLI). You do **not** need `source .env` before launch. Secrets
are **not** read from `.env` automatically — use `secrets.sh` / Passwords app /
`secrets.ps1`. Keep passwords and API keys out of `.env`. Restart the UI after
editing `.env` so the process picks up changes (MinusPod alone re-reads `.env`
when you click **Restart MinusPod** in the Services panel).

### Secrets (`secrets.sh` / `secrets.ps1`)

| Storage (macOS Passwords app) | Env var | Purpose |
|-------------------------------|---------|---------|
| `pocketcasts.com` — username = email | `POCKETCASTS_EMAIL` | Pocket Casts account email. **Required.** |
| `pocketcasts.com` — password | `POCKETCASTS_PASSWORD` | Pocket Casts account password. **Required.** |
| `openrouter.ai` — username `api-key` | `OPENROUTER_API_KEY` | Required when `LLM_PROVIDER=openrouter`. |
| `localhost:5050` — username `admin` | `UI_AUTH_PASSWORD` | Dashboard HTTP Basic Auth password. **Recommended** (UI is reachable on your LAN). |

Legacy generic Keychain services (`pocketcasts-email`, `ui-auth-password`, etc.)
are still read if the Passwords-app entries are missing.

On **Windows**, set the same env vars in `secrets.ps1` instead of Keychain.
On **Linux**, use plain `export` lines in `secrets.sh`.

Optional: `UI_AUTH_USER` (default `admin`) can be set in `.env` — it is not
secret and does not need Keychain.

MinusPod subprocess env intentionally **does not** receive Pocket Casts
credentials; on macOS, `OPENROUTER_API_KEY` is overlaid from Passwords app /
Keychain when starting MinusPod (set the env var on Windows/Linux before launch).

### Tunables (`.env`)

Set `LLM_PROVIDER` in `.env` to choose how MinusPod runs ad detection. See
[Choosing an LLM backend](#step-5--choosing-an-llm-backend) for full examples.

| Variable | Default | Effect |
|----------|---------|--------|
| `LLM_PROVIDER` | `ollama` | `ollama` (local), `openrouter`, `openai-compatible`, or `anthropic`. Non-`ollama` values skip starting Ollama. |
| `OPENAI_MODEL` | `qwen3.5-addetect` | Model name / ID passed to MinusPod. For OpenRouter use `provider/model` slugs; for Ollama use `ollama list` names. |
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | Base URL for Ollama or `openai-compatible` providers (must end in `/v1`). Ignored for `openrouter` and `anthropic`. |
| `OPENAI_API_KEY` | `not-needed` | API key for `openai-compatible` endpoints. Set to your provider's key (OpenAI, Groq, Together, etc.). |
| `OPENROUTER_API_KEY` | — | Required when `LLM_PROVIDER=openrouter`. Store in Passwords app, not `.env`. |
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

### Pocket Casts transcript reuse (optional)

| Variable | Default | Effect |
|----------|---------|--------|
| `PC_TRANSCRIPT_REUSE` | `true` | When Pocket Casts Plus has a generated VTT, verify and inject it to skip Whisper. |
| `DISABLE_TRANSCRIPT_SYNC` | `false` | Skip the entire PC transcript path (fetch, verify, inject). |
| `PC_TRANSCRIPT_MIN_COVERAGE` | `0.97` | Reject partial transcripts (coverage vs audio duration). |
| `PC_TRANSCRIPT_MAX_DURATION_DELTA` | `10` | Floor for allowed gap between VTT end and audio duration. Effective max is `max(10s, audio_duration × (1 − MIN_COVERAGE))` so a 97% coverage floor and a 10s cap do not contradict on long episodes. |
| `PC_TRANSCRIPT_PROBES` | `5` | Fuzzy alignment probe count across the episode. |
| `PC_TRANSCRIPT_MIN_SIMILARITY` | `0.55` | Minimum text match ratio per probe. |
| `PC_TRANSCRIPT_MAX_OFFSET` | `3.0` | Max timestamp drift (seconds) per probe. |
| `PC_TRANSCRIPT_PROBE_WARMUP_SECONDS` | `90` | First probe starts here (skips cold-open misalignment). |
| `PC_TRANSCRIPT_PROBE_MAX_FAILURES` | `1` | Allow this many probe failures before rejecting. |
| `PC_TRANSCRIPT_ALLOW_RSS` | `false` | Allow injecting RSS `podcast:transcript` files (often ad-free masters). |

Validate thresholds against your library: `python scripts/validate_pc_transcripts.py --with-ads-only`.

### MinusPod runtime (optional)

| Variable | Default | Effect |
|----------|---------|--------|
| `WINDOW_SIZE_SECONDS` | `600` | Transcript window size handed to the LLM. |
| `WINDOW_OVERLAP_SECONDS` | `120` | Overlap between consecutive windows. |
| `LARGE_WINDOW_SECONDS` | `3600` | Window size used in place of `WINDOW_SIZE_SECONDS` for 1M-context models (DeepSeek V4, Gemini Flash, Qwen Long, Llama 4 / 3.1-405B). Default 3600 covers a 1hr episode in a single window, cutting per-episode LLM cost. Set lower for smaller windows, higher for longer episodes. Range 300–36000 (10 hr ceiling, sized for 1M-context models). |
| `LARGE_WINDOW_MIN_SECONDS` | `300` | Lower bound for the accepted `LARGE_WINDOW_SECONDS` range. Override in `.env` to tighten the envelope. |
| `LARGE_WINDOW_MAX_SECONDS` | `36000` | Upper bound for the accepted `LARGE_WINDOW_SECONDS` range. Widen for larger-context models (e.g. Gemini 1.5 Pro at 2M). |
| `SKIP_VERIFICATION_UNDER_SECONDS` | `0` (recommended) | Run the verification pass on every episode. **`0`** runs a second LLM pass — catches mid-roll ads (in-house + DAI stacks, timestamp mistakes in pass 1) that full-transcript detection misses. Costs ~2× LLM tokens. **`86400`** (24 h) skips verification on all realistic episodes and halves cost, but may leave mid-roll sponsor reads in place. |
| `ENABLE_PROMPT_CACHING` | `true` | Annotate the system prompt with `cache_control: ephemeral` so the provider can cache it across the ~22 windows of a long episode. **Provider-dependent**: works on OpenRouter and Anthropic (both honour the annotation); **no-op on DeepSeek** (caching is automatic and prefix-based — see [api-docs.deepseek.com/guides/kv_cache](https://api-docs.deepseek.com/guides/kv_cache)) and on Ollama. The code auto-detects the provider from `LLM_PROVIDER` and only annotates when it will be honoured. Cached input tokens are reported in the response log regardless of provider. |
| `AD_DETECTION_MAX_TOKENS` | `16384` | Output token budget per LLM call. With full-transcript windowing (`LARGE_WINDOW_SECONDS=3600`), ad-heavy ~1 hr episodes can exceed 8192 tokens and MinusPod retries forever (`hit max_tokens` / `empty completion` in `/tmp/minuspod.log`). **16384** is the recommended value for this OpenRouter quick setup. Lower for smaller models/contexts; raise further only if truncation persists. |
| `CHAPTERS_ENABLED` | `true` | **Not in .env** — stored in MinusPod DB. Toggle via Settings → Chapters in UI, or `PUT /api/v1/settings/ad-detection` with `{"chaptersEnabled": false}`. Disabling saves ~2 LLM calls/episode (boundary + title). |
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

Local modifications to MinusPod live as patches in
[`patches/`](patches/). The pinned upstream commit is recorded in
[`patches/MINUSPOD_BASE.txt`](patches/MINUSPOD_BASE.txt). `setup_minuspod.sh`
applies them in order with `git apply --3way`; if a patch no longer applies
cleanly against the pinned commit, the script warns and continues rather than
failing the install.

| Patch | Purpose |
|-------|---------|
| [`minuspod-local.patch`](patches/minuspod-local.patch) | Honour `DATA_DIR`, env-tunable window sizes, `detect_tail_gap`, ad padding, `SKIP_VERIFICATION=true`. |
| [`llm-cost-optimizations.patch`](patches/llm-cost-optimizations.patch) | The three LLM cost tunables documented under [MinusPod runtime](#minuspod-runtime-optional): large-window override for 1 M-context models, configurable `SKIP_VERIFICATION_UNDER_SECONDS`, and OpenRouter prompt caching on the system prompt. Adds the "Ad detection" panel in this UI. |
| [`house-ad-detection.patch`](patches/house-ad-detection.patch) | Recognize self-promo / house-ad language in LLM ad reasons so long membership reads (e.g. Giant Bomb Premium) are not rejected by the "no sponsor identified" gate. |

### Tuning without restart

The three LLM cost tunables are exposed in the dashboard under **Ad
detection**. Changes land in the MinusPod database immediately and take effect
on the next episode processed — no service restart required. The dashboard
GETs `/api/v1/settings` to read the current values (showing whether each is at
the default, set in the DB, or overridden by an env var) and PUTs
`/api/v1/settings/ad-detection` to write. Unknown keys are rejected before the
request reaches MinusPod, and MinusPod's own cross-field validation
(`LARGE_WINDOW_SECONDS >= WINDOW_SIZE_SECONDS`) is surfaced back to the UI.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| Browser asks for login at `localhost:5050` | Expected when `UI_AUTH_PASSWORD` is set. Username defaults to `admin`. Password: Passwords app on macOS (search **Pocket Casts Ad-Free UI** or run `security find-internet-password -s localhost -a admin -P 5050 -w`), `secrets.ps1` on Windows, or `secrets.sh` on Linux. |
| `pocketcasts_auth_failed` banner | Wrong Pocket Casts credentials. macOS: update the `pocketcasts.com` login in Passwords, or `security add-internet-password -a "EMAIL" -s pocketcasts.com -r htps -w "NEW" -U`. Windows/Linux: update `secrets.ps1` or `secrets.sh`, then restart the UI. |
| Phone can't reach the dashboard | Mac must be awake, on the same Wi‑Fi, and reachable at `http://<lan-ip>:5050`. Reserve a static LAN IP on your router or use Tailscale for away-from-home access. |
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
| Stuck on `pass1:detecting:N/M` | Ad detection uses your **LLM**, not Whisper. **OpenRouter / cloud:** check `/tmp/minuspod.log` for `hit max_tokens=8192` and `empty completion` — the ad-list JSON was truncated. Raise `AD_DETECTION_MAX_TOKENS` to **16384** (the [OpenRouter quick setup](#quick-setup-openrouter--deepseek-v4-flash) default), restart MinusPod, and re-queue. If it still truncates, lower `LARGE_WINDOW_SECONDS` (e.g. `600`) to split into smaller windows. Also check rate limits and model availability. **Ollama:** large models (`qwen3.5-addetect`) can exceed 10 min per window — use `OPENAI_MODEL=qwen3:14b` on ≤36 GB Macs, or raise `LLM_TIMEOUT_LOCAL`. The stall watchdog restarts Ollama for detecting stages and waits up to 45 min (`EPISODE_STALL_THRESHOLD_LLM_SECONDS`). |
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
source secrets.sh   # or secrets.ps1 on Windows — see Step 2
python -m unittest tests -v
```

The suite covers artwork normalization, date validation, state management,
Patreon detection, transcript parsing, skip/stop semantics, upload ordering,
Up Next queue safety, Pocket Casts iOS-parity (`hasCustomImage` / `colour`),
RSS resolution, processed-podcast detection, the failed-episode abort path,
the `services_manager` helpers, and every `/api/*` endpoint.

## Contributing

Fork the repo on GitHub only if you plan to submit pull requests. Otherwise
clone [SushiTigar/pocket-casts-adfree](https://github.com/SushiTigar/pocket-casts-adfree)
directly.

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

## Known issues & future work

| Area | Status | Notes |
|------|--------|-------|
| **Chapter generation** | Partially working | Produces 1–2 chapters per episode instead of granular boundaries. Root cause: chapter boundary model uses same long-context `deepseek-v4-flash` but prompt/template may need tuning. Workaround: disable with `CHAPTERS_ENABLED=false` (Settings → Chapters or `PUT /settings/ad-detection`). Fix planned: investigate chapter boundary prompt and consider dedicated chapter model setting. |
| **Auto-update guard** | Functional but manual | `update_minuspod()` pins to `d900bdd0` and skips pull if local patches detected. For true upstream updates, run `bash scripts/setup_minuspod.sh` (re-clones at pin, re-applies patches). A proper version-pinning + diff-based patch rebasing tool would be better. |
| **DeepSeek prompt caching** | No-op | DeepSeek auto-caches prefix; our `cache_control: ephemeral` annotation is ignored. Not harmful, just unused tokens. |
| Mid-roll ads still in episode | Full-transcript detection (`LARGE_WINDOW_SECONDS=3600`) often catches only pre/post-roll in one LLM call. Confirm **`SKIP_VERIFICATION_UNDER_SECONDS=0`** (default in quick setup), restart MinusPod, reset processed, and re-queue. Check `/tmp/minuspod.log` for `pass2` / verification lines. Set `86400` only if you accept cheaper runs that may miss mid-rolls. Also check the house-ad filter: `Rejecting suspected content: ... no sponsor identified in reason` in the log indicates a self-promo ad was dropped. |
| **Verification pass** | On by default in quick setup | `SKIP_VERIFICATION_UNDER_SECONDS=0` runs verification on every episode (~2× LLM cost; catches mid-rolls). Set `86400` to skip verification on episodes under 24 h (cheaper, may miss mid-rolls). |

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
