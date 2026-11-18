#!/data/data/com.termux/files/usr/bin/bash
set -e
source "$HOME/peakcut/venv/bin/activate"
termux-wake-lock >/dev/null 2>&1 || true
python "$HOME/peakcut/peakcut_studio.py"
