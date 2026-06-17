#!/usr/bin/env bash
# Clone MinusPod at the pinned commit and apply local patches.
# Idempotent: re-running on a clean checkout is a no-op.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/MinusPod"
REPO="https://github.com/ttlequals0/MinusPod.git"
PIN="61eb125a1e73ffdff6450b2d29d6e75772f5d00a"
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

echo "MinusPod ready at ${TARGET}"
