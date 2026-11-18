# 🎬 ReelBot // Neural 3D Studio

AI-powered live stream & VOD short-form clipper that detects viral moments, burns dynamic word-highlighted 9:16 karaoke captions, and auto-publishes to **Instagram Reels** and **YouTube Shorts**.

## 🌐 Live Hosted Deployment & Mobile Access

- **Live Public HTTPS App (Mobile & Desktop Anywhere)**: [https://6da92fbb6fcd9f.app.lhr.life](https://6da92fbb6fcd9f.app.lhr.life)
- **Local Wi-Fi Mobile Access (Same Network)**: `http://192.168.1.28:5000`
- **Local Desktop Access**: `http://127.0.0.1:5000`
- **GitHub Repository**: [https://github.com/rahil12051211/reelbot](https://github.com/rahil12051211/reelbot)

---

## ⚡ Features

1. **Instant Live Stream DVR Burst (`~4.7s` for `60s` of live stream)**:
   - Resolves direct HLS live manifests and uses `ffmpeg -live_start_index` to pull buffered CDN segments at full network speed without waiting in real time.
2. **Multi-Factor Hook Detection + LLM Viral Re-Ranking**:
   - Scores acoustic RMS spikes, opening 5-second hook energy, speech pace, viral trigger words, and natural sentence boundaries, with optional Claude / xAI Grok re-ranking.
3. **Lightning Parallel 9:16 Rendering (Cinema 80% Vertical Zoom)**:
   - Renders all `N` reels in parallel (`ThreadPoolExecutor`) with fast low-res ambient blur background, 80% vertical height foreground zoom, and yellow word-by-word karaoke `.ass` captions.
4. **Non-Blocking Instagram 2FA & Email/SMS Challenge Login + Auto-Upload First**:
   - Connects directly via Instagram username, password, and 6-digit Email/SMS challenge or 2FA code, caching `ig_session_<username>.json` and uploading reels automatically right after rendering.
5. **AI Edited Reels Vault with 48-Hour Auto-Purge**:
   - Displays all AI-created reels with a live countdown (`48h TTL`) and automatically deletes reels older than 2 days.

---

## 🚀 Quick Start

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

To expose a fresh public HTTPS URL for mobile access anytime:
```bash
ssh -o StrictHostKeyChecking=no -R 80:127.0.0.1:5000 nokey@localhost.run
```
