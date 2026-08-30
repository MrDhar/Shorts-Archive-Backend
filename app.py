import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Shorts Archive Backend", version="1.0.0")

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/data/downloads"))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
MAX_DISCOVER = int(os.getenv("MAX_DISCOVER", "5000"))

YTDLP = os.getenv("YTDLP_BIN", "yt-dlp")

class DiscoverRequest(BaseModel):
    channel_url: str = Field(min_length=8, max_length=2048)

class DownloadRequest(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")


def validate_youtube_url(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in {"http", "https"} or p.netloc.lower() not in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
    }:
        raise HTTPException(400, "Only public YouTube URLs are supported")
    return url.strip()


def run_ytdlp(args: list[str], timeout: int = 180):
    cmd = [YTDLP, "--no-warnings", "--no-progress"] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(500, "yt-dlp is not installed on the backend")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "YouTube request timed out")


def discover_with_client(url: str, client: str):
    # Flat extraction avoids downloading media during discovery.
    r = run_ytdlp([
        "--flat-playlist",
        "--lazy-playlist",
        "--ignore-errors",
        "--extractor-args", f"youtube:player_client={client}",
        "--print", "%(id)s\\t%(title)s\\t%(webpage_url)s",
        "--skip-download",
        url,
    ], timeout=240)
    found = []
    seen = set()
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if not parts:
            continue
        vid = parts[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or vid in seen:
            continue
        seen.add(vid)
        found.append({
            "id": vid,
            "title": parts[1].strip() if len(parts) > 1 else "",
            "url": parts[2].strip() if len(parts) > 2 else f"https://www.youtube.com/shorts/{vid}",
        })
        if len(found) >= MAX_DISCOVER:
            break
    return found, r.stderr


def is_channel_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/").lower()
    return path.endswith("/shorts") or "/@" in path or "/channel/" in path or "/c/" in path or "/user/" in path

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/discover")
def discover(req: DiscoverRequest):
    url = validate_youtube_url(req.channel_url)
    if not is_channel_url(url):
        raise HTTPException(400, "Enter a public YouTube channel URL")

    # The client order is intentional: current yt-dlp guidance identifies
    # android_vr/tv/web_embedded as useful no-PO-token clients, while mweb is
    # the recommended client when a PO-token provider is configured.
    errors = []
    for client in ("android_vr", "tv", "web_embedded", "mweb"):
        try:
            entries, stderr = discover_with_client(url, client)
            if entries:
                return {"entries": entries, "count": len(entries), "client": client}
            if stderr:
                errors.append(f"{client}: {stderr[-1200:]}")
        except HTTPException:
            raise
        except Exception as e:
            errors.append(f"{client}: {e}")

    raise HTTPException(502, "YouTube discovery failed. " + " | ".join(errors)[-5000:])

@app.post("/download")
def download(req: DownloadRequest):
    video_url = f"https://www.youtube.com/shorts/{req.video_id}"
    tempdir = Path(tempfile.mkdtemp(prefix=f"short_{req.video_id}_", dir="/tmp"))
    out_template = str(tempdir / "%(id)s.%(ext)s")
    try:
        # Prefer a single-file H.264/AAC format where available, then fall back
        # to best video+audio and let ffmpeg merge to MP4.
        attempts = [
            ["android_vr", "18/b"],
            ["tv", "bv*+ba/b"],
            ["web_embedded", "bv*+ba/b"],
            ["mweb", "bv*+ba/b"],
        ]
        last_error = ""
        for client, fmt in attempts:
            r = run_ytdlp([
                "--no-playlist",
                "--extractor-args", f"youtube:player_client={client}",
                "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", out_template,
                video_url,
            ], timeout=900)
            files = [p for p in tempdir.iterdir() if p.is_file()]
            if r.returncode == 0 and files:
                src = max(files, key=lambda p: p.stat().st_size)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", src.name)
                final = DOWNLOAD_ROOT / safe_name
                shutil.copy2(src, final)
                return FileResponse(final, media_type="video/mp4", filename=final.name)
            last_error = (r.stderr or r.stdout or "download failed")[-2000:]
        raise HTTPException(502, "YouTube download failed: " + last_error)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
