import logging
import os
import re
import shutil
import subprocess
import tempfile
import secrets
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field


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
    version="1.9.0",
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
# GOOGLE OAUTH CONFIG
# ============================================================

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID",
    ""
)

GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_CLIENT_SECRET",
    ""
)

GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    ""
)

GOOGLE_OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)

GOOGLE_TOKEN_URL = (
    "https://oauth2.googleapis.com/token"
)

GOOGLE_USERINFO_URL = (
    "https://www.googleapis.com/oauth2/v3/userinfo"
)


# Temporary OAuth state storage.
# For a single-instance Render deployment this is sufficient.
oauth_states = set()


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
# BASIC WEB PAGES
# ============================================================

def simple_page(
    title: str,
    content: str,
) -> HTMLResponse:

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport"
              content="width=device-width,initial-scale=1">
        <title>{title}</title>
        <style>
            body {{
                margin: 0;
                padding: 40px 20px;
                background: #0b0b0f;
                color: #f5f5f5;
                font-family: Arial, sans-serif;
                line-height: 1.7;
            }}

            main {{
                max-width: 760px;
                margin: auto;
            }}

            h1 {{
                font-size: 32px;
            }}

            h2 {{
                margin-top: 32px;
            }}

            a {{
                color: #8ab4f8;
            }}
        </style>
    </head>

    <body>
        <main>
            {content}
        </main>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get("/privacy", response_class=HTMLResponse)
def privacy():

    return simple_page(
        "Privacy Policy - Shorts Auto Uploader",
        """
        <h1>Privacy Policy</h1>

        <p>
            Shorts Auto Uploader is an application that helps users
            manage and upload YouTube Shorts.
        </p>

        <h2>YouTube Account Access</h2>

        <p>
            If you choose to connect your YouTube account,
            the application uses Google's OAuth authorization
            system to request the permissions required for
            YouTube functionality.
        </p>

        <h2>Information We Access</h2>

        <p>
            Depending on the features you use, the application
            may receive basic YouTube account information and
            permissions necessary to upload videos to your
            YouTube channel.
        </p>

        <h2>Data Storage</h2>

        <p>
            We do not sell your personal information.
            OAuth credentials are used only to provide the
            requested YouTube functionality.
        </p>

        <h2>Third Parties</h2>

        <p>
            YouTube and Google APIs are operated by Google and
            are subject to Google's own privacy policies and
            terms.
        </p>

        <h2>Contact</h2>

        <p>
            For privacy questions, contact the developer through
            the email address associated with this application.
        </p>
        """,
    )


# ============================================================
# TERMS
# ============================================================

@app.get("/terms", response_class=HTMLResponse)
def terms():

    return simple_page(
        "Terms of Service - Shorts Auto Uploader",
        """
        <h1>Terms of Service</h1>

        <p>
            By using Shorts Auto Uploader, you agree to use the
            application only for lawful purposes and in accordance
            with YouTube's applicable terms and policies.
        </p>

        <h2>YouTube Content</h2>

        <p>
            You are responsible for having the necessary rights
            and permissions for any content that you upload or
            process through the application.
        </p>

        <h2>Account Authorization</h2>

        <p>
            You may disconnect your Google or YouTube account
            at any time through your Google account permissions.
        </p>

        <h2>Availability</h2>

        <p>
            We do not guarantee that the application or YouTube
            services will always be available.
        </p>

        <h2>Contact</h2>

        <p>
            For questions regarding these terms, contact the
            developer through the email address associated with
            the application.
        </p>
        """,
    )


# ============================================================
# OAUTH CONFIG CHECK
# ============================================================

def oauth_configured() -> bool:

    return bool(
        GOOGLE_CLIENT_ID
        and GOOGLE_CLIENT_SECRET
        and GOOGLE_REDIRECT_URI
    )


# ============================================================
# START YOUTUBE OAUTH
# ============================================================

@app.get("/oauth/start")
def oauth_start():

    if not oauth_configured():

        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth is not configured. "
                "Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and "
                "GOOGLE_REDIRECT_URI in Render."
            ),
        )

    state = secrets.token_urlsafe(32)

    oauth_states.add(state)

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    authorization_url = (
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(params)
    )

    return RedirectResponse(
        authorization_url,
        status_code=302,
    )


# ============================================================
# OAUTH CALLBACK
# ============================================================

@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
):

    if not oauth_configured():

        raise HTTPException(
            status_code=500,
            detail="Google OAuth is not configured.",
        )

    code = request.query_params.get(
        "code"
    )

    state = request.query_params.get(
        "state"
    )

    error = request.query_params.get(
        "error"
    )

    if error:

        return simple_page(
            "YouTube Authorization",
            f"""
            <h1>Authorization cancelled</h1>
            <p>Google returned:</p>
            <p>{error}</p>
            <p>
                <a href="/youtube/account">
                    Return to YouTube
                </a>
            </p>
            """,
        )

    if not code:

        raise HTTPException(
            status_code=400,
            detail="Missing OAuth authorization code.",
        )

    if not state or state not in oauth_states:

        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state.",
        )

    oauth_states.discard(state)

    try:

        import requests

        response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )

        if response.status_code != 200:

            logger.error(
                "Google token exchange failed: %s",
                response.text,
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Google authorization failed: "
                    + response.text[:2000]
                ),
            )

        token_data = response.json()

        access_token = token_data.get(
            "access_token"
        )

        if not access_token:

            raise HTTPException(
                status_code=502,
                detail="Google did not return an access token.",
            )

        user_response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={
                "Authorization":
                    f"Bearer {access_token}"
            },
            timeout=30,
        )

        user_data = {}

        if user_response.status_code == 200:

            user_data = user_response.json()

        email = user_data.get(
            "email",
            "your Google account",
        )

        return simple_page(
            "YouTube Authorization Successful",
            f"""
            <h1>✓ YouTube connected</h1>

            <p>
                Your Google account
                <strong>{email}</strong>
                has been successfully authorized.
            </p>

            <p>
                Shorts Auto Uploader can now request the
                YouTube permissions you approved.
            </p>

            <p>
                You can clo
