import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Shorts Archive Backend", version="1.1.0")

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/data/downloads"))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
MAX_DISCOVER = int(os.getenv("MAX_DISCOVER", "5000"))
YTDLP = os.getenv("YTDLP_BIN", "yt-dlp")
POT_BASE_URL = os.getenv("POT_BASE_URL", "http://127.0.0.1:4416")


class DiscoverRequest(BaseModel):
    channel_url: str = Field(min_length=8, max_length=2048)


class DownloadRequest(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")


def validate_youtube_url(url: str) -> str:
    url = url.strip()
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or p.netloc.lower() not in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
    }:
        raise HTTPException(400, "Only public YouTube URLs are supported")
    return url


def normalize_shorts_url(url: str) -> str:
    """Turn a public channel URL such as /@name into its Shorts tab."""
    p = urlparse(url)
    path = p.path.rstrip("/")

    if path.lower().endswith("/shorts"):
        return urlunparse((p.scheme, p.netloc, path, "", "", ""))

    # Handle /@name, /channel/UC..., /c/name and /user/name.
    if path:
        return urlunparse((p.scheme, p.netloc, path + "/shorts", "", "", ""))

    raise HTTPException(400, "Enter a public YouTube channel URL")


def run_ytdlp(args: list[str], timeout: int = 240):
    # One extractor-args value per extractor/plugin avoids overriding provider config.
    cmd = [
        YTDLP,
        "--no-warnings",
        "--no-progress",
        "--extractor-args",
        f"youtubepot-bgutilhttp:base_url={POT_BASE_URL}",
    ] + args

    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise HTTPException(500, "yt-dlp is not installed on the backend")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "YouTube request timed out")


def discover_with_client(url: str, client: str):
    # The /shorts tab is important: a bare channel URL can resolve to the
    # channel home/videos tab instead of the Shorts feed.
    r = run_ytdlp(
        [
            "--flat-playlist",
            "--ignore-errors",
            "--playlist-end", str(MAX_DISCOVER),
            "--extractor-args", f"youtube:player_client={client}",
            "--print", "%(id)s\t%(title)s\t%(webpage_url)s",
            "--skip-download",
            url,
        ],
        timeout=300,
    )

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
        found.append(
            {
                "id": vid,
                "title": parts[1].strip() if len(parts) > 1 else "",
                "url": parts[2].strip()
                if len(parts) > 2 and parts[2].strip()
                else f"https://www.youtube.com/shorts/{vid}",
            }
        )

        if len(found) >= MAX_DISCOVER:
            break

    return found, r.stderr, r.returncode


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/discover")
def discover(req: DiscoverRequest):
    original = validate_youtube_url(req.channel_url)
    shorts_url = normalize_shorts_url(original)

    errors = []

    # Start with clients that do not require a GVS PO token.
    for client in ("android_vr", "tv", "web_embedded", "mweb"):
        try:
            entries, stderr, returncode = discover_with_client(shorts_url, client)

            if entries:
                return {
                    "entries": entries,
                    "count": len(entries),
                    "client": client,
                    "source_url": shorts_url,
                }

            detail = (stderr or "").strip()
            if not detail:
                detail = f"yt-dlp returned no entries (exit code {returncode})"
            errors.append(f"{client}: {detail[-1800:]}")

        except HTTPException:
            raise
        except Exception as e:
            errors.append(f"{client}: {type(e).__name__}: {e}")

    # Returning the actual yt-dlp diagnostics makes future failures debuggable
    # instead of showing only a generic 502.
    detail = " | ".join(errors)[-7000:]
    raise HTTPException(
        502,
        f"YouTube discovery failed. Source: {shorts_url}. {detail}",
    )


@app.post("/download")
def download(req: DownloadRequest):
    video_url = f"https://www.youtube.com/shorts/{req.video_id}"
    tempdir = Path(tempfile.mkdtemp(prefix=f"short_{req.video_id}_", dir="/tmp"))
    out_template = str(tempdir / "%(id)s.%(ext)s")

    try:
        attempts = [
            ("android_vr", "18/b"),
            ("tv", "bv*+ba/b"),
            ("web_embedded", "bv*+ba/b"),
            ("mweb", "bv*+ba/b"),
        ]

        last_error = ""
        for client, fmt in attempts:
            r = run_ytdlp(
                [
                    "--no-playlist",
                    "--extractor-args", f"youtube:player_client={client}",
                    "-f", fmt,
                    "--merge-output-format", "mp4",
                    "-o", out_template,
                    video_url,
                ],
                timeout=900,
            )

            files = [p for p in tempdir.iterdir() if p.is_file()]
            if r.returncode == 0 and files:
                src = max(files, key=lambda p: p.stat().st_size)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", src.name)
                final = DOWNLOAD_ROOT / safe_name
                shutil.copy2(src, final)
                return FileResponse(
                    final,
                    media_type="video/mp4",
                    filename=final.name,
                )

            last_error = (r.stderr or r.stdout or "download failed")[-3000:]

        raise HTTPException(502, "YouTube download failed: " + last_error)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
