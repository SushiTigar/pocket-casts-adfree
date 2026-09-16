#!/usr/bin/env bash
# Clone MinusPod at the pinned commit and apply local patches.
# Idempotent: re-running on a clean checkout is a no-op.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/MinusPod"
REPO="https://github.com/ttlequals0/MinusPod.git"
# Upstream base commit; local customizations live on branch ``local-mods``.
PIN_REF="v2.96.25"
LOCAL_BRANCH="local-mods"
# Core local patch (may fail to apply if upstream drifted past the pin —
# in that case, regenerate from a clean checkout: `git diff` → patch).
PATCH="${ROOT}/patches/minuspod-local.patch"
# Optional additive patches (LLM cost-optimisations, etc.). Each is
# applied best-effort with `git apply --3way`; already-applied patches
# are no-ops because the working tree already contains the changes.
ADDITIONAL_PATCHES=(
  "${ROOT}/patches/llm-cost-optimizations.patch"
  "${ROOT}/patches/adaptive-detection-windows.patch"
  "${ROOT}/patches/whisper-short-clip-guard.patch"
)

apply_patches() {
    local failed=0
    if [[ -f "${PATCH}" ]]; then
        echo "Applying ${PATCH} (best-effort on ${PIN_REF}; often obsolete after rebase)..."
        if ! git apply --3way "${PATCH}"; then
            echo "WARNING: ${PATCH} did not apply on ${PIN_REF}; continuing." >&2
        fi
    fi
    for P in "${ADDITIONAL_PATCHES[@]}"; do
        if [[ -f "${P}" ]]; then
            echo "Applying ${P}..."
            if ! git apply --3way "${P}"; then
                echo "ERROR: ${P} did not apply cleanly." >&2
                failed=1
            fi
        fi
    done
    return "${failed}"
}

if [[ ! -d "${TARGET}/.git" ]]; then
    echo "Cloning MinusPod into ${TARGET}..."
    git clone "${REPO}" "${TARGET}"
fi

cd "${TARGET}"
if ! git remote get-url origin &>/dev/null; then
    git remote add origin "${REPO}"
fi
echo "Fetching ${PIN_REF} from origin..."
git fetch --quiet origin "refs/tags/${PIN_REF}:refs/tags/${PIN_REF}" 2>/dev/null || git fetch --quiet origin --tags
if ! git rev-parse --verify "${PIN_REF}^{commit}" >/dev/null 2>&1; then
    echo "ERROR: PIN_REF ${PIN_REF} is not a valid ref in this repo." >&2
    exit 1
fi

if git show-ref --verify --quiet "refs/heads/${LOCAL_BRANCH}"; then
    echo "Checking out existing ${LOCAL_BRANCH} branch..."
    git checkout "${LOCAL_BRANCH}"
else
    echo "Creating ${LOCAL_BRANCH} from ${PIN_REF}..."
    git checkout -B "${LOCAL_BRANCH}" "${PIN_REF}"
    if ! apply_patches; then
        echo "ERROR: One or more patches failed on fresh ${PIN_REF} checkout." >&2
        exit 1
    fi
    git add -A
    if ! git diff --cached --quiet; then
        git commit -m "pocket-casts-adfree: apply local patch stack on ${PIN_REF}"
    fi
fi

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
