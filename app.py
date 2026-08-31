import logging
import os
import re
import shutil
import subprocess
import tempfile
import secrets
import hmac
import hashlib
import base64
import json
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
# SAFE VALIDATION ERROR HANDLING
#
# FastAPI's default RequestValidationError handler runs the raw
# request body through jsonable_encoder(), whose default bytes
# encoder is `lambda o: o.decode()` (strict UTF-8). If a request
# body contains bytes that are not valid UTF-8, that decode call
# itself raises UnicodeDecodeError *inside the exception handler*,
# which turns what should be a clean 422 response into an
# unhandled 500 ("Exception in ASGI application") and hides the
# real validation error. This handler sanitizes bytes first so a
# bad request always gets a proper, informative 422 instead of
# crashing the app.
# ============================================================

def _safe_jsonable(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {key: _safe_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_jsonable(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def safe_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    safe_errors = _safe_jsonable(exc.errors())

    logger.warning(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        safe_errors,
    )

    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(safe_errors)},
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

MAX_DISCOVER = int(os.getenv("MAX_DISCOVER", "50000"))
DISCOVER_BATCH_DEFAULT = int(os.getenv("DISCOVER_BATCH", "500"))

UPLOAD_QUEUE_FILE = Path(
    os.getenv("UPLOAD_QUEUE_FILE", "/data/upload_queue.json")
)
UPLOAD_DAILY_LIMIT = int(os.getenv("UPLOAD_DAILY_LIMIT", "8"))
UPLOAD_WORKER_SECRET = os.getenv("UPLOAD_WORKER_SECRET", "").strip()

UPLOAD_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

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
    "https://shorts-archive-backend.onrender.com/oauth/callback",
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

# Stateless signed OAuth state. This survives Render restarts/spin-downs.
OAUTH_STATE_SECRET = os.getenv(
    "OAUTH_STATE_SECRET",
    GOOGLE_CLIENT_SECRET or "change-this-oauth-state-secret",
)
OAUTH_STATE_MAX_AGE = int(os.getenv("OAUTH_STATE_MAX_AGE", "600"))

def create_oauth_state() -> str:
    payload = {"nonce": secrets.token_urlsafe(24), "iat": int(time.time())}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(OAUTH_STATE_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return encoded + "." + sig

def verify_oauth_state(state: str) -> bool:
    try:
        encoded, sig = state.split(".", 1)
        expected = hmac.new(OAUTH_STATE_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        if not hmac.compare_digest(supplied, expected):
            return False
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        issued_at = int(json.loads(raw.decode("utf-8"))["iat"])
        now = int(time.time())
        return issued_at <= now and now - issued_at <= OAUTH_STATE_MAX_AGE
    except Exception:
        return False


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
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=DISCOVER_BATCH_DEFAULT, ge=1, le=500)


class UploadQueueAddRequest(BaseModel):
    video_ids: list[str] = Field(default_factory=list, max_length=10000)


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
    offset: int = 0,
    limit: int = DISCOVER_BATCH_DEFAULT,
):

    # Ask yt-dlp only for the requested playlist window. This prevents
    # a channel with thousands of Shorts from being returned as one huge
    # response while still allowing the Android app to page through it.
    playlist_end = offset + limit

    result = run_ytdlp(
        [
            "--flat-playlist",
            "--lazy-playlist",
            "--ignore-errors",

            "--playlist-start",
            str(offset + 1),

            "--playlist-end",
            str(playlist_end),

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

        if len(found) >= limit:
            break

    return (
        found,
        result.stderr or "",
    )


# ============================================================
# SOURCE VIDEO METADATA (for upload title/description)
#
# The client app is currently sending the raw video_id as the
# "title" field instead of the short's actual title (a frontend
# bug). Rather than depend on the app to send the right values,
# fetch the real title/description straight from YouTube using
# the video_id, so uploads always use the source video's exact
# title and hashtags regardless of what the app sends.
# ============================================================

METADATA_SEPARATOR = "\x1f"


def fetch_source_metadata(
    video_id: str,
) -> tuple[str, str]:

    video_url = (
        "https://www.youtube.com/shorts/"
        + video_id
    )

    for client in DISCOVERY_CLIENTS:

        try:
            result = run_ytdlp(
                [
                    "--no-playlist",

                    "--extractor-args",
                    f"youtube:player_client={client}",

                    "--print",
                    f"%(title)s{METADATA_SEPARATOR}%(description)s",

                    "--skip-download",

                    video_url,
                ],
                timeout=60,
            )

        except HTTPException:
            continue

        if result.returncode != 0:
            continue

        line = (result.stdout or "").strip().splitlines()

        if not line:
            continue

        parts = line[0].split(METADATA_SEPARATOR, 1)

        title = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""

        if title:
            return title, description

    return "", ""


HASHTAG_PATTERN = re.compile(r"#(\w+)")


def extract_hashtags(*texts: str) -> list[str]:

    tags = []

    for text in texts:
        for match in HASHTAG_PATTERN.findall(text or ""):
            if match not in tags:
                tags.append(match)

    return tags

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
# REUSE AN ALREADY-DOWNLOADED FILE
#
# If this short was already fetched via /download, its file is
# sitting in DOWNLOAD_ROOT (named "<video_id>.<ext>"). Reuse it
# instead of re-downloading from YouTube for every upload.
# ============================================================

def find_existing_download(
    video_id: str,
) -> Path | None:

    if not DOWNLOAD_ROOT.exists():
        return None

    for path in DOWNLOAD_ROOT.iterdir():

        if (
            path.is_file()
            and path.stem == video_id
            and path.stat().st_size > 0
        ):
            return path

    return None


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
                "GOOGLE_CLIENT_SECRET in Render."
            ),
        )

 

    try:
        credentials = load_google_credentials()

        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="YouTube is not connected. Open /youtube/connect first.",
            )

        return build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not create YouTube service")
        raise HTTPException(
            status_code=500,
            detail="Could not connect to YouTube: " + str(exc),
        )


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
def root():
    return {
        "ok": True,
        "service": "Shorts Archive Backend",
        "version": "2.0.0",
        "status": "running",
        "cookies_configured": COOKIE_SOURCE.exists(),
        "youtube_oauth_configured": google_configured(),
        "youtube_connected": GOOGLE_TOKEN_FILE.exists(),
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "cookies_configured": COOKIE_SOURCE.exists(),
        "youtube_oauth_configured": google_configured(),
        "youtube_connected": GOOGLE_TOKEN_FILE.exists(),
    }


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return HTMLResponse(
        """
        <!doctype html>
        <html><head><meta charset="utf-8"><title>Privacy Policy - Shorts Auto Uploader</title>
        <meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:20px;line-height:1.6">
        <h1>Privacy Policy</h1>
        <p><strong>Shorts Auto Uploader</strong> is a personal utility for downloading public YouTube Shorts and, when explicitly authorized, uploading videos to the user's own YouTube channel.</p>
        <h2>Information we access</h2>
        <p>When you connect YouTube, Google OAuth provides the permissions you approve. We use those permissions only to perform YouTube actions requested by you, such as uploading videos.</p>
        <h2>How data is handled</h2>
        <p>Videos may be stored temporarily on the backend while an upload is being processed. Temporary files used by the automatic archive/upload workflow are deleted after processing.</p>
        <p>OAuth credentials are stored on the backend and are not intentionally shared with third parties. The service does not sell personal information.</p>
        <h2>Contact</h2>
        <p>For privacy questions, contact the developer through the email associated with the application.</p>
        <p>Last updated: August 30, 2026</p>
        </body></html>
        """
    )


# ============================================================
# DISCOVER SHORTS
# ============================================================

@app.post("/discover")
def discover(req: DiscoverRequest):
    original_url = validate_youtube_url(req.channel_url)

    if not is_channel_url(original_url):
        raise HTTPException(
            status_code=400,
            detail="Enter a public YouTube channel URL",
        )

    shorts_url = shorts_channel_url(original_url)
    errors = []

    logger.info(
        "Starting Shorts discovery: %s offset=%d limit=%d",
        shorts_url,
        req.offset,
        req.limit,
    )

    for client in DISCOVERY_CLIENTS:
        try:
            logger.info("Discovery client: %s", client)
            entries, stderr = discover_with_client(
                shorts_url,
                client,
                offset=req.offset,
                limit=min(req.limit, MAX_DISCOVER),
            )

            if entries:
                logger.info(
                    "Discovery succeeded: %d videos via %s",
                    len(entries),
                    client,
                )
                return {
                    "entries": entries,
                    "count": len(entries),
                    "offset": req.offset,
                    "limit": req.limit,
                    "has_more": len(entries) >= req.limit,
                    "client": client,
                    "source": shorts_url,
                }

            if stderr:
                errors.append(f"{client}: {stderr[-2500:]}")

        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Discovery exception with %s", client)
            errors.append(f"{client}: {exc}")

    # An empty page is a valid end-of-channel condition when paginating.
    if req.offset > 0:
        return {
            "entries": [],
            "count": 0,
            "offset": req.offset,
            "limit": req.limit,
            "has_more": False,
            "source": shorts_url,
        }

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed.\n\n"
            + "\n\n".join(errors)[-10000:]
        ),
    )


# ============================================================
# PERSISTENT UPLOAD QUEUE
# ============================================================

_QUEUE_LOCK = __import__("threading").Lock()
_QUEUE_RUN_LOCK = __import__("threading").Lock()


def _queue_default():
    return {"version": 1, "items": []}


def _load_queue():
    try:
        if not UPLOAD_QUEUE_FILE.exists():
            return _queue_default()
        data = json.loads(UPLOAD_QUEUE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return _queue_default()
        return data
    except Exception:
        logger.exception("Could not read upload queue")
        return _queue_default()


def _save_queue(data):
    UPLOAD_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = UPLOAD_QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(UPLOAD_QUEUE_FILE)


def _queue_normalize_id(video_id: str) -> str | None:
    value = str(video_id).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    return None


def _queue_status(data):
    now = time.time()
    items = data.get("items", [])
    uploaded_24h = [
        x for x in items
        if x.get("status") == "uploaded"
        and now - float(x.get("uploaded_at", 0) or 0) < 86400
    ]
    queued = [
        x for x in items
        if x.get("status") in {"queued", "processing"}
    ]
    failed = [x for x in items if x.get("status") == "failed"]

    return {
        "queue_total": len(items),
        "queued": len(queued),
        "failed": len(failed),
        "uploaded_total": sum(
            1 for x in items if x.get("status") == "uploaded"
        ),
        "uploaded_last_24h": len(uploaded_24h),
        "remaining_last_24h": max(0, UPLOAD_DAILY_LIMIT - len(uploaded_24h)),
        "daily_limit": UPLOAD_DAILY_LIMIT,
        "next_video_id": queued[0].get("video_id") if queued else None,
    }


def _worker_authorized(request: Request) -> bool:
    # Fail closed: cron/worker execution is disabled until a secret is set.
    if not UPLOAD_WORKER_SECRET:
        return False
    supplied = request.headers.get("X-Worker-Secret", "")
    return hmac.compare_digest(supplied, UPLOAD_WORKER_SECRET)


@app.post("/upload-queue/add")
def upload_queue_add(req: UploadQueueAddRequest):
    clean_ids = []
    seen_request = set()

    for raw_id in req.video_ids:
        video_id = _queue_normalize_id(raw_id)
        if video_id and video_id not in seen_request:
            seen_request.add(video_id)
            clean_ids.append(video_id)

    if not clean_ids:
        raise HTTPException(
            status_code=400,
            detail="No valid YouTube video IDs were supplied.",
        )

    with _QUEUE_LOCK:
        data = _load_queue()
        existing = {
            item.get("video_id")
            for item in data["items"]
            if item.get("status") in {"queued", "processing", "uploaded"}
        }

        added = 0
        skipped = 0

        for video_id in clean_ids:
            if video_id in existing:
                skipped += 1
                continue

            data["items"].append({
                "video_id": video_id,
                "status": "queued",
                "created_at": time.time(),
                "uploaded_at": None,
                "youtube_video_id": None,
                "error": None,
            })
            existing.add(video_id)
            added += 1

        _save_queue(data)

    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "status": _queue_status(data),
    }


@app.get("/upload-queue")
def upload_queue_status():
    with _QUEUE_LOCK:
        data = _load_queue()
    return {
        "ok": True,
        "status": _queue_status(data),
    }


@app.post("/upload-queue/run")
def upload_queue_run(request: Request):
    if not _worker_authorized(request):
        raise HTTPException(status_code=403, detail="Worker authorization required.")

    # Only one queue execution can run inside this process at a time.
    if not _QUEUE_RUN_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "started": False,
            "reason": "another queue upload is already running",
        }

    try:
        with _QUEUE_LOCK:
            data = _load_queue()

            now = time.time()
            uploaded_24h = sum(
                1 for item in data["items"]
                if item.get("status") == "uploaded"
                and now - float(item.get("uploaded_at", 0) or 0) < 86400
            )

            if uploaded_24h >= UPLOAD_DAILY_LIMIT:
                return {
                    "ok": True,
                    "started": False,
                    "reason": "daily upload limit reached",
                    "status": _queue_status(data),
                }

            item = next(
                (
                    x for x in data["items"]
                    if x.get("status") in {"queued", "processing"}
                ),
                None,
            )

            if item is None:
                return {
                    "ok": True,
                    "started": False,
                    "reason": "queue empty",
                    "status": _queue_status(data),
                }

            # If the previous request died during processing, retry it.
            item["status"] = "processing"
            item["started_at"] = time.time()
            item["error"] = None
            _save_queue(data)

        video_id = item["video_id"]

        try:
            result = youtube_upload_by_id(
                UploadRequest(
                    video_id=video_id,
                    title="",
                    description="",
                    tags=[],
                    privacy_status="public",
                )
            )

            with _QUEUE_LOCK:
                data = _load_queue()
                current = next(
                    (x for x in data["items"] if x.get("video_id") == video_id),
                    None,
                )
                if current:
                    current["status"] = "uploaded"
                    current["uploaded_at"] = time.time()
                    current["youtube_video_id"] = result.get("video_id")
                    current["youtube_url"] = result.get("youtube_url")
                    current["error"] = None
                    _save_queue(data)

            return {
                "ok": True,
                "started": True,
                "result": result,
                "status": _queue_status(data),
            }

        except Exception as exc:
            logger.exception("Queued upload failed for %s", video_id)

            with _QUEUE_LOCK:
                data = _load_queue()
                current = next(
                    (x for x in data["items"] if x.get("video_id") == video_id),
                    None,
                )
                if current:
                    # Put it back into the queue so a later cron run retries it.
                    current["status"] = "queued"
                    current["error"] = str(exc)[-2000:]
                    current["last_failed_at"] = time.time()
                    _save_queue(data)

            raise HTTPException(
                status_code=502,
                detail=f"Queued upload failed for {video_id}: {exc}",
            )

    finally:
        _QUEUE_RUN_LOCK.release()


# ============================================================
# EXISTING DOWNLOAD ENDPOINT
# ============================================================

@app.post("/download")
def download(req: DownloadRequest):
    video_url = "https://www.youtube.com/shorts/" + req.video_id

    tempdir = Path(
        tempfile.mkdtemp(
            prefix=f"short_{req.video_id}_",
            dir="/tmp",
        )
    )

    output_template = str(tempdir / "%(id)s.%(ext)s")
    diagnostics = []

    try:
        logger.info("Starting download for %s", req.video_id)

        for client in DOWNLOAD_CLIENTS:
            logger.info("Checking formats with client: %s", client)

            try:
                format_code, format_output, format_error = get_available_formats(
                    video_url, client
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Format inspection failed")
                diagnostics.append(
                    "\n" + ("=" * 60) +
                    "\nCLIENT: " + client +
                    "\nFORMAT CHECK EXCEPTION:\n" + str(exc)
                )
                continue

            if format_code != 0:
                diagnostics.append(
                    "\n" + ("=" * 60) +
                    "\nCLIENT: " + client +
                    "\nFORMAT DISCOVERY FAILED:\n" +
                    (format_output + "\n" + format_error)[-7000:]
                )
                continue

            if not has_real_media_formats(format_output):
                logger.warning("[%s] No real media formats", client)
                diagnostics.append(
                    "\n" + ("=" * 60) +
                    "\nCLIENT: " + client +
                    "\nNO REAL MEDIA FORMATS FOUND.\n" +
                    format_output[-7000:]
                )
                continue

            for selector in ["bv*+ba/b", "best"]:
                clean_tempdir(tempdir)

                logger.info(
                    "Downloading %s with %s / %s",
                    req.video_id,
                    client,
                    selector,
                )

                result = run_ytdlp(
                    [
                        "--no-playlist",
                        "--extractor-args",
                        f"youtube:player_client={client}",
                        "--retries", "2",
                        "--fragment-retries", "2",
                        "--file-access-retries", "2",
                        "--retry-sleep", "1",
                        "--socket-timeout", "30",
                        "-f", selector,
                        "--merge-output-format", "mp4",
                        "-o", output_template,
                        video_url,
                    ],
                    timeout=300,
                )

                files = get_downloaded_files(tempdir)

                if result.returncode == 0 and files:
                    source_file = max(
                        files,
                        key=lambda path: path.stat().st_size,
                    )
                    logger.info(
                        "Download successful: %s (%d bytes)",
                        req.video_id,
                        source_file.stat().st_size,
                    )
                    return save_download(source_file)

                diagnostics.append(
                    "\n" + ("=" * 60) +
                    "\nCLIENT: " + client +
                    "\nFORMAT SELECTOR: " + selector +
                    "\nAVAILABLE FORMATS:\n" + format_output[-5000:] +
                    "\nDOWNLOAD STDOUT:\n" + (result.stdout or "")[-2500:] +
                    "\nDOWNLOAD STDERR:\n" + (result.stderr or "")[-5000:]
                )

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube download failed.\n\n"
                "Diagnostic information for the exact Short:\n\n" +
                "\n".join(diagnostics)[-30000:]
            ),
        )

    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


# ============================================================
# GOOGLE / YOUTUBE OAUTH
# ============================================================

@app.get("/oauth/start")
def oauth_start():
    if not google_configured():
        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth is not configured. Set "
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render."
            ),
        )

    state = create_oauth_state()

    # PKCE is enabled by google-auth-oauthlib. The verifier must
    # survive Google's browser redirect and be supplied again here.
    flow = Flow.from_client_config(
        make_google_client_config(),
        scopes=YOUTUBE_SCOPES,
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )

    if not flow.code_verifier:
        raise HTTPException(
            status_code=500,
            detail="Could not create the YouTube OAuth code verifier.",
        )

    response = RedirectResponse(
        authorization_url,
        status_code=302,
    )

    # The cookie is only used during the short OAuth handshake.
    # Google redirects back to /oauth/callback, which is covered by
    # this cookie path.
    response.set_cookie(
        key="youtube_oauth_verifier",
        value=flow.code_verifier,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/oauth",
    )

    return response


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return HTMLResponse(
            f"<h2>YouTube authorization was not completed</h2><p>{error}</p>",
            status_code=400,
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth authorization code.",
        )

    if not state or not verify_oauth_state(state):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid or expired OAuth state. "
                "Start a new YouTube connection from /oauth/start."
            ),
        )

    if not google_configured():
        raise HTTPException(
            status_code=500,
            detail="Google OAuth is not configured in Render.",
        )

    code_verifier = request.cookies.get(
        "youtube_oauth_verifier"
    )

    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing OAuth code verifier. "
                "Start a new YouTube connection from /youtube/connect."
            ),
        )

    try:
        # Use the exact verifier generated before the redirect to Google.
        flow = Flow.from_client_config(
            make_google_client_config(),
            scopes=YOUTUBE_SCOPES,
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        flow.redirect_uri = GOOGLE_REDIRECT_URI

        flow.fetch_token(
            code=code,
            code_verifier=code_verifier,
        )

        save_google_credentials(
            flow.credentials
        )

        response = HTMLResponse(
            """
            <!doctype html>
            <html>
            <head>
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>YouTube Connected</title>
            </head>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:60px 20px">
                <h1>✅ YouTube Connected</h1>
                <p>Your YouTube account has been authorized successfully.</p>
                <p>You can return to Shorts Auto Uploader.</p>
            </body>
            </html>
            """
        )

        response.delete_cookie(
            key="youtube_oauth_verifier",
            path="/oauth",
        )

        return response

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "YouTube OAuth callback failed"
        )
        raise HTTPException(
            status_code=500,
            detail="YouTube authorization failed: " + str(exc),
        )


@app.get("/youtube/connect")
def youtube_connect():
    """Browser-friendly endpoint that starts the YouTube OAuth flow."""
    return RedirectResponse(
        "/oauth/start",
        status_code=302,
    )


@app.get("/youtube/account")
def youtube_account():
    service = youtube_service()

    try:
        response = service.channels().list(
            part="snippet,contentDetails,statistics",
            mine=True,
        ).execute()

        items = response.get("items", [])
        if not items:
            return {
                "connected": True,
                "channel": None,
                "message": "YouTube authorization succeeded, but no channel was returned.",
            }

        channel = items[0]
        snippet = channel.get("snippet", {})
        statistics = channel.get("statistics", {})

        return {
            "connected": True,
            "channel": {
                "id": channel.get("id"),
                "title": snippet.get("title", ""),
                "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                "subscribers": statistics.get("subscriberCount"),
                "videos": statistics.get("videoCount"),
                "views": statistics.get("viewCount"),
            },
        }

    except Exception as exc:
        logger.exception("Could not read YouTube account")
        raise HTTPException(
            status_code=502,
            detail="Could not read the connected YouTube account: " + str(exc),
        )


# ============================================================
# YOUTUBE UPLOAD
# ============================================================

def upload_file_to_youtube(
    video_file: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str,
):
    if privacy_status not in {"private", "public", "unlisted"}:
        raise HTTPException(
            status_code=400,
            detail="privacy_status must be private, public, or unlisted",
        )

    service = youtube_service()

    body = {
        "snippet": {
            "title": title[:100] or "Shorts Archive",
            "description": description[:5000],
            "tags": tags[:500],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_file),
        mimetype="video/mp4",
        resumable=True,
    )

    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    return response


def _finish_upload(
    video_file: Path,
    tempdir: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str,
    source_video_id: str | None,
):
    try:
        response = upload_file_to_youtube(
            video_file,
            title,
            description,
            tags,
            privacy_status,
        )

        return {
            "ok": True,
            "uploaded": True,
            "video_id": response.get("id"),
            "youtube_url": (
                "https://www.youtube.com/watch?v=" + response["id"]
                if response.get("id") else None
            ),
            "source_video_id": source_video_id,
        }

    finally:
        # The archive/upload copy is temporary and is removed after upload.
        shutil.rmtree(tempdir, ignore_errors=True)


# ============================================================
# YOUTUBE UPLOAD (multipart form)
#
# The app itself already downloaded the video and posts it here
# as a multipart/form-data upload (fields: title, description,
# privacy_status, and a "video" file) rather than a JSON body
# with a video_id. This endpoint matches that actual request
# shape instead of forcing a contract the app never used.
# ============================================================

# ============================================================
# YOUTUBE UPLOAD (multipart form)
#
# The app itself already downloaded the video and posts it here
# as a multipart/form-data upload (fields: title, description,
# privacy_status, and a "video" file) rather than a JSON body
# with a video_id. This endpoint matches that actual request
# shape instead of forcing a contract the app never used.
#
# NOTE: the app currently sends the raw video_id as "title" and
# always sends privacy_status="private" (frontend bugs, not
# something the user chose per-upload). Rather than upload with
# those wrong values, this endpoint looks up the source video's
# real title/description/hashtags on YouTube using the video_id
# (from the uploaded filename) and always publishes as public.
# ============================================================

@app.post("/youtube/upload")
def youtube_upload(
    video: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    privacy_status: str = Form("private"),
    tags: str = Form(""),
):
    original_name = video.filename or "upload.mp4"
    video_id = Path(original_name).stem or None

    tempdir = Path(
        tempfile.mkdtemp(
            prefix=f"archive_upload_{video_id or 'unknown'}_",
            dir="/tmp",
        )
    )
    video_file = tempdir / original_name

    try:
        with open(video_file, "wb") as out_file:
            shutil.copyfileobj(video.file, out_file)
    finally:
        video.file.close()

    source_title, source_description = (
        fetch_source_metadata(video_id) if video_id else ("", "")
    )

    final_title = (
        source_title
        or title.strip()
        or (f"Shorts Archive - {video_id}" if video_id else "Shorts Archive")
    )[:100]

    final_description = (
        source_description.strip()
        or description.strip()
        or (
            "Archived Short\n\n"
            "#Shorts #YouTubeShorts"
        )
    )

    tag_list = list(dict.fromkeys(
        [tag.strip().lstrip("#") for tag in tags.split(",") if tag.strip()]
        + extract_hashtags(final_title, final_description)
        + ["Shorts", "YouTubeShorts"]
    ))

    # Always publish public, regardless of what the app sends for
    # privacy_status, since that field is currently hardcoded on
    # the app side rather than a deliberate per-upload choice.
    final_privacy_status = "public"

    return _finish_upload(
        video_file,
        tempdir,
        final_title,
        final_description,
        tag_list,
        final_privacy_status,
        source_video_id=video_id,
    )


# ============================================================
# YOUTUBE UPLOAD (by video_id, JSON body)
#
# Used internally by /archive/upload. Downloads (or reuses an
# already-downloaded copy of) the short by its YouTube video_id,
# then uploads it.
# ============================================================

def youtube_upload_by_id(req: UploadRequest):
    existing = find_existing_download(req.video_id)

    if existing is not None:
        tempdir = Path(
            tempfile.mkdtemp(
                prefix=f"archive_upload_{req.video_id}_",
                dir="/tmp",
            )
        )
        video_file = tempdir / existing.name
        shutil.copy2(existing, video_file)

        logger.info(
            "Reusing already-downloaded file for upload: %s",
            existing,
        )
    else:
        video_file = download_to_temp_file(req.video_id)
        tempdir = video_file.parent

    source_title, source_description = fetch_source_metadata(req.video_id)

    title = (
        source_title
        or req.title.strip()
        or f"Shorts Archive - {req.video_id}"
    )[:100]

    description = (
        source_description.strip()
        or req.description.strip()
        or "Archived Short"
    )

    default_hashtags = os.getenv(
        "DEFAULT_HASHTAGS",
        "#Shorts #YouTubeShorts",
    ).strip()

    if default_hashtags:
        description = (
            description.rstrip()
            + "\n\n"
            + default_hashtags
        )[:5000]

    tags = list(dict.fromkeys(
        [tag.strip().lstrip("#") for tag in req.tags if tag.strip()]
        + extract_hashtags(title, description)
        + ["Shorts", "YouTubeShorts"]
    ))

    return _finish_upload(
        video_file,
        tempdir,
        title,
        description,
        tags,
        "public",
        source_video_id=req.video_id,
    )


# Friendly alias for the complete one-short archive workflow.
@app.post("/archive/upload")
def archive_upload(req: ArchiveUploadRequest):
    return youtube_upload_by_id(
        UploadRequest(
            video_id=req.video_id,
            title=req.title,
            description=req.description,
            tags=req.tags,
            privacy_status=req.privacy_status,
        )
    )
