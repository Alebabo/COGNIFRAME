#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN="92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"
mkdir -p "$ROOT/vendor"
if [[ ! -d "$ROOT/vendor/video-use/.git" ]]; then
  git clone https://github.com/browser-use/video-use.git "$ROOT/vendor/video-use"
fi
git -C "$ROOT/vendor/video-use" fetch --quiet origin "$PIN" || true
git -C "$ROOT/vendor/video-use" checkout --detach "$PIN"
uv venv "$ROOT/.venv"
uv pip install --python "$ROOT/.venv/bin/python" -e "$ROOT/vendor/video-use"
uv pip install --python "$ROOT/.venv/bin/python" -e "$ROOT"
echo "video-use pinned at $(git -C "$ROOT/vendor/video-use" rev-parse HEAD)"
