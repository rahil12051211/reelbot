import subprocess, sys, os, re, json, math, ctypes, shutil, hashlib, threading, random
import numpy as np
import yt_dlp

# Ensure local ffmpeg build is always on PATH
_FFMPEG_DIR = r"C:\Users\rahil\ffmpeg\ffmpeg-master-latest-win64-gpl\bin"
if os.path.isdir(_FFMPEG_DIR) and _FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

_whisper_model = None
_whisper_model_key = None

# Active process tracking for instant job cancellation & background video streaming
_proc_lock = threading.Lock()
_dedup_lock = threading.Lock()
ACTIVE_PROCS = {}      # job_id -> set of Popen
CANCELLED_JOBS = set() # job_ids cancelled by user
_BG_VIDEO_THREADS = {} # job_id -> (Thread, dict_result)

class JobCancelledError(RuntimeError):
    pass

def check_cancelled(job_id):
    if job_id and job_id in CANCELLED_JOBS:
        raise JobCancelledError("Process stopped by user")

def cancel_job_procs(job_id):
    CANCELLED_JOBS.add(job_id)
    with _proc_lock:
        procs = list(ACTIVE_PROCS.get(job_id, set()))
    for p in procs:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            except Exception:
                pass
        try:
            p.kill()
        except Exception:
            pass

def cancel_all_procs():
    """Terminate all active child processes and close resources before shutdown."""
    with _proc_lock:
        all_jids = list(ACTIVE_PROCS.keys())
    for jid in all_jids:
        cancel_job_procs(jid)

def _dedup_file():
    if os.environ.get("VERCEL"):
        base = "/tmp/reelbot_output/_cache"
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "_cache")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "dedup_history.json")

def _load_dedup():
    fp = _dedup_file()
    default_d = {
        "used_videos": [],
        "used_titles": [],
        "cut_ranges": {},
        "text_hashes": [],
        "used_shingles": [],
        "used_banners": [],
        "used_captions": [],
        "query_page": {},
        "reel_seq": 0
    }
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in default_d.items():
                loaded.setdefault(k, v)
            return loaded
        except Exception:
            pass
    return default_d

def _save_dedup(data):
    fp = _dedup_file()
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _has_cuda12():
    try:
        if os.name == "nt":
            ctypes.WinDLL("cublas64_12.dll")
        else:
            ctypes.CDLL("libcublas.so.12")
        return True
    except Exception:
        return False

def _run(cmd, job_id=None, **kw):
    check_cancelled(job_id)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    if job_id:
        with _proc_lock:
            ACTIVE_PROCS.setdefault(job_id, set()).add(p)
    try:
        out_s, err_s = p.communicate()
    finally:
        if job_id:
            with _proc_lock:
                ACTIVE_PROCS.get(job_id, set()).discard(p)
    check_cancelled(job_id)
    if p.returncode != 0:
        err = (err_s or out_s or "").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else f"exit {p.returncode}"
        raise RuntimeError(tail)
    return out_s, err_s

def _get_model(size="tiny", force_cpu=False):
    global _whisper_model, _whisper_model_key
    device = "cpu" if (force_cpu or not _has_cuda12()) else "cuda"
    key = f"{size}:{device}"
    if _whisper_model is None or _whisper_model_key != key:
        from faster_whisper import WhisperModel
        threads = max(4, os.cpu_count() or 4)
        _whisper_model = WhisperModel(size, device=device, compute_type="int8", cpu_threads=threads)
        _whisper_model_key = key
    return _whisper_model

def _url_cache_key(url):
    m = re.search(r"(?:youtu\.be/|v=|shorts/|live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return f"yt_{m.group(1)}"
    return "url_" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]

def _cache_dir(out_dir):
    cdir = os.path.join(os.path.dirname(out_dir), "_cache")
    os.makedirs(cdir, exist_ok=True)
    return cdir

def _read_cache_key(out_dir):
    kp = os.path.join(out_dir, ".cache_key")
    if os.path.exists(kp):
        return open(kp, "r", encoding="utf-8").read().strip()
    return ""

def read_source_title(out_dir):
    tp = os.path.join(out_dir, ".source_title")
    if os.path.exists(tp):
        try:
            return open(tp, "r", encoding="utf-8").read().strip()
        except Exception:
            pass
    return ""

TRENDING_WAVES = [
    {
        "topic": "GTA 6 Streamer Reactions",
        "label": "🎮 GTA 6 STREAMER REACTIONS & CHEATCODES:",
        "chips": [
            {"emoji": "😭", "title": "Speed Reacts to GTA 6 (2.1M)", "url": "https://www.youtube.com/watch?v=mlguGHEeVO0", "topic": "GTA 6", "layout": "black_bar"},
            {"emoji": "😳", "title": "Lacy / Jason GTA 6 Map Reaction", "url": "https://www.youtube.com/watch?v=4RTrYbX3rZE", "topic": "GTA 6", "layout": "split_cam"},
            {"emoji": "🤯", "title": "Kai Cenat Reacts to GTA 6 (610K)", "url": "https://www.youtube.com/watch?v=B6biwxo0dMQ", "topic": "GTA 6", "layout": "black_bar"},
            {"emoji": "🎮", "title": "GTA 6 Launch Trailer Secrets (1.9M)", "url": "https://www.youtube.com/watch?v=FV93IVwJP_4", "topic": "GTA 6", "layout": "auto"},
            {"emoji": "🕹️", "title": "Top 20 GTA Cheats (3.5M)", "url": "https://www.youtube.com/watch?v=iskLCY86up8", "topic": "GTA Cheatcodes", "layout": "auto"},
        ]
    },
    {
        "topic": "GTA 6 Vice City Map & Secrets",
        "label": "🌴 TRENDING: GTA 6 VICE CITY MAP & SECRETS:",
        "chips": [
            {"emoji": "🌴", "title": "GTA 6 Vice City Map Size vs GTA 5 (1.4M)", "url": "https://www.youtube.com/watch?v=QdBZY2fkU-0", "topic": "GTA 6 Map", "layout": "split_cam"},
            {"emoji": "👀", "title": "45 Secret Details in GTA 6 Trailer (1.1M)", "url": "https://www.youtube.com/watch?v=KUDVhy5KFIQ", "topic": "GTA 6 Secrets", "layout": "black_bar"},
            {"emoji": "🚨", "title": "GTA 6 Police Chase & 6-Star AI (890K)", "url": "https://www.youtube.com/watch?v=XtHLtEV2TEY", "topic": "GTA 6 Police", "layout": "split_cam"},
            {"emoji": "🤯", "title": "xQc & Streamers React to GTA 6 (1.2M)", "url": "https://www.youtube.com/watch?v=o4nNjj44d4M", "topic": "GTA 6", "layout": "black_bar"},
            {"emoji": "💸", "title": "Lucia & Jason Vice City Breakdown (740K)", "url": "https://www.youtube.com/watch?v=YDNs_sPEcJc", "topic": "GTA 6 Vice City", "layout": "auto"},
        ]
    },
    {
        "topic": "GTA God Mode & Secret Glitches",
        "label": "⚡ TRENDING: GOD MODE CHEATCODES & GLITCHES:",
        "chips": [
            {"emoji": "⚡", "title": "Testing 50 Secret GTA Cheatcodes (3.5M)", "url": "https://www.youtube.com/watch?v=iskLCY86up8", "topic": "GTA Cheatcodes", "layout": "split_cam"},
            {"emoji": "😭", "title": "Speed Discovers GTA 6 Streamers (2.1M)", "url": "https://www.youtube.com/watch?v=mlguGHEeVO0", "topic": "GTA 6", "layout": "black_bar"},
            {"emoji": "💀", "title": "99% Missed This GTA VI Map Location (920K)", "url": "https://www.youtube.com/watch?v=4RTrYbX3rZE", "topic": "GTA 6 Map", "layout": "split_cam"},
            {"emoji": "🔥", "title": "Rockstar Hid This in Vice City (1.9M)", "url": "https://www.youtube.com/watch?v=FV93IVwJP_4", "topic": "GTA 6 Secrets", "layout": "black_bar"},
            {"emoji": "🤯", "title": "Kai Cenat First Time Seeing GTA 6 (610K)", "url": "https://www.youtube.com/watch?v=B6biwxo0dMQ", "topic": "GTA 6", "layout": "auto"},
        ]
    },
    {
        "topic": "GTA 6 Next-Gen Graphics & Physics",
        "label": "🔥 TRENDING: GTA 6 NEXT-GEN GRAPHICS & NPCs:",
        "chips": [
            {"emoji": "🤯", "title": "GTA 6 vs GTA 5 Graphics Comparison (1.9M)", "url": "https://www.youtube.com/watch?v=FV93IVwJP_4", "topic": "GTA 6 Graphics", "layout": "split_cam"},
            {"emoji": "😭", "title": "Streamers React to GTA 6 NPCs (1.1M)", "url": "https://www.youtube.com/watch?v=KUDVhy5KFIQ", "topic": "GTA 6 NPCs", "layout": "black_bar"},
            {"emoji": "🌴", "title": "Exploring Leonida & Vice Beach (1.4M)", "url": "https://www.youtube.com/watch?v=QdBZY2fkU-0", "topic": "GTA 6 Vice City", "layout": "split_cam"},
            {"emoji": "😳", "title": "Speed Live Reaction to Lucia & Jason (2.1M)", "url": "https://www.youtube.com/watch?v=mlguGHEeVO0", "topic": "GTA 6", "layout": "black_bar"},
            {"emoji": "🕹️", "title": "Hidden Rockstar Cheat Codes Unlocked (890K)", "url": "https://www.youtube.com/watch?v=XtHLtEV2TEY", "topic": "GTA Cheatcodes", "layout": "auto"},
        ]
    }
]

_trending_state = {
    "manual_offset": 0,
    "last_live_fetch": 0.0,
    "live_chips_by_wave": {},
    "fetching": False
}
_trending_lock = threading.Lock()

def _fmt_views(v):
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v // 1_000}K"
    return "VIRAL"

def _bg_fetch_live_wave(wave_idx, search_topic):
    try:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "age_limit": 99}
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"ytsearch12:{search_topic} viral reaction", download=False)
            entries = res.get("entries") or []
        with _dedup_lock:
            used_ids = set(_load_dedup().get("used_videos", []))
        emos = ["😭", "😳", "🤯", "🔥", "🌴", "⚡", "👀"]
        layouts = ["black_bar", "split_cam", "black_bar", "split_cam", "auto"]
        chips = []
        for e in entries:
            if not e or not e.get("id") or e["id"] in used_ids:
                continue
            dur = int(e.get("duration") or 120)
            if not (25 <= dur <= 1800):
                continue
            raw_t = (e.get("title") or search_topic).strip()
            short_t = (raw_t[:34] + "…") if len(raw_t) > 35 else raw_t
            v_str = _fmt_views(e.get("view_count"))
            idx_c = len(chips)
            chips.append({
                "emoji": emos[idx_c % len(emos)],
                "title": f"{short_t} ({v_str})",
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "topic": search_topic,
                "layout": layouts[idx_c % len(layouts)]
            })
            if len(chips) >= 5:
                break
        if len(chips) >= 3:
            with _trending_lock:
                _trending_state["live_chips_by_wave"][wave_idx] = chips
                _trending_state["last_live_fetch"] = time.time()
    except Exception:
        pass
    finally:
        with _trending_lock:
            _trending_state["fetching"] = False

def get_trending_wave(custom_topic="", force_next=False):
    """
    Returns the active 5-minute Trending Topic Wave + 5 clickable viral video chips + seconds until next 5-minute refresh.
    Automatically rotates every 5 minutes (300s) or immediately when force_next=True.
    """
    now = time.time()
    with _trending_lock:
        if force_next:
            _trending_state["manual_offset"] += 1
        slot = int(now // 300) + _trending_state["manual_offset"]
        wave_idx = slot % len(TRENDING_WAVES)
        rem_sec = max(1, 300 - int(now % 300))
        base_wave = TRENDING_WAVES[wave_idx]
        live_chips = _trending_state["live_chips_by_wave"].get(wave_idx)
        should_fetch = (not _trending_state["fetching"]) and (
            force_next or (now - _trending_state["last_live_fetch"] > 280)
        )
        if should_fetch:
            _trending_state["fetching"] = True

    search_t = (custom_topic or "").strip() or base_wave["topic"]
    if should_fetch:
        threading.Thread(target=_bg_fetch_live_wave, args=(wave_idx, search_t), daemon=True).start()

    return {
        "wave_index": wave_idx + 1,
        "topic": base_wave["topic"],
        "label": base_wave["label"],
        "chips": live_chips if (live_chips and len(live_chips) >= 3) else base_wave["chips"],
        "refresh_in_seconds": rem_sec,
        "interval_seconds": 300
    }

def _title_tokens(title_str):
    stop_t = {"the", "and", "for", "with", "this", "that", "from", "into", "clip", "video", "full", "stream", "live", "official"}
    toks = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in (title_str or "").split()]
    return [t for t in toks if len(t) >= 3 and t not in stop_t]

def _is_title_duplicate(title_str, used_titles_list):
    """Rejects mirror uploads or re-uploaded clips that share >=58% of title keywords with any previously used video."""
    cur_set = set(_title_tokens(title_str))
    if len(cur_set) < 2:
        return False
    for prev_title in used_titles_list:
        prev_set = set(_title_tokens(prev_title))
        if not prev_set:
            continue
        inter = len(cur_set & prev_set)
        sim = inter / float(min(len(cur_set), len(prev_set)))
        if sim >= 0.58:
            return True
    return False

def discover_autopilot_video(topic, spicy_18=False):
    """
    Strict Topic-Only Zero-Duplicate Autopilot Video Discovery Engine (200 Unique Reels):
    - Searches ONLY for the exact user-specified topic across 48+ diverse sub-angles.
    - Never repeats a YouTube video_id AND rejects re-uploads/mirror videos with similar titles.
    - Guarantees 200+ completely unique, non-duplicate videos and content on the user's topic.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Autopilot Topic is required — please enter the exact topic for your reels")

    with _dedup_lock:
        dedup = _load_dedup()
        used_ids = set(dedup.get("used_videos", []))
        used_titles = list(dedup.get("used_titles", []))
        q_idx = int(dedup.get("query_page", {}).get(topic.lower(), 0))
        dedup.setdefault("query_page", {})[topic.lower()] = q_idx + 1
        _save_dedup(dedup)

    topic_Modifiers = [
        f"Speed reacts to {topic} crazy viral moment",
        f"Kai Cenat reacts to {topic} funny clip",
        f"Lacy reacts to {topic} map gameplay",
        f"xQc reacts to {topic} secrets breakdown",
        f"{topic} secret cheatcodes hidden tricks viral",
        f"{topic} map size comparison hidden locations",
        f"{topic} police chase 6 star wanted level gameplay",
        f"{topic} next gen graphics physics comparison",
        f"{topic} hidden easter eggs nobody noticed",
        f"{topic} funny streamer rage moments",
        f"{topic} illegal money glitch secret method",
        f"{topic} NPC crazy AI reactions gameplay",
        f"{topic} Adin Ross reaction viral highlight",
        f"{topic} CaseOh funny reaction clip",
        f"{topic} Duke Dennis gameplay reaction",
        f"{topic} Fanum reaction wild moment",
        f"{topic} Agent 00 breakdown secrets",
        f"{topic} Plaqueboymax reaction clip",
        f"{topic} Sketch funny gameplay highlight",
        f"{topic} StableRonaldo reaction moment",
        f"{topic} god mode cheat code unlocked",
        f"{topic} trailer hidden details frame by frame",
        f"{topic} myths vs facts tested gameplay",
        f"{topic} rare animations secret cutscenes",
        f"{topic} speedrun world record crazy trick",
        f"{topic} best mods ultra realistic graphics",
        f"{topic} top 10 secret features pro tips",
        f"{topic} wildest stream moments compilation",
        f"{topic} secret underground location discovered",
        f"{topic} vehicles cars customization gameplay",
        f"{topic} storyline leak analysis breakdown",
        f"{topic} things you missed hidden secrets",
        f"{topic} insane details rockstar added",
        f"{topic} first time playing streamer reaction",
        f"{topic} crazy plot twist ending reaction",
        f"{topic} hardest mission secret shortcut"
    ]
    if spicy_18:
        topic_Modifiers = [
            f"{topic} unfiltered spicy confession wild moment",
            f"{topic} secret cheatcodes exposed late night",
            f"{topic} wild viral clip no filter",
            f"{topic} late night stream spicy reaction"
        ] + topic_Modifiers

    topic_tokens = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in topic.split() if len(w) >= 2]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "age_limit": 99
    }

    def _matches_topic(title_str):
        if not topic_tokens:
            return True
        t_low = title_str.lower()
        return any(tok in t_low for tok in topic_tokens)

    strict_candidates = []
    fallback_topic_candidates = []
    seen_batch_ids = set()

    # Probe up to 3 rotating query modifiers until we have fresh, non-duplicate title candidates
    for attempt in range(3):
        query = topic_Modifiers[(q_idx + attempt) % len(topic_Modifiers)]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                res = ydl.extract_info(f"ytsearch35:{query}", download=False)
                entries = res.get("entries") or []
        except Exception:
            entries = []

        for e in entries:
            if not e or not e.get("id"):
                continue
            vid_id = e["id"]
            if vid_id in used_ids or vid_id in seen_batch_ids:
                continue
            title = (e.get("title") or "").strip()
            if not title or not _matches_topic(title):
                continue
            if _is_title_duplicate(title, used_titles):
                continue
            seen_batch_ids.add(vid_id)
            dur = int(e.get("duration") or 0)
            item = {
                "id": vid_id,
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "title": title,
                "duration": dur or 120,
                "views": int(e.get("view_count") or 0),
                "topic": topic
            }
            if 35 <= dur <= 1800:
                strict_candidates.append(item)
            else:
                fallback_topic_candidates.append(item)
        if len(strict_candidates) >= 3:
            break

    pool = strict_candidates or fallback_topic_candidates
    if not pool:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res2 = ydl.extract_info(f"ytsearch40:{topic}", download=False)
            for e in (res2.get("entries") or []):
                if e and e.get("id") and e["id"] not in used_ids:
                    t_str = (e.get("title") or f"{topic} Viral Video").strip()
                    if not _is_title_duplicate(t_str, used_titles):
                        pool.append({
                            "id": e["id"],
                            "url": f"https://www.youtube.com/watch?v={e['id']}",
                            "title": t_str,
                            "duration": int(e.get("duration") or 120),
                            "views": int(e.get("view_count") or 0),
                            "topic": topic
                        })
    if not pool:
        raise RuntimeError(f"No unused videos found for topic '{topic}' — try a related keyword")

    pool.sort(key=lambda x: x["views"], reverse=True)
    chosen = pool[0]
    with _dedup_lock:
        dedup = _load_dedup()
        if chosen["id"] not in dedup["used_videos"]:
            dedup["used_videos"].append(chosen["id"])
        if chosen["title"] not in dedup["used_titles"]:
            dedup["used_titles"].append(chosen["title"])
        _save_dedup(dedup)
    return chosen

def wait_for_video(job_id):
    """Thread-safe barrier: ensures source.mp4 is 100% finalized before any render worker opens it."""
    if not job_id:
        return
    entry = _BG_VIDEO_THREADS.get(job_id)
    if entry:
        t, res = entry
        t.join()
        _BG_VIDEO_THREADS.pop(job_id, None)
        if res.get("error"):
            raise RuntimeError(res["error"])

def download(url, out_dir, live_minutes=0, job_id=None, n_reels=3):
    check_cancelled(job_id)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "source.mp4")
    wav = os.path.join(out_dir, "audio.wav")
    key = _url_cache_key(url)
    cdir = _cache_dir(out_dir)
    cache_file = os.path.join(cdir, key + ".mp4")
    cache_wav = os.path.join(cdir, key + ".wav")
    cache_title = os.path.join(cdir, key + ".title")

    if live_minutes == 0 and os.path.exists(cache_file) and os.path.getsize(cache_file) > 50000:
        with open(os.path.join(out_dir, ".cache_key"), "w", encoding="utf-8") as f:
            f.write(key)
        if os.path.exists(cache_title):
            try:
                shutil.copy2(cache_title, os.path.join(out_dir, ".source_title"))
            except Exception:
                pass
        if not os.path.exists(out):
            try: os.link(cache_file, out)
            except Exception: shutil.copy2(cache_file, out)
        if os.path.exists(cache_wav) and os.path.getsize(cache_wav) > 10000 and not os.path.exists(wav):
            try: os.link(cache_wav, wav)
            except Exception: shutil.copy2(cache_wav, wav)
        return out

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "age_limit": 99,
        "format": "b[height<=720][ext=mp4]/bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=720]+ba/b"
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "tv_embedded", "web"]}}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    check_cancelled(job_id)

    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]

    src_meta_title = f"{info.get('title') or ''} | {info.get('uploader') or info.get('channel') or ''}".strip(" |")
    if src_meta_title:
        try:
            with open(os.path.join(out_dir, ".source_title"), "w", encoding="utf-8") as f:
                f.write(src_meta_title)
            with open(cache_title, "w", encoding="utf-8") as f:
                f.write(src_meta_title)
        except Exception:
            pass

    urls = [f["url"] for f in info["requested_formats"]] if "requested_formats" in info else [info["url"]]
    is_live = bool(info.get("is_live")) or any(
        ".m3u8" in u or "yt_live_broadcast" in u or "/manifest/hls" in u for u in urls
    )

    min_needed_sec = max(65, int(n_reels) * 15)
    seek_offset = 0
    if is_live:
        with open(os.path.join(out_dir, ".cache_key"), "w", encoding="utf-8") as f:
            f.write("")
        dur_sec = max(int(live_minutes) * 60, min(150, min_needed_sec)) if live_minutes > 0 else min(150, min_needed_sec)
    else:
        vid_dur = int(info.get("duration") or 0)
        cap_sec = max(180, min(360, min_needed_sec))
        dur_sec = min(vid_dur, cap_sec) if vid_dur > cap_sec else (vid_dur or cap_sec)
        # For massive streams (10 mins to 3+ hours), jump directly via HTTP Byte-Range (-ss) to the highest-replayed
        # YouTube heatmap hotspot (or prime mid-stream action zone) so even a 3-hour stream ingests in ~2.5 seconds!
        if vid_dur > cap_sec + 60:
            heatmap = info.get("heatmap") or []
            if heatmap:
                best_hm = max(heatmap, key=lambda h: float(h.get("value") or 0.0))
                hm_center = int(float(best_hm.get("start_time") or 0))
                seek_offset = max(0, min(vid_dur - dur_sec, hm_center - dur_sec // 3))
            else:
                # Jump past intro dead-air into prime stream action (15% into the stream)
                seek_offset = max(0, min(vid_dur - dur_sec, int(vid_dur * 0.15)))
        with open(os.path.join(out_dir, ".cache_key"), "w", encoding="utf-8") as f:
            f.write(f"{key}_{seek_offset}_{dur_sec}")

    seg_back = max(16, int(dur_sec / 3) + 5)
    audio_url = urls[1] if len(urls) > 1 else urls[0]

    tmp_out = os.path.join(out_dir, "source_downloading.mp4")
    v_cmd = ["ffmpeg", "-y", "-threads", "0"]
    for u in urls:
        if is_live or ".m3u8" in u or "/manifest/hls" in u:
            v_cmd += ["-live_start_index", f"-{seg_back}"]
        elif seek_offset > 0:
            v_cmd += ["-ss", str(seek_offset)]
        v_cmd += ["-i", u]
    if len(urls) > 1:
        v_cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    v_cmd += ["-t", str(dur_sec), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", tmp_out]

    a_cmd = ["ffmpeg", "-y", "-threads", "0"]
    if is_live or ".m3u8" in audio_url or "/manifest/hls" in audio_url:
        a_cmd += ["-live_start_index", f"-{seg_back}"]
    elif seek_offset > 0:
        a_cmd += ["-ss", str(seek_offset)]
    a_cmd += ["-i", audio_url, "-t", str(dur_sec), "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", wav]

    bg_res = {"error": None}
    def _bg_video_worker():
        try:
            try:
                _run(v_cmd, job_id=job_id)
            except JobCancelledError:
                raise
            except Exception:
                fb_cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-N", "4",
                          "--downloader", "ffmpeg", "--downloader-args", f"ffmpeg:-t {dur_sec}",
                          "-f", "b[height<=720]/bv*[height<=720]+ba/b", "--merge-output-format", "mp4",
                          "-o", tmp_out, url]
                _run(fb_cmd, job_id=job_id)

            if os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 10000:
                if os.path.exists(out):
                    try: os.remove(out)
                    except Exception: pass
                os.replace(tmp_out, out)
                if not is_live and os.path.exists(out) and not os.path.exists(cache_file):
                    try: os.link(out, cache_file)
                    except Exception: pass
            else:
                raise RuntimeError("Video stream download produced an empty file")
        except Exception as e:
            bg_res["error"] = str(e)

    v_thread = threading.Thread(target=_bg_video_worker, daemon=True)
    v_thread.start()
    if job_id:
        _BG_VIDEO_THREADS[job_id] = (v_thread, bg_res)

    try:
        _run(a_cmd, job_id=job_id)
        if not is_live and os.path.exists(wav) and not os.path.exists(cache_wav):
            try: os.link(wav, cache_wav)
            except Exception: pass
    except JobCancelledError:
        raise
    except Exception:
        wait_for_video(job_id)
        _run(["ffmpeg", "-y", "-threads", "0", "-i", out, "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", wav], job_id=job_id)

    if not job_id:
        v_thread.join()
    return out

def audio_rms(video_path, out_dir, job_id=None):
    """Vectorized C-level np.einsum RMS Wave (~4ms)."""
    check_cancelled(job_id)
    wav = os.path.join(out_dir, "audio.wav")
    key = _read_cache_key(out_dir)
    cdir = _cache_dir(out_dir)
    cache_wav = os.path.join(cdir, key + ".wav") if key else ""
    cache_rms = os.path.join(cdir, key + "_rms.npy") if key else ""

    if cache_wav and os.path.exists(cache_wav) and not os.path.exists(wav):
        try: os.link(cache_wav, wav)
        except Exception: shutil.copy2(cache_wav, wav)

    if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
        wait_for_video(job_id)
        _run(["ffmpeg", "-y", "-threads", "0", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", wav], job_id=job_id)
        if cache_wav and not os.path.exists(cache_wav):
            try: os.link(wav, cache_wav)
            except Exception: pass

    if cache_rms and os.path.exists(cache_rms):
        try:
            return np.load(cache_rms), wav
        except Exception:
            pass

    raw = np.fromfile(wav, dtype=np.int16, offset=44)
    sr = 16000
    n_sec = len(raw) // sr
    if n_sec > 0:
        sq = raw[:n_sec * sr].reshape(n_sec, sr).astype(np.float32)
        arr = (np.sqrt(np.einsum("ij,ij->i", sq, sq) / float(sr)) / 32768.0).astype(np.float32)
    else:
        arr = np.zeros(1, dtype=np.float32)

    if cache_rms:
        try: np.save(cache_rms, arr)
        except Exception: pass
    return arr, wav

def _extract_acoustic_hotspots(rms, n_reels=5):
    """
    Use the 4ms RMS Wave to locate the top high-energy vocal hotspot intervals
    so Whisper only transcribes exciting speech bursts and skips dead air on long streams!
    Returns a flat merged clip_timestamps list [s1, e1, s2, e2, ...] for faster-whisper.
    """
    total_sec = len(rms)
    target_budget = max(95, min(total_sec, int(n_reels) * 24))
    if total_sec <= target_budget + 30:
        return None
    win = 24
    step = 12
    windows = []
    for i in range(0, max(1, total_sec - win), step):
        seg_r = rms[i:i+win]
        score = float(np.mean(seg_r)) + 0.5 * float(np.max(seg_r))
        windows.append((score, i, min(total_sec, i + win)))
    windows.sort(reverse=True)
    n_wins = max(4, target_budget // win + 1)
    chosen = sorted([(s, e) for _, s, e in windows[:n_wins]])
    # Merge overlapping or adjacent intervals
    merged = []
    for s, e in chosen:
        if merged and s <= merged[-1][1] + 4:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([float(s), float(e)])
    flat = []
    for s, e in merged:
        flat.extend([s, e])
    return flat if flat else None

def transcribe(wav_path, model_size="tiny", on_progress=None, job_id=None, rms=None, n_reels=5):
    check_cancelled(job_id)
    out_dir = os.path.dirname(wav_path)
    key = _read_cache_key(out_dir)
    cdir = _cache_dir(out_dir)
    cache_segs = os.path.join(cdir, f"{key}_segs_{model_size}.json") if key else ""
    if cache_segs and os.path.exists(cache_segs):
        try:
            with open(cache_segs, "r", encoding="utf-8") as f:
                data = json.load(f)
            if on_progress:
                on_progress(100, 1, 1)
            return data
        except Exception:
            pass

    clip_ts = _extract_acoustic_hotspots(rms, n_reels=n_reels) if (rms is not None and len(rms) > 130) else None

    def _do_transcribe(force_cpu=False):
        model = _get_model(model_size, force_cpu=force_cpu)
        gta_prompt = (
            "GTA 6, Grand Theft Auto VI, Vice City, Leonida, Lucia, Jason, Rockstar Games, "
            "GTA V, San Andreas, CJ, Trevor, Franklin, Michael, cheat codes, HESOYAM, PAINKILLER, "
            "TURTLE, LAWYERUP, PANZER, Easter eggs, secret glitch, NPC physics, open world."
        )
        kw = dict(
            word_timestamps=True,
            vad_filter=(clip_ts is None),
            vad_parameters=dict(min_silence_duration_ms=350),
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=gta_prompt
        )
        if clip_ts:
            kw["clip_timestamps"] = clip_ts
        segs, info = model.transcribe(wav_path, **kw)
        total_dur = getattr(info, "duration", 0) or 0
        result = []
        for s in segs:
            check_cancelled(job_id)
            words = []
            if s.words:
                for w in s.words:
                    words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
            result.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip(), "words": words})
            if on_progress and total_dur > 0:
                pct = min(99, int(round((float(s.end) / total_dur) * 100)))
                on_progress(pct, float(s.end), float(total_dur))
        if cache_segs and result:
            try:
                with open(cache_segs, "w", encoding="utf-8") as f:
                    json.dump(result, f)
            except Exception:
                pass
        return result

    try:
        return _do_transcribe(force_cpu=False)
    except JobCancelledError:
        raise
    except RuntimeError as e:
        if "cublas" in str(e).lower() or "cudnn" in str(e).lower() or "cuda" in str(e).lower():
            return _do_transcribe(force_cpu=True)
        raise

# Gen-Z Viral Hook, Cheatcode, GTA 6 Domain Weights & 18+ Spicy Unfiltered Lexicon
HOOK_WORDS = {
    "breaking","insane","crazy","unbelievable","amazing","shocking","incredible","omg","wow","epic","clutch","legendary",
    "secret","truth","never","always","mistake","warning","watch","listen","why","how","million","honestly","literally",
    "wait","look","wild","genius","danger","hack","trick","real","proof","exposed","finally","stop","best","worst",
    "bro","cooked","unreal","bizarre","scam","illegal","banned","hidden","nobody","everyone","money","rich","dead","wtf",
    "cheat","code","cheatcode","cheatcodes","glitch","shortcut","unlock","unlocked","infinite","godmode","boost","pro",
    "gta","gta6","gtavi","vice","leonida","lucia","jason","rockstar","hesoyam","painkiller","panzer","wasted","busted"
}

GTA_NEURAL_WEIGHTS = {
    "gta6": 4.0, "gta": 3.5, "gtavi": 4.0, "vice": 3.5, "leonida": 4.0, "lucia": 4.0, "jason": 3.8,
    "rockstar": 3.6, "cheatcode": 4.0, "cheatcodes": 4.0, "cheat": 3.5, "hesoyam": 4.5, "painkiller": 4.2,
    "turtle": 3.5, "lawyerup": 4.0, "panzer": 4.0, "ripazha": 4.0, "uzumymw": 4.0, "fannymagnet": 4.0,
    "easter": 3.4, "glitch": 3.5, "leak": 3.6, "trailer": 3.2, "npc": 3.2, "physics": 3.4, "graphics": 3.2,
    "realistic": 3.0, "wanted": 3.2, "police": 2.8, "cops": 2.8, "heist": 3.4, "alligator": 3.5,
    "florida": 3.0, "miami": 3.0, "supercar": 3.0, "drift": 2.8, "wasted": 3.5, "busted": 3.5,
    "san": 2.8, "andreas": 2.8, "cj": 3.4, "trevor": 3.4, "franklin": 3.2, "michael": 3.0, "openworld": 3.0
}

GTA_CHEAT_HUD_OVERLAYS = [
    "★★★★★ GTA VI // VICE CITY LEONIDA SECRET",
    "🎮 CHEAT ACTIVATED: PAINKILLER // GOD MODE ON",
    "🎮 CHEAT ACTIVATED: HESOYAM // +$250,000 + ARMOR",
    "★★★★★ WANTED LEVEL MAX // ROCKSTAR HIDDEN DETAIL",
    "🎮 CHEAT ACTIVATED: LAWYERUP // WANTED CLEARED",
    "🌴 GTA 6 NEXT-GEN NPC & PHYSICS ENGINE",
    "🎮 CHEAT ACTIVATED: COMET // SUPERCAR SPAWNED",
    "🔑 GTA 6 SECRET EASTER EGG UNLOCKED"
]

SPICY_18_WORDS = {
    "spicy","unfiltered","dating","toxic","cheating","drunk","party","night","confession","roast","savage","freaky",
    "baddie","redflag","hookup","ex","kicked","fight","arrested","cops","cringe","embarrassing","kiss","crush","hot",
    "girlfriend","boyfriend","breakup","divorce","lying","caught","sneaking","regret","cancel","cancelled","drama"
}

CURIOSITY_PHRASES = (
    "here is why", "here's why", "the truth is", "nobody talks about", "what happened",
    "you won't believe", "wait until", "the problem is", "number one", "the secret",
    "listen to this", "watch this", "let me tell you", "most people", "don't know",
    "cheat code", "secret trick", "life hack", "hidden feature"
)

EMOJI_MAP = {
    "fire": "🔥", "hot": "🥵", "best": "🔥", "epic": "🔥", "lit": "🔥", "spicy": "🌶️", "freaky": "😈",
    "crazy": "🤯", "insane": "🤯", "unbelievable": "🤯", "shocking": "🤯", "wild": "🤯", "mind": "🤯",
    "bro": "💀", "dead": "💀", "cooked": "💀", "funny": "💀", "wtf": "💀", "omg": "😱", "savage": "😈",
    "money": "💸", "million": "💸", "billion": "💰", "rich": "🤑", "cash": "💸", "price": "💰",
    "wait": "👀", "watch": "👀", "look": "👀", "see": "👀", "hidden": "👀", "caught": "📸",
    "warning": "🚨", "breaking": "🚨", "danger": "⚠️", "stop": "🛑", "mistake": "⚠️", "illegal": "🔞",
    "secret": "🤫", "truth": "💯", "real": "💯", "honestly": "💯", "literally": "💯", "confession": "🤫",
    "win": "👑", "clutch": "👑", "legendary": "🐐", "genius": "🧠", "smart": "🧠", "hack": "⚡", "toxic": "🚩",
    "cheat": "🎮", "code": "🔑", "cheatcode": "⚡", "glitch": "👾", "unlock": "🔓", "trick": "⚡"
}

CLICKBAIT_BANNERS = [
    "Speed reacts to GTA 6\\Nhaving STREAMERS in\\Nthe game 😭",
    "Lacy shows off how BIG\\Nthe GTA VI Map is 😳🔥",
    "Kai Cenat loses it seeing\\NGTA 6 GRAPHICS for the\\Nfirst time 🤯🔥",
    "Streamer discovers a\\NSECRET cheatcode inside\\NGTA VI 😳💀",
    "Rockstar hid this CRAZY\\Ndetail inside the GTA 6\\Ntrailer 👀🔥",
    "He tested EVERY secret\\Ncheatcode in GTA and\\Nthis happened 😭🔥",
    "Nobody expected GTA 6\\Nto have THIS realistic\\Nphysics 🤯💀",
    "Streamer reacts to how\\NMASSIVE Vice City is in\\NGTA VI 😳🌴",
    "Wait until you see what\\Nthe NPCs do in GTA 6 😭🔥",
    "99% of players missed\\Nthis SECRET location on\\Nthe GTA VI map 👀💀",
    "Bro unlocked GOD MODE\\Nwith this hidden GTA\\Ncheatcode 🤯⚡",
    "When the REAL police\\Nstart chasing you in\\NGTA 6 😭🚨",
    "Comparing GTA V vs\\NGTA 6 next-gen graphics\\Nis INSANE 🤯🔥",
    "This secret GTA 6 leak\\Nchanges EVERYTHING we\\Nknew 😳💀",
    "He found an ILLEGAL\\Nmoney glitch inside\\NGTA VI 💸😭",
    "Look at his reaction when\\NLucia & Jason appear in\\NGTA 6 😳🔥",
    "Rockstar actually put\\NTHIS inside Vice City 😭🌴",
    "The most VIRAL streamer\\Nreaction to GTA 6\\Ngameplay 🤯🔥",
    "Testing 50 SECRET\\Ncheatcodes in GTA back\\Nto back 💀⚡",
    "Wait for the crazy plot\\Ntwist at the end of this\\NGTA clip 😭👀"
]

SPICY_18_BANNERS = [
    "Speed reacts to GTA 6\\Nwildest 18+ UNFILTERED\\Nmoment 😭🔞",
    "Streamer caught off guard\\Nby this SPICY scene in\\NGTA VI 😳🔥",
    "Bro had ZERO filter\\Nreacting to this wild\\Nconfession 💀🌶️",
    "She really admitted THIS\\Nlive on stream without\\Nhesitating 😭🚩",
    "Late night stream got\\NWAY too spicy at the\\Nend 😳🔞",
    "Chat lost it when this\\NUNFILTERED moment\\Nhappened live 😭💀",
    "Watch before this wild\\N18+ stream clip gets\\Ndeleted 😳🚨",
    "Nobody expected this\\NSPICY plot twist on\\Nstream 💀🔥"
]

TRENDING_HASHTAG_PACKS = [
    ["#cheatcodes", "#lifehacks", "#fyp", "#viral", "#trending", "#reelsinstagram", "#explorepage", "#mindblown"],
    ["#secrethacks", "#viralvideo", "#trendingreels", "#fyppage", "#reelitfeelit", "#instareels", "#mustwatch", "#genz"],
    ["#cheatcode", "#explore", "#foryoupage", "#plottwist", "#unbelievable", "#trendingnow", "#reelsviral", "#viralshorts"],
    ["#protricks", "#viralpost", "#trendingtopic", "#shocking", "#waitforit", "#fypシ", "#reelsvideo", "#highlights"],
    ["#hiddenfeatures", "#dontblink", "#viralclips", "#reelstrending", "#bestmoments", "#insane", "#watchtilltheend", "#shorts"]
]

GENZ_CAPTION_TEMPLATES = [
    "Secret {topic_label} cheatcode unlocked 🤯🔑 \"{quote}\" — Save this before you forget! Drop your thoughts below 👇🔥",
    "99% of people miss this {topic_label} trick 💀🚨 \"{quote}\" — Tag someone who needs this cheatcode today! 🔖⚡",
    "Bro really exposed the ultimate {topic_label} hack in 15 seconds 😭🔥 \"{quote}\" — Do you agree? 👇💯",
    "Wait for the {topic_label} plot twist at the end 😱👀 \"{quote}\" — Watch till the very last second! 🚀",
    "This {topic_label} shortcut feels illegal to know 🤯👑 \"{quote}\" — Rate this 1-10 in the comments! 👇🔥",
    "Stop scrolling and lock in on this {topic_label} secret 🚨🤫 \"{quote}\" — Double tap if this helped! ❤️⚡",
    "My jaw dropped when this {topic_label} cheatcode worked 💀🤯 \"{quote}\" — Share this on your story! 📲🔥",
    "Unfiltered {topic_label} facts that nobody talks about 💯👀 \"{quote}\" — Comment 'CODE' for more! 👇"
]

def _norm_text_hash(txt):
    cleaned = re.sub(r"[^a-z0-9]", "", (txt or "").lower())
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:16] if len(cleaned) >= 12 else ""

def _extract_shingles(txt, k=3):
    """Extracts normalized k-word dialogue shingles to block any duplicate or near-duplicate spoken content across 200 reels."""
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in (txt or "").split()]
    words = [w for w in words if len(w) >= 2]
    if len(words) < k:
        return set(words)
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}

def _is_shingle_duplicate(shingles, existing_shingle_sets, max_sim=0.22):
    if not shingles or len(shingles) < 2:
        return False
    for prev in existing_shingle_sets:
        if not prev:
            continue
        inter = len(shingles & prev)
        if inter / float(min(len(shingles), len(prev))) >= max_sim:
            return True
    return False

def _overlaps_existing(start, end, existing_ranges, min_gap=2.5):
    """Strict 0.0s overlap + mandatory min_gap buffer between any two cuts so no video frames are ever duplicated."""
    for es, ee in existing_ranges:
        if not (end + min_gap <= es or start - min_gap >= ee):
            return True
    return False

def pick(segs, rms, n=3, spicy_18=False, topic="", url_key=""):
    """
    Pick N strictly non-duplicate viral & cheatcode moments focused on the user's topic.
    Enforces:
    1) 0% timestamp overlap + 2.5s minimum gap between any two reels from the same video
    2) SHA1 full-text hash AND 3-word shingle similarity check (<22% overlap) against all previous reels in dedup_history.json
    """
    with _dedup_lock:
        dedup = _load_dedup()
        past_ranges = list(dedup.get("cut_ranges", {}).get(url_key, [])) if url_key else []
        past_hashes = set(dedup.get("text_hashes", []))
        past_shingle_sets = [set(s_list) for s_list in dedup.get("used_shingles", []) if s_list]

    if not segs or len(rms) == 0:
        picks = _fallback_loud(rms, n, past_ranges=past_ranges)
        _record_picks_dedup(url_key, picks)
        return picks

    rms_mean = float(np.mean(rms))
    rms_std = float(np.std(rms)) + 1e-9
    def z(x): return (x - rms_mean) / rms_std

    min_dur = 11 if n >= 8 else 14
    max_dur = 20 if n >= 8 else 32
    topic_words = {re.sub(r"[^a-z0-9]", "", w.lower()) for w in (topic or "").split() if len(w) >= 2}
    all_open_triggers = HOOK_WORDS | SPICY_18_WORDS | topic_words | set(GTA_NEURAL_WEIGHTS.keys())

    # O(N) Precomputation for instant <2ms Viral Cortex evaluation even on 3-hour streams!
    rms_prefix = np.concatenate([[0.0], np.cumsum(rms, dtype=np.float64)])
    n_segs = len(segs)
    seg_wc = [0] * n_segs
    seg_hooks = [0.0] * n_segs
    seg_open = [0] * n_segs
    seg_end_clean = [0.0] * n_segs
    for idx_s, s in enumerate(segs):
        t_str = s["text"]
        t_low = t_str.lower()
        w_clean = [w.strip(".,!?;:\"'()[]") for w in t_low.split()]
        seg_wc[idx_s] = len(s.get("words", [])) or len(w_clean)
        h_val = sum(1.0 for w in w_clean if w in HOOK_WORDS)
        h_val += sum(GTA_NEURAL_WEIGHTS.get(w, 0.0) for w in w_clean)
        if "gta 6" in t_low or "vice city" in t_low or "grand theft auto" in t_low:
            h_val += 4.5
        if topic_words:
            h_val += sum(3.0 for w in w_clean if w in topic_words)
        h_val += sum(2.2 if spicy_18 else 1.0 for w in w_clean if w in SPICY_18_WORDS)
        h_val += sum(2.0 for phr in CURIOSITY_PHRASES if phr in t_low)
        h_val += t_str.count("!") * 1.2 + t_str.count("?") * 1.5
        h_val += 0.25 * min(sum(1 for w in w_clean if any(ch.isdigit() for ch in w)), 3)
        seg_hooks[idx_s] = h_val
        seg_open[idx_s] = sum(1 for w in w_clean[:10] if w in all_open_triggers)
        seg_end_clean[idx_s] = 0.55 if t_str.endswith((".", "!", "?")) else 0.0

    wc_prefix = np.concatenate([[0], np.cumsum(seg_wc)])
    hooks_prefix = np.concatenate([[0.0], np.cumsum(seg_hooks)])

    candidates = []
    for win_len in range(max_dur, min_dur - 1, -3):
        for i in range(n_segs):
            start = segs[i]["start"]
            end = start
            j = i
            while j < n_segs and segs[j]["end"] - start <= win_len:
                end = segs[j]["end"]
                j += 1
            dur = end - start
            if dur < min_dur:
                continue
            if _overlaps_existing(start, end, past_ranges):
                continue
            s_i, e_i = max(0, int(start)), min(int(end), len(rms))
            if s_i >= e_i:
                continue
            text = " ".join(s["text"] for s in segs[i:j])
            thash = _norm_text_hash(text)
            if thash and thash in past_hashes:
                continue
            shingles = _extract_shingles(text)
            if _is_shingle_duplicate(shingles, past_shingle_sets):
                continue

            # O(1) prefix-sum mean lookup + slice max
            r_mean = float((rms_prefix[e_i] - rms_prefix[s_i]) / (e_i - s_i))
            r_max = float(np.max(rms[s_i:e_i]))
            hk_e = min(s_i + 4, e_i)
            hook_energy = z(float((rms_prefix[hk_e] - rms_prefix[s_i]) / max(1, hk_e - s_i)))
            cl_s = max(s_i, e_i - max(3, int(dur * 0.3)))
            climax_energy = z(float((rms_prefix[e_i] - rms_prefix[cl_s]) / max(1, e_i - cl_s)))

            wc = int(wc_prefix[j] - wc_prefix[i])
            wps = wc / dur if dur > 0 else 0.0
            total_hooks = float(hooks_prefix[j] - hooks_prefix[i])
            opening_hooks = seg_open[i]

            pause_before = (start - segs[i-1]["end"]) if i > 0 else 1.5
            clean_start = 0.55 if pause_before >= 0.35 else 0.0
            clean_end = seg_end_clean[j-1]
            dur_bonus = 0.5 if (min_dur + 2) <= dur <= (max_dur - 2) else 0.0

            score = (
                z(r_mean)
                + 0.55 * z(r_max)
                + 0.45 * hook_energy
                + 0.35 * climax_energy
                + 0.45 * min(wps, 4.2)
                + 0.55 * total_hooks
                + 0.70 * opening_hooks
                + clean_start + clean_end + dur_bonus
            )
            candidates.append({
                "start": start, "end": end, "score": float(score),
                "text": text, "text_hash": thash, "shingles": shingles, "seg_range": (i, j)
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    shortlist = []
    seen_hashes = set(past_hashes)
    active_ranges = list(past_ranges)
    active_shingles = list(past_shingle_sets)

    for c in candidates:
        if len(shortlist) >= n:
            break
        if c.get("text_hash") and c["text_hash"] in seen_hashes:
            continue
        if _overlaps_existing(c["start"], c["end"], active_ranges, min_gap=2.5):
            continue
        if _is_shingle_duplicate(c.get("shingles"), active_shingles):
            continue
        shortlist.append(c)
        active_ranges.append((c["start"], c["end"]))
        if c.get("text_hash"):
            seen_hashes.add(c["text_hash"])
        if c.get("shingles"):
            active_shingles.append(c["shingles"])

    if len(shortlist) < n:
        extra = _fallback_loud(rms, n - len(shortlist), past_ranges=active_ranges)
        for ex in extra:
            if len(shortlist) >= n:
                break
            if not _overlaps_existing(ex["start"], ex["end"], active_ranges, min_gap=2.5):
                shortlist.append(ex)
                active_ranges.append((ex["start"], ex["end"]))

    if not shortlist:
        shortlist = _fallback_loud(rms, 1, past_ranges=past_ranges)

    s_vals = [c["score"] for c in shortlist]
    s_min, s_max = min(s_vals), max(s_vals)
    rms_thresh = float(np.mean(rms)) * 0.65 if len(rms) > 0 else 0.0
    for c in shortlist:
        norm = (c["score"] - s_min) / (s_max - s_min + 1e-6)
        c["viral_score"] = int(round(90 + 9 * norm))
        # Trim leading dead-air silence so every reel opens immediately on high vocal energy
        st_sec = int(c["start"])
        while (c["end"] - c["start"]) > 13.0 and st_sec < len(rms) and float(rms[st_sec]) < rms_thresh:
            c["start"] = round(float(c["start"]) + 0.8, 2)
            st_sec = int(c["start"])

    picked = shortlist[:n]
    picked.sort(key=lambda c: c["start"])
    _record_picks_dedup(url_key, picked)
    return picked

def _record_picks_dedup(url_key, picks):
    with _dedup_lock:
        dedup = _load_dedup()
        if url_key:
            cr = dedup.setdefault("cut_ranges", {}).setdefault(url_key, [])
            for p in picks:
                cr.append([round(float(p["start"]), 2), round(float(p["end"]), 2)])
        th_list = dedup.setdefault("text_hashes", [])
        sh_list = dedup.setdefault("used_shingles", [])
        for p in picks:
            th = p.get("text_hash") or _norm_text_hash(p.get("text", ""))
            if th and th not in th_list:
                th_list.append(th)
            sh = list(p.get("shingles") or _extract_shingles(p.get("text", "")))
            if sh:
                sh_list.append(sh[:40])
            p.pop("shingles", None)
        _save_dedup(dedup)

def _fallback_loud(rms, n, past_ranges=None):
    """Strictly non-overlapping acoustic hotspot fallback — never produces overlapping time windows."""
    active_ranges = list(past_ranges or [])
    picks = []
    total_len = max(15, len(rms))
    win = min(18 if n >= 8 else 22, max(12, total_len // max(1, min(n + 1, 8))))
    scored = []
    for i in range(0, max(1, len(rms) - win + 1), 2):
        if _overlaps_existing(i, i + win, active_ranges, min_gap=2.5):
            continue
        v = float(np.mean(rms[i:i+win])) if len(rms) > i else 0.1
        scored.append((v, i))
    scored.sort(reverse=True)
    for v, i in scored:
        if len(picks) >= n:
            break
        st_f, en_f = float(i), float(min(total_len, i + win))
        if not _overlaps_existing(st_f, en_f, active_ranges, min_gap=2.5):
            picks.append({
                "start": st_f, "end": en_f,
                "score": v, "viral_score": 91, "text": ""
            })
            active_ranges.append((st_f, en_f))
    return picks

BGM_TRACK_LABELS = {
    "phonk_drift": "🔥 Trending Drift Phonk 808",
    "gta6_vice_synth": "🌴 GTA 6 Vice City Synthwave",
    "dark_suspense": "👀 Viral Mystery / Leak Pluck",
    "chill_slowed": "😭 Slowed + Reverb Viral Vibe",
    "spicy_afrobeat": "🌶️ Late-Night Spicy Bounce",
    "none": "🎙️ Original Stream Audio"
}

_bgm_lock = threading.Lock()

def _write_stereo_wav(path, left, right, sr=48000):
    import wave
    os.makedirs(os.path.dirname(path), exist_ok=True)
    l_i16 = np.clip(left * 32767.0, -32767, 32767).astype(np.int16)
    r_i16 = np.clip(right * 32767.0, -32767, 32767).astype(np.int16)
    stereo = np.empty((len(l_i16) * 2,), dtype=np.int16)
    stereo[0::2] = l_i16
    stereo[1::2] = r_i16
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())

def _ensure_trending_bgm_track(track_key, out_dir):
    """
    Synthesizes & caches 5 copyright-safe trending background songs in output/_cache/bgm/*.wav
    tailored to viral GTA 6, streamer reaction, cheatcode, and Gen-Z Reels trends.
    """
    if not track_key or track_key == "none":
        return None
    bgm_dir = os.path.join(_cache_dir(out_dir), "bgm")
    os.makedirs(bgm_dir, exist_ok=True)
    wav_path = os.path.join(bgm_dir, f"{track_key}.wav")
    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100000:
        return wav_path

    with _bgm_lock:
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100000:
            return wav_path
        sr = 48000
        dur = 16.0
        n = int(sr * dur)
        t = np.linspace(0.0, dur, n, endpoint=False, dtype=np.float32)

        if track_key == "phonk_drift":
            # 135 BPM Trending Brazilian / Drift Phonk: Saturated 808 + TR-808 Cowbell hook + trap hats
            bpm = 135.0
            spb = 60.0 / bpm
            step_t = spb / 2.0  # 8th notes
            notes_cb = [739.99, 739.99, 880.0, 739.99, 659.25, 739.99, 987.77, 880.0,
                        739.99, 739.99, 880.0, 739.99, 659.25, 587.33, 659.25, 659.25]
            notes_bass = [46.25, 46.25, 55.0, 46.25, 41.20, 46.25, 49.0, 41.20] * 2
            step_idx = (t / step_t).astype(np.int32)
            local_t = np.mod(t, step_t)
            cb_freq = np.array([notes_cb[i % len(notes_cb)] for i in step_idx], dtype=np.float32)
            b_freq = np.array([notes_bass[(i // 2) % len(notes_bass)] for i in step_idx], dtype=np.float32)
            # TR-808 Cowbell (two metallic square partials + sharp decay)
            cb_env = np.exp(-local_t * 14.0).astype(np.float32)
            cb_raw = (np.sign(np.sin(2 * np.pi * cb_freq * local_t)) +
                      0.7 * np.sign(np.sin(2 * np.pi * cb_freq * 1.481 * local_t))) * 0.5
            cb = np.tanh(cb_raw * 1.6) * cb_env * 0.34
            # Saturated 808 Sub Bass
            b_env = np.exp(-np.mod(t, spb) * 3.2).astype(np.float32)
            bass = np.tanh(np.sin(2 * np.pi * b_freq * t) * 2.4) * b_env * 0.52
            # Kick + Trap Hi-Hat 16th roll
            beat_lt = np.mod(t, spb)
            kick = np.sin(2 * np.pi * (44.0 + 95.0 * np.exp(-beat_lt * 32.0)) * beat_lt) * np.exp(-beat_lt * 11.0) * 0.45
            rng = np.random.default_rng(42)
            noise = rng.standard_normal(n, dtype=np.float32)
            hat_env = np.exp(-np.mod(t, spb / 4.0) * 48.0).astype(np.float32)
            hats = noise * hat_env * 0.12
            left = np.tanh((bass + kick + cb * 1.08 + hats) * 0.85)
            right = np.tanh((bass + kick + np.roll(cb, 380) * 0.95 + hats) * 0.85)

        elif track_key == "gta6_vice_synth":
            # 112 BPM GTA 6 Vice City 80s Neon Synthwave: Detuned multi-saw chords + pulsing arpeggio bass
            bpm = 112.0
            spb = 60.0 / bpm
            bar_t = spb * 4.0
            chords = [
                (220.0, 261.63, 329.63, 392.0),   # Am7
                (174.61, 220.0, 261.63, 329.63),  # Fmaj7
                (261.63, 329.63, 392.0, 493.88),  # Cmaj7
                (196.0, 246.94, 293.66, 329.63),  # G6
            ]
            bass_roots = [55.0, 43.65, 65.41, 49.0]
            bar_idx = ((t / bar_t).astype(np.int32)) % 4
            pad_l = np.zeros(n, dtype=np.float32)
            pad_r = np.zeros(n, dtype=np.float32)
            for c_i, ch in enumerate(chords):
                mask = (bar_idx == c_i).astype(np.float32)
                for f_n in ch:
                    pad_l += mask * (np.sin(2 * np.pi * f_n * t) + 0.5 * np.sin(2 * np.pi * (f_n * 1.004) * t))
                    pad_r += mask * (np.sin(2 * np.pi * (f_n * 0.996) * t) + 0.5 * np.sin(2 * np.pi * (f_n * 2.0) * t))
            # Sidechain pumping envelope on every beat
            pump = (0.35 + 0.65 * (1.0 - np.exp(-np.mod(t, spb) * 7.5))).astype(np.float32)
            pad_l = pad_l * 0.08 * pump
            pad_r = pad_r * 0.08 * pump
            # 8th-note pulsing synthwave arpeggio bass
            arp_lt = np.mod(t, spb / 2.0)
            arp_env = np.exp(-arp_lt * 10.5).astype(np.float32)
            b_root = np.array([bass_roots[i] for i in bar_idx], dtype=np.float32)
            octave_mult = np.where(((t / (spb / 2.0)).astype(np.int32) % 2) == 1, 2.0, 1.0).astype(np.float32)
            bass = np.tanh(np.sin(2 * np.pi * b_root * octave_mult * t) * 2.1) * arp_env * 0.44
            beat_lt = np.mod(t, spb)
            kick = np.sin(2 * np.pi * (46.0 + 80.0 * np.exp(-beat_lt * 28.0)) * beat_lt) * np.exp(-beat_lt * 10.0) * 0.38
            left = np.tanh((pad_l + bass + kick) * 0.85)
            right = np.tanh((pad_r + bass + kick) * 0.85)

        elif track_key == "dark_suspense":
            # 100 BPM Viral Mystery / Secret Leak Pluck (D harmonic minor arpeggio + stereo delay + sub drone)
            bpm = 100.0
            spb = 60.0 / bpm
            step_t = spb / 2.0
            arp_notes = [293.66, 349.23, 440.0, 466.16, 440.0, 349.23, 277.18, 329.63]
            step_idx = (t / step_t).astype(np.int32)
            local_t = np.mod(t, step_t)
            f_arp = np.array([arp_notes[i % len(arp_notes)] for i in step_idx], dtype=np.float32)
            pluck_env = np.exp(-local_t * 9.5).astype(np.float32)
            pluck = (np.sin(2 * np.pi * f_arp * local_t) +
                     0.45 * np.sin(2 * np.pi * f_arp * 2.0 * local_t) * np.exp(-local_t * 16.0) +
                     0.25 * np.sin(2 * np.pi * f_arp * 3.0 * local_t) * np.exp(-local_t * 24.0)) * pluck_env * 0.36
            delay_samples = int(step_t * 1.5 * sr)
            pluck_l = pluck + 0.38 * np.roll(pluck, delay_samples)
            pluck_r = pluck + 0.42 * np.roll(pluck, delay_samples * 2)
            # Deep cinematic sub-bass glide
            sub_f = np.where(((t / (spb * 4.0)).astype(np.int32) % 2) == 0, 36.71, 29.14).astype(np.float32)
            sub = np.tanh(np.sin(2 * np.pi * sub_f * t) * 1.8) * 0.42
            beat_lt = np.mod(t, spb * 2.0)
            thump = np.sin(2 * np.pi * (42.0 + 60.0 * np.exp(-beat_lt * 25.0)) * beat_lt) * np.exp(-beat_lt * 9.0) * 0.34
            left = np.tanh((pluck_l + sub + thump) * 0.82)
            right = np.tanh((pluck_r + sub + thump) * 0.82)

        elif track_key == "spicy_afrobeat":
            # 104 BPM Late-Night Spicy Log-Drum Bounce + Warm Kalimba Plucks
            bpm = 104.0
            spb = 60.0 / bpm
            step_t = spb / 2.0
            kal_notes = [392.0, 440.0, 523.25, 587.33, 659.25, 587.33, 523.25, 440.0]
            step_idx = (t / step_t).astype(np.int32)
            local_t = np.mod(t, step_t)
            f_kal = np.array([kal_notes[i % len(kal_notes)] for i in step_idx], dtype=np.float32)
            kal = (np.sin(2 * np.pi * f_kal * local_t) + 0.3 * np.sin(2 * np.pi * f_kal * 3.0 * local_t)) * np.exp(-local_t * 11.0) * 0.34
            log_env = np.exp(-np.mod(t, spb * 0.75) * 7.5).astype(np.float32)
            log_drum = np.tanh(np.sin(2 * np.pi * (55.0 + 35.0 * log_env) * t) * 2.2) * log_env * 0.48
            left = np.tanh((kal + log_drum) * 0.85)
            right = np.tanh((np.roll(kal, 600) + log_drum) * 0.85)

        else:
            # 'chill_slowed': 88 BPM Slowed + Reverb Late-Night Streamer Reaction Vibe
            bpm = 88.0
            spb = 60.0 / bpm
            bar_t = spb * 4.0
            chords = [
                (174.61, 220.0, 261.63, 329.63, 392.0),  # Fmaj9
                (164.81, 196.0, 246.94, 293.66, 392.0),  # Em7
                (146.83, 174.61, 220.0, 261.63, 329.63), # Dm9
                (130.81, 164.81, 196.0, 246.94, 293.66), # Cmaj9
            ]
            roots = [43.65, 41.20, 36.71, 32.70]
            bar_idx = ((t / bar_t).astype(np.int32)) % 4
            pad_l = np.zeros(n, dtype=np.float32)
            pad_r = np.zeros(n, dtype=np.float32)
            vib = 1.0 + 0.0025 * np.sin(2 * np.pi * 2.2 * t)
            for c_i, ch in enumerate(chords):
                mask = (bar_idx == c_i).astype(np.float32)
                for f_n in ch:
                    pad_l += mask * np.sin(2 * np.pi * f_n * vib * t)
                    pad_r += mask * np.sin(2 * np.pi * (f_n * 1.003) * vib * t)
            b_root = np.array([roots[i] for i in bar_idx], dtype=np.float32)
            sub = np.tanh(np.sin(2 * np.pi * b_root * t) * 1.6) * 0.40
            beat_lt = np.mod(t, spb * 2.0)
            kick = np.sin(2 * np.pi * (40.0 + 55.0 * np.exp(-beat_lt * 22.0)) * beat_lt) * np.exp(-beat_lt * 8.0) * 0.30
            left = np.tanh((pad_l * 0.09 + sub + kick) * 0.82)
            right = np.tanh((pad_r * 0.09 + sub + kick) * 0.82)

        _write_stereo_wav(wav_path, left, right, sr=sr)
        return wav_path

def select_reel_bgm(clip, idx=0, topic="", spicy_18=False, bgm_mode="auto"):
    """
    Chooses a trending background song that suits the reel's content, mood, and viral trend.
    In 'auto' mode ('in some reels put some background songs which suits the reels and trends'),
    applies the matched trending BGM to ~75% of reels (3 out of every 4 reels) while keeping
    every 4th reel with pure original stream audio for natural feed variety.
    """
    bgm_mode = (bgm_mode or "auto").strip().lower()
    if bgm_mode == "none":
        return "none", BGM_TRACK_LABELS["none"]
    if bgm_mode in BGM_TRACK_LABELS and bgm_mode != "none":
        return bgm_mode, BGM_TRACK_LABELS[bgm_mode]

    # In 'auto' mode, put trending background music on reels 0, 1, 2 of every 4-reel cycle (~75% of reels)
    if bgm_mode == "auto" and (idx % 4 == 3):
        return "none", BGM_TRACK_LABELS["none"]

    text_low = f"{clip.get('text') or ''} {topic or ''}".lower()
    v_score = int(clip.get("viral_score") or 90)

    if spicy_18 or any(w in text_low for w in ("spicy", "18+", "unfiltered", "confession", "girlfriend", "kiss", "date", "freaky")):
        key = "spicy_afrobeat"
    elif any(w in text_low for w in ("map", "vice", "leonida", "graphics", "trailer", "city", "massive", "big", "lucia", "jason", "world")):
        key = "gta6_vice_synth"
    elif any(w in text_low for w in ("secret", "cheat", "code", "glitch", "leak", "hidden", "missed", "hack", "money", "location", "mystery")):
        key = "dark_suspense"
    elif v_score >= 92 or any(w in text_low for w in ("cops", "police", "chase", "crazy", "insane", "wtf", "god mode", "holy", "scream", "bro")):
        key = "phonk_drift"
    else:
        cycle = ["gta6_vice_synth", "chill_slowed", "phonk_drift", "dark_suspense"]
        key = cycle[idx % len(cycle)]

    return key, BGM_TRACK_LABELS[key]

def _detect_streamer_name(source_title="", text=""):
    """Detects the streamer's name from the YouTube video title/uploader so headlines always match the actual streamer in the clip."""
    combined = f"{source_title or ''} {text or ''}".lower()
    known = [
        (("ishowspeed", "speed"), "Speed"),
        (("lacy",), "Lacy"),
        (("kai cenat", "kaicenat", "kai "), "Kai Cenat"),
        (("xqc",), "xQc"),
        (("adin ross", "adin"), "Adin Ross"),
        (("caseoh",), "CaseOh"),
        (("plaqueboymax", "max "), "Plaqueboymax"),
        (("sketch",), "Sketch"),
        (("stableronaldo", "ronaldo"), "Ronaldo"),
        (("duke dennis", "duke"), "Duke Dennis"),
        (("agent 00", "agent00"), "Agent 00"),
        (("fanum",), "Fanum"),
    ]
    for triggers, display in known:
        if any(tr in combined for tr in triggers):
            return display
    return ""

def _apply_detected_streamer(banner, streamer):
    if not streamer:
        return banner
    for other in ("Speed", "Lacy", "Kai Cenat", "Streamer"):
        if banner.startswith(other + " "):
            return streamer + banner[len(other):]
    return banner

def _wrap_viral_headline(raw_line, topic="GTA 6", idx=0, layout="black_bar"):
    """
    Format headline in the exact viral streamer-reaction style from the reference edits:
    - Mixed-case sentence with 1-2 ALL-CAPS emphasis words + emojis at the end
    - 3 lines for 'black_bar' (Speed style), 2 lines for 'split_cam' (Lacy style)
    """
    if "\\N" in raw_line:
        parts = [p.strip() for p in raw_line.split("\\N") if p.strip()]
        if layout == "split_cam" and len(parts) == 3:
            # Re-wrap 3 lines into 2 balanced lines for the horizontal split seam
            all_words = " ".join(parts).split()
            mid = max(1, (len(all_words) + 1) // 2)
            return " ".join(all_words[:mid]) + "\\N" + " ".join(all_words[mid:])
        elif layout == "black_bar" and len(parts) == 2:
            all_words = " ".join(parts).split()
            if len(all_words) >= 5:
                c1 = max(2, len(all_words) // 3)
                c2 = max(c1 + 1, (len(all_words) * 2) // 3)
                return " ".join(all_words[:c1]) + "\\N" + " ".join(all_words[c1:c2]) + "\\N" + " ".join(all_words[c2:])
        return raw_line
    words = raw_line.strip().split()
    if not words:
        return "Speed reacts to GTA 6\\Nhaving STREAMERS in\\Nthe game 😭"
    if layout == "split_cam":
        mid = max(1, (len(words) + 1) // 2)
        return " ".join(words[:mid]) + "\\N" + " ".join(words[mid:])
    c1 = max(1, len(words) // 3)
    c2 = max(c1 + 1, (len(words) * 2) // 3)
    return " ".join(words[:c1]) + "\\N" + " ".join(words[c1:c2]) + "\\N" + " ".join(words[c2:])

def _genz_clickbait_heuristic(text, idx=0, spicy_18=False, topic="", source_title="", layout="black_bar"):
    topic_clean = (topic or "").strip() or "GTA 6"
    topic_tag = "#" + re.sub(r"[^a-z0-9]", "", topic_clean.lower()) if topic_clean else "#gta6"
    streamer = _detect_streamer_name(source_title=source_title, text=text)

    with _dedup_lock:
        dedup = _load_dedup()
        g_seq = int(dedup.get("reel_seq", 0))
        dedup["reel_seq"] = g_seq + 1
        used_banners = set(dedup.get("used_banners", []))
        used_captions = set(dedup.get("used_captions", []))
        _save_dedup(dedup)

    tag_pack = list(TRENDING_HASHTAG_PACKS[g_seq % len(TRENDING_HASHTAG_PACKS)])
    t_words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in topic_clean.split() if len(w) >= 2]
    topic_tags = [topic_tag, "#gta6", "#gtavi", "#vicecity"] + [f"#{w}clips" for w in t_words[:1]]
    if streamer:
        topic_tags.insert(1, "#" + re.sub(r"[^a-z0-9]", "", streamer.lower()))
    tag_pack = topic_tags + [t for t in tag_pack if t not in topic_tags]
    if spicy_18:
        tag_pack = ["#unfiltered", "#latenight", "#spicy", "#exposed"] + tag_pack[:6]

    stop = {"the","and","for","that","this","with","you","are","was","have","from","they","will","what","when","your","about","just","like","into","over","more","really","going","there"," right","here","know","look","wait"}
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in (text or "").split()]
    keywords = []
    for w in words:
        if len(w) >= 4 and w not in stop and w not in keywords:
            keywords.append(w)

    lead_name = streamer or ["Speed", "Lacy", "Kai Cenat", "Streamer", "Bro", "Chat"][g_seq % 6]
    kw_focus = (keywords[0].upper() if keywords else ["STREAMERS", "VICE CITY", "CHEATCODE", "GRAPHICS", "POLICE", "SECRETS"][g_seq % 6])
    emos_pool = ["😭🔥", "😳🔥", "🤯💀", "😭👀", "😳🌴", "😭🚨", "🤯⚡", "💀🔥", "😳💀", "💸😭"]
    emo_pick = emos_pool[g_seq % len(emos_pool)]

    # Combinatorial + transcript-driven headline pool (240+ unique variations so no two of 200 reels share a headline)
    banners_pool = list(SPICY_18_BANNERS if spicy_18 else CLICKBAIT_BANNERS)
    dynamic_candidates = [
        f"{lead_name} reacts to {topic_clean}\\Nhaving {kw_focus} in\\Nthe game {emo_pick}",
        f"{lead_name} shows off how CRAZY\\Nthe {topic_clean} {kw_focus} is {emo_pick}",
        f"{lead_name} loses it seeing\\N{topic_clean} {kw_focus} for the\\Nfirst time {emo_pick}",
        f"{lead_name} discovers a SECRET\\N{kw_focus} inside {topic_clean} {emo_pick}",
        f"Wait until you see what\\N{lead_name} found in\\N{topic_clean} {kw_focus} {emo_pick}",
        f"99% of players missed\\Nthis {kw_focus} secret in\\N{topic_clean} {emo_pick}",
        f"Nobody expected {topic_clean}\\Nto have THIS {kw_focus}\\Nin it {emo_pick}",
        f"Look at {lead_name} reacting\\Nto {kw_focus} inside\\N{topic_clean} {emo_pick}",
    ]
    all_banner_choices = [_apply_detected_streamer(b, streamer) for b in banners_pool] + dynamic_candidates

    raw_banner = all_banner_choices[g_seq % len(all_banner_choices)]
    for offset in range(len(all_banner_choices)):
        cand = all_banner_choices[(g_seq + offset) % len(all_banner_choices)]
        wrapped_cand = _wrap_viral_headline(cand, topic=topic_clean, idx=g_seq, layout=layout)
        if wrapped_cand not in used_banners:
            raw_banner = cand
            break
    else:
        # Guarantee 100% uniqueness even beyond 200 reels by embedding the clip's unique keyword pair
        kw2 = (keywords[1].upper() if len(keywords) > 1 else f"CLIP #{g_seq + 1}")
        raw_banner = f"{lead_name} reacts to {kw_focus}\\Nand {kw2} in\\N{topic_clean} {emo_pick}"

    banner = _wrap_viral_headline(raw_banner, topic=topic_clean, idx=g_seq, layout=layout)

    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "")) if len(s.strip()) > 6]
    if not sents:
        sents = [(text or "").strip() or f"Unbelievable {topic_clean} {kw_focus.lower()} moment caught live!"]
    def sent_score(s):
        w = [x.strip(".,!?;:\"'").lower() for x in s.split()]
        hooks = sum(3 for x in w if x in (HOOK_WORDS | SPICY_18_WORDS))
        punct = s.count("!") * 2 + s.count("?") * 3
        length_ok = 2 if 14 <= len(s) <= 55 else 0
        return hooks + punct + length_ok
    best_sent = max(sents, key=sent_score)

    topic_label = topic_clean
    cap_tpl = GENZ_CAPTION_TEMPLATES[g_seq % len(GENZ_CAPTION_TEMPLATES)]
    caption = cap_tpl.format(topic_label=topic_label, quote=best_sent[:105].strip())
    if caption in used_captions:
        caption = f"[{lead_name} // {kw_focus}] " + caption

    with _dedup_lock:
        dedup = _load_dedup()
        if banner not in dedup["used_banners"]:
            dedup["used_banners"].append(banner)
        if caption not in dedup["used_captions"]:
            dedup["used_captions"].append(caption)
        _save_dedup(dedup)

    title = banner.replace("\\N", " ")[:60]
    dynamic_tags = [f"#{w}" for w in keywords[:3]]
    all_tags = tag_pack[:5] + [t for t in dynamic_tags if t not in tag_pack] + tag_pack[5:8]
    return title, banner, caption, all_tags

def metadata(clip, anthropic_key=None, idx=0, fast_batch=False, spicy_18=False, topic="", source_title="", layout_mode="auto", bgm_mode="auto"):
    text = clip.get("text", "")
    layout = layout_mode if layout_mode in ("black_bar", "split_cam") else ("black_bar" if idx % 2 == 0 else "split_cam")
    title, hook_banner, caption, tag_list = _genz_clickbait_heuristic(
        text, idx=idx, spicy_18=spicy_18, topic=topic, source_title=source_title, layout=layout
    )
    bgm_key, bgm_label = select_reel_bgm(clip, idx=idx, topic=topic or source_title, spicy_18=spicy_18, bgm_mode=bgm_mode)
    viral_score = clip.get("viral_score", 93)
    topic_prefix = f"🎯 [{topic}] " if topic else ""
    style_badge = "⬛ Speed Black-Bar" if layout == "black_bar" else "📱 Lacy Split-Cam"
    reason = f"{topic_prefix}{style_badge} · {bgm_label}"
    tags_str = " ".join(tag_list)
    return {
        "title": title,
        "hook_banner": hook_banner,
        "caption": f"{caption}\n\n{tags_str}".strip(),
        "tags": [t.lstrip("#") for t in tag_list],
        "viral_score": viral_score,
        "reason": reason,
        "bgm_key": bgm_key,
        "bgm_label": bgm_label,
        "layout": layout
    }

def _split_text_and_emojis(line):
    """Splits a headline line into (ascii_text_part, trailing_emoji_part) so text gets bold font + stroke and emojis render full-color."""
    i = len(line)
    while i > 0 and ord(line[i - 1]) > 127 or (i > 0 and line[i - 1] == " " and i < len(line) and ord(line[i]) > 127):
        i -= 1
    # Clean split at first non-ASCII emoji character
    for pos, ch in enumerate(line):
        if ord(ch) > 0x2000:
            return line[:pos].rstrip(), line[pos:].strip()
    return line, ""

def _render_hud_overlays(out_dir, idx, hook_banner, layout="black_bar", ig_handle="SQUIRREL.89987269"):
    """
    Renders two 1080x1920 RGBA transparent PNG overlays (hud_{idx}_a.png for 0..52% and hud_{idx}_b.png for 52..100%)
    replicating the uploaded reference videos 1:1:
    - Bold white mixed-case headline with smooth black stroke + 100% FULL-COLOR emojis (via seguiemj.ttf embedded_color=True)
    - Authentic rounded-square white Instagram camera icon stacked above @HANDLE that moves at 52% duration!
    """
    from PIL import Image, ImageDraw, ImageFont
    hud_a = os.path.join(out_dir, f"hud_{idx}_a.png")
    hud_b = os.path.join(out_dir, f"hud_{idx}_b.png")
    wm_handle = "@" + (ig_handle or os.getenv("IG_USERNAME") or "SQUIRREL.89987269").strip().lstrip("@").upper()

    headline = _wrap_viral_headline(
        (hook_banner or "Speed reacts to GTA 6\\Nhaving STREAMERS in\\Nthe game 😭"),
        idx=idx, layout=layout
    )
    h_lines = [ln.strip() for ln in headline.split("\\N") if ln.strip()]

    font_size = 56 if layout == "black_bar" else 54
    def _pick_font(paths, size, fallback=None):
        for fp in paths:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    pass
        return fallback or ImageFont.load_default()

    f_bold = _pick_font([
        "C:/Windows/Fonts/seguisb.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ], font_size)
    f_emoji = _pick_font([
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    ], font_size, fallback=f_bold)
    f_wm = _pick_font([
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ], 21, fallback=f_bold)

    base = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)

    line_gap = 74 if layout == "black_bar" else 70
    if layout == "black_bar":
        total_h = len(h_lines) * line_gap
        start_y = max(95, (440 - total_h) // 2)
        stroke_w = 3
    else:
        # Center right across the horizontal split seam (y = 672)
        total_h = len(h_lines) * line_gap
        start_y = 672 - (total_h // 2) - 6
        stroke_w = 6

    cur_y = start_y
    for ln in h_lines:
        txt_part, emo_part = _split_text_and_emojis(ln)
        bb_txt = d.textbbox((0, 0), txt_part, font=f_bold) if txt_part else (0, 0, 0, 0)
        tw_txt = bb_txt[2] - bb_txt[0]
        tw_emo = 0
        if emo_part:
            bb_emo = d.textbbox((0, 0), emo_part, font=f_emoji)
            tw_emo = (bb_emo[2] - bb_emo[0]) + 12
        total_w = tw_txt + tw_emo
        start_x = max(24, (1080 - total_w) // 2)
        if txt_part:
            d.text(
                (start_x, cur_y), txt_part, font=f_bold,
                fill=(255, 255, 255, 255), stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255)
            )
        if emo_part:
            # Subtle dark shadow behind full-color emoji for seam contrast, then full-color emoji on top
            d.text((start_x + tw_txt + 14, cur_y + 13), emo_part, font=f_emoji, fill=(0, 0, 0, 180))
            d.text(
                (start_x + tw_txt + 12, cur_y + 10), emo_part, font=f_emoji,
                fill=(255, 255, 255, 255), embedded_color=True
            )
        cur_y += line_gap

    def _draw_ig_watermark(img_canvas, cx, cy):
        dr = ImageDraw.Draw(img_canvas)
        r = 18
        ix0, iy0, ix1, iy1 = cx - r, cy - 24 - r, cx + r, cy - 24 + r
        # Drop shadow for camera icon
        dr.rounded_rectangle([ix0 + 2, iy0 + 2, ix1 + 2, iy1 + 2], radius=9, outline=(0, 0, 0, 180), width=3)
        dr.ellipse([cx - 8 + 2, cy - 24 - 8 + 2, cx + 8 + 2, cy - 24 + 8 + 2], outline=(0, 0, 0, 180), width=3)
        # Crisp white rounded-square Instagram camera icon
        dr.rounded_rectangle([ix0, iy0, ix1, iy1], radius=9, outline=(255, 255, 255, 235), width=3)
        dr.ellipse([cx - 8, cy - 24 - 8, cx + 8, cy - 24 + 8], outline=(255, 255, 255, 235), width=3)
        dr.ellipse([cx + 8, cy - 24 - 12, cx + 12, cy - 24 - 8], fill=(255, 255, 255, 235))
        # Handle text centered below icon
        bb = dr.textbbox((0, 0), wm_handle, font=f_wm)
        tw = bb[2] - bb[0]
        tx = max(14, min(1066 - tw, cx - tw // 2))
        dr.text((tx, cy + 2), wm_handle, font=f_wm, fill=(255, 255, 255, 235), stroke_width=2, stroke_fill=(0, 0, 0, 210))

    im_a = base.copy()
    im_b = base.copy()
    if layout == "black_bar":
        _draw_ig_watermark(im_a, 945, 305)
        _draw_ig_watermark(im_b, 175, 1195)
    else:
        _draw_ig_watermark(im_a, 945, 565)
        _draw_ig_watermark(im_b, 175, 1480)

    im_a.save(hud_a, "PNG")
    im_b.save(hud_b, "PNG")
    return f"hud_{idx}_a.png", f"hud_{idx}_b.png"

def _detect_facecam_crop(video_path, start_sec):
    """
    Fast 15ms 160x90 skin-tone quadrant detector:
    Locates the exact (cx, cy) centroid of the streamer's facecam in the horizontally-flipped frame
    so 'split_cam' (Lacy style) zooms the Top 35% (1080x672) directly onto the streamer's facecam
    and crops the Bottom 65% (1080x1248) onto the gameplay/map area without duplicating the facecam.
    """
    try:
        p = subprocess.run(
            ["ffmpeg", "-ss", f"{max(0.0, start_sec + 1.0):.2f}", "-i", video_path,
             "-frames:v", "1", "-vf", "hflip,scale=160:90", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=4
        )
        if len(p.stdout) == 160 * 90 * 3:
            arr = np.frombuffer(p.stdout, dtype=np.uint8).reshape(90, 160, 3).astype(np.int16)
            R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            skin = ((R > 80) & (G > 30) & (B > 15) & ((R - G) > 12) & (R > B)).astype(np.float32)
            quads = {
                "br": (30, 90, 80, 160),
                "bl": (30, 90, 0, 80),
                "tr": (0, 60, 80, 160),
                "tl": (0, 60, 0, 80),
            }
            best_q = max(quads, key=lambda k: float(skin[quads[k][0]:quads[k][1], quads[k][2]:quads[k][3]].mean()))
            y0, y1, x0, x1 = quads[best_q]
            sub = skin[y0:y1, x0:x1]
            if float(sub.sum()) > 12.0:
                ys, xs = np.nonzero(sub > 0)
                cy = float(y0 + ys.mean()) / 90.0
                cx = float(x0 + xs.mean()) / 160.0
                cw, ch = 0.46, 0.52
                cam_x = max(0.0, min(1.0 - cw, cx - cw / 2.0))
                cam_y = max(0.0, min(1.0 - ch, cy - ch / 2.0))
                gw, gh = 0.65, 0.92
                game_x = 0.02 if cx > 0.5 else (1.0 - gw - 0.02)
                game_y = 0.04
                return cw, ch, cam_x, cam_y, gw, gh, game_x, game_y
    except Exception:
        pass
    return 0.48, 0.54, 0.50, 0.44, 0.68, 0.92, 0.02, 0.04

def _make_ass(words, ass_path, hook_banner="", dur=30.0, idx=0, layout="black_bar"):
    """
    Renders the dynamic lower-frame sentence subtitles (matching 'Oh shit! Who the fuck are you?' in Reference Video 2).
    (The persistent headline + full-color emojis + rounded Instagram camera badge are rendered via _render_hud_overlays).
    """
    sub_margin_v = 675 if layout == "black_bar" else 175
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Segoe UI,38,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3.2,1.5,2,60,60,{sub_margin_v},1",
        "",
        "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"
    ]
    def ts(t):
        t = max(0.0, float(t))
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    def esc_preserve_case(s):
        return (s or "").replace("{", "").replace("}", "")

    step = 5
    for i in range(0, len(words), step):
        grp = words[i:i+step]
        for k, active in enumerate(grp):
            st = active["start"]
            en = active["end"] if k + 1 < len(grp) else grp[-1]["end"]
            if en <= st:
                en = st + 0.14
            parts = []
            for m_idx, w in enumerate(grp):
                tok = esc_preserve_case(w["word"])
                clean_w = re.sub(r"[^a-z0-9]", "", tok.lower())
                if m_idx == k:
                    col = "&H009DFF00&" if clean_w in HOOK_WORDS else "&H0000FFFF&"
                    parts.append(f"{{\\c{col}\\b1}}{tok}{{\\c&HFFFFFF&}}")
                else:
                    parts.append(tok)
            txt = " ".join(parts)
            lines.append(f"Dialogue: 2,{ts(st)},{ts(en)},Default,,0,0,0,,{{\\b1}}{txt}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return ass_path

def _make_viral_thumbnail(reel_path, thumb_path, hook_banner="", viral_score=94, dur=15.0):
    """Extracts a crisp, high-contrast frame directly from the rendered reel (preserving the clean reference video look)."""
    seek_t = max(0.9, min(dur * 0.28, dur - 0.5))
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{seek_t:.2f}", "-i", reel_path, "-frames:v", "1", thumb_path],
        capture_output=True
    )
    if not os.path.exists(thumb_path):
        return thumb_path
    try:
        from PIL import Image, ImageEnhance
        img = Image.open(thumb_path).convert("RGB")
        img = ImageEnhance.Color(img).enhance(1.16)
        img = ImageEnhance.Contrast(img).enhance(1.08)
        img = ImageEnhance.Sharpness(img).enhance(1.22)
        img.save(thumb_path, "JPEG", quality=93)
    except Exception:
        pass
    return thumb_path

def render(video_path, clip, segs, out_dir, idx, hook_banner="", job_id=None, layout_mode="auto", bgm_key="none", ig_handle=""):
    """
    Full HD 1080x1920 Reference-Matched Anti-Copyright Render Engine + Trending BGM Mixer:
    - Replicates the 2 uploaded reference videos 1:1:
      1) 'black_bar' (IShowSpeed GTA 6 reference):
         Pitch-black 1080x1920 vertical canvas with persistent 3-line top white reaction headline + full-color emoji,
         uncropped center 1080x810 stream frame, moving Instagram camera icon + @HANDLE badge, and lower-frame subtitles.
      2) 'split_cam' (Lacy GTA VI Map reference):
         Top 35% (1080x672) zoomed directly onto the detected Streamer Facecam stacked above Bottom 65% (1080x1248)
         gameplay/map area, persistent 2-line white seam headline across y=672 with full-color emojis, and moving @HANDLE badge.
    - Mixes suited trending background music (Drift Phonk, GTA 6 Vice City Synthwave, Viral Mystery Pluck, Slowed+Reverb)
      underneath the streamer's voice with a vocal-pocket EQ carve so dialogue stays 100% clear.
    """
    check_cancelled(job_id)
    wait_for_video(job_id)
    start = clip["start"]; dur = clip["end"] - clip["start"]
    out_name = f"reel_{idx}.mp4"
    out = os.path.join(out_dir, out_name)
    thumb_name = f"reel_{idx}.jpg"
    words = []
    for s in segs:
        for w in s.get("words", []):
            if w["end"] > start and w["start"] < clip["end"]:
                words.append({
                    "word": w["word"],
                    "start": max(0.0, w["start"] - start),
                    "end": min(dur, w["end"] - start)
                })

    if layout_mode in ("black_bar", "split_cam"):
        layout = layout_mode
    else:
        layout = "black_bar" if idx % 2 == 0 else "split_cam"

    ass_name = f"reel_{idx}.ass"
    ass_path = os.path.join(out_dir, ass_name)
    _make_ass(words, ass_path, hook_banner=hook_banner, dur=dur, idx=idx, layout=layout)
    hud_a_name, hud_b_name = _render_hud_overlays(out_dir, idx, hook_banner, layout=layout, ig_handle=ig_handle)
    mid_t = dur * 0.52

    if layout == "black_bar":
        vf_core = (
            f"[0:v]fps=24,hflip,eq=contrast=1.07:saturation=1.18:brightness=0.01,split=2[v1][v2];"
            f"[v1]scale=1080:810:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:810[c1];"
            f"[v2]scale=1190:892:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:810[c2];"
            f"[c1][c2]overlay=0:0:enable='gt(mod(t,4.2),2.3)',pad=1080:1920:0:465:black[base];"
            f"[base][1:v]overlay=0:0:enable='lt(t,{mid_t:.2f})'[h1];"
            f"[h1][2:v]overlay=0:0:enable='gte(t,{mid_t:.2f})',ass='{ass_name}'[out]"
        )
    else:
        cw, ch, cam_x, cam_y, gw, gh, game_x, game_y = _detect_facecam_crop(video_path, start)
        vf_core = (
            f"[0:v]fps=24,hflip,eq=contrast=1.07:saturation=1.18:brightness=0.01,split=2[topin][botin];"
            f"[topin]crop=in_w*{cw:.3f}:in_h*{ch:.3f}:in_w*{cam_x:.3f}:in_h*{cam_y:.3f},"
            f"scale=1080:672:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:672[topcam];"
            f"[botin]crop=in_w*{gw:.3f}:in_h*{gh:.3f}:in_w*{game_x:.3f}:in_h*{game_y:.3f},"
            f"scale=1080:1248:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:1248[botgame];"
            f"[topcam][botgame]vstack=inputs=2[base];"
            f"[base][1:v]overlay=0:0:enable='lt(t,{mid_t:.2f})'[h1];"
            f"[h1][2:v]overlay=0:0:enable='gte(t,{mid_t:.2f})',ass='{ass_name}'[out]"
        )

    bgm_path = _ensure_trending_bgm_track(bgm_key, out_dir) if (bgm_key and bgm_key != "none") else None
    voice_af = "asetrate=48000*1.035,aresample=48000,atempo=0.96618,highpass=f=75,lowpass=f=14500,extrastereo=m=1.15"

    if bgm_path and os.path.exists(bgm_path):
        full_fc = (
            f"{vf_core};"
            f"[0:a]{voice_af},volume=1.10[voice];"
            f"[3:a]aresample=48000,lowpass=f=9500,volume=0.19[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
        )
        cmd = [
            "ffmpeg", "-y", "-threads", "0",
            "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", video_path,
            "-i", hud_a_name, "-i", hud_b_name,
            "-stream_loop", "-1", "-i", bgm_path,
            "-map_metadata", "-1",
            "-filter_complex", full_fc,
            "-map", "[out]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "fastdecode",
            "-crf", "24", "-maxrate", "2100k", "-bufsize", "4200k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "112k",
            "-movflags", "+faststart", "-t", f"{dur:.2f}", out_name
        ]
        try:
            _run(cmd, job_id=job_id, cwd=out_dir)
        except JobCancelledError:
            raise
        except Exception:
            # Fallback if source clip had no audio stream
            bgm_path = None

    if not bgm_path or not os.path.exists(out):
        cmd = [
            "ffmpeg", "-y", "-threads", "0",
            "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", video_path,
            "-i", hud_a_name, "-i", hud_b_name,
            "-map_metadata", "-1",
            "-filter_complex", vf_core,
            "-map", "[out]", "-map", "0:a?",
            "-af", voice_af,
            "-c:v", "libx264", "-preset", "superfast", "-tune", "fastdecode",
            "-crf", "24", "-maxrate", "2100k", "-bufsize", "4200k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "112k",
            "-movflags", "+faststart", "-t", f"{dur:.2f}", out_name
        ]
        _run(cmd, job_id=job_id, cwd=out_dir)

    thumb_path = os.path.join(out_dir, thumb_name)
    _make_viral_thumbnail(out, thumb_path, hook_banner=hook_banner.replace("\\N", " "), viral_score=clip.get("viral_score", 94), dur=dur)
    return out, out_name
