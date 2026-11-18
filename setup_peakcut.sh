#!/data/data/com.termux/files/usr/bin/bash
set -e
APP_DIR="$HOME/peakcut"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$HOME/storage/shared" ]; then
  echo "First-time Android storage permission is required."
  termux-setup-storage || true
fi

pkg update -y
pkg install -y python ffmpeg termux-api
mkdir -p "$APP_DIR"
cp "$HERE/peakcut_studio.py" "$APP_DIR/peakcut_studio.py"
cat > "$APP_DIR/start_peakcut.sh" <<'START_SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
set -e
source "$HOME/peakcut/venv/bin/activate"
termux-wake-lock >/dev/null 2>&1 || true
python "$HOME/peakcut/peakcut_studio.py"
START_SCRIPT
chmod +x "$APP_DIR/start_peakcut.sh"
python -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install --upgrade Flask yt-dlp

mkdir -p "$HOME/storage/shared/PeakCut/Clips"
chmod 700 "$HOME/.peakcut" 2>/dev/null || true

echo
echo "PeakCut Studio is installed. Starting the local engine now."
echo "Keep this Termux session open while monitoring."
echo "Open this address in Chrome: http://127.0.0.1:8765"
echo
termux-wake-lock >/dev/null 2>&1 || true
python "$APP_DIR/peakcut_studio.py"
