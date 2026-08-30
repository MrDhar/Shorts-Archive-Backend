import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Setup logging to capture errors in Render logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Shorts Archive Backend",
    version="1.6.0"
)


# ============================================================
# CONFIG
# ============================================================

DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_ROOT", "/data/downloads")
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)

# Render-friendly: Limit discovery to avoid long spinup times
MAX_DISCOVER = int(
    os.getenv("MAX_DISCOVER", "500")
)

YTDLP = os.getenv(
    "YTDLP_BIN",
    "yt-dlp"
)

POT_URL = os.getenv(
    "POT_PROVIDER_URL",
    "http://127.0.0.1:4416"
)

# Render Secret File.
# This location is read-only.
COOKIE_SOURCE = Path(
    "/etc/secrets/cookies.txt"
)

# Writable copy used by yt-dlp.
COOKIE_FILE = Path(
    "/tmp/yt-dlp-cookies.txt"
)


# ============================================================
# MODELS
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
# YOUTUBE URL VALIDATION
# ============================================================

def validate_youtube_url(url: str) -> str:

    url = url.strip()

    parsed = urlparse(url)

    if parsed.scheme not in {
        "http",
        "https"
    }:
        raise HTTPException(
            status_code=400,
            detail="Only public YouTube URLs are supported"
        )

    if parsed.netloc.lower() not in {
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

    parsed = urlparse(url)

    path = parsed.path.rstrip("/")

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
            parsed.scheme,
            parsed.netloc,
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
# COOKIES
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

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not prepare YouTube cookies: "
                + str(exc)
            )
        )


# ============================================================
# RUN YT-DLP
# ============================================================

def run_ytdlp(
    args: list[str],
    timeout: int = 180
):

    cookie_file = prepare_cookie_file()

    command = [
        YTDLP,

        "--no-warnings",
        "--no-progress",

        "--cookies",
        str(cookie_file),

        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL}"
        ),
    ]

    command.extend(args)

    try:

        return subprocess.run(
            command,
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

    result = run_ytdlp(
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

    for line in result.stdout.splitlines():

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

        if len(parts) >= 2:
            title = parts[1].strip()

        webpage_url = ""

        if len(parts) >= 3:
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

    return found, result.stderr


# ============================================================
# SAVE DOWNLOAD
# ============================================================

def save_download(
    source_file: Path
):

    extension = source_file.suffix.lower()

    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".m4v": "video/mp4",
    }

    media_type = media_types.get(
        extension,
        "application/octet-stream"
    )

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        source_file.name
    )

    final_file = (
        DOWNLOAD_ROOT
        / safe_name
    )

    shutil.copy2(
        source_file,
        final_file
    )

    return FileResponse(
        path=final_file,
        media_type=media_type,
        filename=final_file.name
    )


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
# DISCOVER ALL SHORTS
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

    # Render-optimized: Only try 2 fastest clients for discovery
    clients = [
        "mweb",
        "android_vr",
    ]

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

        except Exception as exc:

            errors.append(
                f"{client}: {exc}"
            )

    error_text = " | ".join(
        errors
    )[-5000:]

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed. "
            + error_text
        )
    )


# ============================================================
# DOWNLOAD
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

    output_template = str(
        tempdir / "%(id)s.%(ext)s"
    )

    try:

        # Render-optimized: Only try the 2 fastest, most reliable clients
        # mweb is fastest for shorts, android_vr is reliable fallback
        # Skip tv and web_embedded (slower, less compatible)
        clients = [
            "mweb",
            "android_vr",
        ]

        last_error = ""
        start_time = time.time()

        for client_index, client in enumerate(clients, 1):

            # Fail fast if we're taking too long (avoid Render spindown)
            elapsed = time.time() - start_time
            if elapsed > 180:  # 3 minute total timeout
                logger.warning(f"Total download attempt timeout ({elapsed:.0f}s elapsed), aborting")
                raise HTTPException(
                    status_code=504,
                    detail=f"Download took too long ({elapsed:.0f}s). Likely a network issue."
                )

            logger.info(f"[Client {client_index}/2] Attempting {req.video_id} with {client}")

            # Skip the --list-formats check entirely.
            # It often reports formats that don't actually download.
            # Just go straight to trying the download.

            for old_file in tempdir.iterdir():

                if old_file.is_file():

                    try:
                        old_file.unlink()
                    except Exception:
                        pass

            # Aggressive timeout strategy for Render free tier
            # Shorter timeouts per format, fail fast if not working
            format_attempts = [
                # Most likely to work for YouTube Shorts
                ("best", 120),  # (format_string, timeout_seconds)
                # Fallback if single file doesn't work
                ("bestvideo+bestaudio/best", 180),
            ]

            download_result = None
            attempt_count = 0

            for format_string, timeout_seconds in format_attempts:

                attempt_count += 1
                logger.info(
                    f"[{attempt_count}/2] {req.video_id} with {client} format: {format_string} (timeout: {timeout_seconds}s)"
                )

                download_result = run_ytdlp(
                    [
                        "--no-playlist",
                        "--no-warnings",

                        "--extractor-args",
                        f"youtube:player_client={client}",

                        # Aggressive retries for Render
                        "--retries", "1",
                        "--fragment-retries", "1",
                        "--file-access-retries", "1",
                        "--retry-sleep", "0",

                        # Keep socket alive to prevent spindown issues
                        "--socket-timeout", "30",

                        "-f", format_string,

                        "--merge-output-format", "mp4",

                        "-o", output_template,

                        video_url,
                    ],
                    timeout=timeout_seconds
                )

                if download_result.returncode == 0:
                    logger.info(f"✓ Format '{format_string}' succeeded for {req.video_id}")
                    break
                else:
                    error_msg = (download_result.stderr or download_result.stdout or "unknown")[-100:]
                    logger.warning(
                        f"✗ Format '{format_string}' failed: {error_msg}"
                    )

            files = [
                file
                for file in tempdir.iterdir()
                if file.is_file()
            ]

            if (
                download_result.returncode == 0
                and files
            ):

                source_file = max(
                    files,
                    key=lambda file:
                    file.stat().st_size
                )

                logger.info(
                    f"Successfully downloaded {req.video_id} with {client} "
                    f"({source_file.stat().st_size} bytes)"
                )

                return save_download(
                    source_file
                )

            error_output = (
                download_result.stderr
                or download_result.stdout
                or "Download failed (no output)"
            )

            last_error = (
                f"Client: {client}\n"
                f"DOWNLOAD ERROR:\n"
                + error_output
            )[-5000:]

            logger.warning(
                f"Download attempt failed for {req.video_id} with {client}: {error_output[-200:]}"
            )

        # =====================================================
        # ALL CLIENTS FAILED
        # Return useful diagnostic information.
        # =====================================================

        error_detail = (
            "YouTube download failed.\n\n"
            + last_error
        )
        
        logger.error(
            f"Download failed for video {req.video_id}: {last_error}"
        )

        raise HTTPException(
            status_code=502,
            detail=error_detail
        )

    finally:

        shutil.rmtree(
            tempdir,
            ignore_errors=True
            )
