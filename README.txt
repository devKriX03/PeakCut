PEAKCUT STUDIO — ANDROID / TERMUX EDITION
=========================================

WHAT THIS IS
A phone-hosted local clipping studio. The Python engine runs on your Android
phone through Termux. Chrome is only the control dashboard.

INSTALL ONCE
1. Install Termux from its official F-Droid or GitHub distribution. Do not mix
   Termux and add-ons from different sources.
2. Download peakcut_studio.py and setup_peakcut.sh into your phone's Downloads.
3. Open Termux and run:
      termux-setup-storage
      cd ~/storage/shared/Download
      bash setup_peakcut.sh
4. When the engine starts, open Chrome and visit:
      http://127.0.0.1:8765

START IT AGAIN LATER
Open Termux and run:
    source ~/peakcut/venv/bin/activate
    termux-wake-lock
    bash ~/peakcut/start_peakcut.sh
Then open http://127.0.0.1:8765 in Chrome.

WHERE FILES ARE SAVED
Clips:  Internal storage/PeakCut/Clips
Database and settings: Termux home/.peakcut/peakcut.sqlite3
Uploaded source videos: Termux home/.peakcut/uploads
Clip history is stored in SQLite and survives normal app restarts.

YOUTUBE API KEY
Open Settings in PeakCut Studio, paste your own YouTube Data API v3 key,
press Save API key, then Test connection. The key is stored locally in the
Termux app data on this phone. Do not share your Termux data folder.

CURRENT FUNCTIONALITY
- YouTube recorded-video scanning using yt-dlp and FFmpeg.
- YouTube live-stream scanning using short rolling analysis segments.
- Live chat polling when an active public live chat and valid API key exist.
- Audio loudness, band-limited vocal-energy and low-resolution visual-motion
  signals; chat activity and hype-word bursts when chat is available.
- Automatic MP4 clipping with configurable score threshold, clip limits,
  cooldown, pre-roll and post-roll.
- Manual clipping from a YouTube URL or an uploaded local video.
- Local persistent clip history, download and delete controls.
- Dashboard counters, scan status and detection log.

IMPORTANT LIMITATIONS
- Live chat cannot be analysed for ordinary recorded videos through the
  YouTube live-chat API. Recorded videos use audio and visual signals.
- This is a signal-based detector, not a semantic AI model. It does not truly
  understand gameplay, facial expressions, sarcasm or context. Audio, motion
  and chat spikes are heuristics, so review clips before publishing.
- Live YouTube segment access depends on yt-dlp, the stream's DVR availability,
  and YouTube changes. Some streams disable or limit DVR/rewind.
- This build saves MP4 clips but does not add subtitles/captions.
- Continuous scanning is resource-intensive. Use Wi-Fi, keep the phone cool,
  and preferably keep it charging. The default scan window is 24 seconds and
  source analysis is limited to a low-resolution segment to reduce load.
- Android may stop background processes due to battery management, memory
  pressure, force-stop, reboot or manufacturer restrictions. The Termux
  foreground notification and wake lock improve continuity but cannot promise
  uninterrupted operation after Android kills the process.
- Keep Termux open with the server running. Do not swipe away its persistent
  notification or force-stop the app during a monitoring session.
- The dashboard binds only to 127.0.0.1. It is not exposed to the internet or
  other devices by default.
- Use the tool only with videos you own or have permission to download and edit.

TROUBLESHOOTING
- "Missing ffmpeg/yt-dlp": rerun setup_peakcut.sh, or in Termux run:
      pkg install ffmpeg
      source ~/peakcut/venv/bin/activate
      python -m pip install -U yt-dlp Flask
- If Android stops monitoring while minimized, change Termux battery usage
  to Unrestricted / Don't optimize in Android app settings. OEM restrictions
  can still apply.
- If clips fail for a YouTube live stream, check whether DVR/rewind is enabled.
- If chat says unavailable, make sure the broadcast has public live chat,
  the API key is valid, and YouTube Data API v3 is enabled in the Google Cloud
  project associated with that key.
