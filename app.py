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
from html import escape
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
        and GOOGLE_REDIRECT_URI
        and Flow
        and Credentials
        and build
        and MediaFileUpload
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
                "Google OAuth is not fully configured. "
                "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET "
                "and GOOGLE_REDIRECT_URI in Render."
            ),
        )

    try:
        credentials = load_google_credentials()

        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="YouTube is not connected. Open /oauth/start first.",
            )

        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request as GoogleRequest
            credentials.refresh(GoogleRequest())
            save_google_credentials(credentials)

        if not credentials.valid:
            raise HTTPException(
                status_code=401,
                detail=(
                    "YouTube authorization has expired. "
                    "Open /oauth/start to connect again."
                ),
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

    logger.info("Starting Shorts discovery: %s", shorts_url)

    for client in DISCOVERY_CLIENTS:
        try:
            logger.info("Discovery client: %s", client)
            entries, stderr = discover_with_client(shorts_url, client)

            if entries:
                logger.info(
                    "Discovery succeeded: %d videos via %s",
                    len(entries),
                    client,
                )
                return {
                    "entries": entries,
                    "count": len(entries),
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

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed.\n\n"
            + "\n\n".join(errors)[-10000:]
        ),
    )


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

    flow = Flow.from_client_config(
        make_google_client_config(),
        scopes=YOUTUBE_SCOPES,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )

    return RedirectResponse(authorization_url, status_code=302)


@app.get("/oauth2callback", response_class=HTMLResponse)
def oauth2callback(code: str | None = None, state: str | None = None, error: str | None = None):
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

    try:
        flow = Flow.from_client_config(
            make_google_client_config(),
            scopes=YOUTUBE_SCOPES,
            state=state,
        )
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        flow.fetch_token(code=code)
        save_google_credentials(flow.credentials)

        return RedirectResponse(
            "/youtube/account?connected=1",
            status_code=303,
        )

    except Exception as exc:
        logger.exception("YouTube OAuth callback failed")
        raise HTTPException(
            status_code=500,
            detail="YouTube authorization failed: " + str(exc),
        )


# Alias kept for clients that use the more common callback spelling.
@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback_alias(code: str | None = None, state: str | None = None, error: str | None = None):
    return oauth2callback(code=code, state=state, error=error)


@app.get("/youtube/account", response_class=HTMLResponse)
def youtube_account(connected: str | None = None):

    # Do not call youtube_service() before the user has connected.
    # Otherwise this page returns a JSON 401 instead of the Connect button.
    if not google_configured():
        return HTMLResponse(
            """
            <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>YouTube Account</title>
            <style>body{margin:0;padding:40px 20px;background:#0b0b0f;color:#f5f5f5;font-family:Arial,sans-serif}main{max-width:720px;margin:auto}.card{background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:32px}p{color:#c8c8cf;line-height:1.7}.error{color:#ff9b9b;background:#2a1518;padding:14px 16px;border-radius:12px}a{color:#8ab4f8}</style>
            </head><body><main><div class="card"><h1>YouTube Account</h1>
            <p class="error">Google OAuth is not fully configured on the backend.</p>
            <p>Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI in Render.</p>
            </div></main></body></html>
            """, status_code=500
        )

    if not GOOGLE_TOKEN_FILE.exists():
        return HTMLResponse(
            """
            <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>YouTube Account</title>
            <style>
            *{box-sizing:border-box}body{margin:0;padding:28px 18px 60px;min-height:100vh;background:#0b0b0f;color:#f5f5f5;font-family:Arial,sans-serif}main{max-width:720px;margin:auto}.card{background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:34px;box-shadow:0 18px 50px rgba(0,0,0,.25)}h1{font-size:38px;line-height:1.15;margin:0 0 18px}p{color:#c8c8cf;line-height:1.7;font-size:17px}.status{margin:24px 0;padding:14px 16px;border-radius:14px;background:#202026;color:#d9d9df}.button{display:inline-block;padding:16px 22px;border-radius:14px;background:#fff;color:#111;text-decoration:none;font-weight:700;font-size:16px}.links{margin-top:28px;font-size:14px}.links a{color:#8ab4f8;text-decoration:none;margin-right:16px}
            </style></head><body><main><div class="card"><h1>YouTube Account</h1>
            <p>Connect your YouTube account to enable YouTube upload functionality.</p>
            <div class="status">YouTube is not connected.</div>
            <a class="button" href="/oauth/start">Connect YouTube Account</a>
            <div class="links"><a href="/">Home</a><a href="/privacy">Privacy Policy</a></div>
            </div></main></body></html>
            """, status_code=200
        )

    try:
        service = youtube_service()
        response = service.channels().list(
            part="snippet,contentDetails,statistics",
            mine=True,
        ).execute()
        items = response.get("items", [])

        if not items:
            return HTMLResponse(
                """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>YouTube Account</title></head>
                <body style="font-family:Arial,sans-serif;background:#0b0b0f;color:#f5f5f5;padding:40px 20px"><main style="max-width:720px;margin:auto;background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:32px"><h1>✓ YouTube connected</h1><p>Your Google authorization is valid, but no YouTube channel was returned.</p><p><a href="/oauth/start" style="color:#8ab4f8">Reconnect YouTube</a></p></main></body></html>""", status_code=200
            )

        channel = items[0]
        snippet = channel.get("snippet", {})
        statistics = channel.get("statistics", {})
        title = escape(snippet.get("title", "") or "YouTube Channel")
        thumbnail = escape(snippet.get("thumbnails", {}).get("default", {}).get("url", ""))
        subscribers = escape(str(statistics.get("subscriberCount", "—")))
        videos = escape(str(statistics.get("videoCount", "—")))
        views = escape(str(statistics.get("viewCount", "—")))
        success_message = '<div class="success">✓ YouTube account connected successfully.</div>' if connected == "1" else ""
        image_html = '<img src="' + thumbnail + '" alt="Channel" class="avatar">' if thumbnail else ""

        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>YouTube Account</title>
            <style>
            *{{box-sizing:border-box}}body{{margin:0;padding:28px 18px 60px;min-height:100vh;background:#0b0b0f;color:#f5f5f5;font-family:Arial,sans-serif}}main{{max-width:720px;margin:auto}}.card{{background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:34px}}h1{{font-size:36px;margin:0 0 24px}}.success{{background:#12351f;border:1px solid #245c36;color:#a9f0bd;border-radius:14px;padding:14px 16px;margin-bottom:22px}}.profile{{display:flex;align-items:center;gap:18px;margin-bottom:28px}}.avatar{{width:72px;height:72px;border-radius:50%;object-fit:cover}}.name{{font-size:22px;font-weight:700}}.connected{{color:#8ee6a7;font-size:14px;margin-top:5px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:26px}}.stat{{background:#202026;border-radius:14px;padding:16px}}.value{{display:block;font-size:20px;font-weight:700;margin-bottom:5px}}.label{{color:#9999a3;font-size:13px}}.button{{display:inline-block;padding:14px 20px;border-radius:14px;background:#fff;color:#111;text-decoration:none;font-weight:700}}.links{{margin-top:28px;font-size:14px}}.links a{{color:#8ab4f8;text-decoration:none;margin-right:16px}}@media(max-width:520px){{.card{{padding:26px 22px}}h1{{font-size:32px}}.stats{{grid-template-columns:1fr}}}}
            </style></head><body><main><div class="card"><h1>YouTube Account</h1>{success_message}
            <div class="profile">{image_html}<div><div class="name">{title}</div><div class="connected">● Connected</div></div></div>
            <div class="stats"><div class="stat"><span class="value">{subscribers}</span><span class="label">Subscribers</span></div><div class="stat"><span class="value">{videos}</span><span class="label">Videos</span></div><div class="stat"><span class="value">{views}</span><span class="label">Views</span></div></div>
            <a class="button" href="/oauth/start">Reconnect YouTube</a><div class="links"><a href="/">Home</a><a href="/privacy">Privacy Policy</a></div>
            </div></main></body></html>""", status_code=200
        )

    except HTTPException as exc:
        if exc.status_code == 401:
            return HTMLResponse(
                """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reconnect YouTube</title></head>
                <body style="font-family:Arial,sans-serif;background:#0b0b0f;color:#f5f5f5;padding:40px 20px"><main style="max-width:720px;margin:auto;background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:32px"><h1>YouTube needs to be reconnected</h1><p style="color:#c8c8cf;line-height:1.7">Your saved YouTube authorization is no longer valid. Connect the account again to continue.</p><a href="/oauth/start" style="display:inline-block;padding:15px 20px;border-radius:14px;background:#fff;color:#111;text-decoration:none;font-weight:700">Connect YouTube Account</a></main></body></html>""", status_code=401
            )
        raise
    except Exception as exc:
        logger.exception("Could not read YouTube account")
        return HTMLResponse(
            f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>YouTube Account Error</title></head>
            <body style="font-family:Arial,sans-serif;background:#0b0b0f;color:#f5f5f5;padding:40px 20px"><main style="max-width:720px;margin:auto;background:#17171c;border:1px solid #2b2b33;border-radius:24px;padding:32px"><h1>Could not read YouTube account</h1><p style="color:#c8c8cf;line-height:1.7">{escape(str(exc))}</p><p><a href="/oauth/start" style="color:#8ab4f8">Reconnect YouTube</a></p></main></body></html>""", status_code=502
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


@app.post("/youtube/upload")
def youtube_upload(req: UploadRequest):
    video_file = download_to_temp_file(req.video_id)
    tempdir = video_file.parent

    try:
        title = req.title.strip() or f"Shorts Archive - {req.video_id}"
        description = req.description.strip()

        if not description:
            description = (
                "Archived Short\n\n"
                "#Shorts #YouTubeShorts"
            )

        tags = list(dict.fromkeys(
            [tag.strip().lstrip("#") for tag in req.tags if tag.strip()]
            + ["Shorts", "YouTubeShorts"]
        ))

        response = upload_file_to_youtube(
            video_file,
            title,
            description,
            tags,
            req.privacy_status,
        )

        return {
            "ok": True,
            "uploaded": True,
            "video_id": response.get("id"),
            "youtube_url": (
                "https://www.youtube.com/watch?v=" + response["id"]
                if response.get("id") else None
            ),
            "source_video_id": req.video_id,
        }

    finally:
        # The archive/upload copy is temporary and is removed after upload.
        shutil.rmtree(tempdir, ignore_errors=True)


# Friendly alias for the complete one-short archive workflow.
@app.post("/archive/upload")
def archive_upload(req: ArchiveUploadRequest):
    return youtube_upload(
        UploadRequest(
            video_id=req.video_id,
            title=req.title,
            description=req.description,
            tags=req.tags,
            privacy_status=req.privacy_status,
        )
    )
