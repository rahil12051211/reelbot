import os, sys, uuid, time, json, shutil, threading, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
import pipeline, uploaders

load_dotenv()
app = Flask(__name__)
JOBS = {}
IG_CREDS = {}
OUTPUT_ROOT = os.path.join(os.path.dirname(__file__), "output")
RETENTION_SECONDS = 2 * 24 * 3600  # 2 days (48 hours)
_meta_lock = threading.Lock()

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
                            if os.path.exists(fpath):
                                try: os.remove(fpath)
                                except Exception: pass
                            ass_path = os.path.join(jdir, fname.rsplit(".", 1)[0] + ".ass")
                            if os.path.exists(ass_path):
                                try: os.remove(ass_path)
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
                if fn.startswith("reel_") and fn.endswith(".mp4"):
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

threading.Thread(target=_periodic_cleanup, daemon=True).start()

def list_vault_reels():
    """Return all non-expired AI-generated reels across all jobs, newest first."""
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
                        "title": (mem_clip or c).get("title") or f"AI Reel #{idx+1}",
                        "caption": (mem_clip or c).get("caption") or "",
                        "tags": (mem_clip or c).get("tags") or [],
                        "viral_score": (mem_clip or c).get("viral_score", 88),
                        "reason": (mem_clip or c).get("reason", ""),
                        "start": c.get("start", 0),
                        "end": c.get("end", 30),
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
                ctime = os.path.getmtime(fpath)
                expires_at = ctime + RETENTION_SECONDS
                if expires_at > now:
                    vault.append({
                        "job_id": jid,
                        "idx": 0,
                        "file": fn,
                        "title": f"AI Reel ({fn})",
                        "caption": "#reels #shorts #viral",
                        "tags": ["reels", "shorts", "viral"],
                        "viral_score": 85,
                        "reason": "Recovered from disk",
                        "start": 0,
                        "end": 30,
                        "created_at": ctime,
                        "expires_at": expires_at,
                        "remaining_seconds": max(0, int(expires_at - now)),
                        "ig_status": None,
                        "ig_url": None,
                        "yt_status": None,
                    })
    vault.sort(key=lambda r: r["created_at"], reverse=True)
    return vault

def run_job(job_id, url, n_reels, live_min, model_size, auto_ig, auto_yt):
    job = JOBS[job_id]
    out_dir = os.path.join(OUTPUT_ROOT, job_id)
    try:
        # If Instagram credentials were supplied, verify connection right away so status shows immediately
        user, password = _get_ig_creds(job_id)
        if user and password and (not uploaders.IG_STATUS.get("connected") or uploaders.IG_STATUS.get("username") != user):
            job.update({"status": f"verifying Instagram login (@{user})…", "stage": 1, "progress": 5})
            uploaders.verify_instagram(user, password)

        pipeline.check_cancelled(job_id)
        job.update({"status": "ingesting stream / instant DVR burst…", "stage": 1, "progress": 10})
        video = pipeline.download(url, out_dir, live_min, job_id=job_id)
        pipeline.check_cancelled(job_id)
        job.update({"status": "analyzing vocal energy & audio RMS…", "stage": 2, "progress": 22})
        rms, wav = pipeline.audio_rms(video, out_dir, job_id=job_id)
        pipeline.check_cancelled(job_id)
        job.update({"status": f"transcribing speech ({model_size}) — 0%…", "stage": 3, "progress": 28})
        def _on_prog(pct, cur_s, tot_s):
            job["status"] = f"transcribing speech ({model_size}) — {pct}% ({int(cur_s)}s / {int(tot_s)}s)"
            job["progress"] = 28 + int(pct * 0.35)
        segs = pipeline.transcribe(wav, model_size, on_progress=_on_prog, job_id=job_id)
        pipeline.check_cancelled(job_id)
        job.update({"status": "scoring hooks & ranking viral moments…", "stage": 4, "progress": 66})
        clips = pipeline.pick(segs, rms, n_reels)
        job["clips"] = [{
            "start": c["start"], "end": c["end"], "text": c["text"],
            "viral_score": c.get("viral_score", 85), "status": "rendering"
        } for c in clips]
        anth_key = os.getenv("ANTHROPIC_API_KEY")
        total = max(1, len(clips))
        job.update({
            "status": f"lightning-rendering {total} vertical 9:16 reels in parallel…",
            "stage": 5, "progress": 70
        })
        done_count = 0
        rendered_paths = {}
        def _render_one(i, clip):
            pipeline.check_cancelled(job_id)
            out_path, out_name = pipeline.render(video, clip, segs, out_dir, i, job_id=job_id)
            meta = pipeline.metadata(clip, anth_key)
            now_ts = time.time()
            job["clips"][i].update({
                "file": out_name, "title": meta["title"], "caption": meta["caption"],
                "tags": meta["tags"], "viral_score": meta["viral_score"],
                "reason": meta["reason"], "created_at": now_ts,
                "expires_at": now_ts + RETENTION_SECONDS, "status": "done"
            })
            rendered_paths[i] = out_path
            _save_job_meta(job_id)
            return i

        with ThreadPoolExecutor(max_workers=min(3, total)) as ex:
            futs = [ex.submit(_render_one, i, clip) for i, clip in enumerate(clips)]
            for fut in as_completed(futs):
                fut.result()
                done_count += 1
                job["progress"] = 70 + int((done_count / total) * 18)
                job["status"] = f"rendered {done_count}/{total} reels…"

        # Immediately upload created edits to Instagram first if auto_ig or IG credentials are connected
        should_ig = auto_ig or _ig_ready(job_id)
        if should_ig and _ig_ready(job_id):
            ig_handle = uploaders.IG_STATUS.get("username") or user or "Instagram"
            for i in range(len(clips)):
                pipeline.check_cancelled(job_id)
                job.update({
                    "status": f"uploading reel {i+1}/{total} to Instagram (@{ig_handle})…",
                    "stage": 6,
                    "progress": 88 + int(((i + 0.5) / total) * 11)
                })
                _upload_ig(job_id, i, rendered_paths[i])
        if auto_yt:
            for i in range(len(clips)):
                threading.Thread(target=_upload_yt, args=(job_id, i, rendered_paths[i]), daemon=True).start()

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
    try:
        clip["ig_status"] = "uploading"
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
    except Exception as e:
        clip["ig_status"] = f"error: {e}"
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
        return jsonify({"error": "Enter your Instagram username and password and click Connect Instagram (or uncheck Auto-Publish Instagram)"}), 400
    jid = str(uuid.uuid4())[:8]
    JOBS[jid] = {"status": "starting", "stage": 1, "progress": 4, "url": d.get("url", ""),
                 "created_at": time.time(), "clips": []}
    IG_CREDS[jid] = (user, password)
    threading.Thread(target=run_job, args=(jid, d["url"], d.get("n", 3), d.get("live_min", 0),
                     d.get("model", "tiny"), d.get("auto_ig", True), d.get("auto_yt", False)), daemon=True).start()
    return jsonify({"id": jid})

@app.route("/api/jobs/<jid>")
def get_job(jid):
    if jid not in JOBS:
        return jsonify({"error": "not found"}), 404
    resp = dict(JOBS[jid])
    resp["ig_account"] = uploaders.IG_STATUS
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
        threading.Thread(target=_upload_ig, args=(jid, idx, path), daemon=True).start()
    elif platform == "yt":
        threading.Thread(target=_upload_yt, args=(jid, idx, path), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/reels")
def get_vault_reels():
    return jsonify({"reels": list_vault_reels(), "retention_hours": 48, "ig_account": uploaders.IG_STATUS})

@app.route("/api/reels/<jid>/<filename>", methods=["DELETE"])
def delete_vault_reel(jid, filename):
    jdir = os.path.join(OUTPUT_ROOT, jid)
    fpath = os.path.join(jdir, filename)
    if os.path.exists(fpath):
        try:
            os.remove(fpath)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    ass_path = os.path.join(jdir, filename.rsplit(".", 1)[0] + ".ass")
    if os.path.exists(ass_path):
        try: os.remove(ass_path)
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
    app.run(host="0.0.0.0", debug=False, port=5000, threaded=True)
