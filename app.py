import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import secrets
import time

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
# GOOGLE OAUTH CONFIG
# ============================================================

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID",
    "",
)

GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_CLIENT_SECRET",
    "",
)

# IMPORTANT:
# This is the exact callback URL you provided.
#
# You can also set GOOGLE_REDIRECT_URI in Render Environment
# Variables to the same value.
#
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://shorts-archive-backend.onrender.com/oauth/callback",
)

GOOGLE_OAUTH_SCOPE = " ".join(
    [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]
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


# ============================================================
# OAUTH TOKEN STORAGE
# ============================================================
#
# Tokens are stored on the Render persistent disk if /data
# is persistent.
#
# IMPORTANT:
# Do NOT put the client secret directly in this file.
# Keep GOOGLE_CLIENT_SECRET in Render Environment Variables.
#

OAUTH_STORAGE_FILE = Path(
    os.getenv(
        "OAUTH_STORAGE_FILE",
        "/data/oauth_tokens.json",
    )
)

OAUTH_STORAGE_FILE.parent.mkdir(
    parents=True,
    exist_ok=True,
)


# OAuth state storage for the current process.
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

    safe_title = html.escape(title)

    page = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width,initial-scale=1"
    >

    <title>{safe_title}</title>

    <style>

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            padding: 32px 16px;
            background: #0b0b0f;
            color: #f5f5f5;
            font-family:
                Arial,
                Helvetica,
                sans-serif;
            line-height: 1.7;
        }}

        main {{
            max-width: 760px;
            margin: 0 auto;
            background: #15151a;
            border: 1px solid #292930;
            border-radius: 24px;
            padding: 40px;
        }}

        h1 {{
            font-size: 34px;
            line-height: 1.2;
            margin-top: 0;
            margin-bottom: 28px;
        }}

        h2 {{
            margin-top: 32px;
            font-size: 21px;
        }}

        p {{
            color: #d0d0d6;
            font-size: 17px;
        }}

        a {{
            color: #8ab4f8;
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        .button {{
            display: inline-block;
            margin-top: 16px;
            padding: 15px 22px;
            background: #ffffff;
            color: #111111;
            border-radius: 12px;
            font-weight: 700;
            text-decoration: none;
        }}

        .button:hover {{
            text-decoration: none;
            opacity: 0.9;
        }}

        .success {{
            color: #8ee6a8;
        }}

        .muted {{
            color: #9999a3;
        }}

        .links {{
            margin-top: 32px;
        }}

        @media (max-width: 600px) {{

            body {{
                padding: 16px 10px;
            }}

            main {{
                padding: 28px 22px;
                border-radius: 20px;
            }}

            h1 {{
                font-size: 30px;
            }}

            p {{
                font-size: 16px;
            }}
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

    return HTMLResponse(
        content=page
    )


# ============================================================
# OAUTH HELPERS
# ============================================================

def oauth_configured() -> bool:

    return bool(
        GOOGLE_CLIENT_ID
        and GOOGLE_CLIENT_SECRET
        and GOOGLE_REDIRECT_URI
    )


def load_oauth_data() -> dict:

    if not OAUTH_STORAGE_FILE.exists():
        return {}

    try:

        with OAUTH_STORAGE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as exc:

        logger.error(
            "Could not read OAuth storage: %s",
            exc,
        )

        return {}


def save_oauth_data(
    data: dict,
):

    temporary_file = OAUTH_STORAGE_FILE.with_suffix(
        ".tmp"
    )

    try:

        with temporary_file.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                indent=2,
            )

        os.replace(
            temporary_file,
            OAUTH_STORAGE_FILE,
        )

        try:
            os.chmod(
                OAUTH_STORAGE_FILE,
                0o600,
            )
        except OSError:
            pass

    except Exception:

        try:

            if temporary_file.exists():
                temporary_file.unlink()

        except OSError:
            pass

        raise


def save_google_tokens(
    token_data: dict,
    user_data: dict,
):

    existing = load_oauth_data()

    access_token = token_data.get(
        "access_token"
    )

    refresh_token = token_data.get(
        "refresh_token"
    )

    expires_in = token_data.get(
        "expires_in",
        3600,
    )

    if not access_token:

        raise HTTPException(
            status_code=502,
            detail=(
                "Google did not return an "
                "access token."
            ),
        )

    # Google may not send a refresh token on a
    # subsequent authorization. Preserve the
    # existing refresh token if one already exists.
    if not refresh_token:

        refresh_token = existing.get(
            "refresh_token"
        )

    oauth_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_data.get(
            "token_type",
            "Bearer",
        ),
        "scope": token_data.get(
            "scope",
            GOOGLE_OAUTH_SCOPE,
        ),
        "expires_at": int(
            time.time()
            + int(expires_in)
        ),
        "email": user_data.get(
            "email",
            "",
        ),
        "name": user_data.get(
            "name",
            "",
        ),
        "picture": user_data.get(
            "picture",
            "",
        ),
        "google_sub": user_data.get(
            "sub",
            "",
        ),
        "updated_at": int(
            time.time()
        ),
    }

    save_oauth_data(
        oauth_data
    )

    return oauth_data


def oauth_account() -> dict:

    data = load_oauth_data()

    if not data:
        return {}

    return data


def oauth_connected() -> bool:

    data = oauth_account()

    return bool(
        data.get("access_token")
        or data.get("refresh_token")
    )


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.get(
    "/privacy",
    response_class=HTMLResponse,
)
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
            may receive basic Google account information and
            permissions necessary to access your YouTube account
            and upload videos to your YouTube channel.
        </p>

        <h2>Data Storage</h2>

        <p>
            OAuth credentials are stored by the application only
            for providing the YouTube functionality requested by
            the user. We do not sell personal information.
        </p>

        <h2>Third Parties</h2>

        <p>
            YouTube and Google APIs are operated by Google and
            are subject to Google's own privacy policies and
            terms.
        </p>

        <h2>Disconnecting Your Account</h2>

        <p>
            You can revoke the application's access through your
            Google Account security and connected-app settings.
        </p>

        <h2>Contact</h2>

        <p>
            For privacy questions, contact the developer through
            the email address associated with this application.
        </p>

        <div class="links">
            <a href="/youtube/account">
                You
