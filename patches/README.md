# Patches

This directory holds the local modifications applied to upstream MinusPod so
it runs on hosts without an NVIDIA GPU (e.g. Apple Silicon) and with the
pipeline's preferred ad-detection tuning.

## Files

| File                                | Purpose                                                              |
| ----------------------------------- | -------------------------------------------------------------------- |
| `MINUSPOD_BASE.txt`                 | Upstream tag / commit notes                                          |
| `minuspod-local.patch`              | Legacy **2.34.0 (`d900bdd`)** consolidated diff (GPU-less compose, `DATA_DIR`, tail-gap, etc.) |
| `llm-cost-optimizations.patch`      | Large-window override, skip-verify, prompt-caching tunables in `config.py` / settings API |
| `adaptive-detection-windows.patch` | Safe-window cap + adaptive `resolve_window_size_seconds`, self-promo evidence gate (`__init__.py`, `prompts.py`) |
| `whisper-short-clip-guard.patch`    | Fold sub-second trailing transcription chunks; skip tiny API uploads (`transcriber.py`) |
| `llm-call-reasoning-retry.patch`    | **2.34.0 only** — `TruncatedCompletionError` / reasoning retry in `llm_call.py` |
| `chapter-granularity.patch`         | **2.34.0 only** — chapter token budget (dropped on v2.96.25) |
| `truncation-failfast.patch`         | **2.34.0 only** — window bisect on truncation |
| `cost-optimization-consumers.patch` | **2.34.0 only** — wires llm-cost tunables into `llm_client` / processing |

## Additional patches (live pin: **v2.96.25**)

Applied after optional `minuspod-local.patch` by `scripts/setup_minuspod.sh` and
`services_manager.py` (in this order):

1. `llm-cost-optimizations.patch`
2. `adaptive-detection-windows.patch` (includes former `house-ad-detection` hunks in `prompts.py`)
3. `whisper-short-clip-guard.patch`

Patches are regenerated from `git diff v2.96.25` on `local-mods` (2026-09-16).
`minuspod-local.patch` is best-effort on v2.96.25 (upstream absorbed most hunks).

## Legacy 2.34.0 rebuild

To reproduce the old stack: checkout `d900bdd0`, apply `minuspod-local.patch`,
then the full chain including `llm-call-reasoning-retry`, `chapter-granularity`,
`truncation-failfast`, and `cost-optimization-consumers`. Branch
`local-mods-d900bdd-backup` in `MinusPod/` is a snapshot.

## Upstream MinusPod

As of **2026-09-16**, the live runtime pins **`v2.96.25`** (commit `1477638a`,
annotated tag object `ca4ab897`) on branch **`local-mods`** inside `MinusPod/`.
Upstream **`origin/main` is ~2.97.1**.

Before migrating a production DB to v2.96.25, back up `MinusPod/data/*.db`
(forward-only migrations). Rollback: restore backups and
`git checkout local-mods-d900bdd-backup`.

### v2.96.25 triage (applied)

| Patch | Outcome on v2.96.25 |
|-------|---------------------|
| `llm-cost-optimizations` | **Regenerated** — `config.py`, `llm_call.py` |
| `adaptive-detection-windows` | **Regenerated** — `resolve_window_size_seconds`, `__init__.py`, self-promo evidence gate |
| `whisper-short-clip-guard` | **Regenerated** — API + local chunk sliver fold |
| `llm-call-reasoning-retry` | **Dropped** — upstream `llm_call` + `llm_capabilities` |
| `chapter-granularity` | **Dropped** |
| `truncation-failfast` | **Dropped** — upstream salvage + bisect differ |
| `cost-optimization-consumers` | **Dropped** — mostly upstream |
| `minuspod-local` | **Mostly dropped** — upstream has `DATA_DIR`, tail-gap |

## Re-generating a patch

Scope diffs to the files that patch owns (never `git diff` the whole tree):

```bash
cd MinusPod
git diff v2.96.25 -- src/transcriber.py > ../patches/whisper-short-clip-guard.patch
```

Commit the updated patch in the parent repo alongside any MinusPod commit.
