#!/bin/bash
# Start all services needed for the Pocket Casts ad-free pipeline.
# Usage: ./start_services.sh [--mlx]
#
# Options:
#   --mlx   Use MLX instead of Ollama for ~2x faster LLM inference on Apple Silicon.
#           Requires: pip install mlx-openai-server

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

USE_MLX=false
for arg in "$@"; do
    case "$arg" in
        --mlx) USE_MLX=true ;;
    esac
done

echo "=== Pocket Casts Ad-Free Pipeline: Starting Services ==="

# Load environment variables if secrets.sh or .env exist
if [ -f "$SCRIPT_DIR/secrets.sh" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/secrets.sh"
fi
if [ -f "$SCRIPT_DIR/.env" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
fi

# 1. Start LLM backend (Ollama or MLX)
LLM_PORT=11434
LLM_PROVIDER=${LLM_PROVIDER:-ollama}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-"http://localhost:11434/v1"}
LLM_MODEL=${OPENAI_MODEL:-"qwen3.5-addetect"}

if [ "$LLM_PROVIDER" != "ollama" ]; then
    echo "[1/3] Skipping LLM backend startup (provider is $LLM_PROVIDER)..."
elif [ "$USE_MLX" = true ]; then
    LLM_PORT=8800
    LLM_PROVIDER=ollama
    OPENAI_BASE_URL="http://localhost:$LLM_PORT/v1"
    LLM_MODEL="qwen3.5-35b-a3b"
    echo "[1/3] Starting MLX server on port $LLM_PORT (2x faster than Ollama)..."
    if curl -s "http://localhost:$LLM_PORT/v1/models" > /dev/null 2>&1; then
        echo "  Already running"
    else
        if ! command -v mlx-openai-server &> /dev/null; then
            echo "  ERROR: mlx-openai-server not found. Install with: pip install mlx-openai-server"
            exit 1
        fi
        mlx-openai-server launch \
            --model-path mlx-community/Qwen3.5-35B-A3B-4bit \
            --model-type lm \
            --port "$LLM_PORT" \
            > /tmp/mlx-server.log 2>&1 &
        echo "  PID: $!"
        sleep 10
        if curl -s "http://localhost:$LLM_PORT/v1/models" > /dev/null 2>&1; then
            echo "  OK"
        else
            echo "  WARNING: MLX server may still be loading. Check /tmp/mlx-server.log"
        fi
    fi
else
    echo "[1/3] Starting Ollama..."
    if ! pgrep -x ollama > /dev/null 2>&1; then
        brew services start ollama 2>/dev/null || ollama serve &
        sleep 3
    fi
    echo "  Ollama running. Checking model..."
    if ! ollama list 2>/dev/null | grep -q "qwen3.5:35b-a3b"; then
        echo "  Pulling qwen3.5:35b-a3b (this may take a while)..."
        ollama pull qwen3.5:35b-a3b
    fi
    # Create custom model variant with 16K context for ad detection
    if ! ollama list 2>/dev/null | grep -q "qwen3.5-addetect"; then
        echo "  Creating qwen3.5-addetect (16K context)..."
        cat > /tmp/Modelfile.qwen35 << 'MODELEOF'
FROM qwen3.5:35b-a3b
PARAMETER num_ctx 16384
MODELEOF
        ollama create qwen3.5-addetect -f /tmp/Modelfile.qwen35
    fi
    echo "  OK"
fi

# 2. Start whisper.cpp server
WHISPER_DIR="$SCRIPT_DIR/whisper.cpp"
WHISPER_PORT=8765
echo "[2/3] Starting whisper.cpp server on port $WHISPER_PORT..."
if curl -s "http://localhost:$WHISPER_PORT/health" | grep -q "ok" 2>/dev/null; then
    echo "  Already running"
else
    if [ ! -f "$WHISPER_DIR/build/bin/whisper-server" ]; then
        echo "  ERROR: whisper.cpp not built. Run the setup first."
        exit 1
    fi
    # Threads = perf cores capped at 8 (Metal has a hard 8 command-buffer
    # limit; going higher crashes the GPU backend).
    # `--processors 1`: whisper.cpp #2036 corrupts token timestamps when
    # processors > 1 (timestamps restart per chunk), and we rely on those
    # timestamps for ad cutting.
    WHISPER_CORES=$(sysctl -n hw.performancecores 2>/dev/null \
        || sysctl -n hw.perflevel0.physicalcpu 2>/dev/null \
        || echo 4)
    if [ "$WHISPER_CORES" -gt 8 ]; then WHISPER_CORES=8; fi
    "$WHISPER_DIR/build/bin/whisper-server" \
        --host 0.0.0.0 --port "$WHISPER_PORT" \
        --model "$WHISPER_DIR/models/ggml-large-v3-turbo.bin" \
        --inference-path /v1/audio/transcriptions \
        --dtw large.v3.turbo \
        --no-flash-attn \
        --threads "$WHISPER_CORES" \
        --processors 1 \
        > /tmp/whisper-server.log 2>&1 &
    echo "  PID: $!"
    sleep 8
    if curl -s "http://localhost:$WHISPER_PORT/health" | grep -q "ok"; then
        echo "  OK"
    else
        echo "  WARNING: whisper server may still be loading. Check /tmp/whisper-server.log"
    fi
fi

# 3. Start MinusPod
MINUSPOD_PORT=8000
MINUSPOD_DIR="$SCRIPT_DIR/MinusPod"
echo "[3/3] Starting MinusPod on port $MINUSPOD_PORT..."

# --- Auto-update MinusPod from upstream ---
_update_minuspod() {
    local mp_dir="$1"
    local patch_file="$SCRIPT_DIR/patches/minuspod-local.patch"
    echo "  Checking for MinusPod updates..."
    cd "$mp_dir" || return
    # Fetch quietly; tolerate network failures (offline use)
    if ! git fetch origin --quiet 2>/dev/null; then
        echo "  (Could not reach GitHub — skipping update check)"
        cd "$SCRIPT_DIR"
        return
    fi
    local local_sha remote_sha
    local_sha=$(git rev-parse HEAD 2>/dev/null)
    remote_sha=$(git rev-parse origin/main 2>/dev/null \
              || git rev-parse origin/master 2>/dev/null)
    if [ -z "$remote_sha" ] || [ "$local_sha" = "$remote_sha" ]; then
        echo "  Already at latest ($(git rev-parse --short HEAD))"
        cd "$SCRIPT_DIR"
        return
    fi
    echo "  Update available: $(git rev-parse --short HEAD) → $(git rev-parse --short "$remote_sha")"
    # Discard any locally applied patch before pulling
    git reset --hard HEAD --quiet
    git clean -fd --quiet 2>/dev/null || true
    if ! git pull --ff-only origin main 2>/dev/null && \
       ! git pull --ff-only origin master 2>/dev/null; then
        echo "  WARNING: git pull failed (non-fast-forward?). Skipping update."
        cd "$SCRIPT_DIR"
        return
    fi
    echo "  Pulled to $(git rev-parse --short HEAD)"
    # Reapply our local patches on top of the new upstream
    if [ -f "$patch_file" ]; then
        if git apply "$patch_file" 2>/dev/null; then
            echo "  Local patches applied cleanly."
        else
            echo "  WARNING: patch did not apply cleanly — manual merge may be needed."
            echo "           See: patches/minuspod-local.patch"
        fi
    fi
    # Reinstall Python deps if requirements changed
    if [ -f "requirements.txt" ] && [ -f "venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
        pip install -r requirements.txt --quiet --disable-pip-version-check
        echo "  Dependencies updated."
    fi
    cd "$SCRIPT_DIR"
}

if curl -s "http://localhost:$MINUSPOD_PORT/api/v1/health" | grep -q "healthy" 2>/dev/null; then
    echo "  Already running (skipping update check while running)"
else
    _update_minuspod "$MINUSPOD_DIR"
    (
        cd "$MINUSPOD_DIR/src"
        source ../venv/bin/activate
        DATA_DIR="$MINUSPOD_DIR/data" \
        LLM_PROVIDER="$LLM_PROVIDER" \
        OPENAI_BASE_URL="$OPENAI_BASE_URL" \
        OPENAI_API_KEY="${OPENAI_API_KEY:-not-needed}" \
        OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
        OPENROUTER_PROVIDER_ORDER="${OPENROUTER_PROVIDER_ORDER:-}" \
        OPENROUTER_ALLOW_FALLBACKS="${OPENROUTER_ALLOW_FALLBACKS:-}" \
        OPENROUTER_PROVIDER_SORT="${OPENROUTER_PROVIDER_SORT:-}" \
        OPENROUTER_PROVIDER_ONLY="${OPENROUTER_PROVIDER_ONLY:-}" \
        OPENROUTER_PROVIDER_IGNORE="${OPENROUTER_PROVIDER_IGNORE:-}" \
        ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
        OPENAI_MODEL="$LLM_MODEL" \
        WHISPER_BACKEND=openai-api \
        WHISPER_API_BASE_URL="http://localhost:$WHISPER_PORT/v1" \
        WHISPER_DEVICE=cpu \
        WHISPER_SKIP_PREPROCESS=1 \
        HTTP_TIMEOUT_WHISPER="${HTTP_TIMEOUT_WHISPER:-1800}" \
        API_CHUNK_DURATION_SECONDS="${API_CHUNK_DURATION_SECONDS:-300}" \
        BASE_URL="http://localhost:$MINUSPOD_PORT" \
        HF_HOME="$MINUSPOD_DIR/data/.cache" \
        SKIP_VERIFICATION=true \
        WINDOW_SIZE_SECONDS=600 \
        WINDOW_OVERLAP_SECONDS=120 \
        AD_DETECTION_MAX_TOKENS=4096 \
        OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}" \
        OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}" \
        OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30s}" \
        LLM_TIMEOUT_LOCAL="${LLM_TIMEOUT_LOCAL:-1200}" \
        PYTHONPATH=. python -m flask --app main_app:app run --host 0.0.0.0 --port "$MINUSPOD_PORT" \
            > /tmp/minuspod.log 2>&1 &
    )
    echo "  PID: $!"
    # MinusPod blocks on LLM json_format probe (~30s) + RSS refresh before binding.
    MINUSPOD_OK=false
    for i in $(seq 1 24); do
        if curl -s "http://localhost:$MINUSPOD_PORT/api/v1/health" | grep -q "healthy" 2>/dev/null; then
            MINUSPOD_OK=true
            break
        fi
        sleep 5
    done
    if [ "$MINUSPOD_OK" = true ]; then
        echo "  OK"
    else
        echo "  WARNING: MinusPod may still be starting. Check /tmp/minuspod.log"
    fi

    # Ensure the model is set correctly and disable auto-processing
    sleep 2
    curl -s -X PUT "http://localhost:$MINUSPOD_PORT/api/v1/settings/ad-detection" \
        -H "Content-Type: application/json" \
        -d "{\"claudeModel\": \"$LLM_MODEL\", \"verificationModel\": \"$LLM_MODEL\", \"chaptersModel\": \"$LLM_MODEL\", \"autoProcessEnabled\": false}" > /dev/null 2>&1
fi

echo ""
echo "=== All services started ==="
if [ "$USE_MLX" = true ]; then
    echo "  MLX LLM:     http://localhost:$LLM_PORT (2x faster than Ollama)"
else
    echo "  Ollama:      http://localhost:11434"
fi
echo "  Whisper:     http://localhost:$WHISPER_PORT"
echo "  MinusPod:    http://localhost:$MINUSPOD_PORT"
echo "  MinusPod UI: http://localhost:$MINUSPOD_PORT/ui/"
echo ""

# Load credentials if .env exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    source "$SCRIPT_DIR/.env"
fi

echo "To launch the web UI dashboard:"
echo "  source .env && python3 pocketcasts_adfree.py ui"
echo "  Then open: http://localhost:5050"
echo ""
echo "To run from command line:"
echo "  source .env && python3 pocketcasts_adfree.py test --rss-url 'https://feeds.simplecast.com/54nAGcIl'"
echo "  source .env && python3 pocketcasts_adfree.py auto --rss-url 'https://feeds.simplecast.com/54nAGcIl'"
