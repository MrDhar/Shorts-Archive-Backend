import logging
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


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("shorts-archive")


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Shorts Archive Backend",
    version="1.7.0",
)


# ============================================================
# CONFIG
# ============================================================

DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_ROOT", "/data/downloads")
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)

MAX_DISCOVER = int(
    os.getenv("MAX_DISCOVER", "500")
)

YTDLP = os.getenv(
    "YTDLP_BIN",
    "yt-dlp",
)

POT_URL = os.getenv(
    "POT_PROVIDER_URL",
    "http://127.0.0.1:4416",
)

# Render Secret File.
# IMPORTANT:
# This file is read-only.
COOKIE_SOURCE = Path(
    "/etc/secrets/cookies.txt"
)

# Writable copy for yt-dlp.
COOKIE_FILE = Path(
    "/tmp/yt-dlp-cookies.txt"
)


# ============================================================
# MODELS
# ============================================================

class DiscoverRequest(BaseModel):
    channel_url: str = Field(
        min_length=8,
        max_length=2048,
    )


class DownloadRequest(BaseModel):
    video_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{11}$",
    )


# ============================================================
# YOUTUBE URL HELPERS
# ============================================================

def validate_youtube_url(url: str) -> str:

    url = url.strip()

    parsed = urlparse(url)

    if parsed.scheme not in {
        "http",
        "https",
    }:
        raise HTTPException(
            status_code=400,
            detail="Only public YouTube URLs are supported",
        )

    hostname = parsed.netloc.lower().split(":")[0]

    if hostname not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }:
        raise HTTPException(
            status_code=400,
            detail="Only public YouTube URLs are supported",
        )

    return url


def shorts_channel_url(url: str) -> str:

    parsed = urlparse(url)

    path = parsed.path.rstrip("/")

    if not path:

        raise HTTPException(
            status_code=400,
            detail="Enter a valid YouTube channel URL",
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
            "",
        )
    )


def is_channel_url(url: str) -> bool:

    path = urlparse(url).path.rstrip("/").lower()

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
            ),
        )

    try:

        shutil.copyfile(
            COOKIE_SOURCE,
            COOKIE_FILE,
        )

        return COOKIE_FILE

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not prepare YouTube cookies: "
                + str(exc)
            ),
        )


# ============================================================
# RUN YT-DLP
# ============================================================

def run_ytdlp(
    args: list[str],
    timeout: int = 180,
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

    logger.info(
        "Running yt-dlp: %s",
        " ".join(command),
    )

    try:

        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail="yt-dlp is not installed on the backend",
        )

    except subprocess.TimeoutExpired:

        raise HTTPException(
            status_code=504,
            detail="YouTube request timed out",
        )


# ============================================================
# DISCOVERY
# ============================================================

def discover_with_client(
    url: str,
    client: str,
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
        timeout=240,
    )

    found = []

    seen = set()

    for line in result.stdout.splitlines():

        parts = line.split(
            "\t",
            2,
        )

        if not parts:
            continue

        video_id = parts[0].strip()

        if not re.fullmatch(
            r"[A-Za-z0-9_-]{11}",
            video_id,
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
# DOWNLOAD HELPERS
# ============================================================

def clean_temp_directory(
    tempdir: Path,
):

    for file in tempdir.iterdir():

        if file.is_file():

            try:
                file.unlink()

            except Exception:
                pass


def get_downloaded_files(
    tempdir: Path,
):

    return [
        file
        for file in tempdir.iterdir()
        if (
            file.is_file()
            and file.stat().st_size > 0
        )
    ]


def get_available_formats(
    video_url: str,
    client: str,
):

    logger.info(
        "[%s] Checking available formats for %s",
        client,
        video_url,
    )

    result = run_ytdlp(
        [
            "--no-playlist",

            "--extractor-args",
            f"youtube:player_client={client}",

            "--list-formats",

            video_url,
        ],
        timeout=180,
    )

    return (
        result.returncode,
        result.stdout or "",
        result.stderr or "",
    )


# ============================================================
# SAVE DOWNLOAD
# ============================================================

def save_download(
    source_file: Path,
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
        "application/octet-stream",
    )

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        source_file.name,
    )

    final_file = (
        DOWNLOAD_ROOT
        / safe_name
    )

    shutil.copy2(
        source_file,
        final_file,
    )

    logger.info(
        "Saved download: %s",
        final_file,
    )

    return FileResponse(
        path=final_file,
        media_type=media_type,
        filename=final_file.name,
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "ok": True,
        "service": "Shorts Archive Backend",
        "version": "1.7.0",
        "status": "running",
        "cookies_configured": COOKIE_SOURCE.exists(),
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "cookies_configured": COOKIE_SOURCE.exists(),
    }


# ============================================================
# DISCOVER
# ============================================================

@app.post("/discover")
def discover(
    req: DiscoverRequest,
):

    original_url = validate_youtube_url(
        req.channel_url
    )

    if not is_channel_url(
        original_url
    ):

        raise HTTPException(
            status_code=400,
            detail="Enter a public YouTube channel URL",
        )

    shorts_url = shorts_channel_url(
        original_url
    )

    errors = []

    # IMPORTANT:
    # Use multiple clients.
    # Do not restrict discovery to only mweb/android_vr.
    clients = [
        "android_vr",
        "tv",
        "web_embedded",
        "mweb",
    ]

    logger.info(
        "Starting Shorts discovery: %s",
        shorts_url,
    )

    for client in clients:

        try:

            logger.info(
                "Trying discovery client: %s",
                client,
            )

            entries, stderr = (
                discover_with_client(
                    shorts_url,
                    client,
                )
            )

            if entries:

                logger.info(
                    "Discovery succeeded with %s: %d videos",
                    client,
                    len(entries),
                )

                return {
                    "entries": entries,
                    "count": len(entries),
                    "client": client,
                    "source": shorts_url,
                }

            if stderr:

                errors.append(
                    f"{client}: {stderr[-2000:]}"
                )

        except HTTPException:

            raise

        except Exception as exc:

            logger.exception(
                "Discovery exception with %s",
                client,
            )

            errors.append(
                f"{client}: {exc}"
            )

    error_text = " | ".join(
        errors
    )[-6000:]

    logger.error(
        "All discovery clients failed: %s",
        error_text,
    )

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed.\n"
            + error_text
        ),
    )


# ============================================================
# DOWNLOAD
# ============================================================

@app.post("/download")
def download(
    req: DownloadRequest,
):

    video_url = (
        "https://www.youtube.com/shorts/"
        + req.video_id
    )

    tempdir = Path(
        tempfile.mkdtemp(
            prefix=f"short_{req.video_id}_",
            dir="/tmp",
        )
    )

    output_template = str(
        tempdir / "%(id)s.%(ext)s"
    )

    diagnostics = []

    try:

        # Try several YouTube clients.
        clients = [
            "mweb",
            "android_vr",
            "tv",
            "web_embedded",
        ]

        for client in clients:

            logger.info(
                "========================================"
            )

            logger.info(
                "DOWNLOAD CLIENT: %s",
                client,
            )

            logger.info(
                "VIDEO: %s",
                req.video_id,
            )

            logger.info(
                "========================================"
            )

            # ------------------------------------------------
            # STEP 1:
            # GET EXACT AVAILABLE FORMATS
            # ------------------------------------------------

            try:

                (
                    format_code,
                    format_output,
                    format_error,
                ) = get_available_formats(
                    video_url,
                    client,
                )

            except HTTPException:

                raise

            except Exception as exc:

                logger.exception(
                    "Format discovery failed",
                )

                diagnostics.append(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + f"CLIENT: {client}\n"
                    + "FORMAT DISCOVERY EXCEPTION:\n"
                    + str(exc)
                )

                continue

            logger.info(
                "[%s] Format discovery return code: %s",
                client,
                format_code,
            )

            # ------------------------------------------------
            # FORMAT DISCOVERY FAILED
            # ------------------------------------------------

            if format_code != 0:

                diagnostics.append(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + f"CLIENT: {client}\n"
                    + "FORMAT DISCOVERY FAILED:\n"
                    + format_output[-5000:]
                    + "\n"
                    + format_error[-5000:]
                )

                continue

            # ------------------------------------------------
            # SAVE FORMAT INFORMATION
            # ------------------------------------------------

            format_text = (
                format_output[-7000:]
                if format_output
                else "No format output returned."
            )

            logger.info(
                "[%s] AVAILABLE FORMATS:\n%s",
                client,
                format_text,
            )

            # ------------------------------------------------
            # CLEAN OLD FILES
            # ------------------------------------------------

            clean_temp_directory(
                tempdir
            )

            # ------------------------------------------------
            # STEP 2:
            # DOWNLOAD USING BEST AVAILABLE FORMAT
            # ------------------------------------------------

            logger.info(
                "[%s] Starting download",
                client,
            )

            download_result = run_ytdlp(
                [
                    "--no-playlist",

                    "--extractor-args",
                    f"youtube:player_client={client}",

                    "--retries",
                    "2",

                    "--fragment-retries",
                    "2",

                    "--file-access-retries",
                    "2",

                    "--retry-sleep",
                    "1",

                    "--socket-timeout",
                    "30",

                    # Let yt-dlp select from the
                    # formats it actually found.
                    "-f",
                    "bv*+ba/b",

                    "--merge-output-format",
                    "mp4",

                    "-o",
                    output_template,

                    video_url,
                ],
                timeout=300,
            )

            stdout = (
                download_result.stdout
                or ""
            )

            stderr = (
                download_result.stderr
                or ""
            )

            # ------------------------------------------------
            # CHECK DOWNLOADED FILE
            # ------------------------------------------------

            files = get_downloaded_files(
                tempdir
            )

            if (
                download_result.returncode == 0
                and files
            ):

                source_file = max(
                    files,
                    key=lambda file:
                    file.stat().st_size,
                )

                logger.info(
                    "SUCCESS: %s using %s (%d bytes)",
                    req.video_id,
                    client,
                    source_file.stat().st_size,
                )

                return save_download(
                    source_file
                )

            # ------------------------------------------------
            # SAVE DIAGNOSTIC INFORMATION
            # ------------------------------------------------

            diagnostics.append(
                "\n"
                + "=" * 60
                + "\n"
                + f"CLIENT: {client}\n"
                + "\nAVAILABLE FORMATS:\n"
                + format_text
                + "\n\nDOWNLOAD STDOUT:\n"
                + stdout[-3000:]
                + "\n\nDOWNLOAD STDERR:\n"
                + stderr[-7000:]
            )

            logger.warning(
                "[%s] Download failed:\n%s",
                client,
                stderr[-2500:],
            )

        # ====================================================
        # ALL CLIENTS FAILED
        # ====================================================

        diagnostic_text = "\n".join(
            diagnostics
        )

        logger.error(
            "DOWNLOAD FAILED FOR %s:\n%s",
            req.video_id,
            diagnostic_text[-15000:],
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube download failed.\n\n"
                "Diagnostic information for the exact "
                "Short is included below:\n\n"
                + diagnostic_text[-15000:]
            ),
        )

    finally:

        shutil.rmtree(
            tempdir,
            ignore_errors=True,
    )
