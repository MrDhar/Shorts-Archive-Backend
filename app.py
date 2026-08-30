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


app = FastAPI(
    title="Shorts Archive Backend",
    version="1.0.0"
)


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


class DiscoverRequest(BaseModel):
    channel_url: str = Field(
        min_length=8,
        max_length=2048
    )


class DownloadRequest(BaseModel):
    video_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{11}$"
    )


def validate_youtube_url(url: str) -> str:

    url = url.strip()

    p = urlparse(url)

    if p.scheme not in {"http", "https"}:
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
    """
    Convert a normal YouTube channel URL into
    the channel's /shorts tab.

    Examples:

    /@channel
        -> /@channel/shorts

    /@channel?si=xxxx
        -> /@channel/shorts

    /channel/UCxxxx
        -> /channel/UCxxxx/shorts

    /c/channel
        -> /c/channel/shorts

    /user/channel
        -> /user/channel/shorts

    /@channel/shorts
        -> unchanged
    """

    p = urlparse(url)

    path = p.path.rstrip("/")

    if not path:
        raise HTTPException(
            400,
            "Enter a valid YouTube channel URL"
        )

    lower_path = path.lower()

    if lower_path.endswith("/shorts"):
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


def run_ytdlp(
    args: list[str],
    timeout: int = 180
):

    cmd = [
        YTDLP,
        "--no-warnings",
        "--no-progress",

        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    ] + args

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

        # Since discovery is performed against the
        # channel's /shorts tab, every returned entry
        # is intended to be a Short.
        #
        # We still return the canonical Shorts URL
        # rather than trusting a possibly missing URL.
        if not webpage_url:
            webpage_url = (
                f"https://www.youtube.com/shorts/{vid}"
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


@app.get("/health")
def health():

    return {
        "ok": True
    }


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

    # IMPORTANT:
    # Always use the channel's Shorts tab.
    #
    # This prevents a normal channel URL such as
    # https://youtube.com/@mrbeast
    # from returning the channel's entire
    # video library.
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

            entries, stderr = discover_with_client(
                shorts_url,
                client
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
                    f"{client}: {stderr[-1200:]}"
                )

        except HTTPException:

            raise

        except Exception as e:

            errors.append(
                f"{client}: {e}"
            )

    error_text = " | ".join(
        errors
    )[-5000:]

    raise HTTPException(
        502,
        "YouTube Shorts discovery failed. "
        + error_text
    )


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
            prefix=f"short_{req.video_id}_",
            dir="/tmp"
        )
    )

    out_template = str(
        tempdir / "%(id)s.%(ext)s"
    )

    try:

        # Download attempts for each Short.
        #
        # The Android app can call this endpoint
        # once for every item in the discovered
        # Shorts list, which preserves the existing
        # Download All workflow.
        attempts = [

            (
                "android_vr",
                "18/b"
            ),

            (
                "tv",
                "bv*+ba/b"
            ),

            (
                "web_embedded",
                "bv*+ba/b"
            ),

            (
                "web_safari",
                "bv*+ba/b"
            ),

            (
                "mweb",
                "bv*+ba/b"
            ),
        ]

        last_error = ""

        for client, fmt in attempts:

            r = run_ytdlp(
                [

                    "--no-playlist",

                    "--extractor-args",
                    f"youtube:player_client={client}",

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

            if (
                r.returncode == 0
                and files
            ):

                src = max(
                    files,
                    key=lambda p: p.stat().st_size
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

            last_error = (
                r.stderr
                or r.stdout
                or "download failed"
            )[-3000:]

        raise HTTPException(
            502,
            "YouTube download failed: "
            + last_error
        )

    finally:

        shutil.rmtree(
            tempdir,
            ignore_errors=True
        )
