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
- Prometheus-compatible metrics endpoint at `GET /metrics`.
- Soniox session bootstrap with multilingual hints (`en`, `pl`).
- Token aggregation into Omi `{"segments": [...]}` schema.
- Segment boundaries based on `<end>` token and speaker changes.
- Omi `CloseStream` handling (`finalize` + empty frame to Soniox).
- Keepalive to Soniox after configurable silence interval (`SONIOX_KEEPALIVE_INTERVAL_SECONDS`, default `10`, max `20`).
- Soniox connection retries (1s / 2s / 4s, max 3 attempts).
- Structured JSON logs that include per-session IDs.
- Operational safeguards for global concurrency, payload size, and idle session timeouts.

## Prerequisites

- Python **3.12+**
- A **Soniox API key**
- (Optional) Docker / Docker Compose
- `ffmpeg` installed if you plan to set `AUDIO_PASSTHROUGH=false`

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SONIOX_API_KEY` | ✅ | — | Soniox API key used in the start/config message. |
| `AUTH_BEARER_TOKEN` | ❌ | empty (disabled) | If set, `/stream` requires `Authorization: Bearer <token>` and rejects other clients. |
| `PORT` | ❌ | `8080` | App listen port (used by your process manager). |
| `LOG_LEVEL` | ❌ | `info` | Python logging verbosity. |
| `SONIOX_MODEL` | ❌ | `stt-rt-v4` | Soniox model; fallback can be `stt-rt-v3`. |
| `SONIOX_LANGUAGE_HINTS` | ❌ | `en,pl` | Comma-separated language hints. |
| `AUDIO_PASSTHROUGH` | ❌ | `true` | `true` sends raw input with Soniox `audio_format=auto`; `false` uses PCM config (`pcm_s16le`, 16kHz mono). |
| `OMI_AUDIO_INPUT_FORMAT` | ❌ | `webm` | Input format hint for ffmpeg when `AUDIO_PASSTHROUGH=false` (examples: `webm`, `ogg`, `opus`). |
| `SONIOX_KEEPALIVE_INTERVAL_SECONDS` | ❌ | `10` | Interval for keepalive frames during silence. Values above `20` are clamped to satisfy Soniox real-time API limits. |
| `MAX_MESSAGE_BYTES` | ❌ | `1048576` | Per-message size guard for incoming Omi binary frames. Oversized frames terminate the session. |
| `MAX_IDLE_SECONDS` | ❌ | `120` | Per-connection idle timeout for inbound Omi messages. Idle sessions are terminated. |
| `MAX_CONCURRENT_STREAMS` | ❌ | `100` | Global cap on simultaneously active `/stream` sessions. Sessions over the cap are rejected. |

## Audio compatibility matrix

| Omi audio input | `AUDIO_PASSTHROUGH` | Proxy behavior | When to use |
|---|---|---|---|
| Compressed/containerized input (common Omi default, e.g. `webm`/`ogg`/`opus`) | `true` (default) | Forwards bytes as-is; Soniox auto-detects format. | Use first for lowest proxy CPU and simplest setup. |
| Compressed/containerized input that Soniox does not decode reliably | `false` | Decodes with ffmpeg and forwards 16kHz mono PCM (`pcm_s16le`). | Use when you see decode/format errors or unstable transcript quality. |
| Already-linear PCM stream from upstream | `true` | Forwards bytes as-is. | Use if your upstream already emits Soniox-compatible PCM. |
| Already-linear PCM stream from upstream | `false` | Re-encodes through ffmpeg to 16kHz mono PCM. | Usually unnecessary; only use to normalize inconsistent upstream audio. |

## Local setup

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

Quick checks:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics
```

## Docker setup

Build and run directly:

```bash
docker build -t omi-soniox-proxy .
docker run --rm -p 8080:8080 --env-file .env omi-soniox-proxy
```

Run with Docker Compose:

```bash
cp .env.example .env
# edit .env and set SONIOX_API_KEY
docker compose up --build -d
```

`docker-compose.yml` passes `SONIOX_API_KEY` and the rest of runtime variables into the container via its `environment` section.

## Securing access to your self-hosted proxy

For private self-hosting, set `AUTH_BEARER_TOKEN` in your environment (or `.env` for Compose).  
When enabled, every WebSocket request to `/stream` must include:

```text
Authorization: Bearer <your-token>
```

Connections without a matching bearer token are rejected by the proxy.

## Local and Docker operations runbook

### Healthcheck behavior

- `GET /health` returns `200` with `{"ok": true}` when the app process is running.
- This endpoint checks process liveness, not external Soniox reachability.
- Use `/metrics` for runtime counters and debugging context.

### Restart/backoff strategy

- The proxy already retries Soniox session connect attempts with `1s`, `2s`, then `4s` backoff.
- For process-level resilience:
  - local supervisor: `restart=on-failure`
  - Docker/Compose: `restart: unless-stopped` (or `always` in dedicated appliance setups)
- Keep `MAX_IDLE_SECONDS` and `MAX_CONCURRENT_STREAMS` set to sane values for your host capacity.

### Useful log patterns

Look for these JSON log categories while diagnosing:

- session lifecycle (accepted/rejected/closed with session IDs)
- Soniox connection attempts/failures
- stream termination reasons (oversized frame, idle timeout, upstream close)

### Connecting Omi to the proxy

Point your Omi device at:

```text
wss://<your-domain>/stream
```

If you enabled `AUTH_BEARER_TOKEN`, also configure Omi's custom header fields:

- Header name: `Authorization`
- Header value: `Bearer <the same token from AUTH_BEARER_TOKEN>`

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
