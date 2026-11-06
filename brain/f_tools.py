"""OpenAmika tools layer — MCP client + capability block + post-speech clerk + agent job relay.

Design (docs/TOOLS.md):
  * Users register MCP servers (stdio command or streamable-HTTP URL) in config/tools.json,
    editable from the UI "Tools" page. Discovery runs over the official `mcp` SDK. Personal
    task agents (Hermes Agent, nanoMuse) come in through servers/openamika-agents, which exposes
    every agent as the same four tools: ask / status / answer / stop.
  * She decides, the clerk transcribes. Every enabled server is described to her, in character,
    in a [你手边的工具] block inside both the surface and the deep prompt (capability_block). She
    answers exactly as she always does — agreeing, refusing, stalling. Nothing runs before she has
    spoken.
  * After the turn's text is final, a small metered "clerk" call (after_turn → phase "tools", on
    the configured clerk_role model) reads what the user asked and what she said out loud, and
    outputs only the calls she committed to. Each call must quote the user's own words that
    constitute the request; the quote is verified against the user's text in code, so neither
    she nor the clerk can start anything the user did not ask for this turn. Proactive turns
    (idle reflection, injected cues) never run the clerk.
  * Agent jobs run in the bridge; a poller relays their progress, questions, approval requests
    and the final report back through her (an injected turn, "personality relay"). An agent's
    approval request is released only by the user's words on a later turn (clerk, quote-verified)
    or by a click on the Use page ("Her helpers") — she has no approve path of her own.
  * Per-tool policy for plain MCP servers: "auto" executes, "deny" hides the tool from the clerk,
    "confirm" parks the call as PENDING for the Use page. Agent bridge tools are auto by design:
    their consent lives in the quote check.
  * Health: every agent bridge has a `health` tool (is the agent process up, is the key accepted?).
    A background thread probes it every _HEALTH_TTL seconds; the result shows on the Tools page, goes
    into her capability block ("联系不上") so she does not promise what cannot run, and is re-checked
    right before every `ask` — an ask to an unreachable agent is not sent, she tells the user instead.
  * Autostart: an agent server may carry `autostart` (its own start command). When the probe finds the
    agent unreachable, the brain launches that command detached (so it outlives the brain), reports
    "starting" for a grace period, and gives up after a few attempts (Start button on the Tools page
    resets the budget). The clerk waits a moment for a just-launched agent before giving up on an ask.
    Its paths may use %VARIABLES% and be relative to the OpenAmika folder (launch_path, as in launch.py).
  * Open turns (she speaks on her own after his request, e.g. "fine, having Hermes look it up"): the
    clerk runs with the user's most recent message as the only quote source, at most once per message,
    and never takes approvals/answers from such a turn (webui._tools_after_turn decides when).
  * A result reaches her two ways at once: as an injected turn (spoken at the first quiet moment) and, until
    she has told him, as a "刚送到" entry in her capability block — so a user turn that comes first already
    knows it. The clerk reports which results she conveyed (`told`); a relay she no longer needs is dropped.
  * Delivery is durable: a relay becomes an injected turn, but the speech gateway drops a proactive turn
    when the user talks over it (it is then pruned from history as never spoken). The poller watches each
    relay turn and queues the note again when the turn vanished unspoken (_check_deliveries).
  * Honesty guard: the clerk also flags a concrete result she attributed to a helper that has not reported
    one ("fabricated"); the brain tells her at once to take it back, and a late real result tells her to
    correct herself.
  * IoT needs the accepted IoT safety statement (docs/IOT_DISCLAIMER.md; version and rules in
    iot_disclaimer.py). A smart-home server (the openamika-iot bridge, Home Assistant's MCP server, any
    "kind": "iot") is never started or connected before the current version was accepted on the Tools
    page: the hub refuses it on every path, a config that switches one on is refused (409), and a new
    version holds IoT off again until it is accepted.
  * Everything here is fail-quiet: a broken config, a dead MCP server or a missing `mcp`
    package must never break a conversation turn.

config/tools.json (gitignored; see config/tools.example.json):
{
  "enabled": true,
  "clerk_role": "surface",
  "limits": {"max_calls_per_turn": 3, "call_timeout_s": 20, "poll_interval_s": 2.5},
  "mcpServers": {
    "hermes": {"command": "python", "args": ["-m", "openamika_agents", "hermes"],
               "cwd": "../servers/openamika-agents", "env": {"HERMES_API_KEY": "…"},
               "enabled": true, "kind": "agent",
               "persona_note": "一个会替你跑腿的助手程序……"}
  }
}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import iot_disclaimer

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "tools.json"
IOT_BLOCKED = "iot_disclaimer"    # why a smart-home server is held off: the IoT safety statement was not accepted


class IotNotAccepted(RuntimeError):
    """A smart-home server may not start or stay connected: the current IoT safety statement was not accepted."""


def iot_held(name: str, scfg, cfg=None) -> bool:
    """True when `name` is a smart-home server (the IoT bridge, Home Assistant, any "kind": "iot") and the current IoT
    safety statement (docs/IOT_DISCLAIMER.md, brain/iot_disclaimer.py) is not accepted in config/tools.json."""
    return iot_disclaimer.is_smart_home(name, scfg) and not iot_disclaimer.accepted(_config() if cfg is None else cfg)

_AUTO_PREFIXES = ("list_", "get_", "read_", "search_", "query_", "health", "status", "describe_")
AGENT_TOOLS = frozenset({"ask", "status", "answer", "stop"})
TERMINAL = ("done", "failed", "stopped")
_STATE_ZH = {"running": "进行中", "waiting_approval": "等他批准", "waiting_question": "等他回答",
             "done": "办完了", "failed": "没办成", "stopped": "叫停了"}
AGENT_SYSTEM_TOOLS = frozenset({"status", "health"})   # the brain's own plumbing on an agent bridge; never offered to the clerk
UI_ONLY_TOOLS = frozenset({"permit_join", "discovery"})   # the Smart home panel's controls (pairing); she never sees them
_IOT_SNAPSHOT = {}                # smart-home server -> {"at", "devices": {friendly_name: {nickname, note, state, …}}}
# Her outbox. Every command she gives a helper is first "typed" in her stage bubble at the bubble's own speed
# (ui/use.js typewrite: 26 ms a character, at most 2.6 s) and sent the moment the typing is done; the helper then
# runs it at once. One command at a time, across turns: "on, off, on" can no longer hit a plug within 0.2 s (2026-10-25).
TYPE_MS_PER_CHAR = 26
TYPE_MS_MAX = 2600
_SEND_LOCK = threading.Lock()
_RECENT_USER = deque(maxlen=4)    # (turn_id, text, ts) of his last messages: a follow-up may finish the previous request
_HEALTH_TTL = 30.0                # seconds a health probe stays fresh
_HEALTH_TIMEOUT = 8.0             # a `health` call through the bridge must answer within this
_HEALTH_ZH = {"unreachable": "它那边的程序没开（连不上）", "timeout": "它没有响应", "unauthorized": "钥匙不对（API key / token 被拒）",
              "http_error": "它回了错误", "starting": "它正在启动、还没准备好", "start_failed": "它启动失败了（看 logs/agent-*.log）",
              "refused": "它在线，但拒绝接活（Tools 页上有它的说明）",
              "no_model": "它在线，但它自己的模型没配好（没有可用的 API key），什么事都办不了",
              "degraded": "它连上了一半（比如 MQTT 通了、zigbee2mqtt 没在线），现在不一定能用",
              "bridge": "桥接程序没反应", "error": "它那边出了错"}
# a job that died because the agent could not be reached (not one the agent refused to do: "Hermes refused POST
# /v1/runs: HTTP 429", nanoMuse's "The model provider refused the API key" are answers from a running agent)
_CONNECT_ERROR_RE = re.compile(r"ConnectError|ConnectTimeout|10061|(?:actively|connection) refused|lost contact|"
                               r"nothing answers|no reply from", re.I)
_AUTOSTART_GRACE = 90.0           # seconds an agent gets to come up after we launched it
_AUTOSTART_MIN_GAP = 120.0        # never relaunch more often than this
_AUTOSTART_MAX_ATTEMPTS = 3       # per enable cycle (reset by a config save, the Start button, or the agent coming up)
_ASK_START_WAIT = 25.0            # the clerk waits this long for a just-launched agent before giving up on an ask

_LOG = deque(maxlen=120)          # {ts, turn_id, server, tool, arguments, ok, result, ms, policy}
_PENDING = {}                     # id -> {id, ts, turn_id, server, tool, arguments}  (confirm-policy MCP calls)
_JOBS = {}                        # "server:job" -> job record (agent delegations)
_HEALTH = {}                      # server -> {"ok", "reason", "detail", "version", "at"}  (agent probes, see probe_health)
_AUTOSTART = {}                   # server -> {"attempts", "last", "proc", "cmd", "started_at", "error"}  (see start_agent)
_health_wake = threading.Event()  # set to make the health loop probe now (config saved, Start button)
_RELAY_QUEUE = deque()            # (note, context, reply_lang, attempts) waiting for a quiet moment to be spoken
_DELIVERIES = []                  # relay turns started and not yet known to be spoken (see _check_deliveries)
_DIALOGUE = deque(maxlen=200)     # the tool dialogue: what she told her helpers and what they said back (Use page)
_DELIVERY_MAX_ATTEMPTS = 6
_DELIVERY_EXPIRE_S = 900.0
_g = {}                           # {"globals": <webui globals dict>}

_cfg_sig = None
_cfg_cache = None
_cfg_lock = threading.Lock()
_connect_fail_until = {}          # server -> monotonic time before which we do not retry connecting
_poll_thread = None
_poll_lock = threading.Lock()
_health_thread = None


def _globals() -> dict:
    return _g.get("globals") or {}


def _config():
    """mtime-gated load of config/tools.json with last-good retention."""
    global _cfg_sig, _cfg_cache
    default = {"enabled": False, "clerk_role": "surface", "limits": {}, "mcpServers": {}}
    with _cfg_lock:
        try:
            stat = CONFIG_PATH.stat()
            sig = (stat.st_mtime, stat.st_size)
        except OSError:
            _cfg_sig, _cfg_cache = None, None
            return default
        if sig == _cfg_sig and _cfg_cache is not None:
            return _cfg_cache
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                raw.setdefault("enabled", False)
                raw.setdefault("clerk_role", "surface")
                raw.setdefault("limits", {})
                raw.setdefault("mcpServers", {})
                _cfg_sig, _cfg_cache = sig, raw
        except Exception as error:
            print(f"[tools] config unreadable, keeping last good: {error}")
    return _cfg_cache or default


def _save_config(doc: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    global _cfg_sig
    with _cfg_lock:
        _cfg_sig = None  # force reload


# Secrets in GET /api/build_f/tools are masked; a save that sends the mask back keeps the stored value.
SECRET_MASK = "•••••• saved: leave this text to keep it"
_SECRET_NAME = re.compile(r"key|token|secret|passw|auth|bearer|cookie|credential", re.I)
_NOT_SECRET = re.compile(r"_(?:file|path|dir)$", re.I)   # NANOMUSE_TOKEN_FILE names a file, not the token


def _secret_maps(scfg):
    """(where, map) for each name→value map of a server entry that may hold a secret."""
    if not isinstance(scfg, dict):
        return
    for where in ("env", "headers"):
        if isinstance(scfg.get(where), dict):
            yield where, scfg[where]
    auto = scfg.get("autostart")
    if isinstance(auto, dict) and isinstance(auto.get("env"), dict):
        yield "autostart.env", auto["env"]


def public_config(cfg: dict) -> dict:
    """Copy of tools.json with secret values masked."""
    doc = json.loads(json.dumps(cfg))
    for scfg in (doc.get("mcpServers") or {}).values():
        for _where, values in _secret_maps(scfg):
            for name, value in values.items():
                if value and _SECRET_NAME.search(str(name)) and not _NOT_SECRET.search(str(name)):
                    values[name] = SECRET_MASK
    return doc


def restore_secrets(doc: dict, current: dict):
    """Replace masks in doc with the stored values from current."""
    saved = current.get("mcpServers") or {}
    for server, scfg in (doc.get("mcpServers") or {}).items():
        before = dict(_secret_maps(saved.get(server)))
        for where, values in _secret_maps(scfg):
            for name, value in values.items():
                if value == SECRET_MASK:
                    old = (before.get(where) or {}).get(name)
                    if not old:
                        raise ValueError(f"Server {server!r}: re-enter {name} (no saved value to keep).")
                    values[name] = old


def _limits():
    lim = _config().get("limits") or {}

    def num(key, default, lo, hi):
        try:
            return min(hi, max(lo, float(lim.get(key, default))))
        except (TypeError, ValueError):
            return default
    return {"max_calls": int(num("max_calls_per_turn", 3, 1, 10)),
            "call_timeout": num("call_timeout_s", 20, 3, 120),
            "poll_interval": num("poll_interval_s", 2.5, 1, 30)}


_EVENT_KINDS = {"progress": "progress", "text": "text", "question": "question", "approval": "approval",
                "done": "result", "failed": "error", "stopped": "stopped"}


def _say(server: str, who: str, kind: str, text, job=None, **extra) -> dict:
    """One line of the tool dialogue. who: amika (her words to the helper) · agent (the helper's words back) ·
    you (a Tools-page click) · system (the brain itself: not sent, retracted…)."""
    entry = {"ts": round(time.time(), 3), "server": server, "job": job, "who": who, "kind": kind,
             "text": ("" if text is None else str(text))[:2000]}
    entry.update({k: v for k, v in extra.items() if v is not None})
    _DIALOGUE.append(entry)
    return entry


def _typing_ms(text) -> int:
    """How long her bubble takes to type `text` (the same rule as ui/use.js: the shown line, ≤220 characters)."""
    shown = re.sub(r"\s+", " ", str(text or "").replace("**", "").replace("__", "")).strip()[:220]
    return min(TYPE_MS_MAX, len(shown) * TYPE_MS_PER_CHAR)


def _type_and_send(server: str, line: str, send, kind: str = "ask", job=None, **extra):
    """She types `line` to the helper, then sends: send() runs the moment the typing is done.
    Returns (send()'s result, the dialogue entry). Typing is serialized — the next command starts typing only
    after this one went out — so her outbox is a queue; the helper executes each command as soon as it arrives."""
    with _SEND_LOCK:
        ms = _typing_ms(line)
        entry = _say(server, "amika", kind, line, job=job, typing_ms=ms, **extra)
        time.sleep(ms / 1000.0)
    return send(), entry


def _command_line(server: str, scfg: dict, tool: str, arguments: dict, agent: bool) -> str:
    """What she types to the helper: a task in her words for an agent, a plain instruction for a device."""
    if agent and tool == "ask":
        context = str(arguments.get("context") or "")
        return str(arguments.get("task") or "") + (f"\n（背景：{context}）" if context else "")
    if is_iot_server(server, scfg):
        device = str(arguments.get("device") or "?")
        if tool == "set_state":
            state = str(arguments.get("state") or "").lower()
            return f"toggle {device}" if state == "toggle" else f"turn {device} {state}"
        if tool == "get_state":
            return f"is {device} on?"
        if tool == "set_nickname":
            return f"call {device} “{arguments.get('nickname') or ''}”"
        if tool == "set_note":
            return f"{device} is {arguments.get('note') or ''}"
    return f"{tool} {json.dumps(arguments, ensure_ascii=False)}"


def _note_events(job: dict, events) -> None:
    """Bridge events newer than the job's cursor become dialogue lines (progress, the agent's words,
    questions, approvals, the final result or failure). A step of the bridge's own ("sent", "started"…) keeps its
    `internal` mark: the transcript shows it, the paper doll does not say it."""
    for event in events or []:
        if not isinstance(event, dict):
            continue
        kind = _EVENT_KINDS.get(str(event.get("kind") or ""))
        text = str(event.get("text") or "").strip()
        if kind is None or (not text and kind not in ("result", "error", "stopped")):
            continue
        _say(job["server"], "agent", kind, text or {"result": "（没有带回具体内容）", "error": "没说原因", "stopped": "叫停了"}[kind],
             job=job["job"], internal=True if event.get("internal") else None)


def is_agent_server(scfg: dict, tools=None) -> bool:
    if (scfg or {}).get("kind") == "agent":
        return True
    names = {t["name"] for t in (tools or [])}
    return AGENT_TOOLS <= names


def resolve_policy(server_cfg: dict, tool_name: str, tools=None) -> str:
    explicit = (server_cfg.get("tools") or {}).get(tool_name)
    if explicit in ("auto", "confirm", "deny"):
        return explicit
    if tool_name in AGENT_TOOLS and is_agent_server(server_cfg, tools):
        return "auto"          # consent for agent tools is the quote check, not a Tools-page click
    low = tool_name.lower()
    return "auto" if any(low.startswith(p) or low == p.rstrip("_") for p in _AUTO_PREFIXES) else "confirm"


def _user_name() -> str:
    name = _globals().get("USER_NAME")
    return name if isinstance(name, str) and name.strip() else "他"


# ─────────────────────────── MCP hub (asyncio thread) ───────────────────────────

class _Hub:
    """Holds live MCP sessions inside a dedicated asyncio loop thread.
    All public methods are thread-safe and swallow transport errors into status."""

    def __init__(self):
        self.loop = None
        self.thread = None
        self._sessions = {}   # name -> (AsyncExitStack, ClientSession)
        self.tools = {}       # name -> [{name, description, schema}]
        self.status = {}      # name -> {"connected": bool, "error": str|None, "at": ts}
        self._lock = threading.Lock()

    def _ensure_loop(self):
        with self._lock:
            if self.loop is not None and self.thread is not None and self.thread.is_alive():
                return
            self.loop = asyncio.new_event_loop()

            def runner():
                asyncio.set_event_loop(self.loop)
                self.loop.run_forever()

            self.thread = threading.Thread(target=runner, name="openamika-mcp", daemon=True)
            self.thread.start()

    def _run(self, coro, timeout):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @staticmethod
    def _import_mcp():
        from mcp import ClientSession, StdioServerParameters  # noqa: import guarded by caller
        from mcp.client.stdio import stdio_client
        http_client = None
        for mod, attr in (("mcp.client.streamable_http", "streamablehttp_client"),
                          ("mcp.client.streamable_http", "streamable_http_client"),
                          ("mcp.client.sse", "sse_client")):
            try:
                http_client = getattr(__import__(mod, fromlist=[attr]), attr)
                break
            except Exception:
                continue
        return ClientSession, StdioServerParameters, stdio_client, http_client

    @staticmethod
    def _command(cfg):
        """`python` means the brain's own interpreter (it has the mcp SDK); anything else as given."""
        command = str(cfg.get("command") or "")
        if command.lower() in ("python", "python3", "python.exe"):
            return sys.executable
        return command

    @staticmethod
    def _cwd(cfg):
        cwd = cfg.get("cwd")
        if not cwd:
            return None
        path = Path(cwd)
        if not path.is_absolute():
            path = (Path(__file__).resolve().parent / path).resolve()
        return str(path)

    async def _connect_async(self, name, cfg):
        from contextlib import AsyncExitStack
        ClientSession, StdioServerParameters, stdio_client, http_client = self._import_mcp()
        await self._close_async(name)
        stack = AsyncExitStack()
        try:
            if cfg.get("url"):
                if http_client is None:
                    raise RuntimeError("this mcp SDK build has no HTTP client transport")
                transport = await stack.enter_async_context(
                    http_client(cfg["url"], headers=cfg.get("headers") or None))
            else:
                if not cfg.get("command"):
                    raise RuntimeError("server needs a command (stdio) or url (http)")
                env = _server_env(name, cfg)
                kwargs = dict(command=self._command(cfg), args=list(cfg.get("args") or []), env=env)
                cwd = self._cwd(cfg)
                if cwd:
                    try:
                        params = StdioServerParameters(cwd=cwd, **kwargs)
                    except TypeError:
                        params = StdioServerParameters(**kwargs)
                else:
                    params = StdioServerParameters(**kwargs)
                transport = await stack.enter_async_context(stdio_client(params))
            read, write = transport[0], transport[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
            tools = [{"name": t.name,
                      "description": (getattr(t, "description", "") or "")[:600],
                      "schema": getattr(t, "inputSchema", None) or {}}
                     for t in (listing.tools or [])]
        except BaseException:
            try:
                await stack.aclose()
            except Exception:
                pass
            raise
        self._sessions[name] = (stack, session)
        self.tools[name] = tools
        self.status[name] = {"connected": True, "error": None, "at": time.time()}
        return tools

    async def _close_async(self, name):
        entry = self._sessions.pop(name, None)
        if entry is not None:
            try:
                await entry[0].aclose()
            except Exception:
                pass
        self.status[name] = {"connected": False, "error": self.status.get(name, {}).get("error"), "at": time.time()}

    async def _call_async(self, name, tool, arguments, timeout):
        if name not in self._sessions:
            raise RuntimeError(f"server {name!r} is not connected")
        session = self._sessions[name][1]
        result = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout)
        texts = []
        for item in (getattr(result, "content", None) or []):
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                texts.append(text)
        return ("\n".join(texts).strip() or "(no text result)"), bool(getattr(result, "isError", False))

    def _require_consent(self, name, cfg):
        """No smart-home server starts, connects or stays connected before the current IoT safety statement was
        accepted (docs/IOT_DISCLAIMER.md). Every way to a device comes through here: her turns, the health loop,
        the Smart home panel, Test, approvals and the reconnect in call()."""
        if not iot_held(name, cfg):
            return
        if name in self._sessions:
            self.disconnect(name)
        self.tools.pop(name, None)
        _HEALTH.pop(name, None)                 # no "online" left over from before, and no device list for her
        _IOT_SNAPSHOT.pop(name, None)
        self.status[name] = {"connected": False, "error": iot_disclaimer.REQUIRED_MESSAGE, "at": time.time(),
                             "blocked": IOT_BLOCKED}
        raise IotNotAccepted(iot_disclaimer.REQUIRED_MESSAGE)

    # thread-safe API
    def connect(self, name, cfg, timeout=25):
        self._require_consent(name, cfg)
        try:
            return self._run(self._connect_async(name, cfg), timeout)
        except BaseException as error:  # includes concurrent.futures.TimeoutError
            self.status[name] = {"connected": False, "error": str(error)[:300], "at": time.time()}
            self.tools.pop(name, None)
            raise RuntimeError(str(error)) from None

    def disconnect(self, name, timeout=10):
        try:
            self._run(self._close_async(name), timeout)
        except BaseException:
            pass

    def ensure_connected(self, name, cfg):
        self._require_consent(name, cfg)
        if name in self._sessions and self.status.get(name, {}).get("connected"):
            return self.tools.get(name, [])
        return self.connect(name, cfg)

    def call(self, name, tool, arguments, timeout):
        self._require_consent(name, (_config().get("mcpServers") or {}).get(name) or {})
        try:
            return self._run(self._call_async(name, tool, arguments, timeout), timeout + 5)
        except BaseException as error:
            # one reconnect-and-retry: stdio servers die easily (sleep, crash)
            cfg = (_config().get("mcpServers") or {}).get(name) or {}
            try:
                self.connect(name, cfg)
                return self._run(self._call_async(name, tool, arguments, timeout), timeout + 5)
            except BaseException:
                raise RuntimeError(str(error)[:300]) from None


HUB = _Hub()


def _server_env(name: str, cfg: dict) -> dict:
    """The environment a stdio server starts with: the brain's, then the entry's own `env`. A smart-home server also
    gets the IoT safety statement version accepted in config/tools.json (the bridge refuses to start without it);
    no other server, and no inherited or hand-written value, carries that variable."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()}}
    env.pop(iot_disclaimer.ENV_VAR, None)
    if iot_disclaimer.is_smart_home(name, cfg):
        version = (iot_disclaimer.acceptance(_config()) or {}).get("version")
        if version:
            env[iot_disclaimer.ENV_VAR] = version
    return env


def _active_tools(cfg):
    """[(server, server_cfg, tool_dict, policy)] for enabled servers, policy != deny.
    A server that fails to connect is left alone for 60 s so a dead server cannot slow every turn.
    A smart-home server held off by the IoT safety statement is simply left out (the hub refuses it)."""
    out = []
    now = time.monotonic()
    for name, scfg in (cfg.get("mcpServers") or {}).items():
        if not isinstance(scfg, dict) or not scfg.get("enabled"):
            continue
        if _connect_fail_until.get(name, 0) > now and not HUB.status.get(name, {}).get("connected"):
            continue
        try:
            tools = HUB.ensure_connected(name, scfg)
            _connect_fail_until.pop(name, None)
        except IotNotAccepted:
            continue
        except Exception as error:
            _connect_fail_until[name] = now + 60
            print(f"[tools] {name}: connect failed: {error}")
            continue
        for tool in tools:
            if tool["name"] in UI_ONLY_TOOLS:
                continue
            policy = resolve_policy(scfg, tool["name"], tools)
            if policy != "deny":
                out.append((name, scfg, tool, policy))
    return out


# ─────────────────────────── agent health ───────────────────────────

def _health_zh(record) -> str:
    return _HEALTH_ZH.get(str((record or {}).get("reason") or ""), "它那边出了点问题")


def agent_health(name: str):
    """Last known health of an agent server (None when never probed or not an agent)."""
    return _HEALTH.get(name)


def probe_health(name: str, scfg: dict | None = None, force: bool = False):
    """Ask the bridge whether the agent behind it is really up (its `health` tool). Cached for
    _HEALTH_TTL seconds unless forced. None for servers without a health tool (plain MCP servers:
    being connected is their health). Never raises."""
    tools = HUB.tools.get(name) or []
    if not any(t.get("name") == "health" for t in tools):
        return None
    if scfg is None:
        scfg = (_config().get("mcpServers") or {}).get(name) or {}
    now = time.time()
    cached = _HEALTH.get(name)
    if cached and not force and now - float(cached.get("at") or 0) < _HEALTH_TTL:
        return cached
    try:
        text, is_error = HUB.call(name, "health", {}, _HEALTH_TIMEOUT)
        doc = _parse_llm_json(text) if not is_error else None
        if not isinstance(doc, dict):
            raise RuntimeError(text[:160])
        record = {"ok": bool(doc.get("ok")), "reason": str(doc.get("reason") or ("ok" if doc.get("ok") else "error")),
                  "detail": str(doc.get("detail") or "")[:300], "version": doc.get("version"), "at": now,
                  "note": str(doc.get("note") or "")[:300] or None}   # e.g. "using session X instead of the configured one"
    except Exception as error:
        record = {"ok": False, "reason": "bridge", "version": None, "at": now,
                  "detail": f"the {name} bridge did not answer the health check: {str(error)[:160]}"}
    if record["ok"]:
        _AUTOSTART.pop(name, None)            # it is up: a fresh start budget for the next time it dies
    elif record["reason"] in ("unreachable", "timeout"):
        record = _after_unreachable(name, scfg, record)
    if record["ok"] != (cached.get("ok") if cached else None):
        print(f"[tools] {name}: {'online' if record['ok'] else 'UNREACHABLE'} — {record['detail']}")
    _HEALTH[name] = record
    return record


def mark_unreachable(name: str, detail: str):
    """A job just died of a connection error: reflect it at once instead of waiting for the next probe."""
    _HEALTH[name] = {"ok": False, "reason": "unreachable", "detail": detail[:300], "version": None, "at": time.time()}


# ── autostart: the brain brings the agent's own process up when it is not running ──

def autostart_config(scfg: dict):
    """Normalized {"command", "args", "cwd", "env"} from scfg["autostart"] (a dict, or one command line); None if unset."""
    raw = (scfg or {}).get("autostart")
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw.strip(), posix=False)
        except ValueError:
            parts = raw.strip().split()
        parts = [p.strip('"') for p in parts]
        raw = {"command": parts[0], "args": parts[1:]} if parts else None
    if not isinstance(raw, dict) or not str(raw.get("command") or "").strip():
        return None
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    return {"command": str(raw["command"]).strip(), "args": [str(a) for a in (raw.get("args") or [])],
            "cwd": str(raw.get("cwd") or "") or None, "env": {str(k): str(v) for k, v in env.items()}}


_OPENAMIKA = Path(__file__).resolve().parent.parent


def launch_path(value: str, command: bool = False) -> str:
    """An autostart path as it is run, by the same rule as launch.py's launch_path (the launcher starts the agents
    first, the brain restarts them): %VARIABLES% expanded (%LOCALAPPDATA%\\hermes\\bin\\hermes.exe), a relative path
    taken from the OpenAmika folder. A bare command name (`hermes`) is left to the PATH."""
    text = os.path.expandvars(str(value).strip())
    if command and not ("/" in text or "\\" in text):
        return text
    if Path(text).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith(("\\\\", "//")):
        return text
    return str(_OPENAMIKA / text)


def _agent_log_path(name: str) -> Path:
    logs = Path(__file__).resolve().parent.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"agent-{name}.log"


def _starting_record(name: str, rec: dict, now: float) -> dict:
    if rec.get("error"):
        return {"ok": False, "reason": "start_failed", "version": None, "at": now,
                "detail": f"could not start {name}: {rec['error']}"}
    proc = rec.get("proc")
    if proc is not None and proc.poll() is not None:
        return {"ok": False, "reason": "start_failed", "version": None, "at": now,
                "detail": f"{rec['cmd']} exited with code {proc.returncode} — see logs/agent-{name}.log"}
    return {"ok": False, "reason": "starting", "version": None, "at": now,
            "detail": f"starting {name} ({int(now - float(rec.get('started_at') or now))}s): {rec['cmd']}"}


def start_agent(name: str, scfg: dict, *, force: bool = False):
    """Launch the agent's own process (config `autostart`) detached from the brain, so it outlives a brain
    restart. Throttled: at most _AUTOSTART_MAX_ATTEMPTS per enable cycle, _AUTOSTART_MIN_GAP apart, never
    while a launch is still within its grace period — unless force (the Start button, which also resets
    the budget). Returns the attempt record, or None when nothing was launched (also for a smart-home server
    held off by the IoT safety statement)."""
    cfg = autostart_config(scfg)
    if cfg is None or iot_held(name, scfg):
        return None
    now = time.time()
    rec = _AUTOSTART.get(name) or {"attempts": 0, "last": 0.0, "proc": None, "cmd": "", "started_at": 0.0, "error": None}
    proc = rec.get("proc")
    if force:
        rec["attempts"] = 0
    else:
        if proc is not None and proc.poll() is None and now - float(rec.get("started_at") or 0) < _AUTOSTART_GRACE:
            return rec                                  # still coming up
        if rec["attempts"] >= _AUTOSTART_MAX_ATTEMPTS or now - float(rec.get("last") or 0) < _AUTOSTART_MIN_GAP:
            return None
    cmd = [launch_path(cfg["command"], command=True), *(os.path.expandvars(a) for a in cfg["args"])]
    rec.update({"attempts": rec["attempts"] + 1, "last": now, "started_at": now, "cmd": " ".join(cmd), "error": None})
    try:
        log = open(_agent_log_path(name), "ab")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} OpenAmika starting {name}: {rec['cmd']}\n".encode("utf-8"))
        kwargs = dict(cwd=launch_path(cfg["cwd"]) if cfg["cwd"] else None,
                      env={**os.environ, "PYTHONIOENCODING": "utf-8", **cfg["env"]},
                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            kwargs["start_new_session"] = True
        rec["proc"] = subprocess.Popen(cmd, **kwargs)
        print(f"[tools] {name}: starting agent (pid {rec['proc'].pid}): {rec['cmd']}")
    except Exception as error:
        rec["proc"], rec["error"] = None, f"{type(error).__name__}: {str(error)[:200]}"
        print(f"[tools] {name}: could not start agent: {rec['error']}")
    _AUTOSTART[name] = rec
    _HEALTH[name] = _starting_record(name, rec, now)
    return rec


def _after_unreachable(name: str, scfg: dict | None, record: dict) -> dict:
    """An unreachable probe: launch the agent when autostart is configured; report 'starting' while a launch
    is within its grace period; 'start_failed' once the budget is spent."""
    if not scfg or autostart_config(scfg) is None:
        return record
    now = time.time()
    rec = _AUTOSTART.get(name)
    if rec and rec.get("started_at") and now - float(rec["started_at"]) < _AUTOSTART_GRACE:
        return _starting_record(name, rec, now)
    launched = start_agent(name, scfg)
    if launched is not None:
        return _starting_record(name, launched, now)
    if rec and rec.get("attempts", 0) >= _AUTOSTART_MAX_ATTEMPTS:
        record = dict(record)
        record["reason"] = "start_failed"
        record["detail"] = (f"{name} did not come up after {rec['attempts']} start attempts ({rec.get('cmd')}) — "
                            f"see logs/agent-{name}.log; press Start to try again")
    return record


def _kick_health():
    _health_wake.set()


def _ensure_health_thread():
    global _health_thread
    with _poll_lock:
        if _health_thread is not None and _health_thread.is_alive():
            return
        _health_thread = threading.Thread(target=_health_loop, daemon=True, name="openamika-tools-health")
        _health_thread.start()


def is_iot_server(name: str, scfg: dict | None) -> bool:
    """The smart-home server (servers/openamika-iot): its devices are listed for her and its switch results are
    relayed to her word for word. (Which servers need the IoT safety statement is wider: iot_disclaimer.is_smart_home.)"""
    return iot_disclaimer.is_iot_bridge(name, scfg)


def refresh_iot_snapshot(name: str, devices=None):
    """Remember the smart-home server's device list (nickname, note, state): her capability block and the clerk
    read it without a call. `devices` = a list_devices reply already in hand; else the server is asked."""
    try:
        if devices is None:
            text, is_error = HUB.call(name, "list_devices", {}, _HEALTH_TIMEOUT)
            devices = _parse_llm_json(text) if not is_error else None
        if isinstance(devices, dict):
            _IOT_SNAPSHOT[name] = {"at": time.time(),
                                   "devices": {f: d for f, d in devices.items() if isinstance(d, dict)}}
    except Exception as error:
        print(f"[tools] {name}: could not read the device list: {error}")


def iot_device_lines(name: str, third: bool = False) -> list:
    """The devices as she knows them: '<nickname or name>（<note>）开着/关着'."""
    snap = _IOT_SNAPSHOT.get(name) or {}
    states = {"ON": "开着", "OFF": "关着"}
    out = []
    for fname, d in sorted((snap.get("devices") or {}).items(), key=lambda kv: (not (kv[1].get("nickname") or ""), kv[0])):
        label = (d.get("nickname") or "").strip() or fname
        note = (d.get("note") or "").strip()
        out.append(f"{label}" + (f"（{note}）" if note else "") + states.get(str(d.get("state") or "").upper(), "，状态不明"))
    return out


def _health_loop():
    """Every _HEALTH_TTL seconds: connect the enabled servers and probe the agents, so the Tools page
    and her capability block know an agent is down before anyone asks it for something."""
    while True:
        try:
            cfg = _config()
            if cfg.get("enabled"):
                seen = set()
                for name, scfg, tool, _policy in _active_tools(cfg):
                    if name in seen or tool.get("name") != "health":
                        continue
                    seen.add(name)
                    record = probe_health(name, scfg)
                    if is_iot_server(name, scfg) and record and record.get("ok"):
                        refresh_iot_snapshot(name)
        except Exception as error:
            print(f"[tools] health loop error: {error}")
        _health_wake.wait(_HEALTH_TTL)
        _health_wake.clear()


# ─────────────────────────── what she knows about her tools ───────────────────────────

def _job_line(job: dict) -> str:
    step = job.get("step") or ""
    state = _STATE_ZH.get(job.get("state"), job.get("state", ""))
    tail = f"，{step[:80]}" if step and job.get("state") == "running" else ""
    if job.get("state") not in TERMINAL and job.get("started"):
        tail += f"，已经跑了 {int(time.time() - float(job['started']))} 秒"
    return f"- {job['server']}：{job['task'][:120]}（{state}{tail}）"


def capability_block(voice: str = "self") -> str | None:
    """The [你手边的工具] block for her prompts (voice="self": surface and deep, addressed to her), or the
    [她手边的工具] block for the thinking layer (voice="third": it reasons *about* her, so it must know what
    she can really do, what is actually running, and that nothing may be invented). None when the layer
    is off or nothing is enabled."""
    cfg = _config()
    if not cfg.get("enabled"):
        return None
    tools = _active_tools(cfg)
    if not tools:
        return None
    user = _user_name()
    third = voice == "third"
    she = "她" if third else "你"
    by_server = {}
    for server, scfg, tool, _policy in tools:
        by_server.setdefault(server, (scfg, []))[1].append(tool)
    lines = ["[她手边的工具]" if third else "[你手边的工具]", f"{she}最近多了几样能替{user}办事的东西："]
    for server, (scfg, stools) in by_server.items():
        note = str(scfg.get("persona_note") or "").strip().replace("{user_name}", user)
        if is_agent_server(scfg, stools):
            default = (f"一个会替{she}跑腿的助手程序：能上网查东西、看{user}那边的电脑屏幕、读他的邮件和写邮件草稿、"
                       f"网上下单订外卖之类——把要办的事交代给它，它办完会回来报告。")
            line = f"- {server}：{note or default}"
            health = _HEALTH.get(server)
            if health and health.get("ok") is False:
                line += (f"（注意：它现在联系不上——{_health_zh(health)}。{she}不能答应用它办事，"
                         f"直接告诉{user}它现在用不了、让他去看看）")
            lines.append(line)
        else:
            names = "、".join(t["name"] for t in stools[:8])
            lines.append(f"- {server}：{note or '一组能直接用的小工具'}（{names}）")
            if is_iot_server(server, scfg):
                devices = iot_device_lines(server, third)
                if devices:
                    lines.append(f"  家里的设备（名字、{user}告诉{she}的用途、现在的状态）：" + "；".join(devices))
                lines.append(
                    f"  开关它们：{she}答应了，系统会在{she}说完后替{she}按开关，再把结果原样告诉{she}"
                    f"（DONE = 办好了，NO CHANGE = 本来就是这样，NOT DONE = 没办成、后面写着原因）。"
                    f"结果来之前{she}只能说\"我来关/我来开\"，不能说\"关好了/开了/好了\"；结果来了照实说，没办成就说没办成。"
                    f"{she}看得到上面这份设备清单，不用假装去\"找\"。")
    if third:
        lines.append(
            f"规矩：只有{user}明确开口要她办某件事、她也愿意的时候才会用。她说出口答应之后，事情才由系统替她交办出去，"
            f"所以她不能说\"已经做好了\"。帮不帮是她的自由：可以拒绝、拖着、嫌麻烦、讨价还价；他没开口要的事她不能自作主张。")
        lines.append(
            "工具的结果只会以[工具执行记录]的形式送到她面前；没收到记录就是还没有结果——指导她时绝不能替她编造查到的内容，"
            "不能让她说\"查到了/发给你了/已经办好了\"，也不能替工具回答它问他的问题。")
    else:
        lines.append(
            f"怎么用：只有{user}这一轮明确开口要你做某件事、而你也愿意的时候才会用。你说出口答应了，说完之后事情才真的开始跑"
            f"（系统替你交办），所以别说\"已经做好了\"——先应下来就行；结果回来时会有人提醒你，到时再告诉他。"
            f"要不要帮是你的自由：可以拒绝、拖着、嫌麻烦、讨价还价。他没开口要的事，你不能自作主张去做。")
        lines.append(
            "工具的结果只会以[工具执行记录]的形式送到你面前；没收到记录就是还没有结果——不许编造查到的内容，"
            "不许说\"查到了/发给你了/已经办好了\"，也不要替它回答它问他的问题。等他答了、记录来了再说。")
    now = time.time()
    running = [j for j in _JOBS.values() if j.get("state") not in TERMINAL]
    recent = [j for j in _JOBS.values() if j.get("state") in TERMINAL and now - float(j.get("updated") or 0) < 900]
    if running:
        lines.append(f"正在替他办的事（结果一个都还没回来——他问起只能说还在办；店名、地址、评分、数字一个都不能报，"
                     f"报了就是{she}编的）：")
        lines.extend(_job_line(j) for j in running[-5:])
    waiting = [j for j in running if j.get("pending")]
    if waiting:
        lines.append(f"它在等他回答（他答了{she}才能替它回）：")
        for job in waiting[-5:]:
            pending = job["pending"] or {}
            kind = "要他批准" if pending.get("kind") == "approval" else "问他"
            lines.append(f"- {job['server']}：{kind}——{str(pending.get('text') or '')[:200]}")
    fresh = [j for j in recent if (j.get("notice") or {}).get("queued") and not (j.get("notice") or {}).get("delivered")]
    recent = [j for j in recent if j not in fresh]
    if fresh:
        lines.append(f"刚送到、{user}还没听到的消息（这轮就用{she}自己的话把要点告诉他——他要是在说别的，先回他、再顺口说；"
                     f"别念原文、别报工具名）：")
        for job in fresh[-3:]:
            body = str(job.get("result") or job.get("error") or "").strip().replace("\n", " ")[:600]
            verdict = "办完了" if job.get("state") == "done" else ("没办成" if job.get("state") == "failed" else "叫停了")
            lines.append(f"- {job['server']}：\"{job['task'][:80]}\" → {verdict}：{body}")
    if recent:
        lines.append("刚结束的事（结果已经送到过）：")
        lines.extend(_job_line(j) for j in recent[-3:])
    if not running and not recent and not fresh:
        lines.append(f"眼下没有任何事在替{user}办，也没有刚办完的——所以没有任何结果可报；"
                     f"要是{she}之前好像答应过什么、这里却没有，那件事就根本没交办出去。")
    return "\n".join(lines)


# ─────────────────────────── the clerk (post-speech) ───────────────────────────

def _parse_llm_json(text):
    """Tolerant JSON object extraction: code fences, prose around the object, trailing commas."""
    if not isinstance(text, str) or not text.strip():
        return None
    body = text.strip()
    fence = re.search(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = body[start:end + 1]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            doc = json.loads(attempt, strict=False)
            return doc if isinstance(doc, dict) else None
        except Exception:
            continue
    return None


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).lower()


def quote_ok(quote, user_text: str) -> bool:
    """The clerk's quote must be the user's own words from this turn (punctuation/space-insensitive)."""
    if not isinstance(quote, str):
        return False
    normalized, haystack = _norm(quote), _norm(user_text)
    return len(normalized) >= 2 and normalized in haystack


def _clerk_prompt(tools, user: str, proactive: bool = False) -> str:
    lines = [f"你是对话系统的\"书记员\"（不是说话的角色）。她刚和{user}说完一轮话。",
             f"任务：对照【{user}这轮说的话】和【她这轮说出口的话】，判断她是否答应了替{user}去办某件需要工具的事、"
             "或者回应了某个待处理事项，把她的决定转成工具调用。你不做决定，只做记录。",
             "可用工具："]
    iot_servers = []
    for server, _scfg, tool, policy in tools:
        if tool["name"] in AGENT_SYSTEM_TOOLS and is_agent_server(_scfg, HUB.tools.get(server)):
            continue            # status / health are the brain's plumbing, not something she "uses"
        schema = json.dumps(tool.get("schema") or {}, ensure_ascii=False, separators=(",", ":"))
        if len(schema) > 500:
            schema = schema[:500] + "…"
        note = "（需用户在 Tools 页面确认后才执行）" if policy == "confirm" else ""
        lines.append(f"- {server}__{tool['name']}{note}: {tool.get('description') or '(no description)'}")
        lines.append(f"  参数 JSON Schema: {schema}")
        if is_iot_server(server, _scfg) and server not in iot_servers:
            iot_servers.append(server)
    for server in iot_servers:
        devices = iot_device_lines(server, third=True)
        if devices:
            lines.append(f"{server} 的设备（device 参数用这些名字；也可以照抄{user}的描述，系统会按名字和用途模糊匹配）："
                         + "；".join(devices))
    running = [j for j in _JOBS.values() if j.get("state") not in TERMINAL]
    recent_done = [j for j in _JOBS.values()
                   if j.get("state") == "done" and time.time() - float(j.get("updated") or 0) < 900]
    if running or recent_done:
        lines.append("job 列表（answers 里用 job 编号回应）：")
        for job in running[-6:]:
            pending = job.get("pending") or {}
            extra = ""
            if pending:
                extra = ("，正在等用户批准：" if pending.get("kind") == "approval" else "，正在等用户回答：") + str(pending.get("text") or "")[:300]
            lines.append(f"- job {job['job']}（{job['server']}）：{job['task'][:120]}，状态 {job['state']}{extra}")
        for job in recent_done[-3:]:
            lines.append(f"- job {job['job']}（{job['server']}）：{job['task'][:120]}，已办完（结果：{str(job.get('result') or '')[:160]}）"
                         "——用户若追问或补充要求，用 answers.text 继续同一段对话")
    lines += [
        "",
        "严格规则：",
        f"1. 只有当{user}这轮【明确】要求办某件事、并且她【说出口】答应了（或说正在办）时，才输出 calls。"
        "她拒绝、敷衍、拖延、岔开、没答应、只是在聊天 → calls 为空。",
        f"2. 每个 call 必须带 \"quote\"：从【{user}这轮说的话】里一字不差地抄出能证明他提出了这个要求的片段。抄不出来就不要输出这个 call。",
        f"3. 待处理事项（job 在等批准/回答）：只有{user}这轮明确同意、拒绝或回答了，才输出 answers，同样必须带 quote；"
        "approved 只在批准事项上填 true/false，回答问题时填 text。",
        "4. 她的内心想法（如果给了）只是参考，以她说出口的话为准。",
        "5. 回复必须是一个 JSON 对象，不能有任何其他文字：",
        '{"calls":[{"tool":"server__tool","arguments":{...},"quote":"…"}],'
        '"answers":[{"job":"…","text":"…或 null","approved":true 或 false 或 null,"quote":"…"}],'
        '"fabricated":{"agent":"server 名","quote":"她说出口的那句话"} 或 null,"told":["job 编号"],"note":"一句话说明判断"}',
        '什么都不该做时：{"calls":[],"answers":[],"fabricated":null,"told":[],"note":"…"}',
        f"6. ask 的 task 和 answers 的 text 一律用【{user}说话的语言】写（他说英文就写英文，不要翻成中文），"
        f"用代办程序能听懂的自然语言把事说清楚，保留他说的具体要求（地点、名字、数量等），别加他没说的要求。",
    ]
    lines.append(
        f"7. 诚实检查：她说出口的话里若替某个帮手报了【具体结果】（店名、地址、评分、价格、时间、链接等），而 job 列表里这个帮手"
        f"没有一条已办完并带结果的 job、或者她报的内容跟已办完的结果对不上，那就是她编的——把那句话原样抄进 fabricated.quote"
        f"（必须抄自【她这轮说出口的话】）。只说\"还在查/马上就有\"不算编。")
    lines.append(
        f"8. told：job 列表里已办完的 job，如果她这轮说出口的话已经把它的结果要点告诉了{user}（店名/结论等对得上），"
        f"把这些 job 编号列进 told；没提到就不列。")
    lines.append(
        f"10. 【{user}上一轮说的话】（如果给了）：{user}这轮可能只是在补充或澄清上一轮的要求（比如告诉她设备叫什么、"
        f"\"就是那个\"）。她这轮答应去办上一轮那件事时，照样输出 calls，quote 可以一字不差地抄自上一轮的话。"
        f"【最近的工具结果】里已经办成（DONE / NO CHANGE）的事不要再办；没办成（NOT DONE）的可以按新信息再办一次。")
    if iot_servers:
        lines.append(
            f"11. 开关设备的诚实检查：她说出口的话里若说某个设备已经开了/关了/好了（例如 \"Light's off\"、\"There, done\"、"
            f"\"关好了\"），而你这次的 calls 里没有对应的开关调用、【最近的工具结果】里也没有刚办成的记录，那句就是她编的——"
            f"原样抄进 fabricated.quote，fabricated.agent 填设备工具的 server 名（{'/'.join(iot_servers)}）。"
            f"她只说\"我来关/马上关\"不算编。")
    if proactive:
        lines.append(
            f"9. 这次她是主动开口（不是在回他的话）：只有当她这次明确说出口要替{user}办他上一轮要求的那件事（或说正在办）时，"
            f"才输出 calls，quote 照样必须抄自【{user}上一轮说的话】；answers 一律为空。")
    return "\n".join(lines)


_LANG_NAMES = {"en": "英文", "zh": "中文", "ja": "日文", "ko": "韩文"}


def _lang_label(reply_lang, user_text: str) -> str:
    code = str(reply_lang or "").lower()[:2]
    if code not in _LANG_NAMES:
        if re.search(r"[぀-ヿ]", user_text or ""):
            code = "ja"
        elif re.search(r"[一-鿿]", user_text or ""):
            code = "zh"
        elif re.search(r"[A-Za-z]{3,}", user_text or ""):
            code = "en"
    return f"{_LANG_NAMES.get(code, code or '未知')}（{code or '?'}）"


def _find_job(job_id: str, server: str | None = None):
    if not job_id:
        return None
    for key, job in _JOBS.items():
        if job["job"] == job_id and (server is None or job["server"] == server):
            return job
    return None


def _already_dispatched(source_turn_id: str) -> bool:
    """Did the user turn `source_turn_id` already produce a job or a parked call?"""
    hit = lambda rec: rec.get("source_turn") == source_turn_id or rec.get("turn_id") == source_turn_id
    return (any(hit(j) for j in _JOBS.values()) or any(hit(p) for p in _PENDING.values())
            or any(hit(e) for e in _LOG if e.get("policy") == "unreachable"))


def _register_job(server: str, task: str, turn_id: str, doc: dict, reply_lang=None, source_turn=None,
                  context: str = "", quote=None, asked=None):
    """asked: the dialogue entry of the request she already typed (see _type_and_send); it gets the job id
    instead of a second "ask" line."""
    job_id = str(doc.get("job") or "")
    if not job_id:
        return None
    record = {"key": f"{server}:{job_id}", "server": server, "job": job_id, "task": task,
              "state": str(doc.get("state") or "running"), "step": str(doc.get("step") or ""),
              "cursor": int(doc.get("cursor") or 0), "pending": doc.get("pending") if isinstance(doc.get("pending"), dict) else None,
              "relayed": set(), "started": time.time(), "updated": time.time(),
              "result": doc.get("result"), "error": doc.get("error"), "turn_id": turn_id,
              "source_turn": source_turn or turn_id,       # the user turn whose words authorised it
              "reply_lang": reply_lang if reply_lang in ("zh", "en", "ja") else None}
    _JOBS[record["key"]] = record
    if asked is not None:
        asked["job"] = job_id
    else:
        _say(server, "amika", "ask", task + (f"\n（背景：{context}）" if context else ""), job=job_id, quote=quote)
    _note_events(record, [e for e in (doc.get("events") or []) if isinstance(e, dict)])
    if record["state"] in TERMINAL:
        _record_agent_usage(record, doc)
        _relay_terminal(record)
    elif record["pending"]:
        _relay_pending(record)
    _ensure_poller()
    return record


def run_clerk(turn_id: str, user_text: str, spoken: str, thought: str = "", reply_lang=None, source_turn_id=None):
    """The post-speech clerk. Blocking; call after_turn() for the threaded version. Never raises.
    source_turn_id: set on an open turn (she spoke on her own) — user_text is then the user's most recent
    message, the clerk only transcribes an agreement to *that* request, once per message, and takes no
    answers/approvals from it."""
    try:
        cfg = _config()
        if not cfg.get("enabled") or not (user_text or "").strip():
            return None
        role_complete = _globals().get("F_ROLE_COMPLETE")
        if role_complete is None:
            return None
        proactive = bool(source_turn_id)
        if proactive and _already_dispatched(source_turn_id):
            return None          # his request already went out; "I'm on it" said again must not start it twice
        tools = _active_tools(cfg)
        if not tools:
            return None
        user = _user_name()
        limits = _limits()
        by_name = {f"{server}__{tool['name']}": (server, scfg, tool, policy) for server, scfg, tool, policy in tools}
        now = time.time()
        # his previous message (a follow-up like "I named it Reading Lamp, see if you can see it" finishes it)
        prev_text = ""
        if not proactive:
            earlier = [t for t in _RECENT_USER if t[0] != turn_id and now - t[2] < 300 and t[1].strip() != user_text.strip()]
            prev_text = earlier[-1][1] if earlier else ""
            if not any(t[0] == turn_id for t in _RECENT_USER):
                _RECENT_USER.append((turn_id, user_text, now))
        recent_results = [e for e in list(_LOG)[-12:] if now - float(e.get("ts") or 0) < 300
                          and e.get("policy") in ("auto", "manual") and e.get("server") != "clerk"]
        if proactive:
            content = [f"【{user}上一轮说的话】（她这次是主动开口，不是在回这句；看她是不是在答应办这件事）\n{user_text.strip()[:2000]}",
                       f"【{user}说话的语言】{_lang_label(reply_lang, user_text)}",
                       f"【她这次主动说出口的话】\n{(spoken or '').strip()[:2000] or '（她没说话）'}"]
        else:
            content = [f"【{user}这轮说的话】\n{user_text.strip()[:2000]}",
                       f"【{user}说话的语言】{_lang_label(reply_lang, user_text)}",
                       f"【她这轮说出口的话】\n{(spoken or '').strip()[:2000] or '（这轮她没说话）'}"]
            if prev_text:
                content.insert(0, f"【{user}上一轮说的话】\n{prev_text.strip()[:1000]}")
        if recent_results:
            content.append("【最近的工具结果】\n" + "\n".join(
                f"- {e.get('server')}__{e.get('tool')} {json.dumps(e.get('arguments') or {}, ensure_ascii=False)[:120]} → "
                f"{(e.get('feedback') or str(e.get('result') or ''))[:160]}" for e in recent_results[-5:]))
        if thought and thought.strip():
            content.append(f"【她的内心想法（仅参考）】\n{thought.strip()[:800]}")
        messages = [{"role": "system", "content": _clerk_prompt(tools, user, proactive)},
                    {"role": "user", "content": "\n\n".join(content)}]
        role = cfg.get("clerk_role") if cfg.get("clerk_role") in ("surface", "deep", "reflection", "memory_write") else "surface"
        try:
            result = role_complete(role, messages, phase="tools", turn_id=turn_id)
        except Exception as error:
            print(f"[tools] clerk call failed: {error}")
            return None
        doc = _parse_llm_json((result or {}).get("text"))
        if not isinstance(doc, dict):
            _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": "clerk", "tool": "parse",
                         "arguments": {}, "ok": False, "result": "unparsable clerk reply", "ms": 0, "policy": "clerk"})
            return None
        outcome = {"calls": 0, "answers": 0, "refused": 0, "unreachable": 0, "fabricated": 0, "told": 0,
                   "note": str(doc.get("note") or "")[:200]}
        calls = doc.get("calls") if isinstance(doc.get("calls"), list) else []
        iot_switched = set()       # smart-home servers that got a real call this pass (their result is on its way to her)
        device_results = {}        # server -> ["turn Reading Lamp off → DONE — …", …]: told to her in one report
        for call in calls[:limits["max_calls"]]:
            if not isinstance(call, dict):
                continue
            full = str(call.get("tool") or "")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            entry = by_name.get(full)
            if entry is None:
                _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": "clerk", "tool": full or "?",
                             "arguments": arguments, "ok": False, "result": "unknown tool", "ms": 0, "policy": "clerk"})
                continue
            server, scfg, tool, policy = entry
            if not (quote_ok(call.get("quote"), user_text) or (prev_text and quote_ok(call.get("quote"), prev_text))):
                outcome["refused"] += 1
                _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": server, "tool": tool["name"],
                             "arguments": arguments, "ok": False, "ms": 0, "policy": "refused",
                             "result": f"refused: quote not in the user's words ({str(call.get('quote'))[:60]!r})"})
                continue
            agent = is_agent_server(scfg, HUB.tools.get(server))
            if agent and (tool["name"] in ("answer", "stop") or tool["name"] in AGENT_SYSTEM_TOOLS):
                continue   # job control goes through `answers`; status/health are the brain's own plumbing
            if policy == "confirm":
                pid = uuid.uuid4().hex[:10]
                _PENDING[pid] = {"id": pid, "ts": time.time(), "turn_id": turn_id, "server": server,
                                 "source_turn": source_turn_id or turn_id,
                                 "tool": tool["name"], "arguments": arguments, "quote": call.get("quote")}
                _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": server, "tool": tool["name"],
                             "arguments": arguments, "ok": None, "result": "pending user confirmation (Tools page)",
                             "ms": 0, "policy": "confirm"})
                outcome["calls"] += 1
                continue
            if tool["name"] == "ask" and agent:
                # last look before handing anything over: a dead agent is told to him now, not after a failed run
                health = probe_health(server, scfg, force=True)
                if (health and health.get("ok") is False and health.get("reason") in ("starting", "unreachable", "timeout")
                        and autostart_config(scfg) is not None):
                    # the probe just launched it (or it is still coming up): give it a moment before giving up
                    deadline = time.time() + _ASK_START_WAIT
                    while (time.time() < deadline and health.get("ok") is False
                           and health.get("reason") in ("starting", "unreachable", "timeout")):
                        time.sleep(3)
                        health = probe_health(server, scfg, force=True)
                if health and health.get("ok") is False:
                    outcome["unreachable"] += 1
                    task = str(arguments.get("task") or "")
                    _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": server, "tool": tool["name"],
                                 "arguments": arguments, "ok": False, "ms": 0, "policy": "unreachable",
                                 "source_turn": source_turn_id or turn_id,
                                 "result": f"not sent: {health.get('detail') or 'agent unreachable'}", "quote": call.get("quote")})
                    _say(server, "system", "not_sent", f"{task[:200]} — {health.get('detail') or 'agent unreachable'}")
                    if health.get("reason") == "starting":
                        advice = f"它刚被叫起来、还没准备好。这件事没有交办出去；跟{user}说一声，过半分钟再让你办一次"
                    else:
                        advice = f"这件事没有交办出去；跟{user}说一声，让他看看{server}那边开没开"
                    _relay(f"（你刚答应把\"{task[:80]}\"交给{server}去办，但现在联系不上它——{_health_zh(health)}。{advice}）",
                           reply_lang=reply_lang)
                    continue
            outcome["calls"] += 1
            sent_at = {}

            def send(server=server, tool=tool, arguments=arguments):
                sent_at["t"] = time.perf_counter()
                try:
                    reply, is_error = HUB.call(server, tool["name"], arguments, limits["call_timeout"])
                    return reply, not is_error
                except Exception as error:
                    return f"调用失败：{error}", False

            # she types the command in her bubble first; it goes out the moment the typing is done (her outbox)
            (text, ok), asked = _type_and_send(server, _command_line(server, scfg, tool["name"], arguments, agent),
                                               send, quote=call.get("quote"))
            ms = int((time.perf_counter() - sent_at.get("t", time.perf_counter())) * 1000)
            snip = text if len(text) <= 800 else text[:800] + "…"
            # a smart-home switch answers with one fixed sentence (DONE / NO CHANGE / NOT DONE …): that, not the
            # MCP-level success, is what happened — and what she is told, word for word
            device_doc = _parse_llm_json(text) if ok and is_iot_server(server, scfg) else None
            feedback = str(device_doc.get("feedback") or "") if isinstance(device_doc, dict) else ""
            if feedback:
                ok = bool(device_doc.get("ok", ok))
            _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": server, "tool": tool["name"],
                         "arguments": arguments, "ok": ok, "result": snip[:300], "ms": ms, "policy": "auto",
                         "quote": call.get("quote"), "feedback": feedback or None})
            if feedback:
                iot_switched.add(server)
                _say(server, "agent", "result" if ok else "error", feedback)
                device_results.setdefault(server, []).append(
                    f"{_command_line(server, scfg, tool['name'], arguments, agent)} → {feedback}")
                continue
            if tool["name"] == "ask" and agent:
                parsed = _parse_llm_json(text) if ok else None
                if isinstance(parsed, dict) and parsed.get("job"):
                    _register_job(server, str(arguments.get("task") or ""), turn_id, parsed, reply_lang, source_turn_id,
                                  context=str(arguments.get("context") or ""), quote=call.get("quote"), asked=asked)
                else:
                    _relay(f"（你刚想把\"{str(arguments.get('task') or '')[:80]}\"交给{server}去办，但没交出去：{snip[:200]}。跟{user}说一声）",
                           reply_lang=reply_lang)
            else:
                _relay(f"（你刚替{user}用了{server}的{tool['name']}，结果在下面的工具执行记录里；用你自己的话告诉他，别念原文、别报工具名）",
                       f"[工具执行记录] {server}__{tool['name']} {json.dumps(arguments, ensure_ascii=False)} → {'成功' if ok else '失败'}：{snip}",
                       reply_lang=reply_lang)
        # every switch of this turn reaches her as ONE report (not one interruption per click), each line as the
        # device answered it
        for server, results in device_results.items():
            refresh_iot_snapshot(server)
            report = "；".join(f"{i}. {r}" for i, r in enumerate(results, 1)) if len(results) > 1 else results[0]
            _relay(f"（{server} 的结果，原样为准：{report}——用你自己的话简短告诉{user}：DONE 就说办好了，"
                   f"NO CHANGE 就说本来就是这样，NOT DONE 就照实说没办成、说清原因；别说结果里没有的事，别报工具名）",
                   f"[工具执行记录] {server}：" + "\n".join(results), reply_lang=reply_lang)
        # an open turn carries none of his words from after the agent asked: approvals/answers only on user turns
        answers = [] if proactive else (doc.get("answers") if isinstance(doc.get("answers"), list) else [])
        for item in answers[:limits["max_calls"]]:
            if not isinstance(item, dict):
                continue
            job = _find_job(str(item.get("job") or ""))
            if job is None:
                continue
            if job.get("state") not in TERMINAL and not job.get("pending"):
                continue   # a job that is still working takes no instructions; wait for it (the bridge refuses anyway)
            if not quote_ok(item.get("quote"), user_text):
                outcome["refused"] += 1
                _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": job["server"], "tool": "answer",
                             "arguments": {"job": job["job"]}, "ok": False, "ms": 0, "policy": "refused",
                             "result": "refused: quote not in the user's words"})
                continue
            approved = item.get("approved")
            text_answer = item.get("text") if isinstance(item.get("text"), str) else ""
            pending = job.get("pending") or {}
            if pending.get("kind") == "approval" and approved is None:
                continue   # an approval needs an explicit yes/no
            arguments = {"job": job["job"], "text": text_answer or ""}
            if pending.get("kind") == "approval":
                arguments.update({"approval_id": str(pending.get("id") or ""), "approved": bool(approved)})
            if reply_lang in ("zh", "en", "ja"):
                job["reply_lang"] = reply_lang      # the follow-up's language wins for the next relay
            _job_control(job, "answer", arguments, turn_id, quote=item.get("quote"))
            outcome["answers"] += 1
        fab = doc.get("fabricated")
        fab_agent = str((fab or {}).get("agent") or "").strip()[:40] if isinstance(fab, dict) else ""
        fab_scfg = (cfg.get("mcpServers") or {}).get(fab_agent) if fab_agent else None
        if isinstance(fab, dict) and quote_ok(fab.get("quote"), spoken) and fab_agent and is_iot_server(fab_agent, fab_scfg):
            # she said a switch was flipped, but nothing was switched: tell her the devices' real state
            if fab_agent not in iot_switched:
                quote = str(fab.get("quote") or "").strip()
                outcome["fabricated"] += 1
                _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": fab_agent, "tool": "fabrication",
                             "arguments": {}, "ok": False, "ms": 0, "policy": "fabrication",
                             "result": f"she said a switch happened that did not: {quote[:200]}"})
                _say(fab_agent, "system", "fabricated", quote[:300])
                states = "；".join(iot_device_lines(fab_agent)) or "（设备清单暂时读不到）"
                _relay(f"（你刚才跟{user}说的\"{quote[:100]}\"不是真的——这轮系统没有替你按任何开关。设备现在：{states}。"
                       f"马上跟他说清楚刚才没关成/没开成，问他要不要你现在去办）", reply_lang=reply_lang, tag="correction")
            fab = None
        if isinstance(fab, dict) and quote_ok(fab.get("quote"), spoken):
            quote = str(fab.get("quote") or "").strip()
            agent_name = str(fab.get("agent") or "").strip()[:40]
            running = [j for j in _JOBS.values() if j.get("state") not in TERMINAL
                       and (not agent_name or j["server"] == agent_name)]
            for j in running:
                j["fabricated"] = True          # the real result, when it comes, must tell her to correct herself
            state_zh = "它还在办，结果没回来" if running else "根本没有交办过这件事"
            outcome["fabricated"] += 1
            _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": agent_name or "?", "tool": "fabrication",
                         "arguments": {}, "ok": False, "ms": 0, "policy": "fabrication",
                         "result": f"she reported a result that does not exist: {quote[:200]}"})
            _say(agent_name or "?", "system", "fabricated", quote[:300])
            correction_key = running[0]["key"] if running else None
            _relay(f"（你刚才跟{user}说的\"{quote[:100]}\"不是真的——{agent_name or '帮手'}没有给过你任何结果（{state_zh}）。"
                   f"马上跟他说清楚那是你瞎编的、别再往下编；等真的结果送来再报）", reply_lang=reply_lang,
                   key=correction_key, tag="correction")
        told = doc.get("told") if isinstance(doc.get("told"), list) else []
        for job_id in told[:10]:
            job = _find_job(str(job_id or ""))
            if job is not None and job.get("state") in TERMINAL and (job.get("notice") or {}).get("queued"):
                if not job["notice"].get("delivered"):
                    _mark_delivered(job)
                    outcome["told"] += 1
        if any(outcome[k] for k in ("calls", "answers", "refused", "unreachable", "fabricated", "told")):
            print(f"[tools] clerk: {outcome}")
        return outcome
    except Exception as error:
        print(f"[tools] clerk error (ignored): {error}")
        return None


def after_turn(turn_id: str, user_text: str, spoken: str, thought: str = "", reply_lang=None, source_turn_id=None):
    """Threaded entry point used by webui after a turn's text is final (source_turn_id: see run_clerk)."""
    cfg = _config()
    if not cfg.get("enabled") or not (user_text or "").strip():
        return
    if not any(isinstance(s, dict) and s.get("enabled") for s in (cfg.get("mcpServers") or {}).values()):
        return
    threading.Thread(target=run_clerk, args=(turn_id, user_text, spoken, thought, reply_lang, source_turn_id),
                     daemon=True, name="openamika-clerk").start()


def _job_control(job: dict, tool: str, arguments: dict, turn_id: str = "", quote=None):
    """answer / stop on a job, through the bridge; updates the record from the reply."""
    limits = _limits()
    who = "you" if quote in ("Tools page", "Use page") else "amika"
    if tool == "stop":
        kind, line = "stop", ("叫停" if quote != "expired" else "没人回答，半小时后放弃了")
    elif "approved" in arguments:
        kind, line = ("approve" if arguments.get("approved") else "deny"), str((job.get("pending") or {}).get("text") or "")
    else:
        kind, line = "answer", str(arguments.get("text") or "")
    sent_at = {}

    def send():
        sent_at["t"] = time.perf_counter()
        try:
            reply, is_error = HUB.call(job["server"], tool, arguments, limits["call_timeout"])
            return reply, not is_error
        except Exception as error:
            return f"调用失败：{error}", False

    if who == "amika":
        (text, ok), _entry = _type_and_send(job["server"], line, send, kind=kind, job=job["job"])   # her outbox
    else:
        _say(job["server"], who, kind, line, job=job["job"])                                           # a click: at once
        text, ok = send()
    ms = int((time.perf_counter() - sent_at.get("t", time.perf_counter())) * 1000)
    _LOG.append({"ts": time.time(), "turn_id": turn_id, "server": job["server"], "tool": tool,
                 "arguments": arguments, "ok": ok, "result": text[:300], "ms": ms,
                 "policy": "auto", "quote": quote})
    doc = _parse_llm_json(text) if ok else None
    if isinstance(doc, dict) and not doc.get("error"):
        _apply_status(job, doc, relay=True)
    elif isinstance(doc, dict) and doc.get("error"):
        _say(job["server"], "system", "error", str(doc.get("error"))[:300], job=job["job"])
    elif not ok:
        _say(job["server"], "system", "error", text[:300], job=job["job"])
    return text, ok


# ─────────────────────────── job poller + relay ───────────────────────────

def _ensure_poller():
    global _poll_thread
    with _poll_lock:
        if _poll_thread is not None and _poll_thread.is_alive():
            return
        _poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="openamika-tools-poll")
        _poll_thread.start()


def _poll_loop():
    while True:
        try:
            _poll_once()
        except Exception as error:
            print(f"[tools] poll error: {error}")
        try:
            _drain_relays()
        except Exception as error:
            print(f"[tools] relay error: {error}")
        try:
            _check_deliveries()
        except Exception as error:
            print(f"[tools] delivery check error: {error}")
        time.sleep(_limits()["poll_interval"])


def _poll_once():
    limits = _limits()
    for job in list(_JOBS.values()):
        if job.get("state") in TERMINAL:
            if time.time() - job.get("updated", 0) > 1800:
                _JOBS.pop(job["key"], None)
            continue
        if job.get("pending") and time.time() - float(job.get("updated") or 0) > 1800:
            # nobody answered for half an hour: let it go quietly
            job["relayed_terminal"] = True
            try:
                _job_control(job, "stop", {"job": job["job"]}, job.get("turn_id", ""), quote="expired")
            except Exception:
                pass
            job["state"] = "stopped"
            continue
        try:
            text, is_error = HUB.call(job["server"], "status", {"job": job["job"], "cursor": job.get("cursor", 0)},
                                      limits["call_timeout"])
        except Exception as error:
            job["poll_errors"] = job.get("poll_errors", 0) + 1
            if job["poll_errors"] >= 5:
                job["state"], job["error"] = "failed", f"lost contact with {job['server']}: {str(error)[:120]}"
                job["updated"] = time.time()
                _say(job["server"], "system", "error", job["error"], job=job["job"])
                _relay_terminal(job)
            continue
        doc = _parse_llm_json(text) if not is_error else None
        if not isinstance(doc, dict):
            continue
        if doc.get("error") and not doc.get("state"):
            job["state"], job["error"] = "failed", str(doc.get("error"))[:200]
            job["updated"] = time.time()
            _say(job["server"], "system", "error", job["error"], job=job["job"])
            _relay_terminal(job)
            continue
        job["poll_errors"] = 0
        _apply_status(job, doc, relay=True)


def _apply_status(job: dict, doc: dict, relay: bool):
    """Merge a status snapshot into the record; relay what changed."""
    before = job.get("state")
    before_cursor = int(job.get("cursor", 0))
    job["state"] = str(doc.get("state") or job.get("state") or "running")
    job["step"] = str(doc.get("step") or job.get("step") or "")
    job["cursor"] = max(before_cursor, int(doc.get("cursor") or 0))
    job["pending"] = doc.get("pending") if isinstance(doc.get("pending"), dict) else None
    if doc.get("result") is not None:
        job["result"] = doc.get("result")
    if doc.get("error"):
        job["error"] = doc.get("error")
    job["updated"] = time.time()
    new_events = [e for e in (doc.get("events") or []) if isinstance(e, dict) and int(e.get("i") or 0) > before_cursor]
    for event in new_events:
        if event.get("kind") == "progress":
            _emit_progress(job, event.get("text") or "")
    _note_events(job, new_events)
    if job["state"] in TERMINAL and before not in TERMINAL:
        _record_agent_usage(job, doc)
    if not relay:
        return
    if job["state"] in TERMINAL:
        if before not in TERMINAL:
            _relay_terminal(job)
    elif job.get("pending"):
        _relay_pending(job)


_OR_PRICES = {"at": 0.0, "models": {}}      # OpenRouter's public price list, refreshed at most once a day
_OR_PRICES_URL = "https://openrouter.ai/api/v1/models"


def _openrouter_rates(model: str, fetch=None):
    """List price of an OpenRouter model, USD per million tokens, from OpenRouter's public model list
    (no key needed). None when the list cannot be fetched or does not know the model."""
    now = time.time()
    if now - _OR_PRICES["at"] > 86400 or not _OR_PRICES["models"]:
        try:
            if fetch is None:
                with urllib.request.urlopen(_OR_PRICES_URL, timeout=15) as response:
                    doc = json.load(response)
            else:
                doc = fetch()
            table = {}
            for item in (doc or {}).get("data") or []:
                pricing = item.get("pricing") if isinstance(item, dict) else None
                if not isinstance(pricing, dict) or not item.get("id"):
                    continue
                try:
                    rates = {"input": round(float(pricing.get("prompt") or 0) * 1e6, 6),
                             "output": round(float(pricing.get("completion") or 0) * 1e6, 6)}
                    if pricing.get("input_cache_read") not in (None, ""):
                        rates["cache_read"] = round(float(pricing["input_cache_read"]) * 1e6, 6)
                except (TypeError, ValueError):
                    continue
                table[str(item["id"])] = rates
            if table:
                _OR_PRICES.update(at=now, models=table)
        except Exception as error:
            print(f"[tools] could not fetch OpenRouter prices: {str(error)[:120]}")
            _OR_PRICES["at"] = now - 86400 + 600       # try again in ten minutes, not on every receipt
    return _OR_PRICES["models"].get(model.split("#", 1)[0])


def _borrow_rates(meter, provider: str, model: str):
    """Price a plain model id from a priced variant of the same model ("z-ai/glm-5" from the deep role's
    "z-ai/glm-5#no-reasoning"): same endpoint, same list price. The exact key is registered so the meter's
    receipt snapshot carries it."""
    try:
        table = meter.pricing.setdefault("models", {})     # the meter's own table (an empty one is still the real one)
    except Exception:
        return None
    base = model.split("#", 1)[0]
    for key, rates in table.items():
        plain = str(key).split("#", 1)[0]
        if plain in (f"{provider}:{base}", base) and isinstance(rates, dict) and rates.get("input") is not None:
            table[f"{provider}:{model}"] = dict(rates)
            return rates
    if provider == "openrouter":
        rates = _openrouter_rates(model)
        if rates:
            table[f"{provider}:{model}"] = dict(rates)   # list price; the receipt snapshot carries it
            return rates
    return None


def _record_agent_usage(job: dict, doc: dict) -> None:
    """A finished job's token usage (the bridge sums the agent's own LLM calls) goes into her ledger as
    phase "agent:<server>", priced at the meter's snapshot rates for the agent's configured model — so the
    session cost on the Use page includes what the helper spent, not only her own calls and the clerk."""
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else None
    if not usage or job.get("usage_recorded"):
        return
    meter = _globals().get("F_METER")
    if meter is None:
        return
    prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    if prompt + completion <= 0:
        return
    job["usage_recorded"] = True
    provider = str(usage.get("provider") or "openrouter").strip() or "openrouter"
    model = str(usage.get("model") or "").strip()
    if not model:
        print(f"[tools] {job['server']}: {prompt + completion} tokens spent but no model name to price them "
              f"(set HERMES_MODEL in the server's env)")
        return
    try:
        rates = meter._rates(provider, model) or _borrow_rates(meter, provider, model) or {}
        estimate = (prompt * float(rates.get("input", 0)) + completion * float(rates.get("output", 0))) / 1_000_000
        handle = meter.begin(provider, model, f"agent:{job['server']}", job.get("turn_id", ""),
                             estimated_max_usd=max(estimate, 1e-6))
        receipt = meter.finish(handle, usage={"prompt_tokens": prompt, "completion_tokens": completion}, status="complete")
        cost = receipt.get("cost_usd") if isinstance(receipt, dict) else None
        estimate_usd = receipt.get("estimated_cost_usd") if isinstance(receipt, dict) else None
        label = (f"${float(cost):.4f}" if isinstance(cost, (int, float)) else
                 f"~${float(estimate_usd):.4f} estimated at list price" if isinstance(estimate_usd, (int, float)) else "unpriced")
        print(f"[tools] {job['server']}: booked {prompt + completion} tokens on {provider}:{model} → {label}")
    except Exception as error:
        print(f"[tools] {job['server']}: could not book agent usage: {error}")


def _emit_progress(job: dict, step: str):
    socketio = _globals().get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit("tool_progress", {"server": job["server"], "job": job["job"], "task": job["task"][:120],
                                        "state": job["state"], "step": step[:160]})
    except Exception:
        pass


def _relay_pending(job: dict):
    pending = job.get("pending") or {}
    ident = str(pending.get("id") or "")
    if not ident or ident in job["relayed"]:
        return
    job["relayed"].add(ident)
    user = _user_name()
    task = job["task"][:100]
    text = str(pending.get("text") or "")[:400]
    if pending.get("kind") == "approval":
        note = (f"（{job['server']}在替你办\"{task}\"，现在需要{user}亲口批准才能继续：{text}。"
                f"用你自己的话问他要不要同意；在他明确说同意之前什么都不会发生，你也不能替他答应）")
    else:
        note = f"（{job['server']}在办\"{task}\"时有个问题要问{user}：{text}。用你自己的话替它问他）"
    _relay(note, reply_lang=job.get("reply_lang"))


def _relay_terminal(job: dict):
    if job.get("relayed_terminal"):
        return
    job["relayed_terminal"] = True
    user = _user_name()
    task = job["task"][:100]
    state = job.get("state")
    lang = job.get("reply_lang")
    job["notice"] = {"queued": True, "delivered": False}     # shown in her prompt as "刚送到" until she has told him
    if state == "done":
        result = str(job.get("result") or "").strip() or "（没有带回具体内容）"
        correction = ""
        if job.get("fabricated"):
            correction = f"注意：这是它第一次交结果——你之前跟{user}报过的店名/地址之类全是你编的，得照这份改口、跟他说明。"
        _relay(f"（{job['server']}把你交办的事办完了——\"{task}\"。结果在下面的工具执行记录里；用你自己的话把要点告诉{user}，"
               f"别念原文、别报工具名。{correction}）",
               f"[工具执行记录] {job['server']} 的报告：\n{result[:3000]}", reply_lang=lang, key=job["key"])
    elif state == "failed":
        error = str(job.get("error") or "")
        if _CONNECT_ERROR_RE.search(error):
            mark_unreachable(job["server"], f"{job['server']} could not be reached while working: {error[:160]}")
            why = f"联系不上{job['server']}（它那边的程序没开或者中途挂了）"
        else:
            why = error[:200] or "没说原因"
            # the agent is up but would not do it: ask the bridge now (it remembers a refusal), so the Tools
            # light and her capability block change at once instead of at the next scheduled probe
            threading.Thread(target=probe_health, args=(job["server"], None, True), daemon=True,
                             name="openamika-tools-reprobe").start()
        _relay(f"（{job['server']}没能办成\"{task}\"：{why}。告诉{user}，看他要不要换个办法）", reply_lang=lang, key=job["key"])
    else:
        _relay(f"（\"{task}\"这件事被叫停了，跟{user}说一声）", reply_lang=lang, key=job["key"])


def _relay(note: str, context: str = "", reply_lang=None, attempts: int = 0, key=None, tag: str = "notice"):
    _RELAY_QUEUE.append((note, context, reply_lang, attempts, key, tag))
    _drain_relays()


def _notice_delivered(key, tag: str = "notice") -> bool:
    job = _JOBS.get(key) if key else None
    if job is None:
        return False
    if tag == "correction":
        # "take it back, the helper has not answered" is wrong once the helper has: the result relay says the truth
        return job.get("state") in TERMINAL
    return bool((job.get("notice") or {}).get("delivered"))


def _discard_unspoken(turn_id: str):
    """Drop an injected relay turn that has not been spoken yet (unprotected), so she does not repeat
    what she just told him herself."""
    g = _globals()
    find, lock = g.get("_find_turn"), g.get("_hist_lock")
    cancel, discard = g.get("_cancel_turn_gen"), g.get("discard_turn")
    if find is None:
        return
    try:
        with (lock if lock is not None else contextlib.nullcontext()):
            turn = find(turn_id)
        if turn is None or turn.get("protected"):
            return
        if cancel is not None:
            cancel(turn_id, "discard")
        if discard is not None:
            discard(turn_id)
        print(f"[tools] relay turn {turn_id} dropped: she already told him")
    except Exception as error:
        print(f"[tools] could not drop relay turn {turn_id}: {error}")


def _mark_delivered(job: dict):
    """She has told him (a user turn got there first, or the relay turn was spoken): nothing more to say."""
    notice = job.setdefault("notice", {"queued": True, "delivered": False})
    notice["delivered"] = True
    key = job["key"]
    kept = [item for item in _RELAY_QUEUE if not (item[4] == key and item[5] == "notice")]
    _RELAY_QUEUE.clear()
    _RELAY_QUEUE.extend(kept)
    for item in [d for d in _DELIVERIES if d.get("key") == key]:
        _DELIVERIES.remove(item)
        _discard_unspoken(item["turn_id"])


def _drain_relays():
    g = _globals()
    start = g.get("_start_inject_gen")
    if start is None or not _RELAY_QUEUE:
        return
    sid = g.get("_LAST_SID")
    if not sid:
        return                         # nobody listening yet; keep it for when a client connects
    active = g.get("_ws_active")
    if active:
        return                         # she is mid-turn; try again on the next poll tick
    while True:
        if not _RELAY_QUEUE:
            return
        note, context, reply_lang, attempts, key, tag = _RELAY_QUEUE.popleft()
        if not _notice_delivered(key, tag):
            break                          # a result she already told him about (or a stale correction) needs no turn
    try:
        if reply_lang:
            tid = start(sid, note, context=context, reply_lang=reply_lang)
        else:
            tid = start(sid, note, context=context)
    except TypeError:
        tid = start(sid, note, context=context)    # an older webui without the reply_lang parameter
    except Exception as error:
        print(f"[tools] relay failed: {error}")
        return
    if tid:
        _DELIVERIES.append({"turn_id": tid, "note": note, "context": context, "reply_lang": reply_lang,
                            "at": time.time(), "attempts": attempts, "key": key, "tag": tag})


def _check_deliveries():
    """A relay turn is only 'delivered' once the speech gateway protected it (it started speaking it).
    When the user talks over a proactive turn the gateway drops it and the brain prunes it from history
    unspoken — the note is then queued again, so a job's result is never lost to bad timing."""
    g = _globals()
    find, lock = g.get("_find_turn"), g.get("_hist_lock")
    if find is None or not _DELIVERIES:
        return
    now = time.time()
    for item in list(_DELIVERIES):
        try:
            if lock is not None:
                with lock:
                    turn = find(item["turn_id"])
            else:
                turn = find(item["turn_id"])
        except Exception:
            turn = None
        key, tag = item.get("key"), item.get("tag") or "notice"
        if turn is not None:
            if turn.get("protected"):
                _DELIVERIES.remove(item)                  # the gateway started speaking it: delivered
                job = _JOBS.get(key) if key else None
                if job is not None and tag == "notice":
                    job.setdefault("notice", {"queued": True, "delivered": False})["delivered"] = True
            elif _notice_delivered(key, tag):
                _DELIVERIES.remove(item)                  # she told him herself meanwhile (or the correction went stale)
                _discard_unspoken(item["turn_id"])
            elif now - item["at"] > _DELIVERY_EXPIRE_S:
                _DELIVERIES.remove(item)
            continue
        _DELIVERIES.remove(item)
        if _notice_delivered(key, tag):
            continue
        if item["attempts"] + 1 >= _DELIVERY_MAX_ATTEMPTS:
            print(f"[tools] relay turn {item['turn_id']} was never spoken; giving up after {item['attempts'] + 1} attempts")
            continue
        print(f"[tools] relay turn {item['turn_id']} was never spoken (he talked over it); queuing it again")
        _RELAY_QUEUE.append((item["note"], item["context"], item["reply_lang"], item["attempts"] + 1, key, tag))


# ─────────────────────────── control routes (Tools page) ───────────────────────────

def _public_job(job: dict) -> dict:
    return {k: job.get(k) for k in ("server", "job", "task", "state", "step", "pending", "started", "updated",
                                    "result", "error", "turn_id")}


def _status_payload():
    cfg = _config()
    servers = {}
    for name, scfg in (cfg.get("mcpServers") or {}).items():
        if not isinstance(scfg, dict):
            continue
        discovered = HUB.tools.get(name, [])
        tools = [{"name": tool["name"], "description": tool.get("description") or "",
                  "policy": resolve_policy(scfg, tool["name"], discovered)} for tool in discovered]
        # a smart-home server held off by the IoT safety statement is off, whatever an older session or probe said
        blocked = IOT_BLOCKED if scfg.get("enabled") and iot_held(name, scfg, cfg) else None
        servers[name] = {
            "transport": "http" if scfg.get("url") else "stdio",
            "target": scfg.get("url") or " ".join([str(scfg.get("command") or "")] + [str(a) for a in (scfg.get("args") or [])]),
            "enabled": bool(scfg.get("enabled")),
            "connected": False if blocked else bool(HUB.status.get(name, {}).get("connected")),
            "error": iot_disclaimer.REQUIRED_MESSAGE if blocked else HUB.status.get(name, {}).get("error"),
            "health": None if blocked else _HEALTH.get(name),   # agent servers only: {ok, reason, detail, version, at}
            "autostart": ({k: v for k, v in autostart_config(scfg).items() if k != "env"}
                          if autostart_config(scfg) else None),
            "kind": "agent" if is_agent_server(scfg, discovered) else "mcp",
            "persona_note": str(scfg.get("persona_note") or ""),
            "tools": tools,
            "policies": scfg.get("tools") or {},
            # smart-home servers need the accepted IoT safety statement; "blocked" = switched on but held off for it
            "iot": iot_disclaimer.is_smart_home(name, scfg),
            "blocked": blocked,
        }
    jobs = sorted((_public_job(j) for j in _JOBS.values()), key=lambda j: j["started"] or 0)
    # "config" is tools.json (secrets masked): the page's raw editor and every save are built from it, so the
    # fields the cards do not show (env, cwd, headers) survive a click on the page.
    return {"ok": True, "enabled": bool(cfg.get("enabled")), "clerk_role": cfg.get("clerk_role") or "surface",
            "limits": cfg.get("limits") or {}, "servers": servers, "config": public_config(cfg),
            "iot_disclaimer": iot_disclaimer.status(cfg),
            "dialogue": list(_DIALOGUE)[-80:],
            "pending": sorted(_PENDING.values(), key=lambda p: p["ts"]),
            "jobs": jobs[-20:], "relay_queue": len(_RELAY_QUEUE), "log": list(_LOG)[-50:]}


def install_tools(g):
    """Register the clerk, the capability block and the /api/build_f/tools* routes on the brain app."""
    _g["globals"] = g
    g["F_TOOLS_AFTER_TURN"] = after_turn
    g["F_TOOLS_CAPABILITIES"] = capability_block
    g["F_TOOLS_PHASE"] = None          # the pre-deep phase is gone; she speaks first now

    app, request, jsonify = g["app"], g["request"], g["jsonify"]

    def action_request(action):
        if request.headers.get("Origin") or request.headers.get("X-Build-F-Action") != action:
            raise PermissionError("Use the OpenAmika page for this action.")
        value = request.get_json(silent=True) if request.is_json else None
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required.")
        return value

    @app.route("/api/build_f/tools", methods=["GET"])
    def tools_status():
        return jsonify(_status_payload())

    @app.route("/api/build_f/tools/config", methods=["POST"])
    def tools_config():
        try:
            value = action_request("tools-config")
            doc = value.get("config")
            if not isinstance(doc, dict):
                raise ValueError("Send {config: {...}} with the full tools.json document.")
            clerk_role = doc.get("clerk_role") if doc.get("clerk_role") in ("surface", "deep", "reflection", "memory_write") else "surface"
            clean = {"enabled": bool(doc.get("enabled")), "clerk_role": clerk_role,
                     "limits": doc.get("limits") if isinstance(doc.get("limits"), dict) else {},
                     "mcpServers": {}}
            servers = doc.get("mcpServers")
            if isinstance(servers, dict):
                for name, scfg in servers.items():
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or not isinstance(scfg, dict):
                        raise ValueError(f"Invalid server entry: {name!r}")
                    entry = {"enabled": bool(scfg.get("enabled"))}
                    if scfg.get("url"):
                        entry["url"] = str(scfg["url"])
                        if isinstance(scfg.get("headers"), dict):
                            entry["headers"] = {str(k): str(v) for k, v in scfg["headers"].items()}
                    else:
                        if not scfg.get("command"):
                            raise ValueError(f"Server {name!r} needs a command or url.")
                        entry["command"] = str(scfg["command"])
                        entry["args"] = [str(a) for a in (scfg.get("args") or [])]
                        if isinstance(scfg.get("env"), dict):
                            entry["env"] = {str(k): str(v) for k, v in scfg["env"].items()}
                        if scfg.get("cwd"):
                            entry["cwd"] = str(scfg["cwd"])
                    if scfg.get("kind") in ("agent", "mcp", "iot"):
                        entry["kind"] = scfg["kind"]
                    auto = scfg.get("autostart")
                    if isinstance(auto, dict) and str(auto.get("command") or "").strip():
                        entry["autostart"] = {"command": str(auto["command"]).strip(),
                                              "args": [str(a) for a in (auto.get("args") or [])]}
                        if auto.get("cwd"):
                            entry["autostart"]["cwd"] = str(auto["cwd"])
                        if isinstance(auto.get("env"), dict):
                            entry["autostart"]["env"] = {str(k): str(v) for k, v in auto["env"].items()}
                    elif isinstance(auto, str) and auto.strip():
                        entry["autostart"] = auto.strip()
                    note = scfg.get("persona_note")
                    if isinstance(note, str) and note.strip():
                        entry["persona_note"] = note.strip()[:600]
                    policies = scfg.get("tools")
                    if isinstance(policies, dict):
                        entry["tools"] = {str(k): v for k, v in policies.items() if v in ("auto", "confirm", "deny")}
                    clean["mcpServers"][name] = entry
            # IoT turns on only after the IoT safety statement was accepted (POST /api/build_f/iot/disclaimer); the
            # acceptance on disk is kept as it is, whatever the posted document says about it
            current = _config()
            restore_secrets(clean, current)
            if iot_disclaimer.turns_on_iot(clean) and not iot_disclaimer.accepted(current):
                return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE,
                                "error": iot_disclaimer.REQUIRED_MESSAGE}), 409
            if iot_disclaimer.acceptance(current):
                clean[iot_disclaimer.RECORD_KEY] = iot_disclaimer.acceptance(current)
            _save_config(clean)
            # drop connections of servers that were removed or disabled
            for name in list(HUB.status):
                scfg = clean["mcpServers"].get(name)
                if scfg is None or not scfg.get("enabled"):
                    HUB.disconnect(name)
                    HUB.tools.pop(name, None)
            _connect_fail_until.clear()
            _AUTOSTART.clear()
            _kick_health()
            return jsonify(_status_payload())
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    @app.route("/api/build_f/tools/start", methods=["POST"])
    def tools_start():
        """Start an agent's own process now (config `autostart`) — the Start button on the Tools page."""
        try:
            value = action_request("tools-start")
            name = value.get("server")
            scfg = (_config().get("mcpServers") or {}).get(name)
            if not isinstance(scfg, dict):
                raise ValueError("Unknown server.")
            if not scfg.get("enabled"):
                raise ValueError("Enable the server first.")
            if autostart_config(scfg) is None:
                raise ValueError("No start command configured for this server (autostart).")
            HUB.ensure_connected(name, scfg)
            rec = start_agent(name, scfg, force=True)
            _kick_health()
            return jsonify({"ok": True, "server": name, "health": _HEALTH.get(name),
                            "started": bool(rec and rec.get("proc") is not None)})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except IotNotAccepted as error:
            return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE, "error": str(error)}), 409
        except RuntimeError as error:
            return jsonify({"ok": False, "error": str(error)}), 502

    @app.route("/api/build_f/tools/test", methods=["POST"])
    def tools_test():
        try:
            value = action_request("tools-test")
            name = value.get("server")
            cfg = _config()
            scfg = (cfg.get("mcpServers") or {}).get(name)
            if not isinstance(scfg, dict):
                raise ValueError("Unknown server.")
            tools = HUB.connect(name, scfg)
            _connect_fail_until.pop(name, None)
            health = probe_health(name, scfg, force=True)
            return jsonify({"ok": True, "server": name, "kind": "agent" if is_agent_server(scfg, tools) else "mcp",
                            "health": health,
                            "tools": [{"name": t["name"], "description": t.get("description") or "",
                                       "policy": resolve_policy(scfg, t["name"], tools)} for t in tools]})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except IotNotAccepted as error:
            return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE, "error": str(error)}), 409
        except RuntimeError as error:
            return jsonify({"ok": False, "error": str(error)}), 502

    @app.route("/api/build_f/iot/disclaimer", methods=["POST"])
    def iot_disclaimer_accept():
        """Record that the person read and accepted the current IoT safety statement ({accept: true, version}): the
        Tools page sends it from the statement's dialog, after its "I have read and accept this" box was ticked.
        Only this route writes the acceptance; a config save keeps whatever is on disk."""
        try:
            value = action_request("iot-disclaimer")
            if value.get("accept") is not True:
                raise ValueError("Tick “I have read and accept this” first.")
            if value.get("version") != iot_disclaimer.IOT_DISCLAIMER_VERSION:
                raise ValueError(f"That is not the current IoT safety statement (version "
                                 f"{iot_disclaimer.IOT_DISCLAIMER_VERSION}); reload the Tools page and read it again.")
            doc = json.loads(json.dumps(_config()))
            doc[iot_disclaimer.RECORD_KEY] = iot_disclaimer.record()
            _save_config(doc)
            _connect_fail_until.clear()
            _kick_health()
            print(f"[tools] IoT safety statement {iot_disclaimer.IOT_DISCLAIMER_VERSION} accepted "
                  f"({doc[iot_disclaimer.RECORD_KEY]['accepted_at']})")
            return jsonify({"ok": True, "iot_disclaimer": iot_disclaimer.status(doc)})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    # ── the Smart home panel: devices, readings, naming, manual switching, pairing (servers/openamika-iot) ──
    def _iot_server():
        cfg = _config()
        for name, scfg in (cfg.get("mcpServers") or {}).items():
            if isinstance(scfg, dict) and scfg.get("enabled") and is_iot_server(name, scfg):
                return name, scfg
        return None, None

    def _iot_call(name, scfg, tool, arguments=None, timeout=12.0):
        HUB.ensure_connected(name, scfg)
        text, is_error = HUB.call(name, tool, arguments or {}, timeout)
        doc = _parse_llm_json(text) if not is_error else None
        if not isinstance(doc, dict):
            raise RuntimeError(f"{tool}: {str(text)[:200]}")
        return doc

    @app.route("/api/build_f/iot", methods=["GET"])
    def iot_panel():
        """Everything the Smart home panel shows: the server's health, every device with its nickname, note,
        state, readings and availability, and the pairing/discovery state."""
        name, scfg = _iot_server()
        consent = iot_disclaimer.status(_config())
        if not name:
            return jsonify({"ok": True, "enabled": False, "devices": [], "discovery": None, "health": None,
                            "iot_disclaimer": consent})
        if iot_held(name, scfg):
            # switched on but held off until the IoT safety statement is accepted: nothing is started or asked, and a
            # session left from before (the acceptance withdrawn while running) is dropped by the hub's gate
            try:
                HUB.ensure_connected(name, scfg)
            except Exception:
                pass
            return jsonify({"ok": True, "enabled": True, "connected": False, "server": name, "devices": [],
                            "discovery": None, "health": None, "blocked": IOT_BLOCKED, "iot_disclaimer": consent,
                            "error": iot_disclaimer.REQUIRED_MESSAGE})
        try:
            devices = _iot_call(name, scfg, "list_devices")
            refresh_iot_snapshot(name, devices)          # her device list stays as fresh as the panel's
            discovery = _iot_call(name, scfg, "discovery")
            health = probe_health(name, scfg)
        except Exception as error:
            return jsonify({"ok": True, "enabled": True, "connected": False, "server": name, "devices": [], "discovery": None,
                            "health": _HEALTH.get(name), "iot_disclaimer": consent,
                            "error": f"{type(error).__name__}: {str(error)[:200]}"})
        listing = [{"friendly_name": fname, **(info if isinstance(info, dict) else {})}
                   for fname, info in devices.items() if not str(fname).startswith(("ok", "error"))]
        listing.sort(key=lambda d: ((d.get("nickname") or d["friendly_name"]).lower()))
        return jsonify({"ok": True, "enabled": True, "connected": True, "server": name, "health": health,
                        "devices": listing, "discovery": discovery, "iot_disclaimer": consent})

    @app.route("/api/build_f/iot/device", methods=["POST"])
    def iot_device():
        """One manual action from the panel: {device, action: on|off|toggle} | {device, nickname} | {device, note}."""
        try:
            value = action_request("iot-device")
            name, scfg = _iot_server()
            if not name:
                raise ValueError("No smart-home server is enabled.")
            if iot_held(name, scfg):
                raise IotNotAccepted(iot_disclaimer.REQUIRED_MESSAGE)
            device = str(value.get("device") or "").strip()
            if not device:
                raise ValueError("Which device?")
            if "action" in value:
                action = str(value.get("action") or "").strip().lower()
                if action not in ("on", "off", "toggle"):
                    raise ValueError("action must be on, off or toggle")
                result = _iot_call(name, scfg, "set_state", {"device": device, "state": action})
                what = f"set_state {action}"
            elif "nickname" in value:
                result = _iot_call(name, scfg, "set_nickname", {"device": device, "nickname": str(value.get("nickname") or "")[:60]})
                what = "set_nickname"
            elif "note" in value:
                result = _iot_call(name, scfg, "set_note", {"device": device, "note": str(value.get("note") or "")[:300]})
                what = "set_note"
            else:
                raise ValueError("Send action, nickname or note.")
            _LOG.append({"ts": time.time(), "turn_id": "", "server": name, "tool": what.split()[0], "arguments": {"device": device},
                         "ok": bool(result.get("ok", True)), "result": json.dumps(result, ensure_ascii=False)[:300], "ms": 0,
                         "policy": "manual", "quote": "Smart home panel", "feedback": result.get("feedback")})
            refresh_iot_snapshot(name)                    # a name, note or switch from the panel: she sees it next turn
            return jsonify({"ok": True, "device": device, "result": result})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except IotNotAccepted as error:
            return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE, "error": str(error)}), 409
        except Exception as error:
            return jsonify({"ok": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}), 502

    @app.route("/api/build_f/iot/pairing", methods=["POST"])
    def iot_pairing():
        """Open or close zigbee2mqtt's pairing window from the panel: {enable: bool, seconds?: int}."""
        try:
            value = action_request("iot-pairing")
            name, scfg = _iot_server()
            if not name:
                raise ValueError("No smart-home server is enabled.")
            if iot_held(name, scfg):
                raise IotNotAccepted(iot_disclaimer.REQUIRED_MESSAGE)
            seconds = int(value.get("seconds") or 254) if value.get("enable", True) else 0
            result = _iot_call(name, scfg, "permit_join", {"seconds": seconds})
            discovery = _iot_call(name, scfg, "discovery")
            return jsonify({"ok": True, "result": result, "discovery": discovery})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except IotNotAccepted as error:
            return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE, "error": str(error)}), 409
        except Exception as error:
            return jsonify({"ok": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}), 502

    @app.route("/api/build_f/tools/approve", methods=["POST"])
    def tools_approve():
        """Approve a parked confirm-policy MCP call ({id}) or answer an agent's approval request
        ({server, job, approved}). Both are the user's explicit click."""
        try:
            value = action_request("tools-approve")
            if value.get("job"):
                job = _find_job(str(value.get("job")), str(value.get("server") or "") or None)
                if job is None:
                    raise ValueError("Unknown job.")
                pending = job.get("pending") or {}
                arguments = {"job": job["job"], "text": str(value.get("text") or ""),
                             "approval_id": str(pending.get("id") or ""), "approved": bool(value.get("approved", True))}
                text, ok = _job_control(job, "answer", arguments, job.get("turn_id", ""), quote="Tools page")
                return jsonify({"ok": ok, "result": text[:500], "job": _public_job(job)})
            pid = value.get("id")
            entry = _PENDING.pop(pid, None)
            if entry is None:
                raise ValueError("Unknown or already-handled pending call.")
            cfg = _config()
            scfg = (cfg.get("mcpServers") or {}).get(entry["server"]) or {}
            if iot_held(entry["server"], scfg, cfg):
                _PENDING[pid] = entry          # it keeps waiting: it can run once the IoT safety statement is accepted
                raise IotNotAccepted(iot_disclaimer.REQUIRED_MESSAGE)
            HUB.ensure_connected(entry["server"], scfg)
            text, is_error = HUB.call(entry["server"], entry["tool"], entry["arguments"], _limits()["call_timeout"])
            snip = text if len(text) <= 500 else text[:500] + "…"
            _LOG.append({"ts": time.time(), "turn_id": entry.get("turn_id", ""), "server": entry["server"],
                         "tool": entry["tool"], "arguments": entry["arguments"], "ok": not is_error,
                         "result": snip[:300], "ms": 0, "policy": "confirmed"})
            user = _user_name()
            _relay(f"（{user}刚在 Tools 页面批准了，你替他做了{entry['server']}的{entry['tool']}；结果在下面的工具执行记录里，用你自己的话告诉他）",
                   f"[工具执行记录] {entry['server']}__{entry['tool']} {json.dumps(entry['arguments'], ensure_ascii=False)} → {'成功' if not is_error else '失败'}：{snip}")
            return jsonify({"ok": True, "result": snip, "is_error": is_error})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except IotNotAccepted as error:
            return jsonify({"ok": False, "code": iot_disclaimer.REQUIRED_CODE, "error": str(error)}), 409
        except RuntimeError as error:
            return jsonify({"ok": False, "error": str(error)}), 502

    @app.route("/api/build_f/tools/dismiss", methods=["POST"])
    def tools_dismiss():
        try:
            value = action_request("tools-dismiss")
            if _PENDING.pop(value.get("id"), None) is None:
                raise ValueError("Unknown or already-handled pending call.")
            return jsonify({"ok": True})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    @app.route("/api/build_f/tools/job", methods=["POST"])
    def tools_job():
        """{server, job, action: stop|answer, text?} — stop a job or answer its question from the Tools page."""
        try:
            value = action_request("tools-job")
            job = _find_job(str(value.get("job") or ""), str(value.get("server") or "") or None)
            if job is None:
                raise ValueError("Unknown job.")
            action = value.get("action")
            if action == "stop":
                text, ok = _job_control(job, "stop", {"job": job["job"]}, job.get("turn_id", ""), quote="Tools page")
            elif action == "answer":
                answer = str(value.get("text") or "").strip()
                if not answer:
                    raise ValueError("Give the answer text.")
                text, ok = _job_control(job, "answer", {"job": job["job"], "text": answer}, job.get("turn_id", ""), quote="Tools page")
            else:
                raise ValueError("action must be stop or answer.")
            return jsonify({"ok": ok, "result": text[:500], "job": _public_job(job)})
        except PermissionError as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

    _ensure_health_thread()
    print("[tools] MCP tool layer installed "
          f"(config: {CONFIG_PATH.name}, enabled={_config().get('enabled')}, clerk_role={_config().get('clerk_role')})")
    held = iot_disclaimer.status(_config())["held"]
    if held:
        print(f"[tools] smart home held off ({', '.join(held)}): the IoT safety statement "
              f"{iot_disclaimer.IOT_DISCLAIMER_VERSION} has not been accepted (Tools page)")
