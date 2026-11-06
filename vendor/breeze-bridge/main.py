#!/usr/bin/env python3
"""OpenAmika's Socket.IO voice contract backed by a loopback breeze-server."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import ipaddress
import logging
import math
import re
import sys
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import socketio
from aiohttp import web

SERVER_NAME = "OpenAmika Breeze voice bridge"
LOG = logging.getLogger("voice")
TAG_RE = re.compile(r"【[^】\n]{1,24}】")
# the speech layer's one `.` start anchor; a leading run of exactly three dots is an ellipsis, not an anchor
ANCHOR_RE = re.compile(r"^\s*\.(?!\.\.(?!\.))")
SPEAKABLE_RE = re.compile(r"[^\W_]")
SILENT_REPLY_S = 0.3
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_SEED = 42
BUSY_MAX_WAIT_S = 10.0
BUSY_BACKOFF_S = 0.25


def normalize_text(text: str) -> str:
    """Remove legacy tags and the start anchor, not ellipses or Breeze's parenthesized vocal events."""
    return " ".join(ANCHOR_RE.sub("", TAG_RE.sub("", text)).split())


def speakable(text: str) -> bool:
    return SPEAKABLE_RE.search(text) is not None


def check_cfg_scale(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 1.0 <= value <= 5.0):
        raise ValueError(f"cfg_scale must be between 1.0 and 5.0 (got {value!r})")
    return float(value)


def check_seed(value: Any) -> int:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()))):
        raise ValueError(f"seed must be a whole number (got {value!r})")
    return int(value)


def wrap_pcm(pcm: bytes, sample_rate: int) -> tuple[bytes, float]:
    """Wrap complete mono s16le samples without resampling or changing their bytes."""
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    if len(pcm) % 2:
        raise ValueError("Breeze returned an incomplete 16-bit sample")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue(), (len(pcm) // 2) / sample_rate


def loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=SERVER_NAME)
    ap.add_argument("--host", default="127.0.0.1", help="loopback bind address")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--breeze", default="http://127.0.0.1:8080", help="loopback breeze-server HTTP origin")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="fallback seed (default: 42, Breeze's own default); requests can override it")
    ap.add_argument("--cors", action="store_true", help="allow browser pages from any origin (local testing)")
    ap.add_argument("--request-timeout", type=float, default=120.0,
                    help="seconds for one synthesis, including busy retries but not queue time (default: 120)")
    args = ap.parse_args(argv)
    if not loopback_host(args.host):
        ap.error("--host must be a loopback address (the bridge has no authentication)")
    origin = urlsplit(args.breeze)
    if (origin.scheme not in ("http", "https") or not loopback_host(origin.hostname or "")
            or origin.username is not None or origin.password is not None
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        ap.error("--breeze must be a loopback HTTP origin without credentials, path, query or fragment")
    args.breeze = args.breeze.rstrip("/")
    if not math.isfinite(args.request_timeout) or args.request_timeout <= 0:
        ap.error("--request-timeout must be a positive number")
    return args


@dataclass
class Job:
    sid: str
    task_id: str
    turn_id: str | None
    character_id: str | None
    text: str
    fields: dict[str, str]
    notes: list[str] = field(default_factory=list)
    received: float = field(default_factory=time.perf_counter)


def parse_job(sid: str, data: Any, seed: int = DEFAULT_SEED) -> Job:
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    raw_text = "" if data.get("text") is None else str(data["text"])
    if not raw_text.strip():
        raise ValueError("empty text")
    text = normalize_text(raw_text)
    character_id = str(data["character_id"]) if data.get("character_id") else None
    fields = {"text": text}
    if character_id is not None:
        fields["voice_id"] = character_id
    if data.get("instruction") is not None:
        if not isinstance(data["instruction"], str):
            raise ValueError("instruction must be a string")
        fields["instruction"] = data["instruction"]
    if data.get("cfg_scale") is not None:
        fields["cfg_scale"] = str(check_cfg_scale(data["cfg_scale"]))
    if data.get("seed") is not None:
        fields["seed"] = str(check_seed(data["seed"]))
    elif seed != DEFAULT_SEED:
        # Leave absent optional fields to Breeze unless the operator changes its default.
        fields["seed"] = str(seed)
    notes = []
    asked = str(data.get("audio_format") or "wav").strip().lower()
    if asked != "wav":
        notes.append(f"audio_format {asked!r} is not supported; sent WAV")
    return Job(sid=sid, task_id=str(data.get("task_id") or uuid.uuid4()),
               turn_id=str(data["turn_id"]) if data.get("turn_id") else None,
               character_id=character_id, text=text, fields=fields, notes=notes)


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sio = socketio.AsyncServer(async_mode="aiohttp", async_handlers=False,
                                        cors_allowed_origins="*" if args.cors else None,
                                        max_http_buffer_size=16 * 1024 * 1024,
                                        logger=False, engineio_logger=False)
        self.clients: set[str] = set()
        self.queue: deque[Job] = deque()
        self.queue_lock = asyncio.Lock()
        self.available = asyncio.Event()
        self.active: Job | None = None
        self.active_task: asyncio.Task | None = None
        self.active_response: aiohttp.ClientResponse | None = None
        self.worker: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.language_warned = False
        self.sio.on("connect", self.connect)
        self.sio.on("disconnect", self.disconnect)
        self.sio.on("tts_request_gateway", self.handle)
        self.app = web.Application()
        self.sio.attach(self.app)
        self.app.router.add_get("/ping", self.ping)
        self.app.router.add_get("/api/characters", self.characters)
        self.app.router.add_get("/api/health", self.health)
        self.app.cleanup_ctx.append(self.lifecycle)
        self.app.on_shutdown.append(self.shutdown_clients)

    async def lifecycle(self, _app: web.Application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.args.request_timeout)) as session:
            self.session = session
            self.worker = asyncio.create_task(self.work(), name="breeze-fifo")
            try:
                yield
            finally:
                self.clients.clear()
                self.queue.clear()
                if self.active_response is not None:
                    self.active_response.close()
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker
                self.session = None

    async def shutdown_clients(self, _app: web.Application) -> None:
        # OpenAmika keeps its websocket open; close it before aiohttp waits for handlers.
        if self.sio.eio.sockets:
            await self.sio.eio.disconnect()
        await self.sio.shutdown()

    async def connect(self, sid: str, _environ: Any, _auth: Any = None) -> None:
        self.clients.add(sid)

    async def disconnect(self, sid: str, *_reason: Any) -> None:
        self.clients.discard(sid)
        async with self.queue_lock:
            kept = deque()
            for job in self.queue:
                if job.sid == sid:
                    self.log_job(job, 0.0, job.received, "dropped: client disconnected")
                else:
                    kept.append(job)
            self.queue = kept
            if not self.queue:
                self.available.clear()
            if self.active is not None and self.active.sid == sid:
                if self.active_response is not None:
                    self.active_response.close()
                if self.active_task is not None:
                    # Also cancels a POST waiting for headers or a busy-retry sleep.
                    self.active_task.cancel()

    async def handle(self, sid: str, data: Any) -> None:
        if sid not in self.clients:
            return
        try:
            job = parse_job(sid, data, self.args.seed)
        except ValueError as exc:
            payload = data if isinstance(data, dict) else {}
            await self.sio.emit("error", {"task_id": str(payload.get("task_id") or ""),
                                          "turn_id": payload.get("turn_id"), "error": f"bad request: {exc}"}, to=sid)
            LOG.info("task %s voice=%s cfg=%s chars=%d audio=0.000s elapsed=0.000s RTF=- bad request: %s",
                     payload.get("task_id", ""), payload.get("character_id"), payload.get("cfg_scale", "default"),
                     len(str(payload.get("text") or "")), exc)
            return
        language = str(data.get("language") or "English").strip().lower()
        if language not in ("en", "english") and not self.language_warned:
            LOG.warning("language %r ignored: Breeze bridge is English-only", data["language"])
            self.language_warned = True
        async with self.queue_lock:
            if sid not in self.clients:
                return
            position = len(self.queue) + int(self.active is not None)
            self.queue.append(job)
            # Hold the queue lock until the acknowledgement is sent: even silence must come after it.
            await self.sio.emit("task_queued", {"task_id": job.task_id, "turn_id": job.turn_id,
                                               "sentence_count": 1, "queue_position": position}, to=sid)
            self.available.set()

    async def work(self) -> None:
        while True:
            await self.available.wait()
            async with self.queue_lock:
                if not self.queue:
                    self.available.clear()
                    continue
                self.active = self.queue.popleft()
                if not self.queue:
                    self.available.clear()
                self.active_task = asyncio.create_task(self.render(self.active))
            try:
                await self.active_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.active = None
                self.active_task = None

    async def synthesize(self, job: Job) -> tuple[bytes, int]:
        assert self.session is not None
        busy_deadline = time.monotonic() + BUSY_MAX_WAIT_S
        async with asyncio.timeout(self.args.request_timeout):
            while True:
                async with self.session.post(self.args.breeze + "/v1/audio/speech", data=job.fields,
                                             allow_redirects=False) as response:
                    self.active_response = response
                    try:
                        if response.status == 409:
                            if time.monotonic() >= busy_deadline:
                                raise RuntimeError(f"Breeze remained busy (HTTP 409) for {BUSY_MAX_WAIT_S:g} seconds")
                        elif response.status != 200:
                            detail = (await response.text())[:300].strip()
                            raise RuntimeError(f"Breeze HTTP {response.status}: {detail}")
                        else:
                            try:
                                sample_rate = int(response.headers["X-Sample-Rate"])
                            except (KeyError, ValueError) as exc:
                                raise RuntimeError("Breeze returned no valid X-Sample-Rate") from exc
                            if sample_rate <= 0:
                                raise RuntimeError("Breeze returned a non-positive sample rate")
                            if response.headers.get("X-Sample-Format", "").lower() != "s16le":
                                raise RuntimeError("Breeze did not return s16le PCM")
                            pcm = await response.read()
                            if not pcm:
                                raise RuntimeError("Breeze returned no audio")
                            self.sample_rate = sample_rate
                            return pcm, sample_rate
                    finally:
                        self.active_response = None
                remaining = busy_deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Breeze remained busy (HTTP 409) for {BUSY_MAX_WAIT_S:g} seconds")
                await asyncio.sleep(min(BUSY_BACKOFF_S, remaining))

    async def render(self, job: Job) -> None:
        started = time.perf_counter()
        duration = 0.0
        status = "ok"
        try:
            if speakable(job.text):
                pcm, sample_rate = await self.synthesize(job)
            else:
                sample_rate = self.sample_rate
                pcm = b"\0\0" * round(SILENT_REPLY_S * sample_rate)
                job.notes.append("text has nothing to speak; sent 0.3 s of silence")
            audio, duration = wrap_pcm(pcm, sample_rate)
            if job.sid not in self.clients:
                status = "cancelled: client disconnected"
                return
            result = {"task_id": job.task_id, "turn_id": job.turn_id, "audio": audio,
                      "audio_format": "wav", "audio_duration": duration, "sample_rate": sample_rate,
                      "is_final": True, "text": job.text, "character_id": job.character_id,
                      "elapsed_ms": int((time.perf_counter() - job.received) * 1000)}
            if job.notes:
                result["warning"] = "; ".join(job.notes)
            await self.sio.emit("audio_ready", result, to=job.sid)
        except asyncio.CancelledError:
            status = "cancelled: client disconnected or bridge stopping"
            raise
        except Exception as exc:
            status = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, TimeoutError):
                status = f"Breeze request timed out after {self.args.request_timeout:g} seconds"
            if job.sid in self.clients:
                await self.sio.emit("error", {"task_id": job.task_id, "turn_id": job.turn_id,
                                              "error": status}, to=job.sid)
        finally:
            self.log_job(job, duration, started, status)

    @staticmethod
    def log_job(job: Job, duration: float, started: float, status: str) -> None:
        now = time.perf_counter()
        rtf = f"{(now - started) / duration:.2f}" if duration else "-"
        LOG.info("task %s voice=%s cfg=%s chars=%d audio=%.3fs elapsed=%.3fs RTF=%s %s",
                 job.task_id[:8], job.character_id, job.fields.get("cfg_scale", "default"), len(job.text),
                 duration, now - job.received, rtf, status)

    async def fetch_json(self, path: str) -> Any:
        assert self.session is not None
        async with self.session.get(self.args.breeze + path, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=min(5.0, self.args.request_timeout))) as response:
            if response.status != 200:
                raise RuntimeError(f"Breeze HTTP {response.status}: {(await response.text())[:300].strip()}")
            return await response.json()

    async def breeze_health(self) -> tuple[dict, str | None]:
        try:
            health = await self.fetch_json("/health")
            if not isinstance(health, dict) or health.get("status") != "ok":
                raise RuntimeError("Breeze is not ready")
            rate = health.get("sample_rate")
            if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0:
                self.sample_rate = rate
            return {key: health[key] for key in ("status", "sample_rate", "ws_port") if key in health}, None
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            return {}, f"Breeze unavailable: {type(exc).__name__}: {exc}"

    async def ping(self, _request: web.Request) -> web.Response:
        _, error = await self.breeze_health()
        return web.Response(status=503 if error else 200, text=error or "ok")

    async def health(self, _request: web.Request) -> web.Response:
        upstream, error = await self.breeze_health()
        return web.json_response({"status": "unavailable" if error else "ready", "breeze": self.args.breeze,
                                  "upstream": upstream, "error": error, "sample_rate": self.sample_rate,
                                  "queue_length": len(self.queue),
                                  "active_task_id": self.active.task_id if self.active else None})

    async def characters(self, _request: web.Request) -> web.Response:
        try:
            voices = await self.fetch_json("/v1/voices")
            if not isinstance(voices, list):
                raise RuntimeError("Breeze returned an invalid voice list")
            characters = {}
            for voice in voices:
                if not isinstance(voice, dict) or not isinstance(voice.get("id"), str):
                    raise RuntimeError("Breeze returned an invalid voice entry")
                characters[voice["id"]] = {key: voice.get(key) for key in ("id", "saved", "seconds")}
            current = next((voice_id for voice_id, voice in characters.items() if voice["saved"]), None)
            return web.json_response({"characters": characters, "current": current})
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            return web.json_response({"characters": {}, "current": None,
                                      "error": f"Breeze unavailable: {type(exc).__name__}: {exc}"}, status=503)

    async def run(self) -> int:
        runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=0.5)
        await runner.setup()
        try:
            try:
                await web.TCPSite(runner, self.args.host, self.args.port).start()
            except OSError as exc:
                LOG.error("cannot listen on %s:%d: %s", self.args.host, self.args.port, exc)
                return 1
            LOG.info("%s listening on http://%s:%d -> %s", SERVER_NAME, self.args.host, self.args.port, self.args.breeze)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    for noisy in ("aiohttp.access", "engineio", "socketio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return asyncio.run(Server(args).run())
    except KeyboardInterrupt:
        LOG.info("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
