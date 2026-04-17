#!/usr/bin/env bash
# Build whisper.cpp natively on macOS (Metal acceleration) and download the
# large-v3-turbo model. Skips work that's already done.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/whisper.cpp"
MODEL="ggml-large-v3-turbo.bin"

if [[ ! -d "${TARGET}/.git" ]]; then
    echo "Cloning whisper.cpp into ${TARGET}..."
    git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git "${TARGET}"
fi

cd "${TARGET}"

if [[ ! -f "build/bin/whisper-server" ]]; then
    echo "Building whisper.cpp with Metal acceleration..."
    cmake -B build -DWHISPER_METAL=ON -DWHISPER_METAL_EMBED_LIBRARY=ON >/dev/null
    cmake --build build --config Release -j
fi

if [[ ! -f "models/${MODEL}" ]]; then
    echo "Downloading model ${MODEL}..."
    bash models/download-ggml-model.sh large-v3-turbo
fi

echo "whisper.cpp ready at ${TARGET}"
