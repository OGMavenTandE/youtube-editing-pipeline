#!/usr/bin/env bash
# Idempotent Cloud Agent setup for the local YouTube editing pipeline.
set -euo pipefail

cd "$(dirname "$0")/.."

# System dependencies: FFmpeg/ffprobe (silence trim, MoviePy, loudnorm), Tk
# (the desktop app's customtkinter GUI, imported by the test suite), and
# python3-venv (ensurepip, required to create the virtual environment).
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3-tk python3-venv

# Project virtual environment and Python dependencies.
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pytest

# Playwright Chromium powers HTML slide and thumbnail rendering.
python -m playwright install --with-deps chromium
