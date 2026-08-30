# Shorts Archive Backend

FastAPI backend for Shorts Archive.

## Render deployment

Deploy this repository as a Docker Web Service. No second Render service is required.

The Docker image starts both:

- FastAPI on Render's `$PORT`
- bgutil PO-token provider privately on `127.0.0.1:4416`

The backend explicitly points yt-dlp at the local PO-token provider. This avoids needing a paid Render Private Service.

Health check:

`/health`

Expected response:

`{"ok":true}`

## Environment variables

- `PORT` — supplied automatically by Render
- `MAX_DISCOVER` — optional, default `5000`
- `DOWNLOAD_ROOT` — optional, default `/data/downloads`
- `YTDLP_BIN` — optional, default `yt-dlp`

The bgutil provider is pinned to `1.3.1` to match the Python plugin dependency.
