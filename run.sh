#!/usr/bin/env bash
# Local dev runner: sets up the virtualenv on first run, then starts the bot.
# Usage:  bash run.sh
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it with:  brew install python"
  exit 1
fi

# aiogram supports 3.10-3.14. Newer Python has no prebuilt pydantic-core wheels,
# which makes pip try to compile Rust and fail with a confusing error.
python3 - <<'PYCHECK' || exit 1
import sys
major, minor = sys.version_info[:2]
if (major, minor) < (3, 10) or (major, minor) >= (3, 15):
    print(f"Python {major}.{minor} is not supported (need 3.10-3.14).")
    print("Fix:  brew install python@3.13")
    print("      rm -rf .venv && python3.13 -m venv .venv && bash run.sh")
    raise SystemExit(1)
PYCHECK

if [ ! -d .venv ]; then
  echo "==> Creating virtualenv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Only reinstall when requirements.txt is newer than the last install marker.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "==> Installing dependencies"
  # 'python -m pip' rather than bare 'pip': guarantees the packages land in the
  # interpreter we're about to run, even if PATH or a shell alias says otherwise.
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  touch .venv/.installed
fi

if [ ! -f .env ]; then
  echo "==> No .env found, copying from .env.example"
  cp .env.example .env
  echo "    Put your BOT_TOKEN in .env, then run this again."
  exit 1
fi

echo "==> Starting bot (Ctrl-C to stop)"
exec python bot.py
