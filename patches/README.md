# Patches

This directory holds the local modifications applied to upstream MinusPod so
it runs on hosts without an NVIDIA GPU (e.g. Apple Silicon) and with the
pipeline's preferred ad-detection tuning.

## Files

| File                           | Purpose                                                              |
| ------------------------------ | -------------------------------------------------------------------- |
| `MINUSPOD_BASE.txt`            | Upstream commit the patch applies on top of                          |
| `minuspod-local.patch`         | Consolidated diff (8 files) covering all local edits                 |

## What the patch changes

| File                          | Change                                                                                  |
| ----------------------------- | --------------------------------------------------------------------------------------- |
| `docker-compose.whisper.yml`  | Drop CUDA image + GPU device reservation so the stack comes up on hosts without an NVIDIA GPU |
| `src/storage.py`              | Use `DATA_DIR` env var instead of hard-coded `/app/data`                                |
| `src/database/__init__.py`    | Same `DATA_DIR` override + create dir if missing                                        |
| `src/llm_client.py`           | Disable Ollama "thinking" mode (`reasoning_effort: none`) — saves ~30s per call         |
| `src/main_app/processing.py`  | Honor `SKIP_VERIFICATION=true`; wire `detect_tail_gap` into the heuristic pass          |
| `src/config.py`               | Read transcript window size + overlap from env so we can tune from `start_services.sh`  |
| `src/roll_detector.py`        | Tighter pre-roll regexes + new `detect_tail_gap` for untranscribed outros (env: `TAIL_GAP_MIN_SECONDS`) |
| `src/audio_processor.py`      | Pad ad boundaries 1.5 s before / 2 s after; tail-of-file ads get 5 s after (env: `AD_END_PAD_TAIL`)   |

## Re-generating the patch

If you edit MinusPod sources locally and want to refresh the bundled patch:

```bash
cd MinusPod
git diff > ../patches/minuspod-local.patch
```

Commit the updated patch alongside any code changes.
