"""OpenAmika messengers — text her from Discord and Telegram.

One process, started by launch.py next to the speech gateway (python messengers/service.py). It holds one
Socket.IO link to the brain (its own sid, like a browser tab), a Discord bot and/or a Telegram bot from
config/messengers.json, and a small HTTP API on 127.0.0.1:5215 for the Setup page and the launcher's light:

  GET  /health             {app, ok, brain, bots, light}        the launcher reads `light` for its row
  GET  /api/status         the config (tokens masked) + live bot status  (Setup page, through the gateway)
  POST /api/config         save settings from the Setup page; bots start/stop/restart to match
  POST /api/text {text}    a local text channel: her replies to one message, as JSON (scripts, checks)

Text only: no TTS, no stage. A message from an allowed user becomes one brain turn; her quick line
(surface) and her real reply (deep) go out as plain messages, split to the platform's limit. The turn is
"protected" in the brain once her first message is delivered (what the first TTS audio does on stage).
Turns she starts herself (a reflection making her speak first, a helper's result) reach the chat she last
talked in. Her commands are serialized: one turn at a time, in the order the messages came.

Who may text her: only the user ids on a platform's allow-list (config/messengers.json, Setup page). Anyone
else is told their id once an hour, so the owner can add it. In a Discord server she answers @mentions
only; DMs always. Neither needs Discord's privileged "message content" intent.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web
import socketio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (CONFIG_PATH, LIMITS, TERMINAL_EVENTS, TITLES, apply_update, incoming_text, light,  # noqa: E402
                  load_config, public_view, replies_from, save_config, split_message, turn_context)
import local_guard  # noqa: E402

try:
    import discord
except ImportError:                 # the Setup page says so; Telegram still works
    discord = None

APP = "OpenAmika"
HANE_URL = os.getenv("HANE_URL", "http://127.0.0.1:5202")
HOST, PORT = "127.0.0.1", int(os.getenv("AMIKA_MESSENGERS_PORT", "5215"))
TURN_TIMEOUT_S = 150                # a deep reply with a tool phase can take a while; after this the chat is free
HINT_EVERY_S = 3600                 # how often an unknown sender is told their id
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [messengers] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("messengers")


# ───────────────────────────── chats: where her words go ─────────────────────────────
class Chat:
    platform = "local"

    def __init__(self, key, label):
        self.key, self.label = str(key), label
        self.limit = LIMITS.get(self.platform, 4000)

    def ref(self):
        """What is saved as her last chat (enough to find it again after a restart)."""
        return {"platform": self.platform, "chat_id": self.key, "label": self.label}

    async def send(self, text):
        raise NotImplementedError

    @contextlib.asynccontextmanager
    async def typing(self):
        yield

    def closed(self):
        """The turn that was sent from here is over (delivered, failed or timed out)."""


class DiscordChat(Chat):
    platform = "discord"

    def __init__(self, channel, label):
        super().__init__(channel.id, label)
        self.channel = channel

    async def send(self, text):
        await self.channel.send(text)

    @contextlib.asynccontextmanager
    async def typing(self):
        try:
            async with self.channel.typing():
                yield
        except Exception:           # the indicator is cosmetic; never let it stop a reply
            yield


class TelegramChat(Chat):
    platform = "telegram"

    def __init__(self, bot, chat_id, label):
        super().__init__(chat_id, label)
        self.bot = bot

    async def send(self, text):
        answer = await self.bot.api("sendMessage", chat_id=int(self.key), text=text)
        if not answer.get("ok"):
            raise RuntimeError(f"Telegram sendMessage: {answer.get('description') or answer}")

    @contextlib.asynccontextmanager
    async def typing(self):
        async def keep():           # Telegram shows "typing…" for 5 s per action
            while True:
                try:
                    await self.bot.api("sendChatAction", chat_id=int(self.key), action="typing")
                except Exception:
                    pass
                await asyncio.sleep(4)
        task = asyncio.create_task(keep())
        try:
            yield
        finally:
            task.cancel()


class LocalChat(Chat):
    """POST /api/text: collects her replies for one caller."""
    platform = "local"

    def __init__(self):
        super().__init__(uuid.uuid4().hex[:8], "local text API")
        self.replies = []
        self.finished = asyncio.Event()

    def ref(self):
        return None                 # never "her last chat": there is nobody to text back later

    async def send(self, text):
        self.replies.append(text)

    def closed(self):
        self.finished.set()


# ───────────────────────────── the brain ─────────────────────────────
class BrainLink:
    """Her brain, over Socket.IO — the same contract the speech gateway uses (brain/PROTOCOL.md)."""

    def __init__(self, service):
        self.service = service
        self.sio = socketio.AsyncClient(reconnection=True, reconnection_attempts=0, logger=False, engineio_logger=False)
        self.connected = False
        sio = self.sio

        @sio.event
        async def connect():
            self.connected = True
            log.info(f"brain: linked ({HANE_URL})")

        @sio.event
        async def disconnect():
            self.connected = False
            log.info("brain: link lost")

        for event in ("begin", "surface_done", "deep_done", "end", "error"):
            async def handler(data=None, _event=event):
                await self.service.outbox.on_event(_event, data or {})
            sio.on(event, handler)

    async def start(self):
        """The first connection is retried here (the client only auto-reconnects after it once succeeded)."""
        while not self.connected:
            try:
                await self.sio.connect(HANE_URL, transports=["websocket"])
                return
            except Exception as error:
                log.info(f"brain: not reachable yet ({str(error)[:80]}); retrying")
                await asyncio.sleep(5)

    async def send(self, text, turn_id, context, channel=""):
        # channel: the platform name — the brain picks her "on the phone" scene for text turns
        await self.sio.emit("message", {"text": text, "turn_id": turn_id, "context": context, "channel": channel})

    async def protect(self):
        try:
            await self.sio.call("protect", {}, timeout=5)
        except Exception as error:
            log.warning(f"brain: protect failed: {error}")

    async def speech_done(self, turn_id):
        """What the stage sends once her audio has played out: the brain archives the turn into her memory
        only after this. For a text turn, "played out" is "delivered"."""
        try:
            await self.sio.emit("speech_done", {"turn_id": turn_id})
        except Exception as error:
            log.warning(f"brain: speech_done failed: {error}")


class Turn:
    def __init__(self, turn_id, chat, proactive=False):
        self.turn_id, self.chat, self.proactive = turn_id, chat, proactive
        self.done = asyncio.Event()
        self.protected = False
        self.sent = 0


class Outbox:
    """Her text turns, one at a time: a message in → a brain turn → her messages out."""

    def __init__(self, service):
        self.service = service
        self.queue = asyncio.Queue()
        self.turns = {}

    async def put(self, chat, text, platform):
        await self.queue.put((chat, text, platform))

    async def run(self):
        while True:
            chat, text, platform = await self.queue.get()
            try:
                await self._turn(chat, text, platform)
            except Exception as error:
                log.exception(f"turn failed: {error}")

    async def _turn(self, chat, text, platform):
        brain = self.service.brain
        if not brain.connected:
            with contextlib.suppress(Exception):
                await chat.send("⚠ OpenAmika: her brain is not connected right now — try again in a moment.")
            chat.closed()
            return
        turn = Turn(f"msg-{uuid.uuid4().hex[:12]}", chat)
        self.turns[turn.turn_id] = turn
        log.info(f"{chat.label} → her: {text[:80]!r}")
        try:
            async with chat.typing():
                await brain.send(text, turn.turn_id, turn_context(platform), channel=platform)
                try:
                    await asyncio.wait_for(turn.done.wait(), TURN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning(f"turn {turn.turn_id}: no end after {TURN_TIMEOUT_S}s")
        finally:
            self.turns.pop(turn.turn_id, None)
            chat.closed()

    async def on_event(self, event, data):
        turn_id = str(data.get("turn_id") or "")
        turn = self.turns.get(turn_id)
        if turn is None:
            # a turn she started herself (reflection, a helper's result) lands in the chat she last used
            if event != "begin" or not data.get("proactive"):
                return
            chat = await self.service.last_chat()
            if chat is None:
                log.info(f"turn {turn_id}: she spoke first, but nobody has texted her yet — not delivered")
                return
            turn = Turn(turn_id, chat, proactive=True)
            self.turns[turn_id] = turn
            asyncio.create_task(self._typing_until_done(turn))
            return
        for text in replies_from(event, data):
            await self._deliver(turn, text, system=(event == "error"))
        if event in TERMINAL_EVENTS:
            if event == "end" and turn.protected:
                await self.service.brain.speech_done(turn_id)     # delivered in full: she may remember it now
            turn.done.set()
            if turn.proactive:
                self.turns.pop(turn_id, None)
            log.info(f"turn {turn_id}: {event} · {turn.sent} message(s) to {turn.chat.label}")

    async def _typing_until_done(self, turn):
        async with turn.chat.typing():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(turn.done.wait(), TURN_TIMEOUT_S)
        self.turns.pop(turn.turn_id, None)

    async def _deliver(self, turn, text, system=False):
        for piece in split_message(text, turn.chat.limit):
            try:
                await turn.chat.send(piece)
                turn.sent += 1
            except Exception as error:
                log.warning(f"turn {turn.turn_id}: could not send to {turn.chat.label}: {error}")
                return
        if not system and not turn.protected:
            turn.protected = True          # delivered: the turn stays in her history (the stage's TTS-start rule)
            await self.service.brain.protect()


# ───────────────────────────── the bots ─────────────────────────────
class Bot:
    platform = ""

    def __init__(self, service):
        self.service = service
        self.applied = None             # (token, allowed) in effect, so a config save only restarts what changed
        self.allowed = set()
        self.task = None
        self.hinted = {}
        self.status = {"enabled": False, "connected": False, "name": "", "error": ""}

    def hint_due(self, user_id):
        now = time.monotonic()
        if now - self.hinted.get(user_id, -HINT_EVERY_S) < HINT_EVERY_S:
            return False
        self.hinted[user_id] = now
        return True

    def hint_text(self, user_id):
        return (f"I only answer people on my allow list. Your {TITLES[self.platform]} user ID is {user_id} — "
                f"add it on OpenAmika's Setup page (Text her) and say hi again.")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None
        self.status = {"enabled": False, "connected": False, "name": "", "error": ""}

    async def chat_from_ref(self, ref):
        return None


class DiscordBot(Bot):
    platform = "discord"

    def __init__(self, service):
        super().__init__(service)
        self.client = None

    async def start(self, token, allowed):
        await self.stop()
        self.allowed = set(allowed)
        self.status = {"enabled": True, "connected": False, "name": "", "error": ""}
        if discord is None:
            self.status["error"] = "discord.py is not installed in the speech python (pip install discord.py)"
            return
        intents = discord.Intents.default()      # DMs and @mentions carry their text without the privileged intent
        client = discord.Client(intents=intents)
        self.client = client

        @client.event
        async def on_ready():
            self.status.update(connected=True, name=f"@{client.user.name}", error="")
            log.info(f"discord: online as @{client.user.name} ({client.user.id})")

        @client.event
        async def on_message(message):
            try:
                await self._on_message(message)
            except Exception as error:
                log.warning(f"discord: message handling failed: {error}")

        self.task = asyncio.create_task(self._run(token))

    async def _run(self, token):
        try:
            await self.client.start(token)
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure:
            self.status.update(connected=False, error="token rejected by Discord — reset it in the Developer Portal")
        except discord.PrivilegedIntentsRequired:
            self.status.update(connected=False, error="Discord refused the intents (none privileged are asked for)")
        except Exception as error:
            self.status.update(connected=False, error=f"{type(error).__name__}: {str(error)[:120]}")
        log.info(f"discord: stopped{(' — ' + self.status['error']) if self.status['error'] else ''}")

    async def stop(self):
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        await super().stop()

    async def _on_message(self, message):
        if message.author.bot or self.client is None:
            return
        text = message.content or ""
        if message.guild is not None:
            if self.client.user not in message.mentions:
                return
            text = re.sub(rf"<@!?{self.client.user.id}>", " ", text)
        if message.author.id not in self.allowed:
            if self.hint_due(message.author.id):
                await message.channel.send(self.hint_text(message.author.id))
            return
        text = incoming_text(text)
        if not text:
            return
        where = "DM" if message.guild is None else f"#{getattr(message.channel, 'name', message.channel.id)}"
        chat = DiscordChat(message.channel, f"Discord {where} with @{message.author.name}")
        await self.service.remember_chat(chat)
        await self.service.outbox.put(chat, text, "discord")

    async def chat_from_ref(self, ref):
        if self.client is None or not self.status.get("connected"):
            return None
        channel = self.client.get_channel(int(ref["chat_id"]))
        if channel is None:
            channel = await self.client.fetch_channel(int(ref["chat_id"]))
        return DiscordChat(channel, ref.get("label") or "Discord")


class TelegramBot(Bot):
    """The Bot API over plain HTTPS: long-polling getUpdates, sendMessage, sendChatAction. No library."""
    platform = "telegram"

    def __init__(self, service):
        super().__init__(service)
        self.token = ""
        self.session = None

    async def api(self, method, **params):
        if self.session is None:
            raise RuntimeError("Telegram bot is not running")
        url = TELEGRAM_API.format(token=self.token, method=method)
        async with self.session.post(url, json={k: v for k, v in params.items() if v is not None}) as response:
            return await response.json(content_type=None)

    async def start(self, token, allowed):
        await self.stop()
        self.token, self.allowed = token, set(allowed)
        self.status = {"enabled": True, "connected": False, "name": "", "error": ""}
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75))
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        await super().stop()
        session, self.session = self.session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _run(self):
        offset = None
        try:
            while True:                       # Telegram unreachable (no network yet, a blocked host): keep trying
                try:
                    me = await self.api("getMe")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.status.update(connected=False, error=f"Telegram unreachable: {str(error)[:100]}")
                    await asyncio.sleep(15)
                    continue
                if me.get("ok"):
                    break
                if me.get("error_code") == 401:
                    self.status.update(error="token rejected by Telegram — get a new one from @BotFather")
                    return
                self.status.update(connected=False, error=f"Telegram: {me.get('description') or me}")
                await asyncio.sleep(15)
            self.status.update(connected=True, name=f"@{me['result'].get('username') or 'bot'}", error="")
            log.info(f"telegram: online as {self.status['name']}")
            while True:
                try:
                    batch = await self.api("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.status.update(connected=False, error=f"Telegram unreachable: {str(error)[:100]}")
                    await asyncio.sleep(5)
                    continue
                if not batch.get("ok"):
                    code = batch.get("error_code")
                    self.status.update(connected=False, error=(
                        "another program is polling this bot token (409) — stop it or make a new bot" if code == 409
                        else f"Telegram: {batch.get('description') or batch}"))
                    await asyncio.sleep(10)
                    continue
                self.status.update(connected=True, error="")
                for update in batch.get("result") or []:
                    offset = int(update.get("update_id", 0)) + 1
                    try:
                        await self._on_update(update)
                    except Exception as error:
                        log.warning(f"telegram: update handling failed: {error}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.status.update(connected=False, error=f"{type(error).__name__}: {str(error)[:120]}")
        log.info(f"telegram: stopped{(' — ' + self.status['error']) if self.status['error'] else ''}")

    async def _on_update(self, update):
        message = update.get("message") or {}
        text, sender, chat = message.get("text"), message.get("from") or {}, message.get("chat") or {}
        if not text or sender.get("is_bot") or chat.get("id") is None:
            return
        user_id, chat_id = int(sender.get("id") or 0), int(chat["id"])
        command = text.strip().split()[0].lower() if text.strip().startswith("/") else ""
        if user_id not in self.allowed:
            if command in ("/start", "/id") or self.hint_due(user_id):
                await self.api("sendMessage", chat_id=chat_id, text=self.hint_text(user_id))
            return
        if command == "/id":
            await self.api("sendMessage", chat_id=chat_id, text=f"Your Telegram user ID is {user_id}.")
            return
        if command == "/start":
            await self.api("sendMessage", chat_id=chat_id, text="Hi — text me anything.")
            return
        text = incoming_text(text)
        if not text:
            return
        name = sender.get("username") or sender.get("first_name") or str(user_id)
        label = f"Telegram {'chat' if chat.get('type') == 'private' else chat.get('type', 'group')} with @{name}"
        chat_obj = TelegramChat(self, chat_id, label)
        await self.service.remember_chat(chat_obj)
        await self.service.outbox.put(chat_obj, text, "telegram")

    async def chat_from_ref(self, ref):
        if self.session is None or not self.status.get("connected"):
            return None
        return TelegramChat(self, int(ref["chat_id"]), ref.get("label") or "Telegram")


# ───────────────────────────── the service ─────────────────────────────
class Service:
    def __init__(self):
        self.cfg = load_config()
        self.cfg_stamp = self._stamp()
        self.brain = BrainLink(self)
        self.outbox = Outbox(self)
        self.bots = {"discord": DiscordBot(self), "telegram": TelegramBot(self)}

    @staticmethod
    def _stamp():
        try:
            return CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            return None

    async def apply(self):
        """Start, stop or restart each bot so it matches the config (only what changed is touched)."""
        for platform, bot in self.bots.items():
            block = self.cfg[platform]
            want = (block["token"], tuple(block["allowed_users"])) if block["enabled"] and block["token"] else None
            if want == bot.applied:
                continue
            bot.applied = want
            if want:
                log.info(f"{platform}: starting ({len(want[1])} allowed user(s))")
                await bot.start(want[0], want[1])
            else:
                log.info(f"{platform}: off")
                await bot.stop()

    async def watch_config(self):
        """A config/messengers.json edited by hand is picked up within a few seconds."""
        while True:
            await asyncio.sleep(3)
            stamp = self._stamp()
            if stamp != self.cfg_stamp:
                self.cfg_stamp = stamp
                fresh = load_config()
                fresh["state"] = self.cfg["state"]       # the running service owns the state block
                self.cfg = fresh
                log.info("config: reloaded from disk")
                await self.apply()

    def save(self):
        save_config(self.cfg)
        self.cfg_stamp = self._stamp()

    async def remember_chat(self, chat):
        ref = chat.ref()
        if ref and self.cfg["state"].get("last_chat") != ref:
            self.cfg["state"]["last_chat"] = ref
            self.save()

    async def last_chat(self):
        ref = self.cfg["state"].get("last_chat")
        bot = self.bots.get((ref or {}).get("platform"))
        if not ref or bot is None:
            return None
        try:
            return await bot.chat_from_ref(ref)
        except Exception as error:
            log.warning(f"last chat ({ref.get('label')}) is not reachable: {error}")
            return None

    def bot_status(self):
        return {platform: dict(bot.status) for platform, bot in self.bots.items()}

    def health(self):
        bots = self.bot_status()
        return {"app": APP, "component": "messengers", "ok": True, "brain": self.brain.connected,
                "bots": bots, "light": light(self.brain.connected, bots)}

    def status(self):
        return {"ok": True, **public_view(self.cfg), "live": self.bot_status(), "brain": self.brain.connected,
                "light": light(self.brain.connected, self.bot_status()),
                "discord_library": discord is not None, "config_path": str(CONFIG_PATH)}


@web.middleware
async def local_only(request, handler):
    """local_guard: refuse foreign pages and rebinding hosts."""
    why = local_guard.refusal(request.headers.get("Host"), request.headers.get("Origin"))
    if why:
        log.info(f"refused {request.method} {request.path}: {why}")
        return web.json_response({"ok": False, "error": "Forbidden: use the OpenAmika page."}, status=403)
    return await handler(request)


def build_app(service: Service) -> web.Application:
    app = web.Application(client_max_size=64 * 1024, middlewares=[local_only])

    async def health(_request):
        return web.json_response(service.health())

    async def status(_request):
        return web.json_response(service.status())

    async def config(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON."}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "A JSON object is required."}, status=400)
        service.cfg = apply_update(service.cfg, body)
        service.save()
        await service.apply()
        await asyncio.sleep(1.5)            # long enough for a rejected token to show in the answer
        return web.json_response(service.status())

    async def text(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON."}, status=400)
        message = incoming_text((body or {}).get("text") if isinstance(body, dict) else "")
        if not message:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)
        chat = LocalChat()
        await service.outbox.put(chat, message, "local")
        with contextlib.suppress(asyncio.TimeoutError):          # queued behind other turns, then her own time
            await asyncio.wait_for(chat.finished.wait(), 2 * TURN_TIMEOUT_S)
        return web.json_response({"ok": True, "replies": chat.replies, "finished": chat.finished.is_set()})

    app.router.add_get("/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/config", config)
    app.router.add_post("/api/text", text)
    return app


async def main():
    service = Service()
    runner = web.AppRunner(build_app(service), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info(f"messengers on http://{HOST}:{PORT} · brain {HANE_URL} · config {CONFIG_PATH}")
    tasks = [asyncio.create_task(service.outbox.run()), asyncio.create_task(service.watch_config())]
    await service.apply()
    await service.brain.start()
    try:
        await asyncio.gather(*tasks)
    finally:
        for bot in service.bots.values():
            await bot.stop()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
