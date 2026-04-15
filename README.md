# Omi ↔ Soniox Real-Time STT Proxy

A production-ready Python WebSocket proxy that bridges:

- **Omi wearable/app** (client-side audio stream)
- **Soniox real-time STT** (`stt-rt-v4`)

The proxy accepts Omi audio on `/stream`, forwards frames to Soniox, aggregates Soniox token responses into Omi-compatible transcript segments, and sends JSON results back to Omi.

## Architecture

```text
┌──────────┐         ┌──────────────────┐         ┌──────────────┐
│ Omi App  │◄──WS───►│  Proxy (FastAPI) │◄──WS───►│ Soniox API   │
│ Client   │  audio   │ /stream          │  audio   │ stt-rt-v4    │
│          │  ──────► │                  │  ──────► │              │
│          │  JSON    │  tokens→segments │  tokens  │              │
│          │  ◄────── │                  │  ◄────── │              │
└──────────┘         └──────────────────┘         └──────────────┘
```

## Features

- FastAPI WebSocket endpoint at `GET /stream` (WS upgrade).
- Health endpoint at `GET /health`.
- Soniox session bootstrap with multilingual hints (`en`, `pl`).
- Token aggregation into Omi `{"segments": [...]}` schema.
- Segment boundaries based on `<end>` token and speaker changes.
- Omi `CloseStream` handling (`finalize` + empty frame to Soniox).
- Keepalive to Soniox after configurable silence interval (`SONIOX_KEEPALIVE_INTERVAL_SECONDS`, default `10`, max `20`).
- Soniox connection retries (1s / 2s / 4s, max 3 attempts).

## Prerequisites

- Python **3.12+**
- A **Soniox API key**
- (Optional) Docker / Docker Compose

## Local Development

1. Create and activate a virtual environment.
2. Install dependencies.
3. Configure environment variables.
4. Run the server.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set SONIOX_API_KEY
uvicorn server:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://localhost:8080/health
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SONIOX_API_KEY` | ✅ | — | Soniox API key used in the start/config message. |
| `PORT` | ❌ | `8080` | App listen port (used by your process manager). |
| `LOG_LEVEL` | ❌ | `info` | Python logging verbosity. |
| `SONIOX_MODEL` | ❌ | `stt-rt-v4` | Soniox model; fallback can be `stt-rt-v3`. |
| `SONIOX_LANGUAGE_HINTS` | ❌ | `en,pl` | Comma-separated language hints. |
| `AUDIO_PASSTHROUGH` | ❌ | `true` | `true` sends raw input with Soniox `audio_format=auto`; `false` uses PCM config (`pcm_s16le`, 16kHz mono). |
| `OMI_AUDIO_INPUT_FORMAT` | ❌ | `webm` | Input format hint for ffmpeg when `AUDIO_PASSTHROUGH=false` (examples: `webm`, `ogg`, `opus`). |
| `SONIOX_KEEPALIVE_INTERVAL_SECONDS` | ❌ | `10` | Interval for keepalive frames during silence. Values above `20` are clamped to satisfy Soniox real-time API limits. |

> Note: `AUDIO_PASSTHROUGH=false` requires `ffmpeg` in `PATH`. Incoming Omi audio is transcoded to 16kHz mono PCM (`pcm_s16le`) before forwarding to Soniox.

## Implementation status and remaining TODO

See [`TODO_REVIEW.md`](TODO_REVIEW.md) for a prioritized review of planned work that is still pending.

## Running with Docker

Build and run:

```bash
docker build -t omi-soniox-proxy .
docker run --rm -p 8080:8080 --env-file .env omi-soniox-proxy
```

With Compose:

```bash
cp .env.example .env
# edit .env and set SONIOX_API_KEY
docker compose up --build -d
```

`docker-compose.yml` passes `SONIOX_API_KEY` (and other runtime variables) into the container through the `environment` section, so your VPS only needs a populated `.env` file (or exported shell variables) before `docker compose up`.

## Deploying

### Render

1. Create a new **Web Service** from this repository.
2. Runtime: Docker.
3. Set environment variables (`SONIOX_API_KEY`, optional overrides).
4. Expose port `8080`.

### Fly.io

1. `fly launch` in this repo.
2. Set secrets: `fly secrets set SONIOX_API_KEY=...`.
3. Ensure internal port is `8080`.
4. `fly deploy`.

## Configure Omi

In Omi custom STT backend settings, point your WebSocket URL to:

```text
wss://<your-domain>/stream
```

Expected behavior:

- Omi sends binary audio frames.
- Proxy forwards to Soniox.
- Proxy returns Omi-formatted JSON objects with a `segments` key.

Example outbound payload to Omi:

```json
{
  "segments": [
    {
      "text": "Hello, how are you?",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 1.5
    }
  ]
}
```

## Testing

Unit and protocol-level async bridge tests:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```
