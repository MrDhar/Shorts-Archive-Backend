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


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Shorts Archive Backend",
    version="1.7.0"
)


# ============================================================
# CONFIG
# ============================================================

DOWNLOAD_ROOT = Path(
    os.getenv(
        "DOWNLOAD_ROOT",
        "/data/downloads"
    )
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)


MAX_DISCOVER = int(
    os.getenv(
        "MAX_DISCOVER",
        "500"
    )
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
#
# IMPORTANT:
# This file is READ-ONLY.
#
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

def validate_youtube_url(
    url: str
) -> str:

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


def shorts_channel_url(
    url: str
) -> str:

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

        shorts_path = (
            path
            + "/shorts"
        )

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


def is_channel_url(
    url: str
) -> bool:

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

    if not COOKIE_SOURCE.is_file():

        raise HTTPException(
            status_code=500,
            detail=(
                "YouTube cookies Secret File exists "
                "but is not a regular file"
            )
        )

    try:

        shutil.copyfile(
            COOKIE_SOURCE,
            COOKIE_FILE
        )

        # Restrict permissions on the writable copy.
        try:
            os.chmod(
                COOKIE_FILE,
                0o600
            )
        except Exception:
            pass

        return COOKIE_FILE

    except Exception as exc:

        logger.exception(
            "Could not prepare YouTube cookies"
        )

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

        # bgutil PO-token provider
        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL}"
        ),
    ]

    command.extend(args)

    logger.info(
        "Running yt-dlp with %d arguments",
        len(args)
    )

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        return result

    except FileNotFoundError:

        raise HTTPException(
            status_code=500,
            detail=(
                "yt-dlp is not installed "
                "on the backend"
            )
        )

    except subprocess.TimeoutExpired:

        raise HTTPException(
            status_code=504,
            detail=(
                "YouTube request timed out"
            )
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

        seen.add(
            video_id
        )

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

    return (
        found,
        result.stderr
    )


# ============================================================
# SAVE DOWNLOAD
# ============================================================

def save_download(
    source_file: Path
):

    extension = (
        source_file.suffix.lower()
    )

    media_types = {

        ".mp4":
            "video/mp4",

        ".webm":
            "video/webm",

        ".mkv":
            "video/x-matroska",

        ".mov":
            "video/quicktime",

        ".m4v":
            "video/mp4",
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

    logger.info(
        "Saved download: %s (%d bytes)",
        final_file.name,
        final_file.stat().st_size
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

        "service":
            "Shorts Archive Backend",

        "status":
            "running",

        "version":
            "1.7.0",

        "cookies_configured":
            COOKIE_SOURCE.exists(),

        "pot_provider":
            POT_URL,
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,

        "cookies_configured":
            COOKIE_SOURCE.exists(),

        "pot_provider":
            POT_URL,
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
            detail=(
                "Enter a public YouTube channel URL"
            )
        )

    # Always use the Shorts tab.
    shorts_url = shorts_channel_url(
        original_url
    )

    errors = []

    # Fast discovery clients.
    clients = [
        "mweb",
        "android_vr",
    ]

    for client in clients:

        try:

            logger.info(
                "Discovering Shorts using client: %s",
                client
            )

            entries, stderr = (
                discover_with_client(
                    shorts_url,
                    client
                )
            )

            if entries:

                logger.info(
                    "Found %d Shorts using %s",
                    len(entries),
                    client
                )

                return {
                    "entries":
                        entries,

                    "count":
                        len(entries),

                    "client":
                        client,

                    "source":
                        shorts_url,
                }

            if stderr:

                errors.append(
                    f"{client}: "
                    f"{stderr[-1500:]}"
                )

        except HTTPException:

            raise

        except Exception as exc:

            logger.exception(
                "Discovery failed for %s",
                client
            )

            errors.append(
                f"{client}: {exc}"
            )

    error_text = (
        " | ".join(errors)
    )[-5000:]

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed. "
            + error_text
        )
    )


# ============================================================
# FORMAT DISCOVERY
# ============================================================

def get_available_formats(
    video_url: str,
    client: str
):

    logger.info(
        "Checking available formats for %s "
        "using client %s",
        video_url,
        client
    )

    result = run_ytdlp(
        [
            "--no-playlist",

            "--extractor-args",
            f"youtube:player_client={client}",

            "--list-formats",

            video_url,
        ],
        timeout=180
    )

    output = (
        result.stdout
        or ""
    )

    error = (
        result.stderr
        or ""
    )

    combined = (
        output
        + "\n"
        + error
    ).strip()

    return (
        result.returncode,
        combined
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
            prefix=(
                f"short_{req.video_id}_"
            ),
            dir="/tmp"
        )
    )

    output_template = str(
        tempdir
        / "%(id)s.%(ext)s"
    )

    try:

        # ----------------------------------------------------
        # CLIENT ORDER
        # ----------------------------------------------------

        clients = [
            "mweb",
            "android_vr",
            "web_safari",
        ]

        diagnostics = []

        start_time = time.time()

        # ----------------------------------------------------
        # TRY EACH CLIENT
        # ----------------------------------------------------

        for client_index, client in enumerate(
            clients,
            1
        ):

            elapsed = (
                time.time()
                - start_time
            )

            if elapsed > 480:

                logger.warning(
                    "Total download timeout: %.0fs",
                    elapsed
                )

                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Download took too long. "
                        "YouTube/network request "
                        "did not complete."
                    )
                )

            logger.info(
                "[Client %d/%d] "
                "Processing %s with %s",
                client_index,
                len(clients),
                req.video_id,
                client
            )

            # =================================================
            # STEP 1
            # ASK YT-DLP WHAT FORMATS EXIST
            # =================================================

            try:

                format_returncode, format_output = (
                    get_available_formats(
                        video_url,
                        client
                    )
                )

            except HTTPException:

                raise

            except Exception as exc:

                logger.exception(
                    "Format discovery exception"
                )

                diagnostics.append(
                    "\n"
                    + "=" * 70
                    + "\nCLIENT: "
                    + client
                    + "\nFORMAT DISCOVERY EXCEPTION:\n"
                    + str(exc)
                )

                continue

            logger.info(
                "[%s] Format discovery returned "
                "code %s",
                client,
                format_returncode
            )

            # -------------------------------------------------
            # If format discovery itself failed
            # -------------------------------------------------

            if format_returncode != 0:

                diagnostics.append(
                    "\n"
                    + "=" * 70
                    + "\nCLIENT: "
                    + client
                    + "\nFORMAT DISCOVERY FAILED:\n"
                    + format_output[-7000:]
                )

                logger.warning(
                    "[%s] Format discovery failed:\n%s",
                    client,
                    format_output[-2000:]
                )

                continue

            # =================================================
            # STEP 2
            # STORE THE ACTUAL FORMAT INFORMATION
            # =================================================

            available_format_lines = []

            for line in format_output.splitlines():

                stripped = line.strip()

                if not stripped:
                    continue

                # Ignore yt-dlp headers.
                if stripped.startswith(
                    "ID "
                ):
                    continue

                if stripped.startswith(
                    "────────────────"
                ):
                    continue

                # Keep lines that look like
                # yt-dlp format rows.
                if re.match(
                    r"^[0-9A-Za-z]+(?:\s|\|)",
                    stripped
                ):

                    available_format_lines.append(
                        stripped
                    )

            # If parsing didn't identify rows,
            # retain the raw output anyway.
            if not available_format_lines:

                available_format_lines = (
                    format_output
                    .splitlines()
                )[-100:]

            logger.info(
                "[%s] Available format information:\n%s",
                client,
                "\n".join(
                    available_format_lines[-100:]
                )
            )

            # =================================================
            # STEP 3
            # CLEAN OLD FILES
            # =================================================

            for old_file in tempdir.iterdir():

                if old_file.is_file():

                    try:

                        old_file.unlink()

                    except Exception:

                        pass

            # =================================================
            # STEP 4
            # DOWNLOAD
            #
            # bv* = best available video
            # ba  = best available audio
            # /b  = fallback to best single file
            # =================================================

            logger.info(
                "[%s] Starting download with "
                "bv*+ba/b",
                client
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

                    "-f",
                    "bv*+ba/b",

                    "--merge-output-format",
                    "mp4",

                    "-o",
                    output_template,

                    video_url,
                ],
                timeout=300
            )

            download_stdout = (
                download_result.stdout
                or ""
            )

            download_stderr = (
                download_result.stderr
                or ""
            )

            # =================================================
            # STEP 5
            # CHECK OUTPUT FILE
            # =================================================

            files = [
                file
                for file in tempdir.iterdir()
                if (
                    file.is_file()
                    and file.stat().st_size > 0
                )
            ]

            if (
                download_result.returncode == 0
                and files
            ):
