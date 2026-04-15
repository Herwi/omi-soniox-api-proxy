import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from typing import Any

from aggregator import TokenAggregator
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
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
    raw_value = os.getenv("SONIOX_KEEPALIVE_INTERVAL_SECONDS", "10")
    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid SONIOX_KEEPALIVE_INTERVAL_SECONDS=%r, defaulting to 10",
            raw_value,
        )
        return 10

    if parsed <= 0:
        logger.warning(
            "Non-positive SONIOX_KEEPALIVE_INTERVAL_SECONDS=%s, defaulting to 10",
            parsed,
        )
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
            logger.info("Connected to Soniox on attempt %s", attempt)
            return ws
        except Exception as exc:  # noqa: BLE001
            logger.warning("Soniox connect attempt %s failed: %s", attempt, exc)
            if attempt == MAX_CONNECT_RETRIES:
                raise
            await asyncio.sleep(delay)
            delay *= 2

    raise RuntimeError("Failed to connect to Soniox")


async def _send_segments_to_omi(omi_ws: WebSocket, segments: list[dict[str, Any]]) -> None:
    if not segments:
        return
    await omi_ws.send_json({"segments": segments})


async def _prepare_audio_for_soniox(payload: bytes) -> bytes:
    if AUDIO_PASSTHROUGH:
        return payload
    assert transcoder is not None
    return await transcoder.transcode_chunk(payload)


app = FastAPI(title="Omi ↔ Soniox Real-Time STT Proxy")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.websocket("/stream")
async def stream_proxy(omi_ws: WebSocket) -> None:
    await omi_ws.accept()
    logger.info("Omi client connected")

    aggregator = TokenAggregator()
    soniox_ws: ClientConnection | None = None
    stop_event = asyncio.Event()
    last_audio_ts = time.monotonic()

    async def forward_omi_to_soniox() -> None:
        nonlocal last_audio_ts
        assert soniox_ws is not None

        while not stop_event.is_set():
            try:
                message = await omi_ws.receive()
            except WebSocketDisconnect:
                logger.info("Omi disconnected")
                stop_event.set()
                break

            if message.get("type") == "websocket.disconnect":
                stop_event.set()
                break

            if "bytes" in message and message["bytes"] is not None:
                payload = message["bytes"]
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
                    logger.warning("Received non-JSON text from Omi: %s", raw)
                    continue

                if obj.get("type") == "CloseStream":
                    logger.info("Received CloseStream from Omi")
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

                payload = json.loads(raw)

                if payload.get("error_code") is not None:
                    code = payload.get("error_code")
                    message = payload.get("error_message", "Unknown Soniox error")
                    logger.error("Soniox error %s: %s", code, message)
                    await omi_ws.send_json({"segments": []})
                    stop_event.set()
                    break

                segments = aggregator.process_tokens(payload.get("tokens", []))
                await _send_segments_to_omi(omi_ws, segments)

                if payload.get("finished") is True:
                    logger.info("Soniox signaled finished")
                    trailing = aggregator.flush()
                    await _send_segments_to_omi(omi_ws, trailing)
                    stop_event.set()
                    break
        except ConnectionClosed:
            logger.info("Soniox websocket closed")
            stop_event.set()

    async def keepalive_loop() -> None:
        nonlocal last_audio_ts
        assert soniox_ws is not None
        while not stop_event.is_set():
            await asyncio.sleep(5)
            silence_for = time.monotonic() - last_audio_ts
            if silence_for >= KEEPALIVE_INTERVAL_SECONDS:
                await soniox_ws.send(json.dumps({"type": "keepalive"}))
                logger.debug("Sent keepalive to Soniox after %.2fs silence", silence_for)
                last_audio_ts = time.monotonic()

    try:
        soniox_ws = await connect_to_soniox()

        forward_task = asyncio.create_task(forward_omi_to_soniox())
        reverse_task = asyncio.create_task(forward_soniox_to_omi())
        keepalive_task = asyncio.create_task(keepalive_loop())

        await stop_event.wait()

        for task in (forward_task, reverse_task, keepalive_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        trailing = aggregator.flush()
        await _send_segments_to_omi(omi_ws, trailing)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Proxy session failed: %s", exc)
    finally:
        if soniox_ws is not None:
            with contextlib.suppress(Exception):
                await soniox_ws.close()
        with contextlib.suppress(Exception):
            await omi_ws.close()
        logger.info("Proxy session closed")
