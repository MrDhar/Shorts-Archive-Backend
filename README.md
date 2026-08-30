# Shorts Archive Backend

Backend for the Shorts Archive Android app.

## What it does

- Accepts a public YouTube channel URL at `POST /discover`.
- Enumerates Shorts without downloading them during discovery.
- Downloads an individual Short at `POST /download` and streams the resulting MP4.
- Includes a BgUtils PO-token sidecar for current YouTube extraction requirements.

The yt-dlp project currently recommends a PO-token provider for clients that need it, and notes that YouTube can return HTTP 403 without a valid token. The included `bgutil` provider is the recommended provider listed in the yt-dlp PO Token Guide.

## Run

```bash
docker compose up -d --build
```

Then check:

```text
http://YOUR_SERVER:8000/health
```

Expected:

```json
{"ok":true}
```

## API

`POST /discover`

```json
{"channel_url":"https://www.youtube.com/@example/shorts"}
```

Returns a list of discovered video IDs.

`POST /download`

```json
{"video_id":"dQw4w9WgXcQ"}
```

Returns the MP4 stream.

## Security

This service is intentionally limited to YouTube HTTP(S) URLs. Before putting it on the public internet, add authentication/rate limiting and a maximum request/download quota. Do not expose the BgUtils port publicly.
