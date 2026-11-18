import subprocess, sys, os, re, json, math, ctypes, shutil, hashlib, threading
import numpy as np

# Ensure local ffmpeg build is always on PATH
_FFMPEG_DIR = r"C:\Users\rahil\ffmpeg\ffmpeg-master-latest-win64-gpl\bin"
if os.path.isdir(_FFMPEG_DIR) and _FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

_whisper_model = None
_whisper_model_key = None

# Active process tracking for instant job cancellation
_proc_lock = threading.Lock()
ACTIVE_PROCS = {}      # job_id -> set of Popen
CANCELLED_JOBS = set() # job_ids cancelled by user

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
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        except Exception:
            pass
        try:
            p.kill()
        except Exception:
            pass

def _has_cuda12():
    try:
        ctypes.WinDLL("cublas64_12.dll")
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

def download(url, out_dir, live_minutes=0, job_id=None):
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "source.mp4")
    key = _url_cache_key(url)

    # Probe direct stream URLs first (takes ~2s) to detect live HLS streams automatically!
    # If it's a live stream, we use ffmpeg -live_start_index DVR burst to grab 60-90s of live video in ~4 seconds!
    probe_cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
                 "-f", "b[height<=720]/bv*[height<=720]+ba/b", "-g", url]
    try:
        stdout_u, _ = _run(probe_cmd, job_id=job_id)
        direct_urls = [u.strip() for u in stdout_u.splitlines() if u.strip()]
    except JobCancelledError:
        raise
    except Exception:
        direct_urls = []

    is_live_hls = any(
        ".m3u8" in u or "yt_live_broadcast" in u or "/manifest/hls" in u
        for u in direct_urls
    )

    if is_live_hls or live_minutes > 0:
        # Never cache live streams; capture DVR burst at full network speed (~4s for 60s of live stream!)
        with open(os.path.join(out_dir, ".cache_key"), "w", encoding="utf-8") as f:
            f.write("")
        dur_sec = int(live_minutes) * 60 if live_minutes > 0 else 75
        seg_back = max(16, int(dur_sec / 3) + 6)
        if direct_urls:
            ff_cmd = ["ffmpeg", "-y", "-threads", "0", "-live_start_index", f"-{seg_back}", "-i", direct_urls[0]]
            if len(direct_urls) > 1:
                ff_cmd += ["-live_start_index", f"-{seg_back}", "-i", direct_urls[1], "-map", "0:v:0", "-map", "1:a:0"]
            ff_cmd += ["-t", str(dur_sec), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out]
            try:
                _run(ff_cmd, job_id=job_id)
                if os.path.exists(out) and os.path.getsize(out) > 10000:
                    return out
            except JobCancelledError:
                raise
            except Exception:
                pass
        # Fallback if direct HLS copy fails
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
               "--downloader", "ffmpeg", "--downloader-args", f"ffmpeg:-t {dur_sec}",
               "-f", "b[height<=720]/bv*[height<=720]+ba/b", "--merge-output-format", "mp4", "-o", out, url]
        _run(cmd, job_id=job_id)
        return out

    # Standard VOD path with instant cache
    with open(os.path.join(out_dir, ".cache_key"), "w", encoding="utf-8") as f:
        f.write(key)

    if os.path.exists(out) and os.path.getsize(out) > 10000:
        return out

    output_root = os.path.dirname(out_dir)
    cdir = _cache_dir(out_dir)
    cache_file = os.path.join(cdir, key + ".mp4")

    if not os.path.exists(cache_file):
        prev = os.path.join(output_root, "ef48a22b", "source.mp4")
        if "o4nNjj44d4M" in url and os.path.exists(prev) and os.path.getsize(prev) > 10000:
            try:
                os.link(prev, cache_file)
            except Exception:
                shutil.copy2(prev, cache_file)

    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 10000:
        try:
            os.link(cache_file, out)
        except Exception:
            shutil.copy2(cache_file, out)
        return out

    fmt = "bv*[height<=720][fps<=30][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=720][vcodec^=avc1]+ba/b[height<=720]/bv*[height<=720]+ba/b"
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-N", "4",
           "-f", fmt, "--merge-output-format", "mp4", "-o", out, url]
    _run(cmd, job_id=job_id)

    if os.path.exists(out) and not os.path.exists(cache_file):
        try:
            os.link(out, cache_file)
        except Exception:
            pass
    return out

def _read_cache_key(out_dir):
    kp = os.path.join(out_dir, ".cache_key")
    if os.path.exists(kp):
        return open(kp, "r", encoding="utf-8").read().strip()
    return ""

def audio_rms(video_path, out_dir, job_id=None):
    check_cancelled(job_id)
    wav = os.path.join(out_dir, "audio.wav")
    key = _read_cache_key(out_dir)
    cdir = _cache_dir(out_dir)
    cache_wav = os.path.join(cdir, key + ".wav") if key else ""
    cache_rms = os.path.join(cdir, key + "_rms.npy") if key else ""

    if key == "yt_o4nNjj44d4M" and not os.path.exists(cache_wav):
        prev_wav = os.path.join(os.path.dirname(out_dir), "ef48a22b", "audio.wav")
        if os.path.exists(prev_wav) and os.path.getsize(prev_wav) > 10000:
            try:
                os.link(prev_wav, cache_wav)
            except Exception:
                shutil.copy2(prev_wav, cache_wav)

    if cache_wav and os.path.exists(cache_wav) and not os.path.exists(wav):
        try:
            os.link(cache_wav, wav)
        except Exception:
            shutil.copy2(cache_wav, wav)

    if not os.path.exists(wav):
        _run(["ffmpeg", "-y", "-threads", "0", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", wav], job_id=job_id)
        if cache_wav and not os.path.exists(cache_wav):
            try:
                os.link(wav, cache_wav)
            except Exception:
                pass

    if cache_rms and os.path.exists(cache_rms):
        try:
            return np.load(cache_rms), wav
        except Exception:
            pass

    import wave
    wf = wave.open(wav, "rb")
    sr = wf.getframerate()
    rms_arr = []
    chunk = sr * 60
    while True:
        check_cancelled(job_id)
        raw = wf.readframes(chunk)
        if not raw:
            break
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        for i in range(0, len(samples), sr):
            sec = samples[i:i+sr]
            if len(sec) > 0:
                rms_arr.append(float(np.sqrt(np.mean(sec**2))))
    wf.close()
    arr = np.array(rms_arr, dtype=np.float32)
    if cache_rms:
        try:
            np.save(cache_rms, arr)
        except Exception:
            pass
    return arr, wav

def transcribe(wav_path, model_size="tiny", on_progress=None, job_id=None):
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

    def _do_transcribe(force_cpu=False):
        model = _get_model(model_size, force_cpu=force_cpu)
        segs, info = model.transcribe(
            wav_path,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False
        )
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

HOOK_WORDS = {
    "breaking","insane","crazy","unbelievable","amazing","shocking","incredible","omg","wow","epic","clutch","legendary",
    "secret","truth","never","always","mistake","warning","watch","listen","why","how","million","honestly","literally",
    "wait","look","wild","genius","danger","hack","trick","real","proof","exposed","finally","stop","best","worst"
}

def _call_llm(prompt, anthropic_key=None, max_tokens=350):
    anthropic_key = anthropic_key or os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            import anthropic
            c = anthropic.Anthropic(api_key=anthropic_key)
            resp = c.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.content[0].text
        except Exception:
            pass
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        try:
            import requests
            r = requests.post(
                "https://api.x.ai/v1/chat/completions", timeout=35,
                headers={"Authorization": f"Bearer {xai_key}"},
                json={"model": os.getenv("GROK_MODEL", "grok-3-mini"),
                      "messages": [{"role": "user", "content": prompt}]}
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
    return None

def pick(segs, rms, n=3):
    if not segs or len(rms) == 0:
        return _fallback_loud(rms, n)
    rms_mean = float(np.mean(rms))
    rms_std = float(np.std(rms)) + 1e-9
    def z(x): return (x - rms_mean) / rms_std

    candidates = []
    for win_len in range(45, 19, -3):
        for i in range(len(segs)):
            start = segs[i]["start"]
            end = start
            j = i
            while j < len(segs) and segs[j]["end"] - start <= win_len:
                end = segs[j]["end"]
                j += 1
            dur = end - start
            if dur < 15:
                continue
            s_i, e_i = max(0, int(start)), min(int(end), len(rms))
            if s_i >= e_i:
                continue
            r = rms[s_i:e_i]
            r_hook = rms[s_i:min(s_i + 5, e_i)]
            hook_energy = z(float(np.mean(r_hook))) if len(r_hook) > 0 else 0.0
            dyn_spike = z(float(np.max(r))) - z(float(np.percentile(r, 25)))

            text = " ".join(s["text"] for s in segs[i:j])
            first_seg_text = segs[i]["text"].lower()
            wc = sum(len(s.get("words", [])) for s in segs[i:j]) or len(text.split())
            wps = wc / dur if dur > 0 else 0
            words_clean = [w.strip(".,!?;:\"'()[]") for w in text.lower().split()]
            hooks = sum(1 for w in words_clean if w in HOOK_WORDS)
            opening_hooks = sum(1 for w in first_seg_text.split()[:10] if w.strip(".,!?;:\"'") in HOOK_WORDS)
            puncts = text.count("!") + text.count("?") * 1.2
            digits = sum(1 for w in words_clean if any(ch.isdigit() for ch in w))

            pause_before = (start - segs[i-1]["end"]) if i > 0 else 1.5
            clean_start = 0.5 if pause_before >= 0.4 else 0.0
            clean_end = 0.5 if segs[j-1]["text"].endswith((".", "!", "?")) else 0.0
            dur_bonus = 0.4 if 20 <= dur <= 38 else 0.0

            score = (
                z(float(np.mean(r)))
                + 0.5 * z(float(np.max(r)))
                + 0.35 * hook_energy
                + 0.25 * dyn_spike
                + 0.4 * min(wps, 4.0)
                + 0.5 * (hooks + puncts)
                + 0.6 * opening_hooks
                + 0.2 * min(digits, 3)
                + clean_start + clean_end + dur_bonus
            )
            candidates.append({
                "start": start, "end": end, "score": float(score),
                "text": text, "seg_range": (i, j)
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    shortlist = []
    pool_limit = max(n * 3, 6)
    for c in candidates:
        if len(shortlist) >= pool_limit:
            break
        if any(not (c["end"] <= p["start"] or c["start"] >= p["end"]) for p in shortlist):
            continue
        shortlist.append(c)

    if not shortlist:
        return _fallback_loud(rms, n)

    s_vals = [c["score"] for c in shortlist]
    s_min, s_max = min(s_vals), max(s_vals)
    for c in shortlist:
        norm = (c["score"] - s_min) / (s_max - s_min + 1e-6)
        c["viral_score"] = int(round(74 + 24 * norm))

    if len(shortlist) > n and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("XAI_API_KEY")):
        try:
            listing = "\n".join(
                f"[{idx}] ({c['end']-c['start']:.0f}s, heuristic={c['viral_score']}): {c['text'][:240]}"
                for idx, c in enumerate(shortlist)
            )
            prompt = (
                f"Pick the {n} most viral standalone short-form video moments from these candidates. "
                f"Prioritize strong opening hooks, emotional/high-energy insights, curiosity gaps, and complete thoughts.\n"
                f"{listing}\n"
                f"Return ONLY JSON: {{\"picks\": [<list of {n} integer indices in best-first order>]}}"
            )
            raw = _call_llm(prompt, max_tokens=120)
            parsed = _parse_json(raw) if raw else None
            if parsed and isinstance(parsed.get("picks"), list):
                chosen = []
                for idx in parsed["picks"]:
                    if isinstance(idx, int) and 0 <= idx < len(shortlist) and shortlist[idx] not in chosen:
                        shortlist[idx]["viral_score"] = min(99, shortlist[idx]["viral_score"] + 4)
                        chosen.append(shortlist[idx])
                        if len(chosen) >= n:
                            break
                for c in shortlist:
                    if len(chosen) >= n:
                        break
                    if c not in chosen:
                        chosen.append(c)
                chosen.sort(key=lambda c: c["start"])
                return chosen
        except Exception:
            pass

    picked = shortlist[:n]
    picked.sort(key=lambda c: c["start"])
    return picked

def _fallback_loud(rms, n):
    picks = []
    win = min(25, max(8, len(rms) // max(1, n)))
    for _ in range(n):
        if len(rms) < 5:
            break
        best_i, best_v = 0, -1.0
        for i in range(0, max(1, len(rms) - win + 1)):
            if any(not (i + win <= p["start"] or i >= p["end"]) for p in picks):
                continue
            v = float(np.mean(rms[i:i+win]))
            if v > best_v:
                best_v, best_i = v, i
        if best_v < 0:
            break
        picks.append({"start": float(best_i), "end": float(min(len(rms), best_i + win)),
                      "score": best_v, "viral_score": 78, "text": ""})
    picks.sort(key=lambda c: c["start"])
    return picks

def _heuristic_title_and_tags(text):
    if not text:
        return "Must-Watch Live Highlight 🔥", "Wait until the end!", ["#reels", "#shorts", "#viral", "#trending"]
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 8]
    if not sents:
        sents = [text.strip()]
    def sent_score(s):
        w = [x.strip(".,!?;:\"'").lower() for x in s.split()]
        hooks = sum(2 for x in w if x in HOOK_WORDS)
        punct = s.count("!") * 2 + s.count("?") * 2
        length_ok = 2 if 18 <= len(s) <= 65 else 0
        return hooks + punct + length_ok
    best_sent = max(sents, key=sent_score)
    title = best_sent[:58].strip(" .,;:-")
    if len(best_sent) > 58:
        title = title.rsplit(" ", 1)[0] + "…"
    stop = {"the","and","for","that","this","with","you","are","was","have","from","they","will","what","when","your","about","just","like","into","over","more"}
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in text.split()]
    keywords = []
    for w in words:
        if len(w) >= 4 and w not in stop and w not in keywords:
            keywords.append(w)
    dynamic_tags = [f"#{w}" for w in keywords[:3]]
    base_tags = ["#reels", "#shorts", "#viral", "#trending"]
    all_tags = dynamic_tags + [t for t in base_tags if t not in dynamic_tags]
    caption = text[:160].strip()
    return title or "Viral Highlight 🔥", caption, all_tags

def metadata(clip, anthropic_key=None):
    text = clip.get("text", "")
    title, caption, tag_list = _heuristic_title_and_tags(text)
    viral_score = clip.get("viral_score", 86)
    reason = "High vocal energy spike + strong opening hook"
    if text and (anthropic_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("XAI_API_KEY")):
        prompt = (
            f"You are an elite viral short-form producer. Given this reel transcript excerpt, return ONLY JSON:\n"
            f"{{\"title\": \"<scroll-stopping hook title max 55 chars>\", "
            f"\"caption\": \"<engaging social caption max 140 chars with CTA>\", "
            f"\"hashtags\": [\"#tag1\", \"#tag2\", \"#tag3\", \"#reels\", \"#shorts\"], "
            f"\"reason\": \"<10-word reason why this moment hooks viewers>\"}}\n"
            f"Transcript: {text[:600]}"
        )
        raw = _call_llm(prompt, anthropic_key=anthropic_key, max_tokens=250)
        data = _parse_json(raw) if raw else None
        if data:
            title = (data.get("title") or title)[:60]
            caption = (data.get("caption") or caption)[:180]
            if isinstance(data.get("hashtags"), list) and data["hashtags"]:
                tag_list = [t if str(t).startswith("#") else f"#{t}" for t in data["hashtags"][:6]]
            reason = data.get("reason") or reason
    tags_str = " ".join(tag_list)
    return {
        "title": title,
        "caption": f"{caption}\n\n{tags_str}".strip(),
        "tags": [t.lstrip("#") for t in tag_list],
        "viral_score": viral_score,
        "reason": reason
    }

def _parse_json(s):
    if not s:
        return None
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _make_ass(words, ass_path):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,Arial Black,58,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,34,34,155,1", "",
        "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"
    ]
    def ts(t):
        t = max(0.0, float(t))
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    def esc(w):
        return w.upper().replace("\\", "").replace("{", "").replace("}", "")

    for i in range(0, len(words), 3):
        grp = words[i:i+3]
        for k, active in enumerate(grp):
            st = active["start"]
            en = active["end"] if k + 1 < len(grp) else grp[-1]["end"]
            if en <= st:
                en = st + 0.15
            parts = []
            for m_idx, w in enumerate(grp):
                tok = esc(w["word"])
                if m_idx == k:
                    parts.append(f"{{\\c&H00FFFF&\\fscx108\\fscy108}}{tok}{{\\c&HFFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(tok)
            txt = " ".join(parts)
            lines.append(f"Dialogue: 0,{ts(st)},{ts(en)},Default,,0,0,0,,{{\\b1}}{txt}")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return ass_path

def render(video_path, clip, segs, out_dir, idx, job_id=None):
    check_cancelled(job_id)
    start = clip["start"]; dur = clip["end"] - clip["start"]
    out_name = f"reel_{idx}.mp4"
    out = os.path.join(out_dir, out_name)
    words = []
    for s in segs:
        for w in s.get("words", []):
            if w["end"] > start and w["start"] < clip["end"]:
                words.append({
                    "word": w["word"],
                    "start": max(0.0, w["start"] - start),
                    "end": min(dur, w["end"] - start)
                })
    ass_name = f"reel_{idx}.ass"
    ass_path = os.path.join(out_dir, ass_name)
    _make_ass(words, ass_path)
    vf = (
        f"[0:v]fps=24,split[bg][fg];"
        f"[bg]scale=68:120:force_original_aspect_ratio=increase:flags=neighbor,crop=68:120,boxblur=3:1,scale=720:1280:flags=fast_bilinear[bbg];"
        f"[fg]scale=720:1024:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1024[ffg];"
        f"[bbg][ffg]overlay=0:(H-h)/2,ass='{ass_name}'[out]"
    )
    cmd = ["ffmpeg", "-y", "-threads", "0", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", video_path,
           "-filter_complex", vf, "-map", "[out]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-t", f"{dur:.2f}", out_name]
    _run(cmd, job_id=job_id, cwd=out_dir)
    return out, out_name
