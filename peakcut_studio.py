#!/usr/bin/env python3
"""PeakCut Studio — Android/Termux local clipping workspace.
Run with: python peakcut_studio.py
Requires: Flask, yt-dlp, FFmpeg. Optional YouTube Data API v3 key for live chat signals.
"""
from __future__ import annotations
import os, re, json, time, math, sqlite3, shutil, tempfile, threading, subprocess, urllib.parse, urllib.request
from pathlib import Path
from datetime import datetime, timezone
from array import array
from statistics import median
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename

APP_NAME = "PeakCut Studio"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PEAKCUT_PORT", "8765"))
HOME = Path.home()
DATA_DIR = HOME / ".peakcut"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CLIP_DIR = (HOME / "storage" / "shared" / "PeakCut" / "Clips")
if not (HOME / "storage" / "shared").exists():
    CLIP_DIR = HOME / "PeakCut" / "Clips"
CLIP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "peakcut.sqlite3"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # large local uploads; storage is the practical limit

DEFAULT_SETTINGS = {
    "api_key": "", "auto_clip": True, "clip_limit": 10, "cooldown": 60,
    "pre_roll": 12, "post_roll": 28, "threshold": 2.35,
    "weight_audio": 1.0, "weight_voice": 0.85, "weight_motion": 0.65, "weight_chat": 1.15,
    "scan_seconds": 24, "max_clip_seconds": 90, "creator": "No creator"
}

state_lock = threading.RLock()
monitor_state = {
    "running": False, "phase": "idle", "source": None, "source_type": None,
    "title": "", "video_id": "", "is_live": False, "position": 0,
    "duration": None, "started_at": None, "last_scan": None,
    "segments_scanned": 0, "detections": 0, "clips_created": 0,
    "chat_connected": False, "chat_message": "Chat signal not connected",
    "last_error": "", "session_id": None, "stop": False
}
monitor_thread = None
chat_context = {"chat_id": None, "page_token": None, "poll_after": 0, "poll_ms": 5000,
                "actual_start": None, "buckets": {}, "last_chat_error": ""}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, file_path TEXT NOT NULL,
            source_url TEXT, source_name TEXT, start_sec REAL, end_sec REAL, score REAL,
            reasons TEXT, created_at TEXT NOT NULL, kind TEXT DEFAULT 'auto')""")
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT, at_sec REAL, score REAL,
            reasons TEXT, created_at TEXT NOT NULL, clipped INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL)""")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))

init_db()


def get_settings():
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings").fetchall()
    out = DEFAULT_SETTINGS.copy()
    for r in rows:
        try: out[r["key"]] = json.loads(r["value"])
        except Exception: pass
    return out


def save_settings(payload):
    allowed = set(DEFAULT_SETTINGS)
    clean = {}
    for k, v in payload.items():
        if k not in allowed: continue
        if k == "api_key":
            clean[k] = str(v).strip()[:300]
        elif k == "creator":
            clean[k] = str(v).strip()[:60] or "No creator"
        elif k == "auto_clip":
            clean[k] = bool(v)
        elif k in ("clip_limit", "cooldown", "pre_roll", "post_roll", "scan_seconds", "max_clip_seconds"):
            clean[k] = max(1, min(int(v), 300))
        else:
            clean[k] = max(0.0, min(float(v), 10.0))
    with db() as c:
        for k, v in clean.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
    return get_settings()


def run_cmd(cmd, timeout=180, check=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError((msg[-1] if msg else "Command failed")[:350])
    return p


def parse_youtube_url(url):
    u = urllib.parse.urlparse(url.strip())
    host = (u.netloc or "").lower().replace("www.", "")
    vid = None
    if host in ("youtu.be",): vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        q = urllib.parse.parse_qs(u.query)
        if q.get("v"): vid = q["v"][0]
        elif "/live/" in u.path or "/shorts/" in u.path or "/embed/" in u.path:
            parts = [x for x in u.path.split("/") if x]
            if len(parts) >= 2: vid = parts[1]
    if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ValueError("Enter a valid YouTube video or live-stream URL.")
    return vid, f"https://www.youtube.com/watch?v={vid}"


def is_youtube_url(url):
    try: parse_youtube_url(url); return True
    except Exception: return False


def json_api(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "PeakCutStudio/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def youtube_video_info(video_id, api_key):
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode({
        "part": "snippet,liveStreamingDetails", "id": video_id, "key": api_key
    })
    data = json_api(url)
    items = data.get("items") or []
    if not items: raise RuntimeError("YouTube API could not find this video. Check the URL and API key.")
    item = items[0]
    live = item.get("liveStreamingDetails") or {}
    return {"title": item.get("snippet", {}).get("title", "YouTube video"),
            "chat_id": live.get("activeLiveChatId"),
            "actual_start": live.get("actualStartTime")}


def test_youtube_key(api_key, video_id=None):
    if not api_key: return {"ok": False, "message": "Add your YouTube Data API v3 key first."}
    params = {"part": "id", "maxResults": 1, "key": api_key}
    if video_id: params["id"] = video_id
    try:
        data = json_api("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params))
        return {"ok": True, "message": "API key accepted. YouTube Data API v3 responded successfully.", "quota_note": "API quota is consumed by requests; live chat monitoring makes repeated calls."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:240]}


def poll_live_chat(api_key):
    if not chat_context.get("chat_id"): return
    now = time.time()
    if now < chat_context.get("poll_after", 0): return
    params = {"liveChatId": chat_context["chat_id"], "part": "snippet,authorDetails", "key": api_key}
    if chat_context.get("page_token"): params["pageToken"] = chat_context["page_token"]
    url = "https://www.googleapis.com/youtube/v3/liveChat/messages?" + urllib.parse.urlencode(params)
    try:
        data = json_api(url, timeout=15)
        chat_context["page_token"] = data.get("nextPageToken")
        chat_context["poll_ms"] = max(1000, int(data.get("pollingIntervalMillis", 5000)))
        chat_context["poll_after"] = time.time() + chat_context["poll_ms"] / 1000
        start = chat_context.get("actual_start")
        start_epoch = start.timestamp() if start else None
        for item in data.get("items", []):
            sn = item.get("snippet") or {}
            published = sn.get("publishedAt")
            if not published: continue
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                sec = max(0, int(dt.timestamp() - start_epoch)) if start_epoch else int(time.time() - monitor_state.get("started_at", time.time()))
            except Exception: continue
            b = chat_context["buckets"].setdefault(sec, {"count": 0, "hype": 0})
            b["count"] += 1
            txt = " ".join(str(x) for x in sn.values() if isinstance(x, str)).lower()
            hype_words = ("no way", "insane", "crazy", "bro", "what", "omg", "lol", "lmao", "clutch", "gg", "sheesh", "wtf", "holy")
            if any(w in txt for w in hype_words) or item.get("snippet", {}).get("type") in ("superChatEvent", "superStickerEvent"):
                b["hype"] += 1
        with state_lock:
            monitor_state["chat_connected"] = True
            monitor_state["chat_message"] = f"Live chat connected · polling about every {chat_context['poll_ms']/1000:.1f}s"
    except Exception as e:
        chat_context["last_chat_error"] = str(e)[:180]
        chat_context["poll_after"] = time.time() + 12
        with state_lock:
            monitor_state["chat_connected"] = False
            monitor_state["chat_message"] = "Chat API paused: " + str(e)[:120]


def extract_segment(source, source_type, start, end, live=False, max_height=360):
    temp_dir = Path(tempfile.mkdtemp(prefix="pcscan_"))
    out = temp_dir / "scan.%(ext)s"
    if source_type == "file":
        target = temp_dir / "scan.mp4"
        run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", source,
                 "-t", str(max(1, end-start)), "-vf", f"scale='min(640,iw)':-2", "-c:v", "libx264",
                 "-preset", "ultrafast", "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-y", str(target)], timeout=180)
        return target, temp_dir
    vid, canonical = parse_youtube_url(source)
    fmt = f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/b[height<={max_height}]/b"
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "-f", fmt,
           "--download-sections", f"*{max(0,start):.2f}-{max(start+1,end):.2f}",
           "--merge-output-format", "mp4", "-o", str(out), canonical]
    if live: cmd.insert(-1, "--live-from-start")
    run_cmd(cmd, timeout=210)
    files = list(temp_dir.glob("scan*.mp4"))
    if not files: raise RuntimeError("yt-dlp did not produce an MP4 scan segment")
    return files[0], temp_dir


def pcm_seconds(path, filters=""):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn"]
    if filters: cmd += ["-af", filters]
    cmd += ["-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]
    p = run_cmd(cmd, timeout=90, check=False)
    samples = array('h')
    try: samples.frombytes(p.stdout[:len(p.stdout)//2*2])
    except Exception: return []
    per = 8000
    out = []
    for i in range(0, len(samples), per):
        chunk = samples[i:i+per]
        if not chunk: continue
        rms = math.sqrt(sum(float(x)*float(x) for x in chunk) / len(chunk)) / 32768
        peak = max(abs(x) for x in chunk) / 32768
        out.append((20*math.log10(rms+1e-7), peak))
    return out


def motion_seconds(path):
    p = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf",
                 "fps=1,scale=96:54,format=gray", "-f", "rawvideo", "pipe:1"], timeout=90, check=False)
    frame_size = 96*54
    raw = p.stdout
    frames = [raw[i:i+frame_size] for i in range(0, len(raw)-frame_size+1, frame_size)]
    vals = [0.0]
    for a, b in zip(frames, frames[1:]):
        vals.append(sum(abs(x-y) for x, y in zip(a, b)) / (frame_size*255))
    return vals


def robust_z(values):
    if not values: return []
    m = median(values)
    mad = median([abs(x-m) for x in values])
    scale = max(0.04, mad*1.4826)
    return [max(-3, min(6, (x-m)/scale)) for x in values]


def analyze_segment(path, segment_start, settings):
    loud = pcm_seconds(path)
    voice = pcm_seconds(path, "highpass=f=250,lowpass=f=3600")
    motion = motion_seconds(path)
    n = max(len(loud), len(voice), len(motion))
    if n < 3: return []
    loudv = [x[0] for x in loud] + [-65] * max(0, n-len(loud))
    voicev = [x[0] for x in voice] + [-65] * max(0, n-len(voice))
    motionv = motion + [0] * max(0, n-len(motion))
    lz, vz, mz = robust_z(loudv), robust_z(voicev), robust_z(motionv)
    chatz, chatcounts = [], []
    for i in range(n):
        item = chat_context["buckets"].get(int(segment_start+i), {"count":0,"hype":0})
        chatcounts.append(item["count"] + item["hype"]*1.5)
    cz = robust_z(chატcounts) if False else robust_z(chatcounts)
    w = settings
    scores = []
    for i in range(n):
        score = (lz[i]*float(w["weight_audio"]) + vz[i]*float(w["weight_voice"]) +
                 mz[i]*float(w["weight_motion"]) + cz[i]*float(w["weight_chat"]))
        scores.append(score)
    # Pick local peaks only; a single event should not generate a cluster of clips.
    candidates = []
    for i, sc in enumerate(scores):
        left = scores[i-1] if i else -999
        right = scores[i+1] if i+1 < len(scores) else -999
        if sc >= float(w["threshold"]) and sc >= left and sc >= right:
            reasons = []
            if lz[i] >= 1.5: reasons.append("audio spike")
            if vz[i] >= 1.5: reasons.append("vocal energy")
            if mz[i] >= 1.5: reasons.append("visual motion")
            if cz[i] >= 1.5: reasons.append("chat spike")
            if not reasons: reasons.append("combined signal")
            candidates.append({"at": segment_start+i, "score": round(sc,2), "reasons": reasons,
                               "chat_count": int(chatcounts[i])})
    # Merge peaks less than 18 seconds apart, keeping the strongest.
    merged = []
    for item in candidates:
        if merged and item["at"] - merged[-1]["at"] < 18:
            if item["score"] > merged[-1]["score"]: merged[-1] = item
        else: merged.append(item)
    # Consume old chat counters so SQLite/dashboard memory remains bounded.
    cutoff = int(segment_start - 180)
    for key in list(chat_context["buckets"]):
        if key < cutoff: chat_context["buckets"].pop(key, None)
    return merged


def ffprobe_info(path):
    p = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30)
    try: return float(p.stdout.decode().strip())
    except Exception: return None


def save_clip(source, source_type, start, end, title, score=0, reasons=None, kind="auto", live=False):
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", title)[:32].strip("_") or "clip"
    out = CLIP_DIR / f"{stamp}_{safe_title}_{int(start)}s.mp4"
    if source_type == "file":
        run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", source,
                 "-t", str(end-start), "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264",
                 "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", "-y", str(out)], timeout=240)
    else:
        vid, canonical = parse_youtube_url(source)
        fmt = "bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b"
        cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "-f", fmt,
               "--download-sections", f"*{max(0,start):.2f}-{max(start+1,end):.2f}",
               "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "--remux-video", "mp4",
               "-o", str(out), canonical]
        if live: cmd.insert(-1, "--live-from-start")
        run_cmd(cmd, timeout=300)
        if not out.exists():
            # yt-dlp can append an extension depending on the installed version.
            candidates = list(CLIP_DIR.glob(out.stem + "*"))
            if candidates: out = candidates[0]
    if not out.exists() or out.stat().st_size < 2048:
        raise RuntimeError("Clip rendering did not create a usable MP4 file")
    created = datetime.now(timezone.utc).isoformat()
    with db() as c:
        cur = c.execute("INSERT INTO clips(title,file_path,source_url,source_name,start_sec,end_sec,score,reasons,created_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (title, str(out), source if source_type == "youtube" else None, title, start, end, score,
                         json.dumps(reasons or []), created, kind))
        clip_id = cur.lastrowid
    with state_lock:
        monitor_state["clips_created"] += 1
    return clip_id, out


def add_event(source_name, item, clipped=False):
    with db() as c:
        c.execute("INSERT INTO events(source_name,at_sec,score,reasons,created_at,clipped) VALUES(?,?,?,?,?,?)",
                  (source_name, item["at"], item["score"], json.dumps(item["reasons"]), datetime.now(timezone.utc).isoformat(), 1 if clipped else 0))
    with state_lock: monitor_state["detections"] += 1


def monitor_worker(source, source_type, title, video_id, live, duration, actual_start):
    global monitor_state
    settings = get_settings()
    session_id = int(time.time())
    last_clip_at = -999999
    clips_this_session = 0
    scan = 0
    if live:
        now_elapsed = max(0, int(time.time() - actual_start.timestamp())) if actual_start else 0
        scan = max(0, now_elapsed - max(35, int(settings["scan_seconds"])+8))
    with state_lock:
        monitor_state.update({"running": True, "phase": "scanning", "source": source, "source_type": source_type,
                              "title": title, "video_id": video_id or "", "is_live": live, "position": scan,
                              "duration": duration, "started_at": time.time(), "last_scan": None,
                              "segments_scanned": 0, "detections": 0, "clips_created": 0,
                              "last_error": "", "session_id": session_id, "stop": False})
    try:
        while True:
            with state_lock:
                if monitor_state.get("stop"): break
            settings = get_settings()
            if live and video_id and settings.get("api_key"):
                poll_live_chat(settings["api_key"])
            if live:
                elapsed = max(0, int(time.time() - actual_start.timestamp())) if actual_start else int(time.time() - monitor_state["started_at"])
                end = elapsed - 10
                start = max(0, scan)
                if end <= start + 4:
                    time.sleep(4); continue
                end = min(end, start + int(settings["scan_seconds"]))
            else:
                if duration is not None and scan >= duration:
                    with state_lock: monitor_state["phase"] = "completed"
                    break
                start = scan
                end = start + int(settings["scan_seconds"])
                if duration is not None: end = min(end, duration)
                if end <= start + 2: break
            with state_lock: monitor_state["phase"] = "fetching segment"
            seg_path = None; temp_dir = None
            try:
                seg_path, temp_dir = extract_segment(source, source_type, start, end, live=live, max_height=360)
                with state_lock: monitor_state["phase"] = "analysing audio / motion / chat"
                candidates = analyze_segment(seg_path, start, settings)
                with state_lock:
                    monitor_state["segments_scanned"] += 1
                    monitor_state["last_scan"] = datetime.now().astimezone().strftime("%H:%M:%S")
                    monitor_state["position"] = end
                for item in candidates:
                    add_event(title, item, False)
                    auto = bool(settings.get("auto_clip"))
                    allowed = clips_this_session < int(settings["clip_limit"])
                    cooled = item["at"] - last_clip_at >= int(settings["cooldown"])
                    if auto and allowed and cooled:
                        cstart = max(0, item["at"] - int(settings["pre_roll"]))
                        cend = item["at"] + int(settings["post_roll"])
                        cend = min(cend, cstart + int(settings["max_clip_seconds"]))
                        if live:
                            elapsed_now = max(0, int(time.time() - actual_start.timestamp())) if actual_start else item["at"] + 35
                            cend = min(cend, elapsed_now - 5)
                        if cend - cstart >= 5:
                            try:
                                with state_lock: monitor_state["phase"] = "rendering MP4"
                                save_clip(source, source_type, cstart, cend, title, item["score"], item["reasons"], "auto", live)
                                clips_this_session += 1; last_clip_at = item["at"]
                                with db() as c:
                                    c.execute("UPDATE events SET clipped=1 WHERE id=(SELECT MAX(id) FROM events)")
                            except Exception as clip_error:
                                with state_lock: monitor_state["last_error"] = "Clip render: " + str(clip_error)[:160]
                scan = end
            except Exception as e:
                with state_lock: monitor_state["last_error"] = str(e)[:240]
                # Recoverable failures: move forward by one short interval instead of entering a tight loop.
                scan = end if 'end' in locals() and end > start else start + 12
                time.sleep(3)
            finally:
                if temp_dir: shutil.rmtree(temp_dir, ignore_errors=True)
            with state_lock: monitor_state["phase"] = "scanning"
    except Exception as e:
        with state_lock: monitor_state["last_error"] = str(e)[:240]
    finally:
        with state_lock:
            monitor_state["running"] = False
            if monitor_state.get("phase") not in ("completed",): monitor_state["phase"] = "stopped"


@app.route("/")
def home():
    return render_template_string(HTML)

@app.get("/api/health")
def health():
    missing = [x for x in ("ffmpeg", "ffprobe", "yt-dlp") if not shutil.which(x)]
    return jsonify(ok=not missing, missing=missing, clips_dir=str(CLIP_DIR), db=str(DB_PATH))

@app.get("/api/status")
def status():
    with state_lock: s = dict(monitor_state)
    s.pop("stop", None); s.pop("source", None)
    s["chat_messages"] = monitor_state.get("chat_message", "")
    return jsonify(s)

@app.get("/api/settings")
def settings_get():
    s = get_settings(); s["api_key_set"] = bool(s.get("api_key")); s["api_key"] = ""
    return jsonify(s)

@app.post("/api/settings")
def settings_post():
    payload = request.get_json(force=True, silent=True) or {}
    current = get_settings()
    if "api_key" not in payload: payload["api_key"] = current.get("api_key", "")
    saved = save_settings(payload); saved["api_key_set"] = bool(saved.get("api_key")); saved["api_key"] = ""
    return jsonify(ok=True, settings=saved)

@app.post("/api/test-api")
def test_api():
    data = request.get_json(force=True) or {}
    key = str(data.get("api_key") or get_settings().get("api_key") or "").strip()
    vid = None
    try: vid = parse_youtube_url(str(data.get("url", "")))[0] if data.get("url") else None
    except Exception: pass
    return jsonify(test_youtube_key(key, vid))

@app.post("/api/upload")
def upload():
    f = request.files.get("video")
    if not f or not f.filename: return jsonify(error="Choose a video file first."), 400
    name = secure_filename(f.filename)
    ext = Path(name).suffix.lower()
    if ext not in (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"):
        return jsonify(error="Supported formats: MP4, MKV, MOV, WEBM, M4V and AVI."), 400
    upload_id = str(int(time.time()*1000)) + "_" + re.sub(r"[^a-zA-Z0-9]", "", os.urandom(4).hex())
    path = UPLOAD_DIR / (upload_id + ext)
    f.save(path)
    with db() as c:
        c.execute("INSERT INTO uploads(id,path,name,created_at) VALUES(?,?,?,?)", (upload_id, str(path), name, datetime.now(timezone.utc).isoformat()))
    return jsonify(ok=True, upload_id=upload_id, name=name, size=path.stat().st_size)

@app.post("/api/monitor/start")
def monitor_start():
    global monitor_thread, chat_context
    data = request.get_json(force=True) or {}
    source_type = data.get("source_type", "youtube")
    live = False; duration = None; actual_start = None; video_id = None
    if source_type == "file":
        uid = str(data.get("upload_id", ""))
        with db() as c: row = c.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
        if not row or not Path(row["path"]).exists(): return jsonify(error="Uploaded file not found. Upload it again."), 404
        source, title = row["path"], Path(row["name"]).stem
        try: duration = ffprobe_info(source)
        except Exception as e: return jsonify(error="Could not read local video: " + str(e)), 400
    else:
        try: video_id, source = parse_youtube_url(str(data.get("url", "")))
        except Exception as e: return jsonify(error=str(e)), 400
        title = "YouTube " + video_id
        try:
            meta = json.loads(run_cmd(["yt-dlp", "--no-warnings", "-J", "--skip-download", source], timeout=60).stdout.decode("utf-8", "replace"))
            title = meta.get("title") or title
            live = bool(meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming"))
            duration = meta.get("duration")
            if live and meta.get("release_timestamp"):
                try: actual_start = datetime.fromtimestamp(float(meta["release_timestamp"]), tz=timezone.utc)
                except Exception: actual_start = None
        except Exception as e: return jsonify(error="Could not read this YouTube video: " + str(e)), 400
        api_key = get_settings().get("api_key", "")
        if live and api_key:
            try:
                info = youtube_video_info(video_id, api_key)
                if info.get("actual_start"):
                    actual_start = datetime.fromisoformat(info["actual_start"].replace("Z", "+00:00"))
                chat_context = {"chat_id": info.get("chat_id"), "page_token": None, "poll_after": 0,
                                "poll_ms": 5000, "actual_start": actual_start, "buckets": {}, "last_chat_error": ""}
                if not info.get("chat_id"):
                    with state_lock: monitor_state["chat_message"] = "No active live chat detected for this stream."
            except Exception as e:
                chat_context = {"chat_id": None, "page_token": None, "poll_after": 0, "poll_ms": 5000,
                                "actual_start": None, "buckets": {}, "last_chat_error": str(e)}
    with state_lock:
        if monitor_state.get("running"): return jsonify(error="A monitor is already running. Stop it before starting another."), 409
    chat_context.setdefault("buckets", {})
    with state_lock:
        monitor_state["chat_message"] = ("Chat scan not available for local/recorded video" if not live else
            ("Add a YouTube Data API v3 key to enable live chat peaks" if not get_settings().get("api_key") else "Connecting to live chat"))
    monitor_thread = threading.Thread(target=monitor_worker, args=(source, "file" if source_type == "file" else "youtube",
                                  title, video_id, live, duration, actual_start), daemon=True)
    monitor_thread.start()
    return jsonify(ok=True, title=title, live=live, duration=duration)

@app.post("/api/monitor/stop")
def monitor_stop():
    with state_lock: monitor_state["stop"] = True
    return jsonify(ok=True, message="Stop requested. The current segment will finish safely.")

@app.post("/api/clip/manual")
def manual_clip():
    data = request.get_json(force=True) or {}
    kind = data.get("source_type", "youtube")
    try:
        start = max(0, float(data.get("start", 0)))
        duration = max(1, min(180, float(data.get("duration", 30))))
    except Exception: return jsonify(error="Enter a valid start time and clip duration."), 400
    if kind == "file":
        with db() as c: row = c.execute("SELECT * FROM uploads WHERE id=?", (str(data.get("upload_id", "")),)).fetchone()
        if not row: return jsonify(error="Upload a local video first."), 400
        source, title, live = row["path"], Path(row["name"]).stem, False
    else:
        try: video_id, source = parse_youtube_url(str(data.get("url", "")))
        except Exception as e: return jsonify(error=str(e)), 400
        title = "YouTube " + video_id
        # Determine whether this is currently live for correct DVR handling.
        try:
            meta = json.loads(run_cmd(["yt-dlp", "--no-warnings", "-J", "--skip-download", source], timeout=50).stdout.decode("utf-8", "replace"))
            title = meta.get("title") or title; live = bool(meta.get("is_live"))
        except Exception: live = False
    try:
        clip_id, path = save_clip(source, "file" if kind == "file" else "youtube", start, start+duration,
                                  title, 0, ["manual clip"], "manual", live)
        return jsonify(ok=True, id=clip_id, filename=path.name)
    except Exception as e: return jsonify(error=str(e)[:260]), 500

@app.get("/api/clips")
def clips_list():
    with db() as c: rows = c.execute("SELECT * FROM clips ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r, exists=Path(r["file_path"]).exists(), size=Path(r["file_path"]).stat().st_size if Path(r["file_path"]).exists() else 0) for r in rows])

@app.get("/api/clips/<int:clip_id>/download")
def clip_download(clip_id):
    with db() as c: row = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row or not Path(row["file_path"]).exists(): return jsonify(error="Clip file not found."), 404
    return send_file(row["file_path"], mimetype="video/mp4", as_attachment=True, download_name=Path(row["file_path"]).name)

@app.delete("/api/clips/<int:clip_id>")
def clip_delete(clip_id):
    with db() as c: row = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row: return jsonify(error="Clip not found."), 404
    try: Path(row["file_path"]).unlink(missing_ok=True)
    except Exception: pass
    with db() as c: c.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    return jsonify(ok=True)

@app.get("/api/events")
def events_list():
    with db() as c: rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80").fetchall()
    return jsonify([dict(r) for r in rows])

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#24150f"><title>PeakCut Studio</title>
<style>
:root{--ink:#24150f;--ink2:#392117;--orange:#ff8b2b;--orange2:#ffad65;--cream:#faf7f3;--paper:#fff;--muted:#8a7d75;--line:#e9e1da;--soft:#f6f1ec;--red:#c94545;--green:#27845e;--shadow:0 16px 50px rgba(50,30,18,.08)}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer;border:0}.app{min-height:100vh;display:grid;grid-template-columns:250px minmax(0,1fr)}aside{background:var(--ink);color:#fff;padding:22px 16px;display:flex;flex-direction:column;gap:24px;min-height:100vh;position:sticky;top:0;height:100vh}.brand{display:flex;align-items:center;gap:11px;font-size:24px;font-weight:850;letter-spacing:-1px;padding:3px 5px}.brandmark{width:42px;height:42px;border-radius:13px;background:var(--orange);display:grid;place-items:center;color:#34180c;font-size:23px;font-weight:900;box-shadow:0 8px 20px #ff8b2b44}.brand span:last-child b{color:var(--orange)}.nav{display:grid;gap:7px}.nav button{background:transparent;color:#d7cbc3;padding:13px 14px;border-radius:12px;text-align:left;font-weight:650;display:flex;gap:12px;align-items:center}.nav button.active,.nav button:hover{background:#ff8b2b;color:#27150e}.nav .ico{width:22px;text-align:center;font-size:17px}.aside-foot{margin-top:auto;color:#b5a39a;font-size:12px;line-height:1.55;padding:8px}.main{min-width:0;padding:25px clamp(16px,3vw,44px) 50px;max-width:1550px;width:100%;margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:28px}.top h1{margin:0;font-size:28px;letter-spacing:-1px}.sub{color:var(--muted);margin-top:4px}.status-pill{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:999px;padding:9px 13px;color:#756960;font-size:12px;font-weight:750}.dot{width:8px;height:8px;border-radius:50%;background:#b7aaa0}.dot.green{background:#2eb77d;box-shadow:0 0 0 4px #2eb77d1f}.hero{text-align:center;padding:20px 10px 18px}.eyebrow{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;background:#fff;padding:7px 12px;color:#6f5d52;font-size:12px;font-weight:800;letter-spacing:.04em}.eyebrow b{background:var(--ink);color:#fff;padding:5px 9px;border-radius:999px}.hero h2{font-size:clamp(27px,4vw,42px);letter-spacing:-1.5px;margin:20px 0 8px}.hero p{font-size:16px;color:var(--muted);margin:0 auto;max-width:700px;line-height:1.55}.grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);gap:18px;align-items:start}.panel{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:var(--shadow)}.panel h3{margin:0 0 14px;font-size:17px;letter-spacing:-.3px}.urlbox{display:flex;align-items:center;gap:12px;border:1px solid var(--line);border-radius:16px;background:#fff;padding:5px 14px;margin:12px 0}.urlbox .link{font-size:20px;color:#9e8f84}.urlbox input{border:0;outline:0;min-width:0;flex:1;padding:14px 0;font-size:15px;color:var(--ink)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.btn{border-radius:999px;padding:12px 16px;font-weight:750;background:var(--soft);color:var(--ink);border:1px solid transparent}.btn.primary{background:var(--orange);color:#30170c;box-shadow:0 8px 18px #ff8b2b2b}.btn.dark{background:var(--ink);color:#fff}.btn.ghost{background:#fff;border-color:var(--line)}.btn.danger{background:#fff0ed;color:#a33327}.btn:disabled{opacity:.5;cursor:not-allowed}.small{font-size:12px;color:var(--muted);line-height:1.5}.source-line{font-size:12px;color:var(--muted);margin:10px 0}.file-label{display:inline-flex}.file-label input{display:none}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:16px;padding:15px}.stat .label{color:var(--muted);font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.06em}.stat .num{font-size:26px;font-weight:850;letter-spacing:-1px;margin-top:5px}.stat .hint{color:var(--muted);font-size:11px;margin-top:2px}.chart{height:128px;width:100%;display:flex;align-items:end;gap:5px;padding:10px 2px 0;border-bottom:1px solid var(--line)}.bar{background:linear-gradient(180deg,var(--orange2),var(--orange));border-radius:6px 6px 2px 2px;flex:1;min-width:3px;opacity:.85}.empty{padding:36px 12px;text-align:center;color:var(--muted)}.empty .play{width:64px;height:64px;border-radius:20px;background:var(--orange);display:grid;place-items:center;margin:0 auto 16px;color:#30170c;font-size:29px}.empty strong{display:block;color:var(--ink);font-size:18px;margin-bottom:8px}.clips{display:grid;gap:10px}.clip{display:grid;grid-template-columns:46px minmax(0,1fr) auto;align-items:center;gap:12px;padding:13px;border:1px solid var(--line);border-radius:15px}.clipicon{width:44px;height:44px;border-radius:13px;background:#fff0e2;display:grid;place-items:center;color:#a54c12;font-size:19px}.clip-title{font-weight:800;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.clip-meta{font-size:11px;color:var(--muted);margin-top:4px}.clip-actions{display:flex;gap:6px}.iconbtn{border:1px solid var(--line);background:#fff;border-radius:10px;padding:8px 10px;color:var(--ink)}.section{display:none}.section.active{display:block}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:grid;gap:7px}.field label{font-size:12px;font-weight:800;color:#69584e}.field input,.field select{width:100%;border:1px solid var(--line);background:#fff;border-radius:12px;padding:12px 13px;outline:none;color:var(--ink)}.field input:focus,.urlbox:focus-within{border-color:var(--orange);box-shadow:0 0 0 3px #ff8b2b22}.toggle{display:flex;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:13px;padding:12px 14px;gap:12px}.toggle input{accent-color:var(--orange);width:18px;height:18px}.message{border-radius:12px;padding:11px 13px;font-size:12px;margin:12px 0;display:none}.message.show{display:block}.message.error{background:#fff0ed;color:#a33327}.message.ok{background:#eaf7f0;color:#176344}.activity{display:grid;gap:8px}.activity-item{padding:11px 13px;border:1px solid var(--line);border-radius:13px;display:flex;justify-content:space-between;gap:12px;font-size:12px}.tag{padding:4px 8px;background:#fff0e2;color:#9b4a10;border-radius:999px;font-weight:800;white-space:nowrap}.footer-note{font-size:11px;color:#9c8e85;margin-top:16px;line-height:1.5}.menu-btn{display:none;border:1px solid var(--line);border-radius:10px;background:#fff;padding:8px 11px}.hide{display:none!important}
@media(max-width:900px){.app{grid-template-columns:1fr}aside{position:fixed;z-index:20;left:-270px;width:250px;transition:left .22s;height:100dvh}.app.menu-open aside{left:0}.main{padding:17px 14px 40px}.menu-btn{display:inline-flex}.top{margin-bottom:15px}.top h1{font-size:23px}.grid{grid-template-columns:1fr}.hero{padding:18px 2px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.panel{padding:16px}.clip{grid-template-columns:40px minmax(0,1fr) auto;padding:10px}.clipicon{width:40px;height:40px}.clip-actions .delete{display:none}}
@media(max-width:460px){.settings-grid{grid-template-columns:1fr}.clip-actions .download{font-size:11px;padding:8px}.hero h2{font-size:28px}.hero p{font-size:14px}.stats{gap:8px}.stat{padding:12px}.stat .num{font-size:23px}.row .btn{flex:1;justify-content:center}.status-pill{padding:8px 10px}}
</style></head><body><div class="app" id="app"><aside><div class="brand"><div class="brandmark">▶</div><span>Peak<b>Cut</b></span></div><nav class="nav"><button class="active" data-page="home"><span class="ico">⌂</span>Home</button><button data-page="clips"><span class="ico">▣</span>All clips</button><button data-page="analytics"><span class="ico">◫</span>Analytics</button><button data-page="settings"><span class="ico">⚙</span>Settings</button></nav><div class="aside-foot">PEAKCUT STUDIO<br>Phone-hosted clipping engine<br><span id="sideHealth">Checking engine…</span></div></aside><main class="main"><header class="top"><div style="display:flex;align-items:center;gap:12px"><button class="menu-btn" id="menuBtn">☰</button><div><h1 id="pageTitle">Home</h1><div class="sub" id="pageSub">Good evening, creator.</div></div></div><div class="status-pill"><span class="dot" id="statusDot"></span><span id="statusText">Engine idle</span></div></header>
<section class="section active" id="page-home"><div class="hero"><div class="eyebrow"><b>LIVE</b> REAL-TIME MOMENT DETECTION</div><h2>What do you want to clip today?</h2><p>Paste a YouTube link or upload a recording. PeakCut scans audio, vocal energy, visual motion and live chat spikes to find standout moments.</p></div>
<div class="grid"><div><div class="panel"><h3>Start a clipping session</h3><div class="urlbox"><span class="link">↗</span><input id="url" placeholder="Paste a YouTube video or live-stream link" autocomplete="off"></div><div class="row"><label class="btn file-label">⇧ Upload video<input type="file" id="file" accept="video/*"></label><button class="btn ghost" id="clearSource">Clear</button><button class="btn ghost" id="creatorBtn">◎ <span id="creatorLabel">No creator</span></button></div><div class="source-line" id="sourceLine">YouTube link or local video · MP4 output</div><div class="row" style="margin-top:16px"><button class="btn primary" id="startBtn" style="flex:1">▶ Start automatic clipping</button><button class="btn dark" id="stopBtn" disabled>Stop</button></div><div class="message" id="homeMsg"></div></div>
<div class="stats"><div class="stat"><div class="label">Segments scanned</div><div class="num" id="scanned">0</div><div class="hint">Analysis windows</div></div><div class="stat"><div class="label">Moments found</div><div class="num" id="detected">0</div><div class="hint">Above threshold</div></div><div class="stat"><div class="label">Clips saved</div><div class="num" id="saved">0</div><div class="hint">MP4 files</div></div><div class="stat"><div class="label">Scan position</div><div class="num" id="position">—</div><div class="hint" id="phase">Waiting to start</div></div></div>
<div class="panel"><h3>Detection activity</h3><div class="chart" id="chart"></div><div class="row" style="justify-content:space-between;margin-top:9px"><span class="small" id="scanInfo">No active session</span><span class="small" id="chatInfo">Chat signal inactive</span></div></div></div>
<div><div class="panel"><div class="row" style="justify-content:space-between"><h3 style="margin:0">Recent clips</h3><button class="btn ghost" id="viewAll">View all</button></div><div class="clips" id="recentClips" style="margin-top:13px"><div class="empty"><div class="play">▶</div><strong>No clips yet</strong>Your detected moments will appear here.</div></div></div><div class="panel" style="margin-top:16px"><h3>Manual clipping</h3><div class="settings-grid"><div class="field"><label>Start time (seconds)</label><input id="manualStart" type="number" min="0" value="0"></div><div class="field"><label>Clip length (seconds)</label><input id="manualDuration" type="number" min="1" max="180" value="30"></div></div><button class="btn dark" id="manualBtn" style="width:100%;margin-top:13px">✂ Create MP4 clip</button><div class="small" style="margin-top:8px">Use the currently entered YouTube link or uploaded local file.</div></div></div></div>
<div class="footer-note">For videos you own or have permission to edit. Live chat detection requires a YouTube Data API v3 key and an enabled public live chat.</div></section>
<section class="section" id="page-clips"><div class="panel"><div class="row" style="justify-content:space-between"><div><h3 style="margin:0">Your clip library</h3><div class="small">Saved locally on this phone. History survives app restarts.</div></div><button class="btn ghost" id="refreshClips">↻ Refresh</button></div><div class="clips" id="allClips" style="margin-top:15px"></div></div></section>
<section class="section" id="page-analytics"><div class="stats"><div class="stat"><div class="label">Segments scanned</div><div class="num" id="aScanned">0</div></div><div class="stat"><div class="label">Moments found</div><div class="num" id="aDetected">0</div></div><div class="stat"><div class="label">MP4 clips</div><div class="num" id="aSaved">0</div></div><div class="stat"><div class="label">Current phase</div><div class="num" style="font-size:16px" id="aPhase">Idle</div></div></div><div class="panel"><h3>Detection log</h3><div class="activity" id="events"><div class="empty">No detection events yet.</div></div></div></section>
<section class="section" id="page-settings"><div class="panel"><h3>API connections</h3><div class="field"><label>YouTube Data API v3 key</label><input id="apiKey" type="password" placeholder="Paste your API key here" autocomplete="off"></div><div class="row" style="margin-top:12px"><button class="btn primary" id="saveApi">Save API key</button><button class="btn ghost" id="testApi">Test connection</button><span class="small" id="apiState">Not configured</span></div><div class="small" style="margin-top:10px">The key is saved in this phone's local PeakCut database. It is used for live-chat metadata and message polling. Do not share this phone or its Termux data with others.</div></div>
<div class="panel" style="margin-top:16px"><h3>Clip behaviour</h3><div class="settings-grid"><div class="field"><label>Minimum moment score</label><input id="threshold" type="number" min="0.5" max="10" step="0.1"></div><div class="field"><label>Maximum clips per session</label><input id="clip_limit" type="number" min="1" max="100"></div><div class="field"><label>Cooldown between clips (sec)</label><input id="cooldown" type="number" min="1" max="300"></div><div class="field"><label>Pre-roll (sec)</label><input id="pre_roll" type="number" min="0" max="120"></div><div class="field"><label>Post-roll (sec)</label><input id="post_roll" type="number" min="5" max="180"></div><div class="field"><label>Maximum clip length (sec)</label><input id="max_clip_seconds" type="number" min="5" max="180"></div><div class="field"><label>Scan window (sec)</label><input id="scan_seconds" type="number" min="10" max="60"></div><div class="field"><label>Creator label</label><input id="creator" type="text" maxlength="60" placeholder="No creator"></div></div><div class="toggle" style="margin-top:14px"><div><b>Automatic clipping</b><div class="small">Render MP4 files when strong moments are detected.</div></div><input type="checkbox" id="auto_clip"></div><h3 style="margin-top:20px">Detection weights</h3><div class="settings-grid"><div class="field"><label>Audio peaks</label><input id="weight_audio" type="number" min="0" max="5" step="0.1"></div><div class="field"><label>Vocal energy</label><input id="weight_voice" type="number" min="0" max="5" step="0.1"></div><div class="field"><label>Visual motion</label><input id="weight_motion" type="number" min="0" max="5" step="0.1"></div><div class="field"><label>Chat spikes</label><input id="weight_chat" type="number" min="0" max="5" step="0.1"></div></div><button class="btn primary" id="saveSettings" style="margin-top:16px">Save clipping settings</button><div class="message" id="settingsMsg"></div></div>
<div class="panel" style="margin-top:16px"><h3>Phone engine status</h3><div class="small" id="engineStatus">Checking requirements…</div><div class="small" style="margin-top:9px">The engine runs inside Termux. Keep Termux's ongoing notification enabled and exclude Termux from battery optimization for the best chance of uninterrupted scanning. Android can still terminate apps under memory pressure or after a force-stop.</div></div></section>
</main></div>
<script>
let uploadId=null,uploadName='',chartValues=[],lastStatus={},currentPage='home';
const $=id=>document.getElementById(id);const fmt=s=>{s=Math.max(0,Math.floor(Number(s)||0));return `${Math.floor(s/3600)?Math.floor(s/3600)+':':''}${String(Math.floor(s%3600/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`};
function msg(id,text,type='ok'){let e=$(id);e.textContent=text;e.className='message show '+type;setTimeout(()=>e.classList.remove('show'),6500)}
async function api(url,options={}){let r=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});let d=await r.json().catch(()=>({error:'Unexpected server response'}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function nav(page){currentPage=page;document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));$('page-'+page).classList.add('active');document.querySelectorAll('.nav button').forEach(x=>x.classList.toggle('active',x.dataset.page===page));$('pageTitle').textContent={home:'Home',clips:'All clips',analytics:'Analytics',settings:'Settings'}[page];$('pageSub').textContent={home:'Good evening, creator.',clips:'Your saved MP4 library.',analytics:'Understand what PeakCut is detecting.',settings:'Tune the engine and API connections.'}[page];$('app').classList.remove('menu-open');if(page==='clips')loadClips();if(page==='settings')loadSettings();if(page==='analytics')loadEvents()}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>nav(b.dataset.page));$('menuBtn').onclick=()=>$('app').classList.toggle('menu-open');$('viewAll').onclick=()=>nav('clips');$('creatorBtn').onclick=()=>{let n=prompt('Creator label', $('creatorLabel').textContent);if(n!==null){$('creatorLabel').textContent=n.trim()||'No creator';$('creator').value=n.trim()||'No creator';saveSettings(false)}};
$('file').onchange=async e=>{let f=e.target.files[0];if(!f)return;$('sourceLine').textContent='Uploading '+f.name+'…';let fd=new FormData();fd.append('video',f);try{let r=await fetch('/api/upload',{method:'POST',body:fd});let d=await r.json();if(!r.ok)throw Error(d.error);uploadId=d.upload_id;uploadName=d.name;$('sourceLine').textContent='Local video ready · '+f.name+' · '+(f.size/1048576).toFixed(1)+' MB';$('url').value='';msg('homeMsg','Local video uploaded. Ready to scan.');}catch(err){msg('homeMsg',err.message,'error')}};
$('clearSource').onclick=()=>{uploadId=null;uploadName='';$('file').value='';$('url').value='';$('sourceLine').textContent='YouTube link or local video · MP4 output'};
function getSource(){if(uploadId)return {source_type:'file',upload_id:uploadId};let url=$('url').value.trim();if(!url)throw Error('Paste a YouTube URL or upload a video first.');return {source_type:'youtube',url}}
$('startBtn').onclick=async()=>{try{let src=getSource();$('startBtn').disabled=true;let d=await api('/api/monitor/start',{method:'POST',body:JSON.stringify(src)});msg('homeMsg',(d.live?'Live stream monitoring started.':'Recorded-video scanning started.')+' '+d.title);refreshStatus()}catch(e){$('startBtn').disabled=false;msg('homeMsg',e.message,'error')}};
$('stopBtn').onclick=async()=>{try{await api('/api/monitor/stop',{method:'POST',body:'{}'});msg('homeMsg','Stop requested. The current analysis segment will finish first.')}catch(e){msg('homeMsg',e.message,'error')}};
$('manualBtn').onclick=async()=>{try{let src=getSource();let d=await api('/api/clip/manual',{method:'POST',body:JSON.stringify({...src,start:$('manualStart').value,duration:$('manualDuration').value})});msg('homeMsg','MP4 created: '+d.filename);loadClips();}catch(e){msg('homeMsg',e.message,'error')}};
function renderChart(){let c=$('chart');c.innerHTML='';chartValues.forEach(v=>{let b=document.createElement('div');b.className='bar';b.style.height=(Math.max(4,Math.min(100,v*17)))+'%';c.appendChild(b)})}
async function refreshStatus(){try{let s=await api('/api/status');lastStatus=s;$('statusDot').classList.toggle('green',s.running);$('statusText').textContent=s.running?(s.is_live?'Monitoring live':'Scanning video'):(s.phase==='completed'?'Scan complete':'Engine idle');$('startBtn').disabled=!!s.running;$('stopBtn').disabled=!s.running;$('scanned').textContent=s.segments_scanned||0;$('detected').textContent=s.detections||0;$('saved').textContent=s.clips_created||0;$('position').textContent=s.running?fmt(s.position):'—';$('phase').textContent=s.phase||'Waiting';$('scanInfo').textContent=s.title?(s.title+' · '+(s.last_scan||s.phase)):'No active session';$('chatInfo').textContent=s.chat_messages||'Chat signal inactive';$('aScanned').textContent=s.segments_scanned||0;$('aDetected').textContent=s.detections||0;$('aSaved').textContent=s.clips_created||0;$('aPhase').textContent=s.phase||'Idle';if(s.last_error)$('scanInfo').textContent='Engine note: '+s.last_error;}catch(e){$('statusText').textContent='Engine unavailable';$('statusDot').classList.remove('green')}}
async function loadClips(){try{let rows=await api('/api/clips');renderClips(rows,'allClips');renderClips(rows.slice(0,4),'recentClips')}catch(e){$('allClips').textContent=e.message}}
function renderClips(rows,target){let el=$(target);el.innerHTML='';if(!rows.length){el.innerHTML='<div class="empty"><div class="play">▶</div><strong>No clips yet</strong>Your clips will be stored here after rendering.</div>';return}rows.forEach(r=>{let item=document.createElement('div');item.className='clip';let meta=`${fmt(r.start_sec)} – ${fmt(r.end_sec)} · ${new Date(r.created_at).toLocaleString()} · ${r.kind}`;item.innerHTML=`<div class="clipicon">▶</div><div style="min-width:0"><div class="clip-title"></div><div class="clip-meta"></div><div class="clip-meta"></div></div><div class="clip-actions"><button class="iconbtn download">↓ MP4</button><button class="iconbtn delete">×</button></div>`;item.querySelector('.clip-title').textContent=r.title;item.querySelectorAll('.clip-meta')[0].textContent=meta;item.querySelectorAll('.clip-meta')[1].textContent=(JSON.parse(r.reasons||'[]').join(' · ')||'Manual clip')+' · '+(r.size?((r.size/1048576).toFixed(1)+' MB'):'File missing');item.querySelector('.download').onclick=()=>{location.href='/api/clips/'+r.id+'/download'};item.querySelector('.delete').onclick=async()=>{if(confirm('Delete this saved clip?')){await fetch('/api/clips/'+r.id,{method:'DELETE'});loadClips()}};el.appendChild(item)})}
$('refreshClips').onclick=loadClips;
async function loadEvents(){try{let rows=await api('/api/events');let el=$('events');el.innerHTML='';chartValues=rows.slice(0,34).reverse().map(r=>Math.max(.2,Number(r.score)||.2));renderChart();if(!rows.length){el.innerHTML='<div class="empty">No detection events yet.</div>';return}rows.forEach(r=>{let d=document.createElement('div');d.className='activity-item';let reason=JSON.parse(r.reasons||'[]').join(' · ');d.innerHTML='<div><b></b><div class="small"></div></div><span class="tag"></span>';d.querySelector('b').textContent=fmt(r.at_sec)+' · '+(reason||'Moment');d.querySelector('.small').textContent=r.source_name+' · '+new Date(r.created_at).toLocaleString();d.querySelector('.tag').textContent='Score '+r.score;el.appendChild(d)})}catch(e){}}
async function loadSettings(){try{let s=await api('/api/settings');['threshold','clip_limit','cooldown','pre_roll','post_roll','max_clip_seconds','scan_seconds','weight_audio','weight_voice','weight_motion','weight_chat','creator'].forEach(k=>$(k).value=s[k]);$('auto_clip').checked=s.auto_clip;$('creatorLabel').textContent=s.creator||'No creator';$('apiState').textContent=s.api_key_set?'API key saved on this phone':'Not configured';$('apiKey').placeholder=s.api_key_set?'Saved — enter a new key to replace it':'Paste your API key here';}catch(e){}}
async function saveSettings(show=true){let body={};['threshold','clip_limit','cooldown','pre_roll','post_roll','max_clip_seconds','scan_seconds','weight_audio','weight_voice','weight_motion','weight_chat','creator'].forEach(k=>body[k]=$(k).value);body.auto_clip=$('auto_clip').checked;try{await api('/api/settings',{method:'POST',body:JSON.stringify(body)});if(show)msg('settingsMsg','Clipping settings saved.')}catch(e){if(show)msg('settingsMsg',e.message,'error')}}
$('saveSettings').onclick=()=>saveSettings(true);$('saveApi').onclick=async()=>{try{let key=$('apiKey').value.trim();if(!key)throw Error('Paste your API key before saving.');await api('/api/settings',{method:'POST',body:JSON.stringify({api_key:key})});$('apiKey').value='';$('apiState').textContent='API key saved on this phone';$('apiKey').placeholder='Saved — enter a new key to replace it';msg('settingsMsg','YouTube API key saved locally.')}catch(e){msg('settingsMsg',e.message,'error')}};
$('testApi').onclick=async()=>{try{$('apiState').textContent='Testing…';let d=await api('/api/test-api',{method:'POST',body:JSON.stringify({api_key:$('apiKey').value.trim(),url:$('url').value.trim()})});$('apiState').textContent=d.message;if(!d.ok)msg('settingsMsg',d.message,'error');else msg('settingsMsg',d.message)}catch(e){$('apiState').textContent=e.message}};
async function health(){try{let h=await api('/api/health');$('sideHealth').textContent=h.ok?'Engine ready':'Missing: '+h.missing.join(', ');$('engineStatus').textContent=h.ok?'All required tools detected. Clip output: '+h.clips_dir:'Missing required tools: '+h.missing.join(', ')+'. Install them in Termux first.'}catch(e){$('sideHealth').textContent='Engine offline';$('engineStatus').textContent='Local server is not responding.'}}
health();loadClips();loadSettings();loadEvents();refreshStatus();setInterval(refreshStatus,2500);setInterval(()=>{if(currentPage==='clips')loadClips();if(currentPage==='analytics'||currentPage==='home')loadEvents()},12000);
</script></body></html>'''

if __name__ == "__main__":
    print("\nPeakCut Studio is starting…")
    print(f"Open http://127.0.0.1:{PORT} in your phone browser")
    print(f"MP4 clips will be saved to: {CLIP_DIR}")
    print("Keep this Termux session running while monitoring.\n")
    try:
        subprocess.run(["termux-wake-lock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception: pass
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
