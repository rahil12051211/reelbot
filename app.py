import os, sys, uuid, time, json, queue, shutil, subprocess, threading, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
import pipeline, uploaders

load_dotenv()
app = Flask(__name__)
JOBS = {}
IG_CREDS = {}
OUTPUT_ROOT = "/tmp/reelbot_output" if os.environ.get("VERCEL") else os.path.join(os.path.dirname(__file__), "output")
RETENTION_SECONDS = 2 * 24 * 3600  # 2 days (48 hours)
_meta_lock = threading.Lock()
_ap_lock = threading.Lock()

AUTOPILOT_STATE = {
    "uploaded_count": 0,
    "target_limit": 200,
    "topic": "",
    "shutdown_triggered": False
}

def _trigger_system_shutdown(job_id=None):
    """
    Close all open files & app windows on the laptop, flush all ReelBot state,
    terminate all child processes, and shut down the Windows system after 200 uploaded reels.
    """
    with _ap_lock:
        if AUTOPILOT_STATE["shutdown_triggered"]:
            return
        AUTOPILOT_STATE["shutdown_triggered"] = True
    if job_id and job_id in JOBS:
        JOBS[job_id].update({
            "status": f"✅ {AUTOPILOT_STATE['uploaded_count']}/{AUTOPILOT_STATE['target_limit']} REELS UPLOADED — CLOSING ALL FILES & SHUTTING DOWN LAPTOP",
            "stage": 7,
            "progress": 100
        })
        _save_job_meta(job_id)

    def _shutdown_worker():
        time.sleep(3.0)
        try:
            pipeline.cancel_all_procs()
            for jid in list(JOBS.keys()):
                _save_job_meta(jid)
            sys.stdout.flush()
            sys.stderr.flush()
            if os.name == "nt":
                # 1. Gracefully close all open user application windows & files on the laptop
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { try { $_.CloseMainWindow() | Out-Null } catch {} }"],
                    capture_output=True, timeout=6
                )
                # 2. Trigger Windows laptop system shutdown
                subprocess.Popen(
                    ["shutdown", "/s", "/f", "/t", "8", "/c",
                     "ReelBot Autopilot: 200/200 Viral Reels Uploaded! All files closed — shutting down system."],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_shutdown_worker, daemon=True).start()

def _meta_path(job_id):
    return os.path.join(OUTPUT_ROOT, job_id, "meta.json")

def _save_job_meta(job_id):
    job = JOBS.get(job_id)
    if not job:
        return
    out_dir = os.path.join(OUTPUT_ROOT, job_id)
    if not os.path.isdir(out_dir):
        return
    with _meta_lock:
        try:
            with open(_meta_path(job_id), "w", encoding="utf-8") as f:
                json.dump({
                    "id": job_id,
                    "url": job.get("url", ""),
                    "topic": job.get("topic", ""),
                    "source_title": job.get("source_title", ""),
                    "created_at": job.get("created_at", time.time()),
                    "clips": job.get("clips", [])
                }, f, indent=2)
        except Exception:
            pass

def cleanup_expired_reels():
    """Delete any rendered reels (and empty job folders) older than 2 days (48 hours)."""
    if not os.path.isdir(OUTPUT_ROOT):
        return
    now = time.time()
    with _meta_lock:
        for jid in os.listdir(OUTPUT_ROOT):
            if jid.startswith("_"):
                continue
            jdir = os.path.join(OUTPUT_ROOT, jid)
            if not os.path.isdir(jdir):
                continue
            mpath = os.path.join(jdir, "meta.json")
            changed = False
            if os.path.exists(mpath):
                try:
                    with open(mpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    kept = []
                    for c in data.get("clips", []):
                        fname = c.get("file")
                        if not fname:
                            continue
                        fpath = os.path.join(jdir, fname)
                        ctime = c.get("created_at") or data.get("created_at") or (
                            os.path.getmtime(fpath) if os.path.exists(fpath) else now
                        )
                        if now - ctime >= RETENTION_SECONDS:
                            for ext in (".mp4", ".ass", ".jpg"):
                                p_ext = os.path.join(jdir, fname.rsplit(".", 1)[0] + ext)
                                if os.path.exists(p_ext):
                                    try: os.remove(p_ext)
                                    except Exception: pass
                            changed = True
                        elif os.path.exists(fpath):
                            kept.append(c)
                    if changed:
                        data["clips"] = kept
                        with open(mpath, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                except Exception:
                    pass
            for fn in list(os.listdir(jdir)):
                if fn.startswith("reel_") and fn.endswith((".mp4", ".jpg", ".ass")):
                    fp = os.path.join(jdir, fn)
                    try:
                        if now - os.path.getmtime(fp) >= RETENTION_SECONDS:
                            os.remove(fp)
                    except Exception:
                        pass
            if jid not in JOBS:
                has_reels = any(fn.startswith("reel_") and fn.endswith(".mp4") for fn in os.listdir(jdir))
                try:
                    if not has_reels and (now - os.path.getmtime(jdir) >= RETENTION_SECONDS):
                        shutil.rmtree(jdir, ignore_errors=True)
                except Exception:
                    pass

def _periodic_cleanup():
    while True:
        try:
            cleanup_expired_reels()
        except Exception:
            pass
        time.sleep(300)

if not os.environ.get("VERCEL"):
    threading.Thread(target=_periodic_cleanup, daemon=True).start()

def list_vault_reels():
    cleanup_expired_reels()
    if not os.path.isdir(OUTPUT_ROOT):
        return []
    now = time.time()
    vault = []
    for jid in os.listdir(OUTPUT_ROOT):
        if jid.startswith("_"):
            continue
        jdir = os.path.join(OUTPUT_ROOT, jid)
        if not os.path.isdir(jdir):
            continue
        mpath = os.path.join(jdir, "meta.json")
        known_files = set()
        if os.path.exists(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for idx, c in enumerate(data.get("clips", [])):
                    fname = c.get("file")
                    if not fname:
                        continue
                    fpath = os.path.join(jdir, fname)
                    if not os.path.exists(fpath):
                        continue
                    known_files.add(fname)
                    tname = fname.rsplit(".", 1)[0] + ".jpg"
                    has_thumb = os.path.exists(os.path.join(jdir, tname))
                    ctime = float(c.get("created_at") or data.get("created_at") or os.path.getmtime(fpath))
                    expires_at = ctime + RETENTION_SECONDS
                    rem = max(0, int(expires_at - now))
                    mem_clip = None
                    if jid in JOBS and idx < len(JOBS[jid].get("clips", [])):
                        mem_clip = JOBS[jid]["clips"][idx]
                    vault.append({
                        "job_id": jid,
                        "idx": idx,
                        "file": fname,
                        "thumb": tname if has_thumb else None,
                        "title": (mem_clip or c).get("title") or f"AI Reel #{idx+1}",
                        "caption": (mem_clip or c).get("caption") or "",
                        "tags": (mem_clip or c).get("tags") or [],
                        "viral_score": (mem_clip or c).get("viral_score", 92),
                        "reason": (mem_clip or c).get("reason", ""),
                        "start": c.get("start", 0),
                        "end": c.get("end", 20),
                        "created_at": ctime,
                        "expires_at": expires_at,
                        "remaining_seconds": rem,
                        "ig_status": (mem_clip or c).get("ig_status"),
                        "ig_url": (mem_clip or c).get("ig_url"),
                        "yt_status": (mem_clip or c).get("yt_status"),
                    })
            except Exception:
                pass
        for fn in os.listdir(jdir):
            if fn.startswith("reel_") and fn.endswith(".mp4") and fn not in known_files:
                fpath = os.path.join(jdir, fn)
                tname = fn.rsplit(".", 1)[0] + ".jpg"
                has_thumb = os.path.exists(os.path.join(jdir, tname))
                ctime = os.path.getmtime(fpath)
                expires_at = ctime + RETENTION_SECONDS
                if expires_at > now:
                    vault.append({
                        "job_id": jid,
                        "idx": 0,
                        "file": fn,
                        "thumb": tname if has_thumb else None,
                        "title": f"AI Reel ({fn})",
                        "caption": "#cheatcodes #fyp #viral #reels #shorts",
                        "tags": ["cheatcodes", "fyp", "viral", "reels", "shorts"],
                        "viral_score": 89,
                        "reason": "Recovered from disk",
                        "start": 0,
                        "end": 20,
                        "created_at": ctime,
                        "expires_at": expires_at,
                        "remaining_seconds": max(0, int(expires_at - now)),
                        "ig_status": None,
                        "ig_url": None,
                        "yt_status": None,
                    })
    vault.sort(key=lambda r: r["created_at"], reverse=True)
    return vault

def run_job(job_id, url, n_reels, live_min, model_size, auto_ig, auto_yt, spicy_18=False, topic="", layout_mode="auto", bgm_mode="auto"):
    job = JOBS[job_id]
    out_dir = os.path.join(OUTPUT_ROOT, job_id)
    url_key = pipeline._url_cache_key(url)
    try:
        user, password = _get_ig_creds(job_id)
        if user and password and (not uploaders.IG_STATUS.get("connected") or uploaders.IG_STATUS.get("username") != user):
            job.update({"status": f"verifying Instagram login (@{user})…", "stage": 1, "progress": 5})
            uploaders.verify_instagram(user, password)

        # Cap n_reels to remaining quota before reaching 200 uploaded reels
        rem_quota = max(1, AUTOPILOT_STATE["target_limit"] - AUTOPILOT_STATE["uploaded_count"])
        n_reels = min(int(n_reels), rem_quota)

        pipeline.check_cancelled(job_id)
        topic_tag = f" [Topic: {topic}]" if topic else ""
        src_label = f" ({job['source_title'][:34]}…)" if job.get("source_title") else ""
        job.update({"status": f"ingesting stream{topic_tag}{src_label}…", "stage": 1, "progress": 10})
        video = pipeline.download(
            url, out_dir, live_min, job_id=job_id, n_reels=n_reels,
            topic=topic, source_title=job.get("source_title", ""),
            on_status=lambda msg: job.update({"status": msg})
        )
        detected_src_title = pipeline.read_source_title(out_dir) or job.get("source_title") or ""
        if detected_src_title:
            job["source_title"] = detected_src_title
        pipeline.check_cancelled(job_id)
        job.update({"status": "analyzing vocal energy & vectorized RMS wave…", "stage": 2, "progress": 22})
        rms, wav = pipeline.audio_rms(video, out_dir, job_id=job_id)
        pipeline.check_cancelled(job_id)
        job.update({"status": f"transcribing speech ({model_size}) — 0%…", "stage": 3, "progress": 28})
        def _on_prog(pct, cur_s, tot_s):
            job["status"] = f"transcribing speech ({model_size}) — {pct}% ({int(cur_s)}s / {int(tot_s)}s)"
            job["progress"] = 28 + int(pct * 0.30)
        segs = pipeline.transcribe(wav, model_size, on_progress=_on_prog, job_id=job_id, rms=rms, n_reels=n_reels)
        pipeline.check_cancelled(job_id)
        job.update({"status": f"picking {n_reels} non-duplicate{topic_tag} cheatcode & viral moments…", "stage": 4, "progress": 60})
        clips = pipeline.pick(segs, rms, n_reels, spicy_18=spicy_18, topic=topic, url_key=url_key)
        job["clips"] = [{
            "start": c["start"], "end": c["end"], "text": c["text"],
            "viral_score": c.get("viral_score", 92), "status": "rendering"
        } for c in clips]

        job.update({"status": "finalizing high-speed video buffer…", "stage": 5, "progress": 64})
        pipeline.wait_for_video(job_id)

        anth_key = os.getenv("ANTHROPIC_API_KEY")
        total = max(1, len(clips))
        fast_batch = (total >= 4)
        should_ig = (auto_ig or _ig_ready(job_id)) and _ig_ready(job_id)
        ig_handle = uploaders.IG_STATUS.get("username") or user or "Instagram"

        ig_queue = queue.Queue()
        ig_uploaded_count = 0
        ig_count_lock = threading.Lock()
        n_ig_workers = 2

        def _ig_consumer():
            nonlocal ig_uploaded_count
            while True:
                item = ig_queue.get()
                if item is None:
                    ig_queue.task_done()
                    break
                idx_item, path_item = item
                try:
                    if job_id not in pipeline.CANCELLED_JOBS and not AUTOPILOT_STATE["shutdown_triggered"]:
                        _upload_ig(job_id, idx_item, path_item)
                        if job["clips"][idx_item].get("ig_status") == "done":
                            with ig_count_lock:
                                ig_uploaded_count += 1
                            with _ap_lock:
                                AUTOPILOT_STATE["uploaded_count"] += 1
                                cur_total = AUTOPILOT_STATE["uploaded_count"]
                                lim = AUTOPILOT_STATE["target_limit"]
                            if cur_total >= lim:
                                _trigger_system_shutdown(job_id)
                                ig_queue.task_done()
                                break
                finally:
                    try: ig_queue.task_done()
                    except ValueError: pass

        ig_threads = []
        if should_ig:
            for _ in range(n_ig_workers):
                t_ig = threading.Thread(target=_ig_consumer, daemon=True)
                t_ig.start()
                ig_threads.append(t_ig)

        job.update({
            "status": f"rendering {total} anti-copyright 1080p reels{topic_tag} in parallel…",
            "stage": 5, "progress": 66
        })
        done_count = 0

        def _render_one(i, clip):
            pipeline.check_cancelled(job_id)
            meta = pipeline.metadata(
                clip, anth_key, idx=i, fast_batch=fast_batch, spicy_18=spicy_18,
                topic=topic, source_title=job.get("source_title", ""),
                layout_mode=layout_mode, bgm_mode=bgm_mode
            )
            out_path, out_name = pipeline.render(
                video, clip, segs, out_dir, i,
                hook_banner=meta.get("hook_banner", ""),
                job_id=job_id,
                layout_mode=meta.get("layout", layout_mode),
                bgm_key=meta.get("bgm_key", "none"),
                ig_handle=ig_handle
            )
            now_ts = time.time()
            tname = f"reel_{i}.jpg"
            job["clips"][i].update({
                "file": out_name,
                "thumb": tname if os.path.exists(os.path.join(out_dir, tname)) else None,
                "title": meta["title"], "caption": meta["caption"],
                "hook_banner": meta.get("hook_banner", ""),
                "tags": meta["tags"], "viral_score": meta["viral_score"],
                "reason": meta["reason"],
                "bgm_label": meta.get("bgm_label", ""),
                "created_at": now_ts,
                "expires_at": now_ts + RETENTION_SECONDS, "status": "done"
            })
            _save_job_meta(job_id)
            if should_ig:
                ig_queue.put((i, out_path))
            if auto_yt:
                threading.Thread(target=_upload_yt, args=(job_id, i, out_path), daemon=True).start()
            return i

        workers = 1 if os.environ.get("RENDER") else min(6, total)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_render_one, i, clip) for i, clip in enumerate(clips)]
            for fut in as_completed(futs):
                fut.result()
                done_count += 1
                job["progress"] = 66 + int((done_count / total) * (22 if should_ig else 34))
                if should_ig:
                    job["status"] = f"rendered {done_count}/{total} · uploaded {ig_uploaded_count}/{total} to IG (@{ig_handle}) [Total: {AUTOPILOT_STATE['uploaded_count']}/{AUTOPILOT_STATE['target_limit']}]…"
                else:
                    job["status"] = f"rendered {done_count}/{total} anti-copyright reels…"

        if should_ig and ig_threads:
            for _ in ig_threads:
                ig_queue.put(None)
            while any(t_ig.is_alive() for t_ig in ig_threads):
                pipeline.check_cancelled(job_id)
                if AUTOPILOT_STATE["shutdown_triggered"]:
                    break
                job.update({
                    "status": f"turbo-uploading to Instagram (@{ig_handle}) — {ig_uploaded_count}/{total} posted [Total: {AUTOPILOT_STATE['uploaded_count']}/{AUTOPILOT_STATE['target_limit']}]…",
                    "stage": 6,
                    "progress": 88 + int((ig_uploaded_count / total) * 11)
                })
                time.sleep(0.5)

        if not AUTOPILOT_STATE["shutdown_triggered"]:
            job.update({"status": "done", "stage": 7, "progress": 100})
            _save_job_meta(job_id)
    except pipeline.JobCancelledError:
        job["status"] = "stopped by user"
    except Exception as e:
        if job_id in pipeline.CANCELLED_JOBS:
            job["status"] = "stopped by user"
        else:
            job["status"] = f"error: {e}"
            job["error"] = traceback.format_exc()

def _get_ig_creds(job_id):
    u, p = IG_CREDS.get(job_id, ("", ""))
    u = u or (os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    p = p or (os.getenv("IG_PASSWORD") or "")
    return u, p

def _ig_ready(job_id):
    user, password = _get_ig_creds(job_id)
    return bool(uploaders.IG_STATUS.get("connected")) or bool(user and password) or bool(os.getenv("IG_ACCESS_TOKEN"))

def _find_or_load_clip(job_id, idx):
    if job_id in JOBS and idx < len(JOBS[job_id]["clips"]):
        return JOBS[job_id]["clips"][idx]
    mpath = _meta_path(job_id)
    if os.path.exists(mpath):
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        JOBS[job_id] = {"status": "done", "stage": 7, "progress": 100,
                        "created_at": data.get("created_at", time.time()),
                        "clips": data.get("clips", [])}
        if idx < len(JOBS[job_id]["clips"]):
            return JOBS[job_id]["clips"][idx]
    return None

def _upload_ig(job_id, idx, path):
    clip = _find_or_load_clip(job_id, idx)
    if not clip:
        return
    last_err = None
    for attempt in range(2):
        try:
            clip["ig_status"] = "uploading" if attempt == 0 else "retrying upload…"
            _save_job_meta(job_id)
            user, password = _get_ig_creds(job_id)
            if user and password or uploaders.IG_STATUS.get("connected"):
                res = uploaders.instagram_login(path, clip["caption"], user, password)
                if res.get("url"):
                    clip["ig_url"] = res["url"]
            else:
                base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                video_url = f"{base}/files/{job_id}/{clip['file']}"
                uploaders.instagram(video_url, clip["caption"])
            clip["ig_status"] = "done"
            _save_job_meta(job_id)
            return
        except Exception as e:
            last_err = e
            err_s = str(e).lower()
            # Only auto-retry transient network/transcode timeouts (not auth/challenge/bad-password errors)
            if attempt == 0 and any(k in err_s for k in ("timeout", "connection", "transcode", "500", "502", "503", "504", "reset")):
                time.sleep(3.0)
                continue
            break
    clip["ig_status"] = f"error: {last_err}"
    _save_job_meta(job_id)

def _upload_yt(job_id, idx, path):
    clip = _find_or_load_clip(job_id, idx)
    if not clip:
        return
    try:
        clip["yt_status"] = "uploading"
        _save_job_meta(job_id)
        uploaders.youtube(path, clip["title"], clip["caption"], clip.get("tags", ["Shorts"]))
        clip["yt_status"] = "done"
    except Exception as e:
        clip["yt_status"] = f"error: {e}"
    _save_job_meta(job_id)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ig/status")
def ig_status():
    return jsonify(uploaders.IG_STATUS)

@app.route("/api/trending")
def api_trending():
    topic = (request.args.get("topic") or "").strip()
    force = request.args.get("force") == "1"
    return jsonify(pipeline.get_trending_wave(custom_topic=topic, force_next=force))

@app.route("/api/ig/connect", methods=["POST"])
def ig_connect():
    d = request.get_json(silent=True) or {}
    user = (d.get("ig_username") or os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    password = d.get("ig_password") or os.getenv("IG_PASSWORD") or ""
    code = (d.get("ig_2fa") or os.getenv("IG_2FA_CODE") or "").strip()
    st = uploaders.verify_instagram(user, password, code)
    return jsonify(st)

@app.route("/api/jobs", methods=["POST"])
def create_job():
    d = request.json or {}
    if d.get("llm_key"):
        k = d["llm_key"].strip()
        if k.startswith("xai-"):
            os.environ["XAI_API_KEY"] = k
        else:
            os.environ["ANTHROPIC_API_KEY"] = k
    user = (d.get("ig_username") or os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    password = d.get("ig_password") or os.getenv("IG_PASSWORD") or ""
    if d.get("auto_ig") and not (user and password) and not uploaders.IG_STATUS.get("connected") and not os.getenv("IG_ACCESS_TOKEN"):
        return jsonify({"error": "Enter your Instagram username and password and click Connect IG (or uncheck Auto-Upload)"}), 400
    jid = str(uuid.uuid4())[:8]
    spicy_18 = bool(d.get("spicy_18", False))
    topic = (d.get("topic") or "").strip()
    layout_mode = (d.get("layout_mode") or "auto").strip()
    bgm_mode = (d.get("bgm_mode") or "auto").strip()
    JOBS[jid] = {"status": "starting", "stage": 1, "progress": 4, "url": d.get("url", ""),
                 "topic": topic, "source_title": d.get("source_title", ""),
                 "created_at": time.time(), "clips": []}
    IG_CREDS[jid] = (user, password)
    threading.Thread(
        target=run_job,
        args=(jid, d["url"], d.get("n", 5), d.get("live_min", 0),
              d.get("model", "tiny"), d.get("auto_ig", True), d.get("auto_yt", False),
              spicy_18, topic, layout_mode, bgm_mode),
        daemon=True
    ).start()
    return jsonify({"id": jid})

@app.route("/api/autopilot", methods=["POST"])
def start_autopilot():
    """
    Strict User-Topic Autonomous Autopilot Mode:
    Requires the user's explicit topic, discovers only videos matching that topic (cheatcodes + viral),
    prevents duplicates, and auto-shuts down the system after uploading 200 reels!
    """
    d = request.json or {}
    topic = (d.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "Please enter the topic for your Autopilot Reels"}), 400
    if AUTOPILOT_STATE["uploaded_count"] >= AUTOPILOT_STATE["target_limit"]:
        _trigger_system_shutdown()
        return jsonify({"error": "200/200 Reels quota reached — system is shutting down"}), 400

    AUTOPILOT_STATE["topic"] = topic
    if d.get("llm_key"):
        k = d["llm_key"].strip()
        if k.startswith("xai-"):
            os.environ["XAI_API_KEY"] = k
        else:
            os.environ["ANTHROPIC_API_KEY"] = k
    user = (d.get("ig_username") or os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    password = d.get("ig_password") or os.getenv("IG_PASSWORD") or ""
    spicy_18 = bool(d.get("spicy_18", False))
    try:
        chosen = pipeline.discover_autopilot_video(topic=topic, spicy_18=spicy_18)
    except Exception as e:
        return jsonify({"error": f"Autopilot discovery error: {e}"}), 400

    jid = str(uuid.uuid4())[:8]
    JOBS[jid] = {
        "status": f"🤖 Autopilot [{topic}]: {chosen['title'][:42]}…",
        "stage": 1, "progress": 6,
        "url": chosen["url"],
        "topic": topic,
        "source_title": chosen["title"],
        "created_at": time.time(), "clips": []
    }
    layout_mode = (d.get("layout_mode") or "auto").strip()
    bgm_mode = (d.get("bgm_mode") or "auto").strip()
    vid_dur = int(chosen.get("duration") or 120)
    max_unique_per_vid = 1 if vid_dur < 65 else (2 if vid_dur < 140 else 3)
    ap_n = max(1, min(int(d.get("n", 3)), max_unique_per_vid))
    IG_CREDS[jid] = (user, password)
    threading.Thread(
        target=run_job,
        args=(jid, chosen["url"], ap_n, 0,
              d.get("model", "tiny"), d.get("auto_ig", True), d.get("auto_yt", False),
              spicy_18, topic, layout_mode, bgm_mode),
        daemon=True
    ).start()
    return jsonify({
        "id": jid, "url": chosen["url"], "title": chosen["title"],
        "topic": topic, "autopilot": AUTOPILOT_STATE
    })

@app.route("/api/jobs/<jid>")
def get_job(jid):
    if jid not in JOBS:
        return jsonify({"error": "not found"}), 404
    resp = dict(JOBS[jid])
    resp["ig_account"] = uploaders.IG_STATUS
    resp["autopilot"] = AUTOPILOT_STATE
    return jsonify(resp)

@app.route("/api/jobs/<jid>/cancel", methods=["POST"])
def cancel_job(jid):
    pipeline.cancel_job_procs(jid)
    if jid in JOBS:
        JOBS[jid]["status"] = "stopped by user"
    return jsonify({"ok": True})

@app.route("/api/jobs/<jid>/upload/<int:idx>/<platform>", methods=["POST"])
def upload(jid, idx, platform):
    clip = _find_or_load_clip(jid, idx)
    if not clip:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    if d.get("ig_username") or d.get("ig_password"):
        IG_CREDS[jid] = ((d.get("ig_username") or "").strip().lstrip("@"), d.get("ig_password") or "")
    if d.get("caption"):
        clip["caption"] = d["caption"]
        _save_job_meta(jid)
    path = os.path.join(OUTPUT_ROOT, jid, clip["file"])
    if platform == "ig":
        def _manual_ig():
            _upload_ig(jid, idx, path)
            if clip.get("ig_status") == "done":
                with _ap_lock:
                    AUTOPILOT_STATE["uploaded_count"] += 1
                    cur_total = AUTOPILOT_STATE["uploaded_count"]
                    lim = AUTOPILOT_STATE["target_limit"]
                if cur_total >= lim:
                    _trigger_system_shutdown(jid)
        threading.Thread(target=_manual_ig, daemon=True).start()
    elif platform == "yt":
        threading.Thread(target=_upload_yt, args=(jid, idx, path), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/reels")
def get_vault_reels():
    return jsonify({
        "reels": list_vault_reels(),
        "retention_hours": 48,
        "ig_account": uploaders.IG_STATUS,
        "autopilot": AUTOPILOT_STATE
    })

@app.route("/api/reels/<jid>/<filename>", methods=["DELETE"])
def delete_vault_reel(jid, filename):
    jdir = os.path.join(OUTPUT_ROOT, jid)
    for ext in (".mp4", ".ass", ".jpg"):
        p_ext = os.path.join(jdir, filename.rsplit(".", 1)[0] + ext)
        if os.path.exists(p_ext):
            try: os.remove(p_ext)
            except Exception: pass
    mpath = os.path.join(jdir, "meta.json")
    if os.path.exists(mpath):
        with _meta_lock:
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["clips"] = [c for c in data.get("clips", []) if c.get("file") != filename]
                with open(mpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
    if jid in JOBS:
        JOBS[jid]["clips"] = [c for c in JOBS[jid].get("clips", []) if c.get("file") != filename]
    return jsonify({"ok": True})

@app.route("/files/<jid>/<name>")
def serve_file(jid, name):
    return send_from_directory(os.path.join(OUTPUT_ROOT, jid), name)

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=int(os.environ.get("PORT", 5000)), threaded=True)
