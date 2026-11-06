"""Breeze bridge contract and cancellation against loopback-only synthetic PCM servers."""
import asyncio
import importlib.util
import io
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
import socketio
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("breeze_bridge", ROOT / "vendor" / "breeze-bridge" / "main.py")
bridge = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bridge
_spec.loader.exec_module(bridge)


@pytest.mark.parametrize(("text", "expected"), [
    ("【casual_happy】Hello there.", "Hello there."),
    (" \t【happy2】Hello\n there. ", "Hello there."),
    ("...I mean, no.", "...I mean, no."),
    (".5 litres, please.", ".5 litres, please."),
    ("Hello, 【sad1】  my friend.", "Hello, my friend."),
    ("【happy2】(sigh) Seriously? (laugh) (cough) (clears throat)",
     "(sigh) Seriously? (laugh) (cough) (clears throat)"),
    ("【" + "a" * 24 + "】Hi.", "Hi."),
    ("【" + "a" * 25 + "】Hi.", "【" + "a" * 25 + "】Hi."),
    ("【line\nbreak】Hi.", "【line break】Hi."),
    ("【sad1】... ?! …", "... ?! …"),
    ("【sad1】...", "..."),
    ("", ""),
])
def test_normalize_text(text, expected):
    assert bridge.normalize_text(text) == expected


@pytest.mark.parametrize(("sentence", "spoken"), [
    (".5 litres, please.", ".5 litres, please."),
    ("...I mean, no.", "...I mean, no."),
    ("(sigh) Seriously?", "(sigh) Seriously?"),
])
def test_direction_mode_removes_the_start_anchor_exactly_once(sentence, spoken):
    # speech/api.py sends 【key】 + "." + sentence; the adapter (direction mode) then the bridge see it
    import voice_adapter
    payload = {"text": "【casual_happy】." + sentence, "character_id": "main_voice"}
    voice_adapter.adapt_request(payload, voice_adapter.default_state()._replace(mode=voice_adapter.MODE_DIRECTION))
    assert bridge.normalize_text(payload["text"]) == spoken


@pytest.mark.parametrize("text", ["【happy2】...", "【sad1】?! …", "___", "~", "😀"])
def test_punctuation_has_nothing_to_speak(text):
    assert not bridge.speakable(bridge.normalize_text(text))


@pytest.mark.parametrize("text", ["(sigh)", "(laugh)", "(cough)", "(clears throat)", "one", "123"])
def test_vocal_events_and_words_are_speakable(text):
    assert bridge.speakable(bridge.normalize_text(text))


@pytest.mark.parametrize("rate", [8000, 24000, 44100])
def test_wrap_pcm_preserves_rate_samples_and_duration(rate):
    pcm = struct.pack("<5h", -32768, -100, 0, 100, 32767)
    encoded, duration = bridge.wrap_pcm(pcm, rate)
    with bridge.wave.open(io.BytesIO(encoded), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == rate
        assert wav.getnframes() == 5
        assert wav.readframes(5) == pcm
    assert len(encoded) == 44 + len(pcm)
    assert duration == 5 / rate


def test_wrap_pcm_rejects_truncated_samples():
    with pytest.raises(ValueError):
        bridge.wrap_pcm(b"\x00", 24000)


@pytest.mark.parametrize("value", [1, 1.0, 3, 4.5, 5.0])
def test_cfg_scale_valid(value):
    assert bridge.check_cfg_scale(value) == float(value)


@pytest.mark.parametrize("value", [True, False, "3", None, 0.9, 5.1, float("nan"), float("inf"), -float("inf")])
def test_cfg_scale_invalid(value):
    with pytest.raises(ValueError):
        bridge.check_cfg_scale(value)


def test_optional_fields_and_configured_seed():
    base = {"text": "Hello", "character_id": "main_voice"}
    assert bridge.parse_job("client", base).fields == {"text": "Hello", "voice_id": "main_voice"}
    assert bridge.parse_job("client", dict(base, instruction=None, cfg_scale=None, seed=None)).fields == {
        "text": "Hello", "voice_id": "main_voice"}
    assert bridge.parse_job("client", base, seed=7).fields["seed"] == "7"
    assert bridge.parse_job("client", dict(base, seed=99), seed=7).fields["seed"] == "99"


class FakeBreeze:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Queue()
        self.closed = asyncio.Queue()
        self.gates = {}
        self.header_gates = {}
        self.busy_remaining = 0
        self.always_busy = False
        self.status = 200
        self.sample_format = "s16le"
        self.health_status = 200
        self.voices = [
            {"id": "temporary", "saved": False, "seconds": 1.5, "ref_text": "private temporary transcript"},
            {"id": "main_voice", "saved": True, "seconds": 4.0, "ref_text": "private saved transcript"},
            {"id": "other", "saved": True, "seconds": 2.5, "ref_text": "private other transcript"},
        ]
        self.pcm = b"\x01\x00\xff\xff" * 200
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/v1/voices", self.list_voices)
        self.app.router.add_post("/v1/audio/speech", self.speech)

    async def health(self, _request):
        return web.json_response({"status": "ok", "sample_rate": 8000, "ws_port": 8081}, status=self.health_status)

    async def list_voices(self, _request):
        return web.json_response(self.voices)

    async def speech(self, request):
        fields = dict(await request.post())
        self.calls.append(fields)
        text = fields["text"]
        if self.always_busy or self.busy_remaining:
            self.busy_remaining = max(0, self.busy_remaining - 1)
            return web.Response(status=409, text="all slots busy")
        if self.status != 200:
            return web.Response(status=self.status, text="synthetic upstream failure")
        await self.started.put(text)
        response = web.StreamResponse(headers={"X-Sample-Rate": "8000", "X-Sample-Format": self.sample_format})
        try:
            if text in self.header_gates:
                while not self.header_gates[text].is_set():
                    if request.transport is None or request.transport.is_closing():
                        await self.closed.put(text)
                        return web.Response()
                    await asyncio.sleep(0.01)
            await response.prepare(request)
            # Split a sample across HTTP chunks: HTTP framing must not affect PCM framing.
            await response.write(self.pcm[:3])
            await response.write(self.pcm[3:])
            gate = self.gates.get(text)
            while gate is not None and not gate.is_set():
                try:
                    await asyncio.wait_for(gate.wait(), 0.01)
                except TimeoutError:
                    await response.write(b"\x00\x00")
            await response.write_eof()
        except asyncio.CancelledError:
            # aiohttp TestServer cancels handlers on transport closure; production servers may instead
            # discover the same disconnect at their next write.
            await self.closed.put(text)
            raise
        except (ConnectionError, RuntimeError):
            await self.closed.put(text)
        return response


class Client:
    def __init__(self):
        self.sio = socketio.AsyncClient(reconnection=False)
        self.queued = asyncio.Queue()
        self.audio = asyncio.Queue()
        self.errors = asyncio.Queue()
        self.sio.on("task_queued", self.queued.put)
        self.sio.on("audio_ready", self.audio.put)
        self.sio.on("error", self.errors.put)

    async def request(self, task_id, text="Hello", **fields):
        await self.sio.emit("tts_request_gateway", {"task_id": task_id, "turn_id": "turn-" + task_id,
                                                    "text": text, "character_id": "main_voice", **fields})
        return await asyncio.wait_for(self.queued.get(), 3)

    async def result(self):
        return await asyncio.wait_for(self.audio.get(), 3)

    async def error(self):
        return await asyncio.wait_for(self.errors.get(), 3)


class TestBridgeServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBreeze()
        self.upstream = TestServer(self.fake.app)
        await self.upstream.start_server()
        args = bridge.parse_args(["--breeze", str(self.upstream.make_url("")), "--request-timeout", "3"])
        self.bridge = bridge.Server(args)
        self.server = TestServer(self.bridge.app)
        await self.server.start_server()
        self.clients = []
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        for client in self.clients:
            if client.sio.connected:
                await client.sio.disconnect()
        for gate in (*self.fake.gates.values(), *self.fake.header_gates.values()):
            gate.set()
        await self.http.close()
        await self.server.close()
        await self.upstream.close()

    async def client(self):
        client = Client()
        await client.sio.connect(str(self.server.make_url("")), transports=["websocket"])
        self.clients.append(client)
        return client

    async def test_http_health_and_private_voice_projection(self):
        async with self.http.get(self.server.make_url("/ping")) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "ok")
        async with self.http.get(self.server.make_url("/api/characters")) as response:
            data = await response.json()
        self.assertEqual(data, {"characters": {
            "temporary": {"id": "temporary", "saved": False, "seconds": 1.5},
            "main_voice": {"id": "main_voice", "saved": True, "seconds": 4.0},
            "other": {"id": "other", "saved": True, "seconds": 2.5}}, "current": "main_voice"})
        async with self.http.get(self.server.make_url("/api/health")) as response:
            self.assertEqual((await response.json())["status"], "ready")
        self.fake.health_status = 503
        async with self.http.get(self.server.make_url("/ping")) as response:
            self.assertEqual(response.status, 503)
            self.assertTrue(await response.text())
        await self.upstream.close()
        async with self.http.get(self.server.make_url("/ping")) as response:
            self.assertEqual(response.status, 503)
            self.assertTrue(await response.text())

    async def test_rebinding_hosts_and_foreign_pages_are_refused(self):
        for headers in ({"Host": "evil.test:8002"}, {"Origin": "http://evil.test"}):
            for path in ("/ping", "/api/characters", "/api/health"):
                async with self.http.get(self.server.make_url(path), headers=headers) as response:
                    self.assertEqual(response.status, 403, (path, headers))
        page = Client()
        with self.assertRaises(socketio.exceptions.ConnectionError):
            await page.sio.connect(str(self.server.make_url("")), transports=["websocket"],
                                   headers={"Origin": "http://evil.test"})
        self.assertFalse(self.fake.calls)

    async def test_cors_admits_other_pages_but_never_a_rebinding_host(self):
        args = bridge.parse_args(["--breeze", str(self.upstream.make_url("")), "--cors"])
        server = TestServer(bridge.Server(args).app)
        await server.start_server()
        try:
            async with self.http.get(server.make_url("/ping"), headers={"Origin": "http://evil.test"}) as response:
                self.assertEqual(response.status, 200)
            async with self.http.get(server.make_url("/ping"), headers={"Host": "evil.test"}) as response:
                self.assertEqual(response.status, 403)
        finally:
            await server.close()

    async def test_characters_with_no_saved_voice(self):
        self.fake.voices = [{"id": "temporary", "saved": False, "seconds": 1.5}]
        async with self.http.get(self.server.make_url("/api/characters")) as response:
            self.assertIsNone((await response.json())["current"])

    async def test_direction_forwarding_and_complete_wav(self):
        client = await self.client()
        other = await self.client()
        queued = await client.request("direction", "【casual_happy】(sigh) Seriously?", instruction="Irritated.",
                                      cfg_scale=3, seed=11, language="Japanese", audio_format="opus")
        self.assertEqual(queued["queue_position"], 0)
        self.assertEqual(queued["sentence_count"], 1)
        result = await client.result()
        self.assertEqual(self.fake.calls, [{"text": "(sigh) Seriously?", "voice_id": "main_voice",
                                           "instruction": "Irritated.", "cfg_scale": "3.0", "seed": "11"}])
        self.assertEqual(result["task_id"], "direction")
        self.assertEqual(result["turn_id"], "turn-direction")
        self.assertEqual(result["character_id"], "main_voice")
        self.assertTrue(result["is_final"])
        self.assertTrue(result["warning"])
        self.assertEqual(result["audio_format"], "wav")
        self.assertEqual(result["sample_rate"], 8000)
        self.assertEqual(result["audio_duration"], 0.05)
        with bridge.wave.open(io.BytesIO(result["audio"]), "rb") as wav:
            self.assertEqual(wav.readframes(wav.getnframes()), self.fake.pcm)
        self.assertTrue(other.audio.empty())
        self.assertTrue(other.queued.empty())
        self.assertTrue(other.errors.empty())

    async def test_blank_and_invalid_cfg_do_not_queue_or_call_breeze(self):
        client = await self.client()
        for task_id, fields in [("blank", {"text": " \n "}), ("cfg", {"text": "Hello", "cfg_scale": "3"})]:
            await client.sio.emit("tts_request_gateway", {"task_id": task_id, "turn_id": "turn", **fields})
            error = await client.error()
            self.assertEqual(error["task_id"], task_id)
            self.assertEqual(error["turn_id"], "turn")
            self.assertTrue(error["error"].startswith("bad request: "))
            if task_id == "blank":
                self.assertEqual(error["error"], "bad request: empty text")
        self.assertTrue(client.queued.empty())
        self.assertFalse(self.fake.calls)

    async def test_punctuation_returns_silence_without_breeze(self):
        client = await self.client()
        await client.request("silence", "【happy2】... ?!", audio_format="opus")
        result = await client.result()
        self.assertTrue(result["warning"])
        self.assertEqual(result["audio_duration"], 0.3)
        self.assertEqual(result["audio_format"], "wav")
        with bridge.wave.open(io.BytesIO(result["audio"]), "rb") as wav:
            self.assertEqual(wav.getnframes(), 7200)
            self.assertEqual(wav.readframes(7200), b"\x00\x00" * 7200)
        self.assertFalse(self.fake.calls)

    async def test_global_fifo_across_clients(self):
        first, second = await self.client(), await self.client()
        gate = self.fake.gates["first"] = asyncio.Event()
        self.assertEqual((await first.request("one", "first"))["queue_position"], 0)
        self.assertEqual(await asyncio.wait_for(self.fake.started.get(), 3), "first")
        self.assertEqual((await second.request("two", "second"))["queue_position"], 1)
        self.assertEqual((await first.request("three", "third"))["queue_position"], 2)
        self.assertEqual([call["text"] for call in self.fake.calls], ["first"])
        gate.set()
        self.assertEqual((await first.result())["task_id"], "one")
        self.assertEqual((await second.result())["task_id"], "two")
        self.assertEqual((await first.result())["task_id"], "three")
        self.assertEqual([call["text"] for call in self.fake.calls], ["first", "second", "third"])

    async def test_disconnect_drops_queue_closes_stream_and_keeps_worker(self):
        first, second = await self.client(), await self.client()
        self.fake.gates["first"] = asyncio.Event()
        await first.request("one", "first")
        self.assertEqual(await asyncio.wait_for(self.fake.started.get(), 3), "first")
        await first.request("drop", "do not synthesize")
        await second.request("two", "second")
        await first.sio.disconnect()
        self.assertEqual(await asyncio.wait_for(self.fake.closed.get(), 3), "first")
        self.assertEqual((await second.result())["task_id"], "two")
        await second.request("three", "third")
        self.assertEqual((await second.result())["task_id"], "three")
        self.assertEqual([call["text"] for call in self.fake.calls], ["first", "second", "third"])
        self.assertTrue(first.audio.empty())
        self.assertTrue(first.errors.empty())

    async def test_disconnect_while_waiting_for_response_headers(self):
        first, second = await self.client(), await self.client()
        self.fake.header_gates["first"] = asyncio.Event()
        await first.request("one", "first")
        self.assertEqual(await asyncio.wait_for(self.fake.started.get(), 3), "first")
        await second.request("two", "second")
        await first.sio.disconnect()
        self.assertEqual(await asyncio.wait_for(self.fake.closed.get(), 3), "first")
        self.assertEqual((await second.result())["task_id"], "two")

    async def test_busy_retries_preserve_fifo(self):
        first, second = await self.client(), await self.client()
        self.fake.busy_remaining = 2
        with patch.object(bridge, "BUSY_BACKOFF_S", 0.01):
            await first.request("one", "first")
            await second.request("two", "second")
            self.assertEqual((await first.result())["task_id"], "one")
            self.assertEqual((await second.result())["task_id"], "two")
        self.assertEqual([call["text"] for call in self.fake.calls], ["first", "first", "first", "second"])

    async def test_busy_wait_is_bounded_and_worker_recovers(self):
        client = await self.client()
        self.fake.always_busy = True
        with patch.object(bridge, "BUSY_BACKOFF_S", 0.01), patch.object(bridge, "BUSY_MAX_WAIT_S", 0.04):
            await client.request("busy")
            self.assertEqual((await client.error())["task_id"], "busy")
        self.assertGreater(len(self.fake.calls), 1)
        self.fake.always_busy = False
        await client.request("next")
        self.assertEqual((await client.result())["task_id"], "next")

    async def test_disconnect_cancels_busy_wait(self):
        client = await self.client()
        self.fake.always_busy = True
        with patch.object(bridge, "BUSY_BACKOFF_S", 0.05):
            await client.request("busy")
            while not self.fake.calls:
                await asyncio.sleep(0.005)
            await client.sio.disconnect()
            # A different client's silence request is a FIFO barrier after cancellation.
            other = await self.client()
            await other.request("silence", "...")
            await other.result()
            calls_after_disconnect = len(self.fake.calls)
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.fake.calls), calls_after_disconnect)
            self.assertIsNone(self.bridge.active)

    async def test_request_timeout_aborts_stream_and_keeps_worker(self):
        client = await self.client()
        self.bridge.args.request_timeout = 0.06
        self.fake.gates["slow"] = asyncio.Event()
        await client.request("timeout", "slow")
        self.assertEqual((await client.error())["task_id"], "timeout")
        self.assertEqual(await asyncio.wait_for(self.fake.closed.get(), 3), "slow")
        self.bridge.args.request_timeout = 3
        await client.request("next")
        self.assertEqual((await client.result())["task_id"], "next")

    async def test_upstream_error_and_wrong_format_are_not_audio(self):
        client = await self.client()
        self.fake.status = 500
        await client.request("failure")
        self.assertEqual((await client.error())["task_id"], "failure")
        self.fake.status = 200
        self.fake.sample_format = "f32le"
        await client.request("format")
        self.assertEqual((await client.error())["task_id"], "format")
        self.assertTrue(client.audio.empty())
