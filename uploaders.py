import os, time, json, glob, subprocess, threading, requests

_ig_lock = threading.Lock()
_ig_upload_sem = threading.Semaphore(2)  # Allow 2 concurrent Reel uploads for maximum bandwidth throughput
_ig_client = None
_ig_user = None
_uploaded_cover_ids = set()

def _optimize_ig_client(cl):
    """Strip artificial instagrapi delays, enable connection pooling, and deduplicate cover photo_rupload on configure retries."""
    cl.delay_range = [0, 0]
    cl.request_timeout = 20
    try:
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=2)
        cl.private.mount("https://", adapter)
        cl.private.mount("http://", adapter)
    except Exception:
        pass
    orig_photo_rupload = getattr(cl, "photo_rupload", None)
    if orig_photo_rupload and not getattr(cl, "_fast_rupload_patched", False):
        def _cached_photo_rupload(path, upload_id="", *args, **kwargs):
            key = f"{upload_id}_{path}"
            if upload_id and key in _uploaded_cover_ids:
                return upload_id, 1080, 1920
            res = orig_photo_rupload(path, upload_id, *args, **kwargs)
            if upload_id:
                _uploaded_cover_ids.add(key)
            return res
        cl.photo_rupload = _cached_photo_rupload
        cl._fast_rupload_patched = True
    return cl

# Non-blocking Email/SMS Challenge & 2FA state
_challenge_wait_event = threading.Event()
_challenge_step_event = threading.Event()
_submitted_code = None
_waiting_for_challenge = False
_pending_cl = None
_pending_user = None

IG_STATUS = {"connected": False, "username": "", "full_name": "", "needs_2fa": False, "error": ""}

def _session_dir():
    return "/tmp" if os.environ.get("VERCEL") else os.path.dirname(__file__)

def _try_autoload_session():
    """Auto-restore saved Instagram session on startup if available."""
    global _ig_client, _ig_user, IG_STATUS
    base_dir = _session_dir()
    sessions = sorted(glob.glob(os.path.join(base_dir, "ig_session_*.json")), key=os.path.getmtime, reverse=True)
    if not sessions:
        return
    sess_file = sessions[0]
    uname = os.path.basename(sess_file)[len("ig_session_"):-len(".json")]
    try:
        from instagrapi import Client
        cl = Client()
        _optimize_ig_client(cl)
        cl.load_settings(sess_file)
        if cl.user_id:
            _ig_client = cl
            _ig_user = uname
            IG_STATUS = {"connected": True, "username": uname, "full_name": uname, "needs_2fa": False, "error": ""}
    except Exception:
        pass

_try_autoload_session()

def verify_instagram(username=None, password=None, code=None):
    """
    Fast, non-blocking Instagram login that handles both Authenticator 2FA AND
    Instagram Email/SMS 6-digit challenge prompts without ever blocking on stdin input().
    """
    global _ig_client, _ig_user, _pending_cl, _pending_user, IG_STATUS
    global _submitted_code, _waiting_for_challenge

    username = (username or _pending_user or os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    password = password or os.getenv("IG_PASSWORD") or ""
    code = (code or os.getenv("IG_2FA_CODE") or "").strip().replace(" ", "") or None

    if not username or not password:
        IG_STATUS = {
            "connected": False, "username": username, "full_name": "",
            "needs_2fa": False, "error": "Enter your Instagram username and password"
        }
        return IG_STATUS

    # Case 1: An Email/SMS challenge thread is currently paused waiting for the user's 6-digit code
    if _waiting_for_challenge and code:
        _submitted_code = code
        _challenge_step_event.clear()
        _challenge_wait_event.set()
        _challenge_step_event.wait(timeout=20)
        return IG_STATUS

    # If a previous challenge thread was waiting and user clicked Connect without a code, release it
    if _waiting_for_challenge and not code:
        _submitted_code = ""
        _challenge_wait_event.set()
        time.sleep(0.2)

    _challenge_step_event.clear()
    _challenge_wait_event.clear()
    _submitted_code = code

    def _bg_login():
        global _ig_client, _ig_user, _pending_cl, _pending_user, IG_STATUS
        global _submitted_code, _waiting_for_challenge
        from instagrapi import Client
        from instagrapi.exceptions import (
            BadPassword, ChallengeRequired, TwoFactorRequired, LoginRequired,
        )
        session = os.path.join(_session_dir(), f"ig_session_{username}.json")

        def _custom_challenge_handler(uname, choice):
            global IG_STATUS, _waiting_for_challenge, _submitted_code
            # If user already provided the 6-digit code in the request, use it immediately
            if _submitted_code and len(_submitted_code) >= 6:
                c_val = _submitted_code
                _submitted_code = None
                return c_val
            choice_label = getattr(choice, "name", str(choice)).replace("ChallengeChoice.", "")
            IG_STATUS = {
                "connected": False,
                "username": username,
                "full_name": "",
                "needs_2fa": True,
                "error": f"Instagram sent a 6-digit verification code to your {choice_label}. Enter it in the '6-Digit Code' box and click '🔐 Verify 2FA & Connect IG'!"
            }
            _waiting_for_challenge = True
            _challenge_wait_event.clear()
            # Release the initial HTTP /api/ig/connect call so the UI immediately prompts for the 6-digit code
            _challenge_step_event.set()
            # Wait up to 3 minutes for user to paste the 6-digit code and click Verify
            _challenge_wait_event.wait(timeout=180)
            _waiting_for_challenge = False
            return _submitted_code or False

        try:
            if code and _pending_cl is not None and _pending_user == username:
                cl = _pending_cl
            else:
                cl = Client()
                cl.delay_range = [0, 0]
                cl.request_timeout = 15
                if os.path.exists(session) and not code:
                    try:
                        cl.load_settings(session)
                    except Exception:
                        pass
            cl.challenge_code_handler = _custom_challenge_handler

            if code and _pending_cl is not None:
                cl.login(username, password, verification_code=code)
            elif code:
                # Could be either TOTP 2FA or pre-entered code
                cl.login(username, password, verification_code=code)
            else:
                cl.login(username, password)

            cl.dump_settings(session)
            _optimize_ig_client(cl)
            _ig_client = cl
            _ig_user = username
            _pending_cl = None
            _pending_user = None
            os.environ["IG_USERNAME"] = username
            os.environ["IG_PASSWORD"] = password
            IG_STATUS = {"connected": True, "username": username, "full_name": username, "needs_2fa": False, "error": ""}
        except TwoFactorRequired:
            _pending_cl = cl
            _pending_user = username
            os.environ["IG_USERNAME"] = username
            os.environ["IG_PASSWORD"] = password
            IG_STATUS = {
                "connected": False,
                "username": username,
                "full_name": "",
                "needs_2fa": True,
                "error": "2FA code required — enter your 6-digit code and click '🔐 Verify 2FA & Connect IG'"
            }
        except BadPassword:
            _ig_client = None
            _pending_cl = None
            IG_STATUS = {"connected": False, "username": username, "full_name": "", "needs_2fa": False, "error": "Instagram rejected that password"}
        except ChallengeRequired:
            _ig_client = None
            _pending_cl = cl
            _pending_user = username
            if not IG_STATUS.get("needs_2fa"):
                IG_STATUS = {
                    "connected": False, "username": username, "full_name": "", "needs_2fa": True,
                    "error": "Check your Instagram email/SMS or app for a 6-digit code (or 'This Was Me' prompt), enter the code above, and click '🔐 Verify 2FA & Connect IG'"
                }
        except LoginRequired:
            _ig_client = None
            _pending_cl = None
            if os.path.exists(session):
                try: os.remove(session)
                except Exception: pass
            IG_STATUS = {"connected": False, "username": username, "full_name": "", "needs_2fa": False, "error": "Session expired — click Connect again"}
        except Exception as e:
            _ig_client = None
            err = str(e).replace(password, "***") if password else str(e)
            needs_code = any(k in err.lower() for k in ("two-factor", "verification_code", "2fa", "challenge", "code"))
            if needs_code:
                _pending_cl = cl
                _pending_user = username
            IG_STATUS = {"connected": False, "username": username, "full_name": "", "needs_2fa": needs_code, "error": err}
        finally:
            _challenge_step_event.set()

    t = threading.Thread(target=_bg_login, daemon=True)
    t.start()
    _challenge_step_event.wait(timeout=20)
    return IG_STATUS

def instagram_login(path, caption, username=None, password=None):
    """
    Turbo Parallel Instagram Reel Uploader:
    - Uses configure_timeout=1.2 (eliminating instagrapi's default 10s-20s sleep per poll)
    - Caches photo_rupload per upload_id so cover thumbnails are never re-uploaded on transcode polls
    - Supports 2 concurrent upload workers via _ig_upload_sem for maximum upload throughput.
    """
    global _ig_client, _ig_user, IG_STATUS
    username = (username or _ig_user or os.getenv("IG_USERNAME") or "").strip().lstrip("@")
    password = password or os.getenv("IG_PASSWORD") or ""
    with _ig_lock:
        if _ig_client is None or (username and _ig_user != username):
            st = verify_instagram(username, password)
            if not st.get("connected"):
                raise RuntimeError(st.get("error") or "Instagram login failed")
        _optimize_ig_client(_ig_client)

    thumb = path.rsplit(".", 1)[0] + ".jpg"
    if not os.path.exists(thumb):
        subprocess.run(["ffmpeg", "-y", "-ss", "0.5", "-i", path, "-frames:v", "1", thumb], capture_output=True)
    # Use a dedicated cover copy so instagrapi's crop_thumbnail never mutates the Vault's display thumbnail
    ig_cover = path.rsplit(".", 1)[0] + "_igcover.jpg"
    if os.path.exists(thumb):
        try:
            import shutil
            shutil.copy2(thumb, ig_cover)
        except Exception:
            ig_cover = thumb
    thumb_arg = ig_cover if os.path.exists(ig_cover) else (thumb if os.path.exists(thumb) else None)

    with _ig_upload_sem:
        try:
            media = _ig_client.clip_upload(path, caption, thumbnail=thumb_arg, configure_timeout=1.2)
            code = getattr(media, "code", None)
            pk = getattr(media, "pk", None)
            url = f"https://www.instagram.com/reel/{code}/" if code else ""
            return {"id": str(pk or ""), "code": str(code or ""), "url": url}
        except Exception as e:
            msg = str(e).replace(password, "***") if password else str(e)
            raise RuntimeError(msg) from None
        finally:
            if ig_cover != thumb and os.path.exists(ig_cover):
                try: os.remove(ig_cover)
                except Exception: pass

def instagram(video_url, caption):
    uid = os.getenv("IG_USER_ID")
    token = os.getenv("IG_ACCESS_TOKEN")
    if not uid or not token:
        raise ValueError("IG_USER_ID and IG_ACCESS_TOKEN required")
    base = "https://graph.facebook.com/v21.0"
    r = requests.post(f"{base}/{uid}/media", params={
        "media_type": "REELS", "video_url": video_url,
        "caption": caption, "access_token": token
    })
    r.raise_for_status()
    container_id = r.json()["id"]
    for _ in range(60):
        time.sleep(5)
        r = requests.get(f"{base}/{container_id}", params={"fields": "status_code", "access_token": token})
        r.raise_for_status()
        status = r.json().get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError(f"IG container error: {r.json()}")
    else:
        raise TimeoutError("IG container not ready after 5 min")
    r = requests.post(f"{base}/{uid}/media_publish", params={
        "creation_id": container_id, "access_token": token
    })
    r.raise_for_status()
    return r.json()

def youtube(path, title, description, tags):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = None
    token_file = "yt_token.json"
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", scopes)
        creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": title, "description": description + "\n#Shorts", "tags": tags + ["Shorts"],
                        "categoryId": "22"}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, chunksize=10*1024*1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp
