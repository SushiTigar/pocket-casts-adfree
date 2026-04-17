#!/usr/bin/env bash
# Clone MinusPod at the pinned commit and apply local patches.
# Idempotent: re-running on a clean checkout is a no-op.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${ROOT}/MinusPod"
REPO="https://github.com/ttlequals0/MinusPod.git"
PIN="0f754f82943db28c8f10a086de87dc8026414fd3"
PATCH="${ROOT}/patches/minuspod-local.patch"

if [[ ! -f "${PATCH}" ]]; then
    echo "Missing patch file: ${PATCH}" >&2
    exit 1
fi

if [[ ! -d "${TARGET}/.git" ]]; then
    echo "Cloning MinusPod into ${TARGET}..."
    git clone "${REPO}" "${TARGET}"
fi

cd "${TARGET}"
echo "Pinning to ${PIN}..."
git fetch --quiet origin
git reset --hard "${PIN}"
git clean -fd

echo "Applying ${PATCH}..."
git apply "${PATCH}"

if [[ ! -d "venv" ]]; then
    echo "Creating Python virtualenv..."
    python3 -m venv venv
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "MinusPod ready at ${TARGET}"
