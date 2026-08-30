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
    version="1.8.0",
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

COOKIE_SOURCE = Path(
    "/etc/secrets/cookies.txt"
)

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
# YOUTUBE CLIENTS
# ============================================================

# Order matters.
#
# default,mweb:
#   Main attempt. Current yt-dlp guidance recommends mweb
#   with a PO-token provider.
#
# web_safari:
#   Useful fallback because it can expose HLS formats.
#
# android_vr:
#   Does not currently require a PO token.
#
# mweb:
#   Final direct mweb attempt.
#
YOUTUBE_CLIENTS = [
    "default,mweb",
    "web_safari",
    "android_vr",
    "mweb",
]


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
# OPTIONAL JS RUNTIME
# ============================================================

def get_js_runtime_args() -> list[str]:

    """
    Newer yt-dlp YouTube extraction can require a JS runtime.

    Only add one if it actually exists in the container.
    This prevents the backend from breaking if Render doesn't
    have one installed.
    """

    for runtime in [
        "deno",
        "node",
        "bun",
        "qjs",
        "quickjs",
    ]:

        if shutil.which(runtime):

            logger.info(
                "Using yt-dlp JS runtime: %s",
                runtime,
            )

            return [
                "--js-runtimes",
                runtime,
            ]

    logger.warning(
        "No Deno/Node/Bun/QuickJS runtime detected"
    )

    return []


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

        # Current YouTube PO-token provider.
        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL}"
        ),
    ]

    # Add JS runtime when available.
    command.extend(
        get_js_runtime_args()
    )

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
# TEMP FILE HELPERS
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


# ============================================================
# FORMAT DISCOVERY
# ============================================================

def get_available_formats(
    video_url: str,
    client: str,
):

    logger.info(
        "[%s] Checking exact formats for %s",
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


def extract_format_lines(
    format_output: str,
) -> list[str]:

    lines = []

    for line in format_output.splitlines():

        stripped = line.strip()

        if not stripped:
            continue

        # Skip headers.
        if stripped.startswith("ID "):
            continue

        if stripped.startswith(
            "────────────────"
        ):
            continue

        # Ignore storyboard-only formats.
        if "mhtml" in stripped.lower():
            continue

        lines.append(
            stripped
        )

    return lines


def has_real_video_format(
    format_output: str,
) -> bool:

    """
    Detect whether yt-dlp received actual media formats.

    Storyboard-only responses contain mhtml and aren't useful
    for downloading the Short.
    """

    for line in format_output.splitlines():

        lowered = line.lower()

        if "mhtml" in lowered:
            continue

        # Common actual media indicators.
        if (
            "mp4" in lowered
            or "webm" in lowered
            or "avc" in lowered
            or "vp9" in lowered
            or "av01" in lowered
            or "audio only" in lowered
            or "video only" in lowered
        ):
            return True

    return False


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
        "Saved file: %s",
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
        "version": "1.8.0",
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
# DISCOVER SHORTS
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

    logger.info(
        "========================================"
    )

    logger.info(
        "STARTING SHORTS DISCOVERY"
    )

    logger.info(
        "SOURCE: %s",
        shorts_url,
    )

    logger.info(
        "========================================"
    )

    for client in YOUTUBE_CLIENTS:

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
                    f"{client}: {stderr[-2500:]}"
                )

        except HTTPException:

            raise

        except Exception as exc:

            logger.exception(
                "Discovery failed with %s",
                client,
            )

            errors.append(
                f"{client}: {exc}"
            )

    error_text = " | ".join(
        errors
    )[-8000:]

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed.\n\n"
            + error_text
        ),
    )


# ============================================================
# DOWNLOAD SHORT
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

        logger.info(
            "========================================"
        )

        logger.info(
            "STARTING DOWNLOAD"
        )

        logger.info(
            "VIDEO ID: %s",
            req.video_id,
        )

        logger.info(
            "URL: %s",
            video_url,
        )

        logger.info(
            "========================================"
        )

        for client in YOUTUBE_CLIENTS:

            logger.info(
                "----------------------------------------"
            )

            logger.info(
                "CLIENT: %s",
                client,
            )

            logger.info(
                "----------------------------------------"
            )

            # =================================================
            # STEP 1 — GET ACTUAL FORMATS
            # =================================================

            try:

                (
                    format_return_code,
                    format_output,
                    format_error,
                ) = get_available_formats(
                    video_url,
                    client,
                )

            except Exception as exc:

                logger.exception(
                    "Format inspection failed"
                )

                diagnostics.append(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + f"CLIENT: {client}\n"
                    + "FORMAT INSPECTION EXCEPTION:\n"
                    + str(exc)
                )

                continue

            format_lines = extract_format_lines(
                format_output
            )

            real_formats = has_real_video_format(
                format_output
            )

            logger.info(
                "[%s] format return code: %s",
                client,
                format_return_code,
            )

            logger.info(
                "[%s] real media formats: %s",
                client,
                real_formats,
            )

            if format_lines:

                logger.info(
                    "[%s] usable format lines:\n%s",
                    client,
                    "\n".join(
                        format_lines[-100:]
                    ),
                )

            # =================================================
            # FORMAT DISCOVERY FAILED
            # =================================================

            if format_return_code != 0:

                diagnostics.append(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + f"CLIENT: {client}\n"
                    + "FORMAT DISCOVERY FAILED\n\n"
                    + format_output[-5000:]
                    + "\n"
                    + format_error[-5000:]
                )

                continue

            # =================================================
            # STORYBOARD ONLY
            # =================================================

            if not real_formats:

                logger.warning(
                    "[%s] Only storyboard/non-media formats returned",
                    client,
                )

                diagnostics.append(
                    "\n"
                    + "=" * 60
                    + "\n"
                    + f"CLIENT: {client}\n"
                    + "NO REAL MEDIA FORMATS\n\n"
                    + format_output[-6000:]
                )

                continue

            # =================================================
            # STEP 2 — DOWNLOAD
            # =================================================

            clean_temp_directory(
                tempdir
            )

            download_attempts = [
                "bv*+ba/b",
                "best",
            ]

            download_succeeded = False
            last_stdout = ""
            last_stderr = ""

            for format_selector in download_attempts:

                logger.info(
                    "[%s] Trying format selector: %s",
                    client,
                    format_selector,
                )

                clean_temp_directory(
                    tempdir
                )

                result = run_ytdlp(
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
                        format_selector,

        
