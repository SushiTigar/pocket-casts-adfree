# Patches

This directory holds the local modifications applied to upstream MinusPod so
it runs on hosts without an NVIDIA GPU (e.g. Apple Silicon) and with the
pipeline's preferred ad-detection tuning.

## Files

| File                           | Purpose                                                              |
| ------------------------------ | -------------------------------------------------------------------- |
| `MINUSPOD_BASE.txt`            | Upstream commit the patch applies on top of                          |
| `minuspod-local.patch`         | Consolidated diff covering all local edits (applies on `MINUSPOD_BASE`) |
| `llm-cost-optimizations.patch` | Large-window override, `SKIP_VERIFICATION_UNDER_SECONDS`, OpenRouter prompt caching |
| `house-ad-detection.patch`     | Allow self-promo / house-ad language through the ad-evidence gate in `src/ad_detector/prompts.py` |

## What the patch changes

| File                          | Change                                                                                  |
| ----------------------------- | --------------------------------------------------------------------------------------- |
| `docker-compose.whisper.yml`  | Drop CUDA image + GPU device reservation so the stack comes up on hosts without an NVIDIA GPU |
| `src/storage.py`              | Use `DATA_DIR` env var instead of hard-coded `/app/data`                                |
| `src/database/__init__.py`    | Same `DATA_DIR` override + create dir if missing                                        |
| `src/llm_client.py`           | Disable Ollama "thinking" mode; OpenRouter provider routing via `OPENROUTER_PROVIDER_*` env |
| `src/main_app/processing.py`  | Honor `SKIP_VERIFICATION=true`; wire `detect_tail_gap` into the heuristic pass          |
| `src/config.py`               | Env tunables; `get_openrouter_provider_config()` for cheap OpenRouter host pinning |
| `src/transcriber.py`          | Chunk progress callback; skip loudnorm when `WHISPER_SKIP_PREPROCESS=1`                 |
| `src/roll_detector.py`        | Tighter pre-roll regexes + new `detect_tail_gap` for untranscribed outros (env: `TAIL_GAP_MIN_SECONDS`) |
| `src/audio_processor.py`      | Pad ad boundaries 1.5 s before / 2 s after; tail-of-file ads get 5 s after (env: `AD_END_PAD_TAIL`)   |

## Additional patches

Applied after `minuspod-local.patch` by `scripts/setup_minuspod.sh` and
`services_manager.py` (in this order):

1. `llm-cost-optimizations.patch` — LLM cost tunables and Ad detection UI panel
2. `house-ad-detection.patch` — self-promo evidence in ad gate (`SELF_PROMO_KEYWORDS`)

## Re-generating the patch

If you edit MinusPod sources locally and want to refresh the bundled patch:

```bash
cd MinusPod
git diff > ../patches/minuspod-local.patch
```

Commit the updated patch alongside any code changes.