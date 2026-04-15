import importlib
import json
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

    class _PlainTextResponse:
        def __init__(self, content, media_type=None):
            self.content = content
            self.media_type = media_type

    fastapi_responses.PlainTextResponse = _PlainTextResponse  # type: ignore[attr-defined]
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


class _FakeOmiWebSocket:
    def __init__(self, incoming_messages: list[dict]):
        self._incoming = server.asyncio.Queue()
        for message in incoming_messages:
            self._incoming.put_nowait(message)
        self.sent_json: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        return await self._incoming.get()

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)

    async def close(self) -> None:
        self.closed = True


class _FakeSonioxWebSocket:
    def __init__(self, incoming_payloads: list[str | bytes]):
        self._incoming = server.asyncio.Queue()
        for payload in incoming_payloads:
            self._incoming.put_nowait(payload)
        self.sent: list[str | bytes] = []
        self.closed = False

    def enqueue(self, payload: str | bytes) -> None:
        self._incoming.put_nowait(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self._incoming.get()
        if payload is StopAsyncIteration:
            raise StopAsyncIteration
        return payload

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class StreamProxyProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._old_message_limit = server.MAX_MESSAGE_BYTES

    def tearDown(self) -> None:
        server.MAX_MESSAGE_BYTES = self._old_message_limit

    async def test_close_stream_sends_finalize_and_eof(self) -> None:
        omi_ws = _FakeOmiWebSocket([
            {"type": "websocket.receive", "text": json.dumps({"type": "CloseStream"})}
        ])
        soniox_ws = _FakeSonioxWebSocket([])

        old_connect = server.connect_to_soniox
        try:
            async def _connect_stub():
                return soniox_ws

            server.connect_to_soniox = _connect_stub
            await server.stream_proxy(omi_ws)
        finally:
            server.connect_to_soniox = old_connect

        self.assertTrue(omi_ws.accepted)
        self.assertIn(json.dumps({"type": "finalize"}), soniox_ws.sent)
        self.assertIn(b"", soniox_ws.sent)
        self.assertTrue(omi_ws.closed)
        self.assertTrue(soniox_ws.closed)

    async def test_finished_true_flushes_trailing_segment(self) -> None:
        omi_ws = _FakeOmiWebSocket([])
        soniox_ws = _FakeSonioxWebSocket([
            json.dumps(
                {
                    "tokens": [
                        {
                            "text": "hello",
                            "start_ms": 0,
                            "end_ms": 200,
                            "is_final": True,
                            "speaker": "1",
                        }
                    ],
                    "finished": True,
                }
            )
        ])

        old_connect = server.connect_to_soniox
        try:
            async def _connect_stub():
                return soniox_ws

            server.connect_to_soniox = _connect_stub
            await server.stream_proxy(omi_ws)
        finally:
            server.connect_to_soniox = old_connect

        self.assertEqual(len(omi_ws.sent_json), 1)
        self.assertEqual(omi_ws.sent_json[0]["segments"][0]["text"], "hello")

    async def test_soniox_error_sends_empty_segments(self) -> None:
        omi_ws = _FakeOmiWebSocket([])
        soniox_ws = _FakeSonioxWebSocket([
            json.dumps({"error_code": "BAD_REQUEST", "error_message": "bad audio"})
        ])

        old_connect = server.connect_to_soniox
        try:
            async def _connect_stub():
                return soniox_ws

            server.connect_to_soniox = _connect_stub
            await server.stream_proxy(omi_ws)
        finally:
            server.connect_to_soniox = old_connect

        self.assertIn({"segments": []}, omi_ws.sent_json)

    async def test_keepalive_is_sent_during_silence(self) -> None:
        omi_ws = _FakeOmiWebSocket([])
        soniox_ws = _FakeSonioxWebSocket([])

        old_connect = server.connect_to_soniox
        old_keepalive = server.KEEPALIVE_INTERVAL_SECONDS
        old_sleep = server.asyncio.sleep
        original_sleep = old_sleep

        async def _fast_sleep(_seconds: float) -> None:
            await original_sleep(0)

        async def _enqueue_finished() -> None:
            await original_sleep(0.01)
            soniox_ws.enqueue(json.dumps({"tokens": [], "finished": True}))

        try:
            async def _connect_stub():
                return soniox_ws

            server.connect_to_soniox = _connect_stub
            server.KEEPALIVE_INTERVAL_SECONDS = 0
            server.asyncio.sleep = _fast_sleep

            enqueue_task = server.asyncio.create_task(_enqueue_finished())
            await server.stream_proxy(omi_ws)
            await enqueue_task
        finally:
            server.connect_to_soniox = old_connect
            server.KEEPALIVE_INTERVAL_SECONDS = old_keepalive
            server.asyncio.sleep = old_sleep

        self.assertIn(json.dumps({"type": "keepalive"}), soniox_ws.sent)

    async def test_oversized_message_closes_stream_without_forwarding(self) -> None:
        server.MAX_MESSAGE_BYTES = 4
        omi_ws = _FakeOmiWebSocket([{"type": "websocket.receive", "bytes": b"12345"}])
        soniox_ws = _FakeSonioxWebSocket([])

        old_connect = server.connect_to_soniox
        try:
            async def _connect_stub():
                return soniox_ws

            server.connect_to_soniox = _connect_stub
            await server.stream_proxy(omi_ws)
        finally:
            server.connect_to_soniox = old_connect

        self.assertEqual(soniox_ws.sent, [])

    def test_metrics_endpoint_is_prometheus_compatible(self) -> None:
        content = server.metrics.render()
        self.assertIn("soniox_connection_attempts_total", content)
        self.assertIn("transcript_segments_sent_total", content)


if __name__ == "__main__":
    unittest.main()
