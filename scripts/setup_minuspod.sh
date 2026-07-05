#!/usr/bin/env bash
# Clone MinusPod at the pinned commit and apply local patches.
# Idempotent: re-running on a clean checkout is a no-op.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/MinusPod"
REPO="https://github.com/ttlequals0/MinusPod.git"
PIN="d900bdd0622b89089247bafe6a5f9db87876233a"
# Core local patch (may fail to apply if upstream drifted past the pin —
# in that case, regenerate from a clean checkout: `git diff` → patch).
PATCH="${ROOT}/patches/minuspod-local.patch"
# Optional additive patches (LLM cost-optimisations, etc.). Each is
# applied best-effort with `git apply --3way`; already-applied patches
# are no-ops because the working tree already contains the changes.
ADDITIONAL_PATCHES=(
  "${ROOT}/patches/llm-cost-optimizations.patch"
)

if [[ ! -d "${TARGET}/.git" ]]; then
    echo "Cloning MinusPod into ${TARGET}..."
    git clone "${REPO}" "${TARGET}"
fi

cd "${TARGET}"
echo "Pinning to ${PIN}..."
git fetch --quiet origin
git reset --hard "${PIN}"
git clean -fd
# Drop the `origin` remote so accidental pushes don't stray into upstream.
git remote remove origin 2>/dev/null || true

if [[ -f "${PATCH}" ]]; then
    echo "Applying ${PATCH}..."
    if ! git apply --3way "${PATCH}"; then
        echo "WARNING: ${PATCH} did not apply cleanly (likely upstream drift)." >&2
        echo "         Regenerate it from a known-good checkout: cd MinusPod && git diff <pin> > ${PATCH}" >&2
    fi
fi

for P in "${ADDITIONAL_PATCHES[@]}"; do
    if [[ -f "${P}" ]]; then
        echo "Applying ${P}..."
        if ! git apply --3way "${P}"; then
            echo "WARNING: ${P} did not apply cleanly. Check the output above." >&2
        fi
    fi
done

if [[ ! -d "venv" ]]; then
    echo "Creating Python virtualenv..."
    python3 -m venv venv
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install --quiet --upgrade pip
if ! pip install --quiet -r requirements.txt; then
    echo "requirements.txt failed (likely due to Python 3.14+ compatibility). Falling back to requirements.in..."
    pip install --quiet -r requirements.in
fi

# MinusPod relies on ffprobe via subprocess for audio-duration probes; without it
# every transcription fails before reaching Whisper ("No such file or directory:
# 'ffprobe'"). We only need ffprobe (not full ffmpeg functions), but installing
# the whole ffmpeg formula is the only Homebrew path that ships it.
if ! command -v ffprobe >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing ffmpeg (provides ffprobe) via Homebrew..."
        brew install ffmpeg
    else
        echo "WARNING: ffprobe is missing and Homebrew is not available." >&2
        echo "         Install ffmpeg manually or transcription will fail." >&2
    fi
fi

echo "MinusPod ready at ${TARGET}"
