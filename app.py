import os
import re
import shutil
import subprocess
import tempfile
import base64
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


app = FastAPI(
    title="Shorts Archive Backend",
    version="1.2.0"
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


MAX_DISCOVER = int(
    os.getenv("MAX_DISCOVER", "5000")
)


YTDLP = os.getenv(
    "YTDLP_BIN",
    "yt-dlp"
)


POT_URL = os.getenv(
    "POT_PROVIDER_URL",
    "http://127.0.0.1:4416"
)


# ------------------------------------------------------------
# YouTube cookies
#
# On Render, set:
#
# YOUTUBE_COOKIES_BASE64
#
# to the base64 encoded contents of a Netscape-format
# cookies.txt file belonging to an account you control.
#
# The server writes it to /tmp at startup.
# ------------------------------------------------------------

COOKIE_FILE = Path(
    "/tmp/youtube_cookies.txt"
)


def setup_cookies():
    encoded = os.getenv(
        "YOUTUBE_COOKIES_BASE64",
        ""
    ).strip()

    if not encoded:
        return None

    try:
        decoded = base64.b64decode(
            encoded
        )

        COOKIE_FILE.write_bytes(
            decoded
        )

        return str(COOKIE_FILE)

    except Exception as e:

        print(
            "WARNING: Could not create YouTube "
            f"cookie file: {e}"
        )

        return None


COOKIE_PATH = setup_cookies()


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
# URL HELPERS
# ============================================================

def validate_youtube_url(url: str) -> str:

    url = url.strip()

    p = urlparse(url)

    if p.scheme not in {
        "http",
        "https"
    }:

        raise HTTPException(
            400,
            "Only public YouTube URLs are supported"
        )

    if p.netloc.lower() not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }:

        raise HTTPException(
            400,
            "Only public YouTube URLs are supported"
        )

    return url


def shorts_channel_url(url: str) -> str:

    p = urlparse(url)

    path = p.path.rstrip("/")

    if not path:

        raise HTTPException(
            400,
            "Enter a valid YouTube channel URL"
        )

    if path.lower().endswith("/shorts"):

        shorts_path = path

    else:

        shorts_path = path + "/shorts"

    return urlunparse(
        (
            p.scheme,
            p.netloc,
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
# YT-DLP
# ============================================================

def run_ytdlp(
    args: list[str],
    timeout: int = 180
):

    cmd = [
        YTDLP,

        "--no-warnings",
        "--no-progress",

        # PO-token provider
        "--extractor-args",
        (
            "youtubepot-bgutilhttp:"
            f"base_url={POT_URL};"
            "disable_innertube=1"
        ),
    ]


    # --------------------------------------------------------
    # Add cookies when configured.
    # --------------------------------------------------------

    if COOKIE_PATH:

        cmd.extend([
            "--cookies",
            COOKIE_PATH
        ])


    cmd.extend(args)


    print(
        "Running yt-dlp:",
        " ".join(
            (
                "[cookies]"
                if x == COOKIE_PATH
                else x
            )
            for x in cmd
        )
    )


    try:

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )


    except FileNotFoundError:

        raise HTTPException(
            500,
            "yt-dlp is not installed on the backend"
        )


    except subprocess.TimeoutExpired:

        raise HTTPException(
            504,
            "YouTube request timed out"
        )


# ============================================================
# DISCOVERY
# ============================================================

def discover_with_client(
    url: str,
    client: str
):

    r = run_ytdlp(
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


    for line in r.stdout.splitlines():

        parts = line.split(
            "\t",
            2
        )


        if not parts:

            continue


        vid = parts[0].strip()


        if not re.fullmatch(
            r"[A-Za-z0-9_-]{11}",
            vid
        ):

            continue


        if vid in seen:

            continue


        seen.add(vid)


        title = (
            parts[1].strip()
            if len(parts) > 1
            else ""
        )


        webpage_url = (
            parts[2].strip()
            if len(parts) > 2
            else ""
        )


        if not webpage_url:

            webpage_url = (
                "https://www.youtube.com/"
                f"shorts/{vid}"
            )


        found.append(
            {
                "id": vid,
                "title": title,
                "url": webpage_url,
            }
        )


        if len(found) >= MAX_DISCOVER:

            break


    return found, r.stderr


# ============================================================
# ROOT / HEALTH
# ============================================================

@app.get("/")
def root():

    return {
        "ok": True,
        "service": "Shorts Archive Backend",
        "status": "running",
        "cookies_configured": bool(
            COOKIE_PATH
        )
    }


@app.get("/health")
def health():

    return {
        "ok": True,
        "cookies_configured": bool(
            COOKIE_PATH
        )
    }


# ============================================================
# DISCOVER ENDPOINT
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
            400,
            "Enter a public YouTube channel URL"
        )


    shorts_url = shorts_channel_url(
        original_url
    )


    errors = []


    clients = (
        "android_vr",
        "tv",
        "web_embedded",
        "mweb",
    )


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
                    f"{client}: "
                    f"{stderr[-1200:]}"
                )


        except HTTPException:

            raise


        except Exception as e:

            errors.append(
                f"{client}: {e}"
            )


    raise HTTPException(

        502,

        "YouTube Shorts discovery failed. "
        + " | ".join(errors)[-5000:]
    )


# ============================================================
# DOWNLOAD ENDPOINT
# ============================================================

@app.post("/download")
def download(
    req: DownloadRequest
):

    video_url = (
        "https://www.youtube.com/"
        f"shorts/{req.video_id}"
    )


    tempdir = Path(
        tempfile.mkdtemp(
            prefix=(
                f"short_{req.video_id}_"
            ),
            dir="/tmp"
        )
    )


    out_template = str(
        tempdir / "%(id)s.%(ext)s"
    )


    try:

        attempts = [

            (
                "mweb",
                "bv*+ba/b"
            ),

            (
                "web_safari",
                "bv*+ba/b"
            ),

            (
                "android_vr",
                "bv*+ba/b"
            ),

            (
                "tv",
                "bv*+ba/b"
            ),

            (
                "web_embedded",
                "bv*+ba/b"
            ),
        ]


        last_error = ""


        for client, fmt in attempts:


            # ------------------------------------------------
            # Remove files from previous attempt
            # ------------------------------------------------

            for old_file in tempdir.iterdir():

                if old_file.is_file():

                    try:

                        old_file.unlink()

                    except Exception:

                        pass


            # ------------------------------------------------
            # Download
            # ------------------------------------------------

            r = run_ytdlp(
                [

                    "--no-playlist",

                    "--extractor-args",
                    (
                        "youtube:"
                        f"player_client={client}"
                    ),

                    "--retries",
                    "3",

                    "--fragment-retries",
                    "3",

                    "--file-access-retries",
                    "3",

                    "--retry-sleep",
                    "1",

                    "-f",
                    fmt,

                    "--merge-output-format",
                    "mp4",

                    "-o",
                    out_template,

                    video_url,

                ],

                timeout=900
            )


            files = [

                p

                for p in tempdir.iterdir()

                if p.is_file()

            ]


            # ------------------------------------------------
            # Success
            # ------------------------------------------------

            if (
                r.returncode == 0
                and files
            ):


                src = max(

                    files,

                    key=lambda p:
                    p.stat().st_size

                )


                safe_name = re.sub(

                    r"[^A-Za-z0-9._-]+",

                    "_",

                    src.name

                )


                final = (
                    DOWNLOAD_ROOT
                    / safe_name
                )


                shutil.copy2(

                    src,

                    final

                )


                return FileResponse(

                    final,

                    media_type="video/mp4",

                    filename=final.name

                )


            # ------------------------------------------------
            # Save error
            # ------------------------------------------------

            last_error = (

                r.stderr

                or r.stdout

                or "download failed"

            )[-4000:]


        # ----------------------------------------------------
        # All clients failed
        # ----------------------------------------------------

        raise HTTPException(

            502,

            "YouTube download failed after all "
            "download methods were attempted:\n"
            + last_error

        )


    finally:

        shutil.rmtree(

            tempdir,

            ignore_errors=True

                )
