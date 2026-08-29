#!/usr/bin/env bash
# Build whisper.cpp natively (Metal on macOS, CUDA on Linux with NVIDIA,
# plain CPU otherwise) and download the large-v3-turbo model.
# Skips work that's already done.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/whisper.cpp"
MODEL="ggml-large-v3-turbo.bin"
# Pin whisper.cpp to a known-good tag (update when upgrading)
WHISPER_CPP_TAG="v1.6.4"

if [[ ! -d "${TARGET}/.git" ]]; then
    echo "Cloning whisper.cpp into ${TARGET}..."
    git clone https://github.com/ggml-org/whisper.cpp.git "${TARGET}"
fi

cd "${TARGET}"

# Checkout the pinned tag
git fetch --tags origin >/dev/null 2>&1
git checkout "${WHISPER_CPP_TAG}" >/dev/null 2>&1

# Determine build flags based on platform
CMAKE_ARGS=""
UNAME_S=$(uname -s)
UNAME_M=$(uname -m)

if [[ "${UNAME_S}" == "Darwin" ]]; then
    # macOS: prefer Metal on Apple Silicon, fallback to CPU on Intel
    if [[ "${UNAME_M}" == "arm64" ]]; then
        CMAKE_ARGS="-DWHISPER_METAL=ON -DWHISPER_METAL_EMBED_LIBRARY=ON"
        echo "Building whisper.cpp with Metal acceleration (Apple Silicon)..."
    else
        CMAKE_ARGS=""
        echo "Building whisper.cpp (CPU only, Intel Mac)..."
    fi
elif [[ "${UNAME_S}" == "Linux" ]]; then
    # Linux: enable CUDA if WHISPER_CUDA=1 is set
    if [[ "${WHISPER_CUDA:-0}" == "1" ]]; then
        CMAKE_ARGS="-DGGML_CUDA=ON"
        echo "Building whisper.cpp with CUDA acceleration..."
    else
        CMAKE_ARGS=""
        echo "Building whisper.cpp (CPU only, Linux)..."
    fi
else
    # Other platforms (Windows, etc.): CPU only
    CMAKE_ARGS=""
    echo "Building whisper.cpp (CPU only)..."
fi

if [[ ! -f "build/bin/whisper-server" ]]; then
    cmake -B build ${CMAKE_ARGS} >/dev/null
    cmake --build build --config Release -j
fi

if [[ ! -f "models/${MODEL}" ]]; then
    echo "Downloading model ${MODEL}..."
    bash models/download-ggml-model.sh large-v3-turbo
fi

echo "whisper.cpp ready at ${TARGET}"
