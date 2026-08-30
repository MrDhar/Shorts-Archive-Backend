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
    version="1.9.1",
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


# OAuth state storage.
# Suitable for a single Render instance.
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
<html lang="en">
<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>{title}</title>

    <style>
        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            padding: 40px 20px;
            background: #0b0b0f;
            color: #f5f5f5;
            font-family:
                Arial,
                Helvetica,
                sans-serif;
            line-height: 1.7;
        }}

        main {{
            width: 100%;
            max-width: 760px;
            margin: 0 auto;
        }}

        h1 {{
            font-size: 32px;
            margin-bottom: 24px;
        }}

        h2 {{
            font-size: 21px;
            margin-top: 32px;
        }}

        p {{
            color: #d0d0d5;
        }}

        a {{
            color: #8ab4f8;
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        .card {{
            background: #15151a;
            border: 1px solid #292930;
            border-radius: 14px;
            padding: 24px;
        }}

        .button {{
            display: inline-block;
            margin-top: 10px;
            padding: 12px 18px;
            border-radius: 9px;
            background: #ffffff;
            color: #111111;
            font-weight: 600;
            text-decoration: none;
        }}

        .button:hover {{
            text-decoration: none;
            opacity: 0.9;
        }}

        .links {{
            margin-top: 30px;
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
        content=html
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
<div class="card">

<h1>Privacy Policy</h1>

<p>
Shorts Auto Uploader is an application that helps
users manage and upload YouTube Shorts.
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
the permissions necessary to upload videos to your
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

<div class="links">
<a href="/">Home</a>
&nbsp; · &nbsp;
<a href="/terms">Terms of Service</a>
&nbsp; · &nbsp;
<a href="/youtube/account">YouTube Account</a>
</div>

</div>
""",
    )


# ============================================================
# TERMS
# ============================================================

@app.get(
    "/terms",
    response_class=HTMLResponse,
)
def terms():

    return simple_page(
        "Terms of Service - Shorts Auto Uploader",
        """
<div class="card">

<h1>Terms of Service</h1>

<p>
By using Shorts Auto Uploader, you agree to use
the application only for lawful purposes and in
accordance with YouTube's applicable terms and
policies.
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
this application.
</p>

<div class="links">
<a href="/">Home</a>
&nbsp; · &nbsp;
<a href="/privacy">Privacy Policy</a>
&nbsp; · &nbsp;
<a href="/youtube/account">YouTube Account</a>
</div>

</div>
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
# YOUTUBE ACCOUNT PAGE
# ============================================================

@app.get(
    "/youtube/account",
    response_class=HTMLResponse,
)
def youtube_account():

    status_text = (
        "OAuth configuration is ready."
        if oauth_configured()
        else
        "OAuth configuration is not complete."
    )

    if oauth_configured():

        connect_section = """
<a class="button" href="/oauth/start">
    Connect YouTube Account
</a>
"""

    else:

        connect_section = """
<p>
The server administrator needs to configure
Google OAuth in Render before YouTube
authorization can be started.
</p>
"""

    return simple_page(
        "YouTube Account - Shorts Auto Uploader",
        f"""
<div class="card">

<h1>YouTube Account</h1>

<p>
Connect your YouTube account to enable
YouTube upload functionality.
</p>

<p>
<strong>{status_text}</strong>
</p>

{connect_section}

<div class="links">

<a href="/">Home</a>
&nbsp; · &nbsp;

<a href="/privacy">
    Privacy Policy
</a>
&nbsp; · &nbsp;

<a href="/terms">
    Terms of Service
</a>

</div>

</div>
""",
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

@app.get(
    "/oauth/callback",
    response_class=HTMLResponse,
)
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
<div class="card">

<h1>Authorization cancelled</h1>

<p>
Google returned:
</p>

<p>
<strong>{error}</strong>
</p>

<p>
<a class="button" href="/youtube/account">
    Return to YouTube
</a>
</p>

</div>
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

        token_response = requests.post(
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

        if token_response.status_code != 200:

            logger.error(
                "Google token exchange failed: %s",
                token_response.text,
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Google authorization failed: "
                    + token_response.text[:2000]
                ),
            )

        token_data = token_response.json()

        access_token = token_data.get(
            "access_token"
        )

        if not access_token:

            raise HTTPException(
                status_code=502,
                detail=(
                    "Google did not return "
                    "an access token."
                ),
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
<div class="card">

<h1>✓ YouTube connected</h1>

<p>
Your Google account
<strong>{email}</strong>
has been successfully authorized.
</p>

<p>
Google successfully returned an OAuth access
token for the permissions you approved.
</p>

<p>
You can now close this page and return to
the Shorts Auto Uploader app.
</p>

<div class="links">

<a href="/youtube/account">
    Return to YouTube Account
</a>

&nbsp; · &nbsp;

<a href="/privacy">
    Privacy Policy
</a>

</div>

</div>
""",
        )

    except HTTPException:

        raise

    except Exception as exc:

        logger.exception(
            "OAuth callback failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "OAuth callback failed: "
                + str(exc)
            ),
        )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "ok": True,
        "service": "Shorts Archive Backend",
        "version": "1.9.1",
        "status": "running",
        "cookies_configured": (
            COOKIE_SOURCE.exists()
        ),
        "oauth_configured": oauth_configured(),
        "routes": [
            "/",
            "/health",
            "/privacy",
            "/terms",
            "/youtube/account",
            "/oauth/start",
            "/oauth/callback",
        ],
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "cookies_configured": (
            COOKIE_SOURCE.exists()
        ),
        "oauth_configured": oauth_configured(),
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
            detail=(
                "Enter a public YouTube "
                "channel URL"
            ),
        )

    shorts_url = shorts_channel_url(
        original_url
    )

    errors = []

    logger.info(
        "Starting Shorts discovery: %s",
        shorts_url,
    )

    for client in DISCOVERY_CLIENTS:

        try:

            logger.info(
                "Discovery client: %s",
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
                    "Discovery succeeded: "
                    "%d videos via %s",
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

                errors.append(
                    f"{client}: "
                    f"{stderr[-2500:]}"
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

    raise HTTPException(
        status_code=502,
        detail=(
            "YouTube Shorts discovery failed.\n\n"
            + "\n\n".join(
                errors
            )[-10000:]
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
            prefix=(
                f"short_{req.video_id}_"
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
            "Starting download for %s",
            req.video_id,
        )

        # ----------------------------------------------------
        # TRY EACH YOUTUBE CLIENT
        # ----------------------------------------------------

        for client in DOWNLOAD_CLIENTS:

            logger.info(
                "Checking formats with client: %s",
                client,
            )

            # ------------------------------------------------
            # STEP 1:
            # LIST FORMATS FOR THE EXACT SHORT
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

            # ------------------------------------------------
            # FORMAT DISCOVERY FAILED
            # ------------------------------------------------

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

            # ------------------------------------------------
            # CHECK FOR REAL VIDEO/AUDIO
            # ------------------------------------------------

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

            # ------------------------------------------------
            # STEP 2:
            # DOWNLOAD
            # ------------------------------------------------

            selectors = [
                "bv*+ba/b",
                "best",
            ]

            for selector in selectors:

                clean_tempdir(
                    tempdir
                )

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
                        "Download successful: %s "
                        "(%d bytes)",
                        req.video_id,
                        source_file.stat().st_size,
                    )

                    return save_download(
                        source_file
                    )

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

        # ----------------------------------------------------
        # ALL CLIENTS FAILED
        # ----------------------------------------------------

        diagnostic_text = "\n".join(
            diagnostics
        )[-30000:]

        logger.error(
            "Download failed for %s",
            req.video_id,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube download failed.\n\n"
                "Diagnostic information for "
                "the exact Short:\n\n"
                + diagnostic_text
            ),
        )

    finally:

        shutil.rmtree(
            tempdir,
            ignore_errors=True,
        )
