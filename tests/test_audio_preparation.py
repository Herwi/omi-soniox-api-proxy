import importlib
import sys
import types
import unittest


def _install_server_import_stubs() -> None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None  # type: ignore[attr-defined]
    sys.modules.setdefault("dotenv", dotenv)

    fastapi = types.ModuleType("fastapi")

    class _FastAPI:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

        def websocket(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    class _WebSocket:
        pass

    class _WebSocketDisconnect(Exception):
        pass

    fastapi.FastAPI = _FastAPI  # type: ignore[attr-defined]
    fastapi.WebSocket = _WebSocket  # type: ignore[attr-defined]
    fastapi.WebSocketDisconnect = _WebSocketDisconnect  # type: ignore[attr-defined]
    sys.modules.setdefault("fastapi", fastapi)

    fastapi_responses = types.ModuleType("fastapi.responses")

    class _JSONResponse:
        def __init__(self, content):
            self.content = content

    fastapi_responses.JSONResponse = _JSONResponse  # type: ignore[attr-defined]
    sys.modules.setdefault("fastapi.responses", fastapi_responses)

    websockets = types.ModuleType("websockets")
    sys.modules.setdefault("websockets", websockets)

    websockets_asyncio = types.ModuleType("websockets.asyncio")
    sys.modules.setdefault("websockets.asyncio", websockets_asyncio)

    websockets_asyncio_client = types.ModuleType("websockets.asyncio.client")

    class _ClientConnection:
        pass

    async def _connect(*args, **kwargs):
        raise RuntimeError("connect stub should not be called in tests")

    websockets_asyncio_client.ClientConnection = _ClientConnection  # type: ignore[attr-defined]
    websockets_asyncio_client.connect = _connect  # type: ignore[attr-defined]
    sys.modules.setdefault("websockets.asyncio.client", websockets_asyncio_client)

    websockets_exceptions = types.ModuleType("websockets.exceptions")

    class _ConnectionClosed(Exception):
        pass

    websockets_exceptions.ConnectionClosed = _ConnectionClosed  # type: ignore[attr-defined]
    sys.modules.setdefault("websockets.exceptions", websockets_exceptions)


_install_server_import_stubs()
server = importlib.import_module("server")


class _FakeTranscoder:
    async def transcode_chunk(self, payload: bytes) -> bytes:
        return b"pcm:" + payload


class AudioPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_audio_passthrough(self) -> None:
        old_passthrough = server.AUDIO_PASSTHROUGH
        old_transcoder = server.transcoder
        try:
            server.AUDIO_PASSTHROUGH = True
            server.transcoder = None
            payload = b"abc"

            prepared = await server._prepare_audio_for_soniox(payload)

            self.assertEqual(prepared, payload)
        finally:
            server.AUDIO_PASSTHROUGH = old_passthrough
            server.transcoder = old_transcoder

    async def test_prepare_audio_uses_transcoder_when_disabled(self) -> None:
        old_passthrough = server.AUDIO_PASSTHROUGH
        old_transcoder = server.transcoder
        try:
            server.AUDIO_PASSTHROUGH = False
            server.transcoder = _FakeTranscoder()

            prepared = await server._prepare_audio_for_soniox(b"chunk")

            self.assertEqual(prepared, b"pcm:chunk")
        finally:
            server.AUDIO_PASSTHROUGH = old_passthrough
            server.transcoder = old_transcoder


if __name__ == "__main__":
    unittest.main()
