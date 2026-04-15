import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from typing import Any

from aggregator import TokenAggregator
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("omi-soniox-proxy")

SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_API_KEY = os.getenv("SONIOX_API_KEY", "")
SONIOX_MODEL = os.getenv("SONIOX_MODEL", "stt-rt-v4")
SONIOX_LANGUAGE_HINTS = [
    hint.strip() for hint in os.getenv("SONIOX_LANGUAGE_HINTS", "en,pl").split(",") if hint.strip()
]
AUDIO_PASSTHROUGH = os.getenv("AUDIO_PASSTHROUGH", "true").lower() == "true"
OMI_AUDIO_INPUT_FORMAT = os.getenv("OMI_AUDIO_INPUT_FORMAT", "webm")
MAX_CONNECT_RETRIES = 3
MAX_SONIOX_KEEPALIVE_INTERVAL_SECONDS = 20
AUTH_BEARER_TOKEN = os.getenv("AUTH_BEARER_TOKEN", "").strip()


def _get_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, defaulting to %s",
            name,
            raw_value,
            default,
        )
        return default

    if minimum is not None and parsed < minimum:
        logger.warning(
            "%s=%s is below minimum %s, defaulting to %s",
            name,
            parsed,
            minimum,
            default,
        )
        return default
    return parsed


MAX_MESSAGE_BYTES = _get_int_env("MAX_MESSAGE_BYTES", 1048576, minimum=1)
MAX_IDLE_SECONDS = _get_int_env("MAX_IDLE_SECONDS", 120, minimum=1)
MAX_CONCURRENT_STREAMS = _get_int_env("MAX_CONCURRENT_STREAMS", 100, minimum=1)

_active_stream_semaphore = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)


class PrometheusMetrics:
    def __init__(self) -> None:
        self._soniox_connection_attempts = 0
        self._soniox_connection_failures = 0
        self._stream_sessions_started = 0
        self._stream_sessions_rejected = 0
        self._stream_sessions_unauthorized = 0
        self._transcript_segments_sent = 0
        self._soniox_message_latency_seconds_sum = 0.0
        self._soniox_message_latency_seconds_count = 0

    def inc_connection_attempt(self) -> None:
        self._soniox_connection_attempts += 1

    def inc_connection_failure(self) -> None:
        self._soniox_connection_failures += 1

    def inc_stream_started(self) -> None:
        self._stream_sessions_started += 1

    def inc_stream_rejected(self) -> None:
        self._stream_sessions_rejected += 1

    def inc_stream_unauthorized(self) -> None:
        self._stream_sessions_unauthorized += 1

    def inc_segments_sent(self, count: int) -> None:
        self._transcript_segments_sent += count

    def observe_soniox_message_latency(self, seconds: float) -> None:
        self._soniox_message_latency_seconds_sum += seconds
        self._soniox_message_latency_seconds_count += 1

    def render(self) -> str:
        return "\n".join(
            [
                "# HELP soniox_connection_attempts_total Total Soniox connection attempts.",
                "# TYPE soniox_connection_attempts_total counter",
                f"soniox_connection_attempts_total {self._soniox_connection_attempts}",
                "# HELP soniox_connection_failures_total Total Soniox connection failures.",
                "# TYPE soniox_connection_failures_total counter",
                f"soniox_connection_failures_total {self._soniox_connection_failures}",
                "# HELP stream_sessions_started_total Total stream sessions accepted.",
                "# TYPE stream_sessions_started_total counter",
                f"stream_sessions_started_total {self._stream_sessions_started}",
                "# HELP stream_sessions_rejected_total Total stream sessions rejected by safeguards.",
                "# TYPE stream_sessions_rejected_total counter",
                f"stream_sessions_rejected_total {self._stream_sessions_rejected}",
                "# HELP stream_sessions_unauthorized_total Total stream sessions rejected due to auth.",
                "# TYPE stream_sessions_unauthorized_total counter",
                f"stream_sessions_unauthorized_total {self._stream_sessions_unauthorized}",
                "# HELP transcript_segments_sent_total Total transcript segments emitted to Omi.",
                "# TYPE transcript_segments_sent_total counter",
                f"transcript_segments_sent_total {self._transcript_segments_sent}",
                "# HELP soniox_message_latency_seconds Soniox message processing latency summary.",
                "# TYPE soniox_message_latency_seconds summary",
                (
                    "soniox_message_latency_seconds_sum "
                    f"{self._soniox_message_latency_seconds_sum}"
                ),
                (
                    "soniox_message_latency_seconds_count "
                    f"{self._soniox_message_latency_seconds_count}"
                ),
                "",
            ]
        )


metrics = PrometheusMetrics()


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, sort_keys=True))


class AudioTranscoder:
    def __init__(self, input_format: str) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            raise RuntimeError(
                "AUDIO_PASSTHROUGH=false requires ffmpeg in PATH to decode Omi audio to PCM"
            )
        self._input_format = input_format
        self._ffmpeg_path = ffmpeg_path

    async def transcode_chunk(self, payload: bytes) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self._ffmpeg_path,
            "-loglevel",
            "error",
            "-f",
            self._input_format,
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate(input=payload)
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="ignore").strip()
            logger.warning(
                "Dropping audio frame: ffmpeg failed to transcode chunk (%s)",
                error_text or f"exit code {process.returncode}",
            )
            return b""
        return stdout


transcoder: AudioTranscoder | None = None
if not AUDIO_PASSTHROUGH:
    transcoder = AudioTranscoder(OMI_AUDIO_INPUT_FORMAT)


def _resolve_keepalive_interval_seconds() -> int:
    parsed = _get_int_env("SONIOX_KEEPALIVE_INTERVAL_SECONDS", 10, minimum=1)
    if parsed < 1:
        return 10
    if parsed > MAX_SONIOX_KEEPALIVE_INTERVAL_SECONDS:
        logger.warning(
            (
                "SONIOX_KEEPALIVE_INTERVAL_SECONDS=%s exceeds Soniox limit; "
                "clamping to %s"
            ),
            parsed,
            MAX_SONIOX_KEEPALIVE_INTERVAL_SECONDS,
        )
        return MAX_SONIOX_KEEPALIVE_INTERVAL_SECONDS
    return parsed


KEEPALIVE_INTERVAL_SECONDS = _resolve_keepalive_interval_seconds()


async def connect_to_soniox() -> ClientConnection:
    if not SONIOX_API_KEY:
        raise RuntimeError("SONIOX_API_KEY environment variable is required")

    delay = 1
    for attempt in range(1, MAX_CONNECT_RETRIES + 1):
        metrics.inc_connection_attempt()
        try:
            ws = await connect(SONIOX_URL, ping_interval=20, ping_timeout=20)
            config: dict[str, Any] = {
                "api_key": SONIOX_API_KEY,
                "model": SONIOX_MODEL,
                "audio_format": "auto" if AUDIO_PASSTHROUGH else "pcm_s16le",
                "language_hints": SONIOX_LANGUAGE_HINTS,
                "enable_language_identification": True,
                "enable_endpoint_detection": True,
                "enable_speaker_diarization": True,
            }
            if not AUDIO_PASSTHROUGH:
                config["sample_rate"] = 16000
                config["num_channels"] = 1

            await ws.send(json.dumps(config))
            _log_event(logging.INFO, "soniox_connect_success", attempt=attempt)
            return ws
        except Exception as exc:  # noqa: BLE001
            metrics.inc_connection_failure()
            _log_event(
                logging.WARNING,
                "soniox_connect_failure",
                attempt=attempt,
                error=str(exc),
            )
            if attempt == MAX_CONNECT_RETRIES:
                raise
            await asyncio.sleep(delay)
            delay *= 2

    raise RuntimeError("Failed to connect to Soniox")


async def _send_segments_to_omi(omi_ws: WebSocket, segments: list[dict[str, Any]]) -> None:
    if not segments:
        return
    await omi_ws.send_json({"segments": segments})
    metrics.inc_segments_sent(len(segments))


async def _prepare_audio_for_soniox(payload: bytes) -> bytes:
    if AUDIO_PASSTHROUGH:
        return payload
    assert transcoder is not None
    return await transcoder.transcode_chunk(payload)


def _is_authorized_stream_request(authorization_header: str | None) -> bool:
    if not AUTH_BEARER_TOKEN:
        return True
    if authorization_header is None:
        return False

    auth_scheme, _, auth_value = authorization_header.partition(" ")
    if auth_scheme.lower() != "bearer":
        return False

    provided_token = auth_value.strip()
    if not provided_token:
        return False

    return secrets.compare_digest(provided_token, AUTH_BEARER_TOKEN)


app = FastAPI(title="Omi ↔ Soniox Real-Time STT Proxy")


@app.get("/health")
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/metrics")
async def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.websocket("/stream")
async def stream_proxy(omi_ws: WebSocket) -> None:
    session_id = str(uuid.uuid4())

    authorization_header = omi_ws.headers.get("authorization")
    if not _is_authorized_stream_request(authorization_header):
        metrics.inc_stream_unauthorized()
        _log_event(logging.WARNING, "stream_unauthorized", session_id=session_id)
        await omi_ws.close(code=1008)
        return

    await omi_ws.accept()
    _log_event(logging.INFO, "omi_client_connected", session_id=session_id)

    aggregator = TokenAggregator()
    soniox_ws: ClientConnection | None = None
    stop_event = asyncio.Event()
    last_audio_ts = time.monotonic()
    last_omi_message_ts = time.monotonic()
    slot_acquired = False

    async def forward_omi_to_soniox() -> None:
        nonlocal last_audio_ts, last_omi_message_ts
        assert soniox_ws is not None

        while not stop_event.is_set():
            try:
                message = await omi_ws.receive()
            except WebSocketDisconnect:
                _log_event(logging.INFO, "omi_disconnected", session_id=session_id)
                stop_event.set()
                break

            if message.get("type") == "websocket.disconnect":
                stop_event.set()
                break

            last_omi_message_ts = time.monotonic()

            if "bytes" in message and message["bytes"] is not None:
                payload = message["bytes"]
                if len(payload) > MAX_MESSAGE_BYTES:
                    _log_event(
                        logging.WARNING,
                        "message_too_large",
                        session_id=session_id,
                        bytes=len(payload),
                        max_bytes=MAX_MESSAGE_BYTES,
                    )
                    stop_event.set()
                    break
                if len(payload) <= 2:
                    # Omi heartbeat ping, do not forward as audio.
                    continue

                prepared_payload = await _prepare_audio_for_soniox(payload)
                if not prepared_payload:
                    continue

                await soniox_ws.send(prepared_payload)
                last_audio_ts = time.monotonic()
            elif "text" in message and message["text"] is not None:
                raw = message["text"]
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    _log_event(
                        logging.WARNING,
                        "omi_non_json_text",
                        session_id=session_id,
                    )
                    continue

                if obj.get("type") == "CloseStream":
                    _log_event(logging.INFO, "omi_close_stream", session_id=session_id)
                    await soniox_ws.send(json.dumps({"type": "finalize"}))
                    await soniox_ws.send(b"")
                    stop_event.set()
                    break

    async def forward_soniox_to_omi() -> None:
        assert soniox_ws is not None

        try:
            async for raw in soniox_ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")

                started = time.monotonic()
                payload = json.loads(raw)
                metrics.observe_soniox_message_latency(time.monotonic() - started)

                if payload.get("error_code") is not None:
                    code = payload.get("error_code")
                    message = payload.get("error_message", "Unknown Soniox error")
                    _log_event(
                        logging.ERROR,
                        "soniox_error",
                        session_id=session_id,
                        code=code,
                        message=message,
                    )
                    await omi_ws.send_json({"segments": []})
                    stop_event.set()
                    break

                segments = aggregator.process_tokens(payload.get("tokens", []))
                await _send_segments_to_omi(omi_ws, segments)

                if payload.get("finished") is True:
                    _log_event(logging.INFO, "soniox_finished", session_id=session_id)
                    trailing = aggregator.flush()
                    await _send_segments_to_omi(omi_ws, trailing)
                    stop_event.set()
                    break
        except ConnectionClosed:
            _log_event(logging.INFO, "soniox_websocket_closed", session_id=session_id)
            stop_event.set()

    async def keepalive_loop() -> None:
        nonlocal last_audio_ts
        assert soniox_ws is not None
        while not stop_event.is_set():
            await asyncio.sleep(5)
            silence_for = time.monotonic() - last_audio_ts
            if silence_for >= KEEPALIVE_INTERVAL_SECONDS:
                await soniox_ws.send(json.dumps({"type": "keepalive"}))
                _log_event(
                    logging.DEBUG,
                    "keepalive_sent",
                    session_id=session_id,
                    silence_for_seconds=round(silence_for, 2),
                )
                last_audio_ts = time.monotonic()

    async def idle_timeout_loop() -> None:
        nonlocal last_omi_message_ts
        while not stop_event.is_set():
            await asyncio.sleep(1)
            idle_for = time.monotonic() - last_omi_message_ts
            if idle_for >= MAX_IDLE_SECONDS:
                _log_event(
                    logging.WARNING,
                    "session_idle_timeout",
                    session_id=session_id,
                    idle_for_seconds=round(idle_for, 2),
                    max_idle_seconds=MAX_IDLE_SECONDS,
                )
                stop_event.set()
                break

    try:
        if _active_stream_semaphore.locked():
            metrics.inc_stream_rejected()
            _log_event(logging.WARNING, "stream_rejected_capacity", session_id=session_id)
            return
        await _active_stream_semaphore.acquire()
        slot_acquired = True
        metrics.inc_stream_started()
        soniox_ws = await connect_to_soniox()

        forward_task = asyncio.create_task(forward_omi_to_soniox())
        reverse_task = asyncio.create_task(forward_soniox_to_omi())
        keepalive_task = asyncio.create_task(keepalive_loop())
        idle_timeout_task = asyncio.create_task(idle_timeout_loop())

        await stop_event.wait()

        for task in (forward_task, reverse_task, keepalive_task, idle_timeout_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        trailing = aggregator.flush()
        await _send_segments_to_omi(omi_ws, trailing)
    except Exception as exc:  # noqa: BLE001
        _log_event(
            logging.ERROR,
            "proxy_session_failed",
            session_id=session_id,
            error=str(exc),
        )
    finally:
        if slot_acquired:
            _active_stream_semaphore.release()
        if soniox_ws is not None:
            with contextlib.suppress(Exception):
                await soniox_ws.close()
        with contextlib.suppress(Exception):
            await omi_ws.close()
        _log_event(logging.INFO, "proxy_session_closed", session_id=session_id)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = _get_int_env("PORT", 8080, minimum=1)
    uvicorn.run("server:app", host=host, port=port, log_level=LOG_LEVEL.lower())
