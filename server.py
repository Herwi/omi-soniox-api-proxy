import asyncio
import contextlib
import json
import logging
import os
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
KEEPALIVE_INTERVAL_SECONDS = 30
MAX_CONNECT_RETRIES = 3


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
                await soniox_ws.send(payload)
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
