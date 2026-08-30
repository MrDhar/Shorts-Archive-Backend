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


app = FastAPI(
    title="Shorts Archive Backend",
    version="1.4.1"
)


# ============================================================
# CONFIGURATION
# ============================================================

DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_ROOT", "/data/downloads")
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)

MAX_DISCOVER = int(
    os.getenv("MAX_DISCOVER", "5000")
)

YTDLP = os.getenv(
    "YTDLP_BIN",
    "yt-dlp"
)

POT_URL = os.getenv(
    "POT_PROVIDER_URL",
    "http://127.0.0.1:4416"
)

# Render Secret File
# This file is READ-ONLY.
COOKIE_SOURCE = Path(
    "/etc/secrets/cookies.txt"
)

# Writable copy used by yt-dlp.
COOKIE_FILE = Path(
    "/tmp/yt-dlp-cookies.txt"
)


# ============================================================
# REQUEST MODELS
# ============================================================

class DiscoverRequest(BaseModel):
    channel_url: str = Field(
        min_length=8,
        max_length=2048
    )


class DownloadRequest(BaseModel):
    video_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{11}$"
    )


# ============================================================
# YOUTUBE URL HELPERS
# ============================================================

def validate_youtube_url(url: str) -> str:

    url = url.strip()

    p = urlparse(url)

    if p.scheme not in {
        "http",
        "https"
    }:
        raise HTTPException(
            status_code=400,
            detail="Only public YouTube URLs are supported"
        )

    if p.netloc.lower() not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }:
        raise HTTPException(
            status_code=400,
            detail="Only public YouTube URLs are supported"
        )

    return url


def shorts_channel_url(url: str) -> str:

    p = urlparse(url)

    path = p.path.rstrip("/")

    if not path:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid YouTube channel URL"
        )

    if path.lower().endswith("/shorts"):
        shorts_path = path
    else:
        shorts_path = path + "/shorts"

    return urlunparse(
        (
            p.scheme,
            p.netloc,
            shorts_path,
            "",
            "",
            ""
        )
    )


def is_channel_url(url: str) -> bool:

    path = urlparse(
        url
    ).path.rstrip("/").lower()

    return (
        path.endswith("/shorts")
        or "/@" in path
        or "/channel/" in path
        or "/c/" in path
        or "/user/" in path
    )


# ============================================================
# COOKIE HANDLING
# ============================================================

def prepare_cookie_file() -> Path:

    if not COOKIE_SOURCE.exists():

        raise HTTPException(
            status_code=500,
            detail=(
                "YouTube cookies.txt was not found "
                "in Render Secret Files"
            )
        )

    try:

        shutil.copyfile(
            COOKIE_SOURCE,
            COOKIE_FILE
        )

        return COOKIE_FILE

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Could not prepare YouTube cookies: {e}"
        )


# ============================================================
# RUN YT-DLP
# ============================================================

def run_ytdlp(
    args: list[str],
    timeout: int = 180
):

    cookie_file = prepare_cookie_file()

    cmd = [
        YTDLP,

        "--no-warnings",
        "--no-progress",

        # Writable cookie copy.
        "--cookies",
        str(cookie_file),

        # PO-token provider.
        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL};"
            "disable_innertube=1"
        ),
    ]

    cmd.extend(args)

    try:

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail="yt-dlp is not installed on the backend"
        )

    except subprocess.TimeoutExpired:

        raise HTTPException(
            status_code=504,
            detail="YouTube request timed out"
        )


# ============================================================
# DISCOVERY
# ============================================================

def discover_with_client(
    url: str,
    client: str
):

    r = run_ytdlp(
        [
            "--flat-playlist",

            "--lazy-playlist",

            "--ignore-errors",

            "--extractor-args",
            f"youtube:player_client={client}",

            "--print",
            "%(id)s\t%(title)s\t%(webpage_url)s",

            "--skip-download",

            url,
        ],
        timeout=240
    )

    found = []
    seen = set()

    for line in r.stdout.splitlines():

        parts = line.split(
            "\t",
            2
        )

        if not parts:
            continue

        video_id = parts[0].strip()

        if not re.fullmatch(
            r"[A-Za-z0-9_-]{11}",
            video_id
        ):
            continue

        if video_id in seen:
            continue

        seen.add(video_id)

        title = ""

        if len(parts) > 1:
            title = parts[1].strip()

        webpage_url = ""

        if len(parts) > 2:
            webpage_url = parts[2].strip()

        if not webpage_url:

            webpage_url = (
                "https://www.youtube.com/shorts/"
                + video_id
            )

        found.append(
            {
                "id": video_id,
                "title": title,
                "url": webpage_url,
            }
        )

        if len(found) >= MAX_DISCOVER:
            break

    return found, r.stderr


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "ok": True,
        "service": "Shorts Archive Backend",
        "status": "running",
        "cookies_configured": COOKIE_SOURCE.exists()
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "cookies_configured": COOKIE_SOURCE.exists()
    }


# ============================================================
# DISCOVER SHORTS
# ============================================================

@app.post("/discover")
def discover(
    req: DiscoverRequest
):

    original_url = validate_youtube_url(
        req.channel_url
    )

    if not is_channel_url(
        original_url
    ):

        raise HTTPException(
            status_code=400,
            detail="Enter a public YouTube channel URL"
        )

    shorts_url = shorts_channel_url(
        original_url
    )

    errors = []

    clients = (
        "android_vr",
        "tv",
        "web_embedded",
        "mweb",
    )

    for client in clients:

        try:

            entries, stderr = (
                discover_with_client(
                    shorts_url,
                    client
                )
            )

            if entries:

                return {
                    "entries": entries,
                    "count": len(entries),
                    "client": client,
                    "source": shorts_url,
                }

            if stderr:

                errors.append(
                    f"{client}: {stderr[-1200:]}"
                )

        except HTTPException:

            raise

        except Exception as e:

            errors.append(
                f"{client}: {e}"
            )

    error_text = " | ".join(
        errors
    )

    error_text = error_text[-5000:]

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed. "
            + error_text
        )
    )


# ============================================================
# DOWNLOAD ONE SHORT
# ============================================================

@app.post("/download")
def download(
    req: DownloadRequest
):

    video_url = (
        "https://www.youtube.com/shorts/"
        + req.video_id
    )

    tempdir = Path(
        tempfile.mkdtemp(
            prefix=f"short_{req.video_id}_",
            dir="/tmp"
        )
    )

    out_template = str(
        tempdir / "%(id)s.%(ext)s"
    )

    try:

        attempts = [
            (
                "mweb",
                "bv*+ba/b"
            ),
            (
                "web_safari",
                "bv*+ba/b"
            ),
            (
                "android_vr",
                "bv*+ba/b"
            ),
            (
                "tv",
                "bv*+ba/b"
            ),
            (
                "web_embedded",
                "bv*+ba/b"
            ),
        ]

        last_error = ""

        for client, fmt in attempts:

            # Remove files from previous attempt.
            for old_file in tempdir.iterdir():

                if old_file.is_file():

                    try:
                        old_file.unlink()
                    except Exception:
                        pass

            r = run_ytdlp(
                [
                    "--no-playlist",

                    "--extractor-args",
                    f"youtube:player_client={client}",

                    "--retries",
                    "3",

                    "--fragment-retries",
                    "3",

                    "--file-access-retries",
                    "3",

                    "--retry-sleep",
                    "1",

                    "-f",
                    fmt,

                    "--merge-output-format",
                    "mp4",

                    "-o",
                    out_template,

                    video_url,
                ],
                timeout=900
            )

            files = [
                p
                for p in tempdir.iterdir()
                if p.is_file()
            ]

            if (
                r.returncode == 0
                and files
            ):

                src = max(
                    files,
                    key=lambda p: p.stat().st_size
                )

                safe_name = re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    src.name
                )

                final = (
                    DOWNLOAD_ROOT
                    / safe_name
                )

                shutil.copy2(
                    src,
                    final
                )

                return FileResponse(
                    path=final,
                    media_type="video/mp4",
                    filename=final.name
                )

            last_error = (
                r.stderr
                or r.stdout
                or "download failed"
            )[-4000:]

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube download failed after all "
                "download methods were attempted:\n"
                + last_error
            )
        )

    finally:

        shutil.rmtree(
            tempdir,
            ignore_errors=True
        )
