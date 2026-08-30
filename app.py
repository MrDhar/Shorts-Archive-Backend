import logging
import os
import re
import shutil
import subprocess
import tempfile
import secrets
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

# ============================================================
# OPTIONAL GOOGLE / YOUTUBE IMPORTS
# ============================================================

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    Credentials = None
    Flow = None
    build = None
    MediaFileUpload = None


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("shorts-archive")


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Shorts Archive Backend",
    version="2.0.0",
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
# GOOGLE / YOUTUBE CONFIG
# ============================================================

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID",
    "",
)

GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_CLIENT_SECRET",
    "",
)

GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://shorts-archive-backend.onrender.com/oauth2callback",
)

GOOGLE_TOKEN_FILE = Path(
    os.getenv(
        "GOOGLE_TOKEN_FILE",
        "/data/google_token.json",
    )
)

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# OAuth state is kept temporarily in memory.
# This is sufficient while the service is running.
OAUTH_STATES = set()


# ============================================================
# CLIENTS
# ============================================================

DISCOVERY_CLIENTS = [
    "mweb",
    "android_vr",
    "web_embedded",
    "tv",
]

DOWNLOAD_CLIENTS = [
    "mweb",
    "android_vr",
    "web_safari",
    "web_embedded",
    "tv",
]


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


class UploadRequest(BaseModel):
    video_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{11}$",
    )

    title: str = Field(
        default="",
        max_length=100,
    )

    description: str = Field(
        default="",
        max_length=5000,
    )

    tags: list[str] = Field(
        default_factory=list,
    )

    privacy_status: str = Field(
        default="private",
    )


class ArchiveUploadRequest(BaseModel):
    video_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{11}$",
    )

    title: str = Field(
        default="",
        max_length=100,
    )

    description: str = Field(
        default="",
        max_length=5000,
    )

    tags: list[str] = Field(
        default_factory=list,
    )

    privacy_status: str = Field(
        default="private",
    )


# ============================================================
# YOUTUBE URL VALIDATION
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

    host = parsed.netloc.lower().split(":")[0]

    if host not in {
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

    if not path.lower().endswith("/shorts"):
        path += "/shorts"

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
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
# JAVASCRIPT RUNTIME
# ============================================================

def js_runtime_args() -> list[str]:

    for runtime in (
        "deno",
        "node",
        "bun",
        "qjs",
    ):

        if shutil.which(runtime):

            logger.info(
                "Using JS runtime: %s",
                runtime,
            )

            return [
                "--js-runtimes",
                runtime,
            ]

    logger.warning(
        "No JavaScript runtime detected"
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

        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL}"
        ),
    ]

    command.extend(
        js_runtime_args()
    )

    command.extend(
        args
    )

    logger.info(
        "Running yt-dlp: %s",
        " ".join(args),
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
            detail=(
                "yt-dlp is not installed "
                "on the backend"
            ),
        )

    except subprocess.TimeoutExpired:

        raise HTTPException(
            status_code=504,
            detail=(
                "YouTube request timed out"
            ),
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

    return (
        found,
        result.stderr or "",
    )


# ============================================================
# TEMP DIRECTORY
# ============================================================

def clean_tempdir(
    tempdir: Path,
):

    for item in tempdir.iterdir():

        if item.is_file():

            try:
                item.unlink()
            except OSError:
                pass


def get_downloaded_files(
    tempdir: Path,
):

    return [
        path
        for path in tempdir.iterdir()
        if (
            path.is_file()
            and path.stat().st_size > 0
        )
    ]


# ============================================================
# FORMAT DISCOVERY
# ============================================================

def get_available_formats(
    video_url: str,
    client: str,
):

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


def has_real_media_formats(
    format_output: str,
) -> bool:

    for line in format_output.splitlines():

        lowered = line.lower()

        if "mhtml" in lowered:
            continue

        if any(
            marker in lowered
            for marker in (
                "video only",
                "audio only",
                "mp4",
                "webm",
                "avc1",
                "vp9",
                "av01",
            )
        ):

            return True

    return False


# ============================================================
# SAVE DOWNLOAD
# ============================================================

def save_download(
    source_file: Path,
):

    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".m4v": "video/mp4",
    }

    extension = source_file.suffix.lower()

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
        media_type=media_types.get(
            extension,
            "application/octet-stream",
        ),
        filename=final_file.name,
    )


# ============================================================
# INTERNAL DOWNLOAD TO TEMP FILE
#
# IMPORTANT:
# This is separate from /download.
# Your existing /download endpoint remains untouched.
# ============================================================

def download_to_temp_file(
    video_id: str,
) -> Path:

    video_url = (
        "https://www.youtube.com/shorts/"
        + video_id
    )

    tempdir = Path(
        tempfile.mkdtemp(
            prefix=(
                f"archive_upload_{video_id}_"
            ),
            dir="/tmp",
        )
    )

    output_template = str(
        tempdir / "%(id)s.%(ext)s"
    )

    diagnostics = []

    try:

        logger.info(
            "Starting temporary download for upload: %s",
            video_id,
        )

        for client in DOWNLOAD_CLIENTS:

            logger.info(
                "Checking formats with client: %s",
                client,
            )

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
                    "Format inspection failed"
                )

                diagnostics.append(
                    "\n"
                    + ("=" * 60)
                    + "\nCLIENT: "
                    + client
                    + "\nFORMAT CHECK EXCEPTION:\n"
                    + str(exc)
                )

                continue

            if format_code != 0:

                diagnostics.append(
                    "\n"
                    + ("=" * 60)
                    + "\nCLIENT: "
                    + client
                    + "\nFORMAT DISCOVERY FAILED:\n"
                    + (
                        format_output
                        + "\n"
                        + format_error
                    )[-7000:]
                )

                continue

            if not has_real_media_formats(
                format_output
            ):

                logger.warning(
                    "[%s] No real media formats",
                    client,
                )

                diagnostics.append(
                    "\n"
                    + ("=" * 60)
                    + "\nCLIENT: "
                    + client
                    + "\nNO REAL MEDIA FORMATS FOUND.\n"
                    + format_output[-7000:]
                )

                continue

            selectors = [
                "bv*+ba/b",
                "best",
            ]

            for selector in selectors:

                clean_tempdir(
                    tempdir
                )

                logger.info(
                    "Temporary downloading %s with %s / %s",
                    video_id,
                    client,
                    selector,
                )

                result = run_ytdlp(
                    [
                        "--no-playlist",

                        "--extractor-args",
                        (
                            "youtube:"
                            f"player_client={client}"
                        ),

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
                        selector,

                        "--merge-output-format",
                        "mp4",

                        "-o",
                        output_template,

                        video_url,
                    ],
                    timeout=300,
                )

                files = get_downloaded_files(
                    tempdir
                )

                if (
                    result.returncode == 0
                    and files
                ):

                    source_file = max(
                        files,
                        key=lambda path:
                        path.stat().st_size,
                    )

                    logger.info(
                        "Temporary download successful: %s "
                        "(%d bytes)",
                        video_id,
                        source_file.stat().st_size,
                    )

                    return source_file

                stdout = (
                    result.stdout or ""
                )

                stderr = (
                    result.stderr or ""
                )

                diagnostics.append(
                    "\n"
                    + ("=" * 60)
                    + "\nCLIENT: "
                    + client
                    + "\nFORMAT SELECTOR: "
                    + selector
                    + "\nAVAILABLE FORMATS:\n"
                    + format_output[-5000:]
                    + "\nDOWNLOAD STDOUT:\n"
                    + stdout[-2500:]
                    + "\nDOWNLOAD STDERR:\n"
                    + stderr[-5000:]
                )

        diagnostic_text = "\n".join(
            diagnostics
        )[-30000:]

        raise HTTPException(
            status_code=502,
            detail=(
                "Temporary YouTube download failed.\n\n"
                + diagnostic_text
            ),
        )

    except Exception:

        shutil.rmtree(
            tempdir,
            ignore_errors=True,
        )

        raise


# ============================================================
# GOOGLE OAUTH HELPERS
# ============================================================

def google_configured() -> bool:

    return bool(
        GOOGLE_CLIENT_ID
        and GOOGLE_CLIENT_SECRET
        and Flow
    )


def make_google_client_config():

    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                GOOGLE_REDIRECT_URI,
            ],
        }
    }


def save_google_credentials(
    credentials,
):

    GOOGLE_TOKEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        GOOGLE_TOKEN_FILE,
        "w",
        encoding="utf-8",
    ) as token_file:

        token_file.write(
            credentials.to_json()
        )

    logger.info(
        "Google OAuth credentials saved to %s",
        GOOGLE_TOKEN_FILE,
    )


def load_google_credentials():

    if not GOOGLE_TOKEN_FILE.exists():

        return None

    try:

        credentials = Credentials.from_authorized_user_file(
            str(GOOGLE_TOKEN_FILE),
            YOUTUBE_SCOPES,
        )

        return credentials

    except Exception as exc:

        logger.warning(
            "Could not load Google token: %s",
            exc,
        )

        return None


def youtube_service():

    if not google_configured():

        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth is not configured. "
                "Set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SEC
