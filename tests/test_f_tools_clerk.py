"""brain/f_tools — the post-speech clerk, quote-verified consent, capability block and job relay.
All LLM and MCP traffic is faked; nothing here touches the network."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "brain") not in sys.path:
    sys.path.insert(0, str(ROOT / "brain"))

import f_tools  # noqa: E402

AGENT_TOOLS = [
    {"name": "ask", "description": "把一件事交给 hermes 去办", "schema": {"type": "object", "properties": {"task": {"type": "string"}}}},
    {"name": "status", "description": "进度", "schema": {}},
    {"name": "answer", "description": "回应", "schema": {}},
    {"name": "stop", "description": "叫停", "schema": {}},
    {"name": "health", "description": "在不在线", "schema": {}},
]
HERMES_CFG = {"enabled": True, "kind": "agent", "command": "python", "persona_note": "你电脑上的小帮手，{user_name}装的"}
IOT_CFG = {"enabled": True, "command": "python", "tools": {"set_state": "confirm"}}
IOT_TOOLS = [{"name": "get_state", "description": "读设备状态", "schema": {}},
             {"name": "set_state", "description": "改设备状态", "schema": {}}]


class FakeHub:
    """Stands in for f_tools.HUB: records calls, answers from a script."""

    def __init__(self):
        self.calls = []
        self.tools = {"hermes": AGENT_TOOLS, "iot": IOT_TOOLS}
        self.status = {"hermes": {"connected": True}, "iot": {"connected": True}}
        self.replies = {}

    def call(self, server, tool, arguments, timeout):
        self.calls.append((server, tool, dict(arguments)))
        reply = self.replies.get((server, tool))
        if callable(reply):
            reply = reply(arguments)
        if reply is None and tool == "health":
            reply = {"ok": True, "reason": "ok", "detail": "fake agent online", "version": "9.9"}
        if reply is None:
            reply = json.dumps({"job": "j1", "state": "running", "step": "", "cursor": 1, "events": [], "pending": None})
        return (reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)), False

    def ensure_connected(self, name, cfg):
        return self.tools.get(name, [])


@pytest.fixture
def wired(monkeypatch):
    """Patch config, hub, role model and the webui globals the layer reaches into."""
    hub = FakeHub()
    monkeypatch.setattr(f_tools, "HUB", hub)
    monkeypatch.setattr(f_tools, "_ensure_poller", lambda: None)
    monkeypatch.setattr(f_tools, "_ensure_health_thread", lambda: None)
    cfg = {"enabled": True, "clerk_role": "surface", "limits": {}, "mcpServers": {"hermes": HERMES_CFG, "iot": IOT_CFG}}
    monkeypatch.setattr(f_tools, "_config", lambda: cfg)
    f_tools._JOBS.clear()
    f_tools._HEALTH.clear()
    f_tools._AUTOSTART.clear()
    f_tools._DELIVERIES.clear()
    f_tools._DIALOGUE.clear()
    f_tools._PENDING.clear()
    f_tools._RELAY_QUEUE.clear()
    f_tools._LOG.clear()
    f_tools._connect_fail_until.clear()
    relays = []
    role_replies = []

    def role_complete(role, messages, *, phase=None, turn_id=""):
        role_replies.append({"role": role, "phase": phase, "messages": messages})
        return {"text": state["reply"]}

    state = {"reply": '{"calls":[],"answers":[],"note":"nothing"}'}
    g = {"F_ROLE_COMPLETE": role_complete, "USER_NAME": "Sam", "_LAST_SID": "sid-1", "_ws_active": {},
         "_start_inject_gen": lambda sid, note, context="", **kw: relays.append((sid, note, context)) or f"tid-{len(relays)}"}
    f_tools._g["globals"] = g
    yield {"hub": hub, "cfg": cfg, "g": g, "relays": relays, "state": state, "role_replies": role_replies}
    f_tools._g.clear()
    f_tools._JOBS.clear()
    f_tools._HEALTH.clear()
    f_tools._AUTOSTART.clear()
    f_tools._DELIVERIES.clear()


# ───────────────────────────── pure helpers ─────────────────────────────

def test_quote_ok_is_punctuation_and_space_insensitive():
    user = "Amika, can you open Chrome and find the nearest Chinese restaurant?"
    assert f_tools.quote_ok("open chrome and find the nearest chinese restaurant", user)
    assert f_tools.quote_ok("Find the nearest Chinese restaurant?", user)
    assert not f_tools.quote_ok("order sushi", user)
    assert not f_tools.quote_ok("a", user)
    assert not f_tools.quote_ok(None, user)
    assert f_tools.quote_ok("帮我开灯", "你能帮我开灯吗？")
    assert not f_tools.quote_ok("帮我关灯", "你能帮我开灯吗？")


def test_parse_llm_json_is_tolerant():
    assert f_tools._parse_llm_json('```json\n{"calls":[],"note":"x"}\n```') == {"calls": [], "note": "x"}
    assert f_tools._parse_llm_json('Sure: {"calls":[{"tool":"a"},],"note":"x",}') == {"calls": [{"tool": "a"}], "note": "x"}
    assert f_tools._parse_llm_json("no json here") is None
    assert f_tools._parse_llm_json("[1,2]") is None
    assert f_tools._parse_llm_json("") is None


def test_resolve_policy_agent_tools_are_auto_plain_tools_keep_rules():
    assert f_tools.resolve_policy(HERMES_CFG, "ask", AGENT_TOOLS) == "auto"
    assert f_tools.resolve_policy({"enabled": True}, "ask", AGENT_TOOLS) == "auto"       # detected by tool set
    assert f_tools.resolve_policy({"enabled": True, "tools": {"ask": "confirm"}}, "ask", AGENT_TOOLS) == "confirm"
    assert f_tools.resolve_policy(IOT_CFG, "get_state", IOT_TOOLS) == "auto"
    assert f_tools.resolve_policy(IOT_CFG, "set_state", IOT_TOOLS) == "confirm"
    assert f_tools.resolve_policy({"enabled": True}, "ask", IOT_TOOLS) == "confirm"       # not an agent server


# ───────────────────────────── capability block ─────────────────────────────

def test_capability_block_describes_servers_rules_and_jobs(wired):
    block = f_tools.capability_block()
    assert block.startswith("[你手边的工具]")
    assert "hermes：你电脑上的小帮手，Sam装的" in block
    assert "iot：" in block and "get_state" in block
    assert "自作主张" in block and "别说\"已经做好了\"" in block
    assert "不许编造查到的内容" in block
    assert "正在替他办的事" not in block
    f_tools._JOBS["hermes:j9"] = {"key": "hermes:j9", "server": "hermes", "job": "j9", "task": "找附近的中餐馆",
                                  "state": "waiting_question", "step": "", "cursor": 0, "relayed": set(),
                                  "pending": {"kind": "question", "id": "q1", "text": "你在哪个城市？"},
                                  "started": 0, "updated": 0}
    block = f_tools.capability_block()
    assert "正在替他办的事" in block and "找附近的中餐馆" in block and "等他回答" in block
    assert "问他——你在哪个城市？" in block


def test_capability_block_is_none_when_disabled(wired):
    wired["cfg"]["enabled"] = False
    assert f_tools.capability_block() is None


# ───────────────────────────── the clerk ─────────────────────────────

def test_clerk_dispatches_ask_with_a_verified_quote(wired):
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Open Chrome and find the nearest Chinese restaurant"},
                   "quote": "find the nearest Chinese restaurant"}],
        "answers": [], "note": "she agreed"})
    outcome = f_tools.run_clerk("t1", "Amika, open Chrome and find the nearest Chinese restaurant please",
                                "Fine, fine. I'll have it looked up.", "")
    assert outcome["calls"] == 1 and outcome["refused"] == 0
    assert [c for c in wired["hub"].calls if c[1] != "health"] == [("hermes", "ask", {"task": "Open Chrome and find the nearest Chinese restaurant"})]
    assert wired["hub"].calls[0] == ("hermes", "health", {})          # the agent is probed right before the ask
    assert "hermes:j1" in f_tools._JOBS and f_tools._JOBS["hermes:j1"]["state"] == "running"
    assert wired["relays"] == []                       # nothing to say yet; the poller relays later
    call = wired["role_replies"][0]
    assert call["role"] == "surface" and call["phase"] == "tools"
    assert "【Sam这轮说的话】" in call["messages"][1]["content"]
    assert "【Sam说话的语言】英文（en）" in call["messages"][1]["content"]
    assert "I'll have it looked up" in call["messages"][1]["content"]
    assert "一律用【Sam说话的语言】写" in call["messages"][0]["content"]


def test_clerk_lists_recently_finished_jobs_for_follow_ups(wired):
    f_tools._JOBS["hermes:j7"] = {"key": "hermes:j7", "server": "hermes", "job": "j7", "task": "find a restaurant",
                                  "state": "done", "step": "", "cursor": 4, "relayed": set(), "pending": None,
                                  "result": "Golden Dragon, 5 min away", "started": 0, "updated": __import__("time").time(),
                                  "turn_id": "t0"}
    wired["hub"].replies[("hermes", "answer")] = {"outcome": "continued", "job": "j7", "state": "running", "step": "",
                                                   "cursor": 5, "events": [], "pending": None}
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [{"job": "j7", "text": "Also check their opening hours", "approved": None,
                                                                    "quote": "check their opening hours"}]})
    outcome = f_tools.run_clerk("t1", "nice, can you check their opening hours too?", "Sure, hang on.", "", "en")
    assert outcome["answers"] == 1
    assert wired["hub"].calls == [("hermes", "answer", {"job": "j7", "text": "Also check their opening hours"})]
    assert f_tools._JOBS["hermes:j7"]["state"] == "running"
    assert "已办完（结果：Golden Dragon" in wired["role_replies"][0]["messages"][0]["content"]


def test_clerk_refuses_a_call_whose_quote_is_not_the_users_words(wired):
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "order sushi"}, "quote": "order me sushi"}],
        "answers": []})
    outcome = f_tools.run_clerk("t1", "what are you up to tonight?", "Nothing much.", "")
    assert outcome["calls"] == 0 and outcome["refused"] == 1
    assert wired["hub"].calls == []
    assert any(e["policy"] == "refused" for e in f_tools._LOG)


def test_clerk_does_nothing_when_she_refused(wired):
    wired["state"]["reply"] = '{"calls":[],"answers":[],"note":"she said no"}'
    outcome = f_tools.run_clerk("t1", "order me takeout", "No. Do it yourself.", "")
    assert outcome["calls"] == 0 and wired["hub"].calls == [] and f_tools._JOBS == {}


def test_clerk_skips_proactive_and_empty_turns(wired):
    assert f_tools.run_clerk("t1", "   ", "hello", "") is None
    assert wired["role_replies"] == []


def test_clerk_parks_confirm_policy_tools_for_the_tools_page(wired):
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "iot__set_state", "arguments": {"device": "lamp", "state": "on"}, "quote": "turn on the lamp"}],
        "answers": []})
    outcome = f_tools.run_clerk("t1", "turn on the lamp", "Okay, turning it on.", "")
    assert outcome["calls"] == 1 and wired["hub"].calls == []
    assert len(f_tools._PENDING) == 1
    pending = next(iter(f_tools._PENDING.values()))
    assert pending["server"] == "iot" and pending["tool"] == "set_state" and pending["quote"] == "turn on the lamp"


def test_clerk_relays_immediate_results_of_plain_tools(wired):
    wired["hub"].replies[("iot", "get_state")] = "lamp: off"
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "iot__get_state", "arguments": {"device": "lamp"}, "quote": "is the lamp on"}],
        "answers": []})
    f_tools.run_clerk("t1", "is the lamp on?", "Let me look.", "")
    assert wired["hub"].calls == [("iot", "get_state", {"device": "lamp"})]
    assert len(wired["relays"]) == 1
    sid, note, context = wired["relays"][0]
    assert sid == "sid-1" and "get_state" in note and "lamp: off" in context


def test_clerk_answers_a_pending_approval_only_with_consent_words(wired):
    f_tools._JOBS["hermes:j1"] = {"key": "hermes:j1", "server": "hermes", "job": "j1", "task": "order takeout",
                                  "state": "waiting_approval", "step": "", "cursor": 3, "relayed": {"p1"},
                                  "pending": {"kind": "approval", "id": "p1", "text": "pay 58 to Meituan?"},
                                  "started": 0, "updated": 0, "turn_id": "t0"}
    wired["hub"].replies[("hermes", "answer")] = {"outcome": "ok", "job": "j1", "state": "running", "step": "paying",
                                                   "cursor": 4, "events": [], "pending": None}
    # 1. consent words present → released
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [{"job": "j1", "approved": True, "text": None, "quote": "yes go ahead"}]})
    outcome = f_tools.run_clerk("t1", "yes go ahead and pay", "Okay, I'll tell it yes.", "")
    assert outcome["answers"] == 1
    assert wired["hub"].calls == [("hermes", "answer", {"job": "j1", "text": "", "approval_id": "p1", "approved": True})]
    assert f_tools._JOBS["hermes:j1"]["state"] == "running" and f_tools._JOBS["hermes:j1"]["pending"] is None
    # 2. no consent words → refused, nothing sent
    f_tools._JOBS["hermes:j1"].update({"state": "waiting_approval", "pending": {"kind": "approval", "id": "p2", "text": "x"}})
    wired["hub"].calls.clear()
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [{"job": "j1", "approved": True, "quote": "sure"}]})
    outcome = f_tools.run_clerk("t2", "hmm let me think about it", "Take your time.", "")
    assert outcome["refused"] == 1 and wired["hub"].calls == []
    # 3. an approval without an explicit yes/no is ignored even with a quote
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [{"job": "j1", "approved": None, "text": "ok", "quote": "think about it"}]})
    outcome = f_tools.run_clerk("t3", "let me think about it", "Sure.", "")
    assert outcome["answers"] == 0 and wired["hub"].calls == []


# ───────────────────────────── poller + relay ─────────────────────────────

def _job(**over):
    base = {"key": "hermes:j1", "server": "hermes", "job": "j1", "task": "find the nearest Chinese restaurant",
            "state": "running", "step": "", "cursor": 0, "relayed": set(), "pending": None,
            "started": 0, "updated": 0, "turn_id": "t0"}
    base.update(over)
    f_tools._JOBS[base["key"]] = base
    return base


def test_poll_relays_the_final_report_once(wired):
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "Golden Dragon, 2 blocks away, open till 22:00"}
    f_tools._poll_once()
    assert len(wired["relays"]) == 1
    _sid, note, context = wired["relays"][0]
    assert "办完了" in note and "Golden Dragon" in context
    f_tools._poll_once()                                   # terminal jobs are not polled or relayed again
    assert len(wired["relays"]) == 1 and wired["hub"].calls.count(("hermes", "status", {"job": "j1", "cursor": 0})) == 1


def test_poll_relays_questions_and_approvals_once_each(wired):
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "waiting_question", "step": "", "cursor": 2,
                                                   "events": [], "pending": {"kind": "question", "id": "q1", "text": "Which city?"}}
    f_tools._poll_once()
    f_tools._poll_once()
    assert len(wired["relays"]) == 1 and "Which city?" in wired["relays"][0][1]
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "waiting_approval", "step": "", "cursor": 3,
                                                   "events": [], "pending": {"kind": "approval", "id": "p1", "text": "pay 58?"}}
    f_tools._poll_once()
    assert len(wired["relays"]) == 2 and "批准" in wired["relays"][1][1] and "pay 58?" in wired["relays"][1][1]


def test_relay_waits_while_she_is_mid_turn(wired):
    wired["g"]["_ws_active"]["turn-x"] = {"cancel": None}
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "failed", "cursor": 1, "events": [], "error": "boom"}
    f_tools._poll_once()
    assert wired["relays"] == [] and len(f_tools._RELAY_QUEUE) == 1
    wired["g"]["_ws_active"].clear()
    f_tools._drain_relays()
    assert len(wired["relays"]) == 1 and "没能办成" in wired["relays"][0][1]


def test_poll_gives_up_after_repeated_transport_errors(wired):
    _job()

    def boom(arguments):
        raise RuntimeError("bridge gone")
    wired["hub"].replies[("hermes", "status")] = boom

    def failing_call(server, tool, arguments, timeout):
        raise RuntimeError("bridge gone")
    wired["hub"].call = failing_call
    for _ in range(5):
        f_tools._poll_once()
    assert f_tools._JOBS["hermes:j1"]["state"] == "failed" and len(wired["relays"]) == 1


def test_status_payload_lists_jobs_and_kinds(wired):
    _job(state="running", step="opening browser")
    payload = f_tools._status_payload()
    assert payload["clerk_role"] == "surface"
    assert payload["servers"]["hermes"]["kind"] == "agent" and payload["servers"]["iot"]["kind"] == "mcp"
    assert payload["servers"]["hermes"]["persona_note"].startswith("你电脑上的小帮手")
    assert payload["jobs"][0]["step"] == "opening browser"
    assert payload["config"] == wired["cfg"]           # tools.json, env/cwd included, for the page's editor (no secrets here)


# ───────────────────────────── health ─────────────────────────────

def test_probe_health_caches_and_can_be_forced(wired):
    first = f_tools.probe_health("hermes", HERMES_CFG)
    assert first["ok"] is True and first["version"] == "9.9"
    f_tools.probe_health("hermes", HERMES_CFG)
    assert wired["hub"].calls.count(("hermes", "health", {})) == 1          # still fresh → no second call
    f_tools.probe_health("hermes", HERMES_CFG, force=True)
    assert wired["hub"].calls.count(("hermes", "health", {})) == 2
    assert f_tools.probe_health("iot", IOT_CFG) is None                      # plain MCP servers have no probe
    assert f_tools._status_payload()["servers"]["hermes"]["health"]["ok"] is True
    assert f_tools._status_payload()["servers"]["iot"]["health"] is None


def test_capability_block_warns_her_when_an_agent_is_unreachable(wired):
    f_tools._HEALTH["hermes"] = {"ok": False, "reason": "unreachable", "detail": "Hermes: nothing answers", "version": None, "at": 0}
    block = f_tools.capability_block()
    assert "hermes：" in block and "联系不上" in block and "程序没开" in block and "不能答应用它办事" in block
    assert "没有任何事在替Sam办" in block                                       # nothing running → she is told so
    third = f_tools.capability_block(voice="third")
    assert third.startswith("[她手边的工具]") and "她不能答应用它办事" in third and "绝不能替她编造" in third
    assert "[你手边的工具]" not in third and "没有任何事在替Sam办" in third


def test_clerk_does_not_send_a_task_to_an_unreachable_agent(wired):
    wired["hub"].replies[("hermes", "health")] = {"ok": False, "reason": "unreachable",
                                                   "detail": "Hermes: nothing answers at http://127.0.0.1:8642"}
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find the nearest malatang"}, "quote": "find the nearest malatang"}],
        "answers": []})
    outcome = f_tools.run_clerk("t1", "can you help me find the nearest malatang", "Okay, having Hermes find it.", "", "en")
    assert outcome["unreachable"] == 1 and outcome["calls"] == 0
    assert [c for c in wired["hub"].calls if c[1] == "ask"] == []
    assert f_tools._JOBS == {}
    assert len(wired["relays"]) == 1 and "联系不上" in wired["relays"][0][1] and "没有交办出去" in wired["relays"][0][1]
    assert any(e["policy"] == "unreachable" and e["result"].startswith("not sent") for e in f_tools._LOG)
    assert f_tools._HEALTH["hermes"]["ok"] is False
    # a later open turn about the same message must not produce a second "it's down" relay
    assert f_tools.run_clerk("open-1", "can you help me find the nearest malatang", "Hermes, go.", "", "en", source_turn_id="t1") is None
    assert len(wired["relays"]) == 1


def test_failed_job_with_a_connection_error_is_explained_in_plain_words(wired):
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "failed", "cursor": 1, "events": [],
                                                   "error": "ConnectError: [WinError 10061] No connection could be made"}
    f_tools._poll_once()
    assert len(wired["relays"]) == 1
    note = wired["relays"][0][1]
    assert "联系不上hermes" in note and "10061" not in note
    assert f_tools._HEALTH["hermes"]["ok"] is False and f_tools._HEALTH["hermes"]["reason"] == "unreachable"


def test_a_job_the_agent_itself_refused_is_not_called_unreachable(wired):
    """A running agent that would not do it (Hermes's HTTP 429, nanoMuse's model refusing its key) is reported with
    its reason, and its health is asked again; only a connection error means "联系不上"."""
    for error in ("Hermes refused POST /v1/runs: HTTP 429 concurrent_run_limit",
                  "The model provider refused the API key. Check it under Connections."):
        wired["relays"].clear()
        f_tools._HEALTH.clear()
        _job()
        wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "failed", "cursor": 1, "events": [], "error": error}
        f_tools._poll_once()
        note = wired["relays"][0][1]
        assert "联系不上" not in note and error[:40] in note
        assert (f_tools._HEALTH.get("hermes") or {}).get("reason") != "unreachable"
    assert f_tools._CONNECT_ERROR_RE.search("httpx.ConnectError: [Errno 111] Connection refused")
    assert f_tools._CONNECT_ERROR_RE.search("No connection could be made because the target machine actively refused it")


def test_clerk_never_sees_status_or_health_as_tools(wired):
    wired["state"]["reply"] = '{"calls":[],"answers":[],"note":"nothing"}'
    f_tools.run_clerk("t1", "hi there", "Hey.", "")
    prompt = wired["role_replies"][0]["messages"][0]["content"]
    assert "hermes__ask" in prompt and "hermes__status" not in prompt and "hermes__health" not in prompt


# ───────────────────────────── open turns ─────────────────────────────

def test_clerk_on_an_open_turn_transcribes_her_agreement_to_his_last_request_once(wired):
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find the nearest malatang"}, "quote": "find the nearest malatang"}],
        "answers": [{"job": "j1", "approved": True, "quote": "find the nearest malatang"}]})
    outcome = f_tools.run_clerk("open-1", "hey can you help me find the nearest malatang bc it raining",
                                "Okay, malatang incoming — having Hermes find the closest one.", "", "en",
                                source_turn_id="u-1")
    assert outcome["calls"] == 1 and outcome["answers"] == 0                 # answers are never taken from an open turn
    assert wired["hub"].calls[-1] == ("hermes", "ask", {"task": "Find the nearest malatang"})
    job = f_tools._JOBS["hermes:j1"]
    assert job["turn_id"] == "open-1" and job["source_turn"] == "u-1"
    call = wired["role_replies"][0]
    assert "上一轮说的话" in call["messages"][1]["content"] and "主动开口" in call["messages"][0]["content"]
    # a second open turn ("still working on it") must not start it again — and costs no clerk call
    assert f_tools.run_clerk("open-2", "hey can you help me find the nearest malatang bc it raining",
                             "Still cooking in the background.", "", "en", source_turn_id="u-1") is None
    assert len(wired["role_replies"]) == 1 and len([c for c in wired["hub"].calls if c[1] == "ask"]) == 1


def test_open_turn_after_a_user_turn_that_already_dispatched_is_skipped(wired):
    _job(turn_id="u-7", source_turn="u-7")
    assert f_tools.run_clerk("open-9", "find me a restaurant", "On it.", "", "en", source_turn_id="u-7") is None
    assert wired["role_replies"] == []


# ───────────────────────────── autostart ─────────────────────────────

class FakeProc:
    """Stands in for subprocess.Popen: records the launch, pretends to run (or to have exited)."""
    launches = []

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.pid, self.returncode = cmd, kwargs, 4242, None
        FakeProc.launches.append(self)

    def poll(self):
        return self.returncode


@pytest.fixture
def launchable(wired, monkeypatch, tmp_path):
    FakeProc.launches = []
    monkeypatch.setattr(f_tools.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(f_tools, "_agent_log_path", lambda name: tmp_path / f"agent-{name}.log")
    wired["cfg"]["mcpServers"]["hermes"] = dict(HERMES_CFG, autostart={"command": "C:/hermes/bin/hermes.exe", "args": ["gateway"],
                                                                        "cwd": "C:/hermes", "env": {"FOO": "1"}})
    wired["hub"].replies[("hermes", "health")] = {"ok": False, "reason": "unreachable", "detail": "Hermes: nothing answers"}
    return wired


def test_autostart_config_accepts_a_command_line_or_a_dict():
    assert f_tools.autostart_config({"autostart": 'C:/x/hermes.exe gateway'}) == {"command": "C:/x/hermes.exe", "args": ["gateway"], "cwd": None, "env": {}}
    assert f_tools.autostart_config({"autostart": '"C:/Program Files/x/nanomuse.exe" serve --port 8787'})["command"] == "C:/Program Files/x/nanomuse.exe"
    assert f_tools.autostart_config({"autostart": {"command": " a.exe ", "args": ["b", 1], "cwd": "C:/d", "env": {"K": 2}}}) == \
        {"command": "a.exe", "args": ["b", "1"], "cwd": "C:/d", "env": {"K": "2"}}
    assert f_tools.autostart_config({"autostart": ""}) is None and f_tools.autostart_config({}) is None
    assert f_tools.autostart_config({"autostart": {"args": ["x"]}}) is None


def test_unreachable_agent_is_launched_once_and_reported_as_starting(launchable):
    cfg = launchable["cfg"]["mcpServers"]["hermes"]
    first = f_tools.probe_health("hermes", cfg, force=True)
    assert first["reason"] == "starting" and "hermes.exe gateway" in first["detail"]
    assert len(FakeProc.launches) == 1
    launch = FakeProc.launches[0]
    assert launch.cmd == ["C:/hermes/bin/hermes.exe", "gateway"] and launch.kwargs["cwd"] == "C:/hermes"
    assert launch.kwargs["env"]["FOO"] == "1" and launch.kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    if os.name == "nt":
        assert launch.kwargs["creationflags"] & 0x00000008            # DETACHED_PROCESS: outlives the brain
    # still within the grace period: no second launch, still "starting"
    again = f_tools.probe_health("hermes", cfg, force=True)
    assert again["reason"] == "starting" and len(FakeProc.launches) == 1
    assert "正在启动" in f_tools.capability_block()                     # she is told it is coming up, not dead
    # it answers: online, and the start budget is reset
    launchable["hub"].replies[("hermes", "health")] = {"ok": True, "reason": "ok", "detail": "online", "version": "1"}
    assert f_tools.probe_health("hermes", cfg, force=True)["ok"] is True
    assert "hermes" not in f_tools._AUTOSTART
    payload = f_tools._status_payload()["servers"]["hermes"]
    assert payload["autostart"] == {"command": "C:/hermes/bin/hermes.exe", "args": ["gateway"], "cwd": "C:/hermes"}   # env stays private


def test_autostart_gives_up_after_three_attempts_until_start_is_pressed(launchable, monkeypatch):
    monkeypatch.setattr(f_tools, "_AUTOSTART_GRACE", 0.0)
    monkeypatch.setattr(f_tools, "_AUTOSTART_MIN_GAP", 0.0)
    cfg = launchable["cfg"]["mcpServers"]["hermes"]
    for _ in range(3):
        f_tools.probe_health("hermes", cfg, force=True)
    assert len(FakeProc.launches) == 3
    spent = f_tools.probe_health("hermes", cfg, force=True)
    assert spent["reason"] == "start_failed" and "press Start" in spent["detail"] and len(FakeProc.launches) == 3
    assert "启动失败" in f_tools.capability_block()
    # the Start button forces one more launch with a fresh budget
    rec = f_tools.start_agent("hermes", cfg, force=True)
    assert rec["attempts"] == 1 and len(FakeProc.launches) == 4 and f_tools._HEALTH["hermes"]["reason"] == "starting"


def test_autostart_reports_the_exit_code_when_the_process_dies(launchable):
    cfg = launchable["cfg"]["mcpServers"]["hermes"]
    f_tools.probe_health("hermes", cfg, force=True)
    FakeProc.launches[0].returncode = 1
    dead = f_tools.probe_health("hermes", cfg, force=True)
    assert dead["reason"] == "start_failed" and "exited with code 1" in dead["detail"]


def test_autostart_paths_expand_variables_and_resolve_from_the_openamika_folder(launchable, monkeypatch):
    """The brain starts what the launcher starts (launch.py launch_path): %LOCALAPPDATA%-style variables, paths relative
    to the OpenAmika folder, a bare command left to the PATH. The Tools page still shows the entry as written."""
    monkeypatch.setenv("OA_TEST_HOME", "C:/oa-test-home")
    cfg = launchable["cfg"]["mcpServers"]["hermes"]
    cfg["autostart"] = {"command": "%OA_TEST_HOME%/bin/hermes.exe", "args": ["gateway", "--home=%OA_TEST_HOME%"],
                        "cwd": "%OA_TEST_HOME%"}
    f_tools.start_agent("hermes", cfg, force=True)
    assert FakeProc.launches[-1].cmd == ["C:/oa-test-home/bin/hermes.exe", "gateway", "--home=C:/oa-test-home"]
    assert FakeProc.launches[-1].kwargs["cwd"] == "C:/oa-test-home"
    assert f_tools._status_payload()["servers"]["hermes"]["autostart"]["command"] == "%OA_TEST_HOME%/bin/hermes.exe"
    cfg["autostart"] = {"command": "Nanomuse/venv/Scripts/nanomuse.exe", "args": ["serve"], "cwd": "Nanomuse/data"}
    f_tools.start_agent("hermes", cfg, force=True)
    root = Path(f_tools.__file__).resolve().parent.parent
    assert FakeProc.launches[-1].cmd == [str(root / "Nanomuse/venv/Scripts/nanomuse.exe"), "serve"]
    assert FakeProc.launches[-1].kwargs["cwd"] == str(root / "Nanomuse/data")
    cfg["autostart"] = "hermes gateway"
    f_tools.start_agent("hermes", cfg, force=True)
    assert FakeProc.launches[-1].cmd == ["hermes", "gateway"] and FakeProc.launches[-1].kwargs["cwd"] is None


def test_no_autostart_means_plain_unreachable(wired):
    wired["hub"].replies[("hermes", "health")] = {"ok": False, "reason": "unreachable", "detail": "Hermes: nothing answers"}
    assert f_tools.probe_health("hermes", HERMES_CFG, force=True)["reason"] == "unreachable"
    assert f_tools._status_payload()["servers"]["hermes"]["autostart"] is None


def test_clerk_waits_for_a_just_launched_agent_then_sends_the_ask(launchable, monkeypatch):
    monkeypatch.setattr(f_tools.time, "sleep", lambda s: None)
    answers = iter([{"ok": False, "reason": "unreachable", "detail": "down"},
                    {"ok": False, "reason": "unreachable", "detail": "down"},
                    {"ok": True, "reason": "ok", "detail": "online", "version": "1"}])
    launchable["hub"].replies[("hermes", "health")] = lambda arguments: next(answers)
    launchable["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find the nearest malatang"}, "quote": "find the nearest malatang"}],
        "answers": []})
    outcome = f_tools.run_clerk("t1", "can you help me find the nearest malatang", "Okay, having Hermes find it.", "", "en")
    assert outcome["calls"] == 1 and outcome["unreachable"] == 0
    assert len(FakeProc.launches) == 1
    assert [c for c in launchable["hub"].calls if c[1] == "ask"] == [("hermes", "ask", {"task": "Find the nearest malatang"})]
    assert "hermes:j1" in f_tools._JOBS


def test_clerk_tells_him_to_retry_when_the_agent_is_still_starting(launchable, monkeypatch):
    monkeypatch.setattr(f_tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(f_tools, "_ASK_START_WAIT", 0.0)
    launchable["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find the nearest malatang"}, "quote": "find the nearest malatang"}],
        "answers": []})
    outcome = f_tools.run_clerk("t1", "can you help me find the nearest malatang", "Okay, having Hermes find it.", "", "en")
    assert outcome["unreachable"] == 1 and len(FakeProc.launches) == 1
    assert "过半分钟再让你办一次" in launchable["relays"][0][1]


# ───────────────────────────── durable delivery ─────────────────────────────

def test_a_relay_turn_the_user_talked_over_is_queued_again(wired):
    history = {}                                             # turn_id -> turn dict, as the brain's history
    wired["g"]["_find_turn"] = lambda tid: history.get(tid)
    wired["g"]["_hist_lock"] = __import__("threading").RLock()
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "Haidilao Hot Pot Downtown, 4.8 stars, open till midnight"}
    f_tools._poll_once()
    assert len(wired["relays"]) == 1 and len(f_tools._DELIVERIES) == 1
    tid = f_tools._DELIVERIES[0]["turn_id"]
    # the gateway dropped the proactive turn and the brain pruned it (not in history any more)
    f_tools._check_deliveries()
    assert f_tools._DELIVERIES == [] and len(f_tools._RELAY_QUEUE) == 1
    f_tools._drain_relays()                                  # a quiet moment: spoken again
    assert len(wired["relays"]) == 2 and "Haidilao" in wired["relays"][1][2]
    assert f_tools._DELIVERIES[0]["attempts"] == 1
    # this time the gateway protected it (started speaking): delivered, nothing more to do
    history[f_tools._DELIVERIES[0]["turn_id"]] = {"turn_id": f_tools._DELIVERIES[0]["turn_id"], "protected": True}
    f_tools._check_deliveries()
    assert f_tools._DELIVERIES == [] and len(f_tools._RELAY_QUEUE) == 0


def test_handover_waits_while_the_turn_is_still_unspoken_and_gives_up_eventually(wired):
    history = {"tid-1": {"turn_id": "tid-1", "protected": False}}
    wired["g"]["_find_turn"] = lambda tid: history.get(tid)
    f_tools._relay("（note）", "", "en")
    assert len(f_tools._DELIVERIES) == 1
    f_tools._check_deliveries()                              # still in history, not yet spoken: keep waiting
    assert len(f_tools._DELIVERIES) == 1 and len(f_tools._RELAY_QUEUE) == 0
    f_tools._DELIVERIES[0]["attempts"] = f_tools._DELIVERY_MAX_ATTEMPTS - 1
    history.clear()                                          # pruned again, budget spent
    f_tools._check_deliveries()
    assert f_tools._DELIVERIES == [] and len(f_tools._RELAY_QUEUE) == 0


def test_without_history_access_handover_tracking_is_inert(wired):
    f_tools._relay("（note）", "", None)
    f_tools._check_deliveries()
    assert len(f_tools._DELIVERIES) == 1                     # nothing known, nothing re-queued


# ───────────────────────────── honesty guard ─────────────────────────────

def test_clerk_flags_a_result_she_invented_and_makes_her_take_it_back(wired):
    _job(task="Find the nearest hot pot")                    # running, no result yet
    wired["state"]["reply"] = json.dumps({
        "calls": [], "answers": [],
        "fabricated": {"agent": "hermes", "quote": "Hermes found it. Sichuan Hot Pot Garden on 4th Street, 4.2 stars"},
        "note": "no result exists yet"})
    outcome = f_tools.run_clerk("t5", "have you found a hot pot yet or no",
                                "Okay, let me check what Hermes found. Hermes found it. Sichuan Hot Pot Garden on 4th Street, 4.2 stars, open till 10.",
                                "", "en")
    assert outcome["fabricated"] == 1 and outcome["calls"] == 0
    assert len(wired["relays"]) == 1
    note = wired["relays"][0][1]
    assert "不是真的" in note and "Sichuan Hot Pot Garden" in note and "还在办" in note
    assert f_tools._JOBS["hermes:j1"]["fabricated"] is True
    assert any(e["policy"] == "fabrication" for e in f_tools._LOG)
    # the real result later tells her to correct herself
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "Haidilao Hot Pot Downtown, 4.8 stars"}
    f_tools._poll_once()
    assert len(wired["relays"]) == 2 and "全是你编的" in wired["relays"][1][1] and "Haidilao" in wired["relays"][1][2]


def test_fabrication_flag_needs_her_actual_words(wired):
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [], "fabricated": {"agent": "hermes", "quote": "Hermes found Golden Dragon"}})
    outcome = f_tools.run_clerk("t6", "anything yet?", "Still looking, give it a sec.", "", "en")
    assert outcome["fabricated"] == 0 and wired["relays"] == []


def test_running_jobs_say_no_result_yet_with_elapsed_time(wired):
    _job(task="Find the nearest hot pot", started=__import__("time").time() - 95)
    block = f_tools.capability_block()
    assert "结果一个都还没回来" in block and "已经跑了 9" in block and "Find the nearest hot pot" in block
    assert "一个都不能报" in block


def test_clerk_ignores_answers_aimed_at_a_job_that_is_still_working(wired):
    _job()                                                   # running, nothing pending
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [{"job": "j1", "text": "check again", "approved": None, "quote": "check again"}]})
    outcome = f_tools.run_clerk("t7", "can you check again", "Hermes is on it already.", "", "en")
    assert outcome["answers"] == 0 and [c for c in wired["hub"].calls if c[1] == "answer"] == []


# ───────────────────────────── real-time results in her prompt ─────────────────────────────

def _history_backed(wired):
    """A fake brain history: the inject starter registers each relay turn (unprotected) and the brain's
    discard hooks are recorded."""
    history, discarded = {}, []
    wired["g"]["_find_turn"] = lambda tid: history.get(tid)
    wired["g"]["_hist_lock"] = __import__("threading").RLock()
    wired["g"]["discard_turn"] = lambda tid: discarded.append(tid) or history.pop(tid, None)
    wired["g"]["_cancel_turn_gen"] = lambda tid, reason: None

    def start(sid, note, context="", **kw):
        wired["relays"].append((sid, note, context))
        tid = f"tid-{len(wired['relays'])}"
        history[tid] = {"turn_id": tid, "protected": False}
        return tid
    wired["g"]["_start_inject_gen"] = start
    return history, discarded


def test_a_fresh_result_sits_in_her_prompt_until_she_has_told_him(wired):
    history, discarded = _history_backed(wired)
    _job(task="Find the nearest hot pot")
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "Haidilao Hot Pot Downtown, 4.8 stars, open till midnight"}
    f_tools._poll_once()                                     # result arrives: relay turn started (not yet spoken)
    assert len(wired["relays"]) == 1 and len(f_tools._DELIVERIES) == 1
    block = f_tools.capability_block()
    assert "刚送到" in block and "Haidilao" in block and "还没听到" in block
    assert "刚结束的事" not in block
    # a user turn comes first and she tells him herself → the clerk reports it
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [], "fabricated": None, "told": ["j1"], "note": "she told him"})
    outcome = f_tools.run_clerk("t8", "found anything yet?", "Yeah — Haidilao downtown, 4.8 stars, open till midnight.", "", "en")
    assert outcome["told"] == 1
    assert f_tools._JOBS["hermes:j1"]["notice"]["delivered"] is True
    assert discarded == ["tid-1"] and f_tools._DELIVERIES == []   # the unspoken relay turn is dropped: no repeat
    block = f_tools.capability_block()
    assert "刚送到" not in block and "刚结束的事" in block and "Find the nearest hot pot" in block


def test_a_queued_relay_is_dropped_once_she_told_him(wired):
    _history_backed(wired)
    wired["g"]["_ws_active"]["turn-x"] = {"cancel": None}   # she is mid-turn: the relay waits in the queue
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "Golden Dragon, 2 blocks away"}
    f_tools._poll_once()
    assert wired["relays"] == [] and len(f_tools._RELAY_QUEUE) == 1
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [], "fabricated": None, "told": ["j1"]})
    f_tools.run_clerk("t9", "so?", "Golden Dragon, two blocks from you.", "", "en")
    assert len(f_tools._RELAY_QUEUE) == 0
    wired["g"]["_ws_active"].clear()
    f_tools._drain_relays()
    assert wired["relays"] == []                             # nothing left to say


def test_a_spoken_relay_turn_marks_the_result_handed_over(wired):
    history, _ = _history_backed(wired)
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "done deal"}
    f_tools._poll_once()
    history["tid-1"]["protected"] = True                     # the gateway started speaking the relay turn
    f_tools._check_deliveries()
    assert f_tools._DELIVERIES == [] and f_tools._JOBS["hermes:j1"]["notice"]["delivered"] is True
    assert "刚送到" not in f_tools.capability_block()


def test_told_ignores_unknown_or_running_jobs(wired):
    _job()                                                   # still running: nothing to have told
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [], "told": ["j1", "nope"]})
    outcome = f_tools.run_clerk("t10", "hi", "Hey.", "", "en")
    assert outcome["told"] == 0 and "notice" not in f_tools._JOBS["hermes:j1"]


# ───────────────────────────── the tool dialogue (Use page) ─────────────────────────────

def test_tool_dialogue_records_her_ask_and_everything_that_comes_back(wired):
    wired["hub"].replies[("hermes", "ask")] = {"job": "j1", "state": "running", "step": "started", "cursor": 1, "pending": None,
                                                "events": [{"i": 1, "ts": 1.0, "kind": "progress", "text": "started",
                                                            "internal": True}]}
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find the nearest hot pot", "context": "he is hungry"},
                   "quote": "find me a hot pot"}], "answers": []})
    f_tools.run_clerk("t1", "can you find me a hot pot", "Sure, having Hermes look.", "", "en")
    lines = list(f_tools._DIALOGUE)
    assert lines[0]["who"] == "amika" and lines[0]["kind"] == "ask" and lines[0]["server"] == "hermes"
    assert lines[0]["text"].startswith("Find the nearest hot pot") and "he is hungry" in lines[0]["text"]
    assert lines[0]["quote"] == "find me a hot pot" and lines[0]["job"] == "j1"
    assert lines[1]["who"] == "agent" and lines[1]["kind"] == "progress" and lines[1]["text"] == "started"
    assert lines[1]["internal"] is True                      # the bridge's own step: in the log, never a doll bubble
    # the helper works, asks, and finishes
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 4, "pending": None,
                                                   "result": "Haidilao Downtown, 4.8 stars",
                                                   "events": [{"i": 2, "kind": "progress", "text": "browser: google maps"},
                                                              {"i": 3, "kind": "text", "text": "Found a few places"},
                                                              {"i": 4, "kind": "done", "text": "Haidilao Downtown, 4.8 stars"}]}
    f_tools._poll_once()
    kinds = [(e["who"], e["kind"]) for e in f_tools._DIALOGUE]
    assert kinds == [("amika", "ask"), ("agent", "progress"), ("agent", "progress"), ("agent", "text"), ("agent", "result")]
    assert [e.get("internal") for e in f_tools._DIALOGUE] == [None, True, None, None, None]
    assert list(f_tools._DIALOGUE)[-1]["text"] == "Haidilao Downtown, 4.8 stars"
    # a replayed snapshot (the answer tool returns events from 0) adds nothing twice
    f_tools._apply_status(f_tools._JOBS["hermes:j1"], wired["hub"].replies[("hermes", "status")], relay=False)
    assert len(f_tools._DIALOGUE) == 5
    payload = f_tools._status_payload()
    assert payload["dialogue"][-1]["kind"] == "result" and payload["dialogue"][0]["quote"] == "find me a hot pot"


def test_tool_dialogue_shows_her_answers_and_the_tools_page_clicks(wired):
    _job(state="waiting_approval", pending={"kind": "approval", "id": "p1", "text": "pay 58 to Meituan?"}, cursor=3)
    wired["hub"].replies[("hermes", "answer")] = {"outcome": "ok", "job": "j1", "state": "running", "step": "paying",
                                                   "cursor": 4, "events": [], "pending": None}
    f_tools._job_control(f_tools._JOBS["hermes:j1"], "answer",
                         {"job": "j1", "text": "", "approval_id": "p1", "approved": True}, "t1", quote="yes go ahead")
    f_tools._job_control(f_tools._JOBS["hermes:j1"], "stop", {"job": "j1"}, "t1", quote="Tools page")
    who_kind = [(e["who"], e["kind"], e["text"]) for e in f_tools._DIALOGUE]
    assert who_kind[0] == ("amika", "approve", "pay 58 to Meituan?")
    assert who_kind[1] == ("you", "stop", "叫停")


def test_tool_dialogue_marks_what_was_not_sent_or_retracted(wired):
    wired["hub"].replies[("hermes", "health")] = {"ok": False, "reason": "unreachable", "detail": "Hermes: nothing answers"}
    wired["state"]["reply"] = json.dumps({
        "calls": [{"tool": "hermes__ask", "arguments": {"task": "Find hot pot"}, "quote": "find hot pot"}], "answers": [],
        "fabricated": {"agent": "hermes", "quote": "Hermes found Sichuan Hot Pot Garden"}})
    f_tools.run_clerk("t1", "find hot pot please", "Hermes found Sichuan Hot Pot Garden on 4th street.", "", "en")
    kinds = [(e["who"], e["kind"]) for e in f_tools._DIALOGUE]
    assert ("system", "not_sent") in kinds and ("system", "fabricated") in kinds


# ───────────────────────────── the agent's own spend lands in her ledger ─────────────────────────────

class FakeMeter:
    def __init__(self, rates=None, table=None):
        self.rates, self.begun, self.finished = rates, [], []
        self.pricing = {"models": dict(table or {})}

    def _rates(self, provider, model):
        return self.rates or self.pricing["models"].get(f"{provider}:{model}")

    def begin(self, provider, model, phase, turn_id="", estimated_max_usd=None):
        self.begun.append((provider, model, phase, turn_id, estimated_max_usd))
        return f"rid-{len(self.begun)}"

    def finish(self, handle, usage=None, status="complete", **kw):
        self.finished.append((handle, usage, status))
        return {"id": handle, "cost_usd": 0.0123}


def test_a_finished_job_books_the_agents_tokens_in_her_ledger(wired):
    meter = FakeMeter(rates={"input": 0.5, "output": 2.0})
    wired["g"]["F_METER"] = meter
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "4",
                                                   "usage": {"provider": "openrouter", "model": "z-ai/glm-5",
                                                             "input_tokens": 320000, "output_tokens": 1500, "runs": 1}}
    f_tools._poll_once()
    assert meter.begun == [("openrouter", "z-ai/glm-5", "agent:hermes", "t0", (320000 * 0.5 + 1500 * 2.0) / 1_000_000)]
    assert meter.finished == [("rid-1", {"prompt_tokens": 320000, "completion_tokens": 1500}, "complete")]
    f_tools._apply_status(f_tools._JOBS["hermes:j1"], wired["hub"].replies[("hermes", "status")], relay=False)
    assert len(meter.begun) == 1                                   # never booked twice


def test_agent_usage_without_a_model_name_is_not_booked(wired):
    meter = FakeMeter(rates=None)
    wired["g"]["F_METER"] = meter
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "4",
                                                   "usage": {"provider": "openrouter", "model": "", "input_tokens": 10, "output_tokens": 1}}
    f_tools._poll_once()
    assert meter.begun == []


def test_agent_usage_is_priced_from_a_variant_of_the_same_model(wired):
    meter = FakeMeter(table={"openrouter:z-ai/glm-5#no-reasoning": {"input": 0.5, "output": 2.0, "cache_read": 0.5}})
    wired["g"]["F_METER"] = meter
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "53F",
                                                   "usage": {"provider": "openrouter", "model": "z-ai/glm-5",
                                                             "input_tokens": 1_000_000, "output_tokens": 0, "runs": 1}}
    f_tools._poll_once()
    assert meter.begun[0][4] == 0.5                                   # priced at the variant's rate, not 1e-6
    assert meter.pricing["models"]["openrouter:z-ai/glm-5"]["input"] == 0.5   # and registered under the exact key


def test_a_correction_that_could_not_be_spoken_before_the_result_arrived_is_dropped(wired):
    history, discarded = _history_backed(wired)
    _job(task="Find tonight's low")
    wired["state"]["reply"] = json.dumps({"calls": [], "answers": [],
                                          "fabricated": {"agent": "hermes", "quote": "Hermes says tonight's low is 53"}})
    f_tools.run_clerk("t1", "what's tonight's low", "Hermes says tonight's low is 53.", "", "en")
    assert len(wired["relays"]) == 1 and len(f_tools._DELIVERIES) == 1   # the correction turn started, not yet spoken
    # the real result lands before the correction could be spoken; the user talked over the correction turn
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "53F tonight"}
    f_tools._poll_once()
    history.pop("tid-1", None)                                          # pruned unspoken
    f_tools._check_deliveries()
    notes = [r[1] for r in wired["relays"]]
    assert len(notes) == 2 and "办完了" in notes[1]                      # the result relay only
    assert all("不是真的" not in item[0] for item in f_tools._RELAY_QUEUE)   # the stale correction was not queued again


def test_agent_usage_is_priced_from_openrouters_public_list_when_nothing_local_matches(wired, monkeypatch):
    meter = FakeMeter(table={})
    wired["g"]["F_METER"] = meter
    f_tools._OR_PRICES.update(at=0.0, models={})
    fetched = {"data": [{"id": "z-ai/glm-5", "pricing": {"prompt": "0.000001", "completion": "0.0000032", "input_cache_read": "0.0000002"}}]}
    real = f_tools._openrouter_rates
    monkeypatch.setattr(f_tools, "_openrouter_rates", lambda model, fetch=None: real(model, fetch=lambda: fetched))
    _job()
    wired["hub"].replies[("hermes", "status")] = {"job": "j1", "state": "done", "step": "", "cursor": 5, "events": [],
                                                   "pending": None, "result": "7:24 AM",
                                                   "usage": {"provider": "openrouter", "model": "z-ai/glm-5",
                                                             "input_tokens": 250_000, "output_tokens": 1_000, "runs": 1}}
    f_tools._poll_once()
    assert abs(meter.begun[0][4] - (250_000 * 1.0 + 1_000 * 3.2) / 1e6) < 1e-9   # list price, per million
    assert meter.pricing["models"]["openrouter:z-ai/glm-5"] == {"input": 1.0, "output": 3.2, "cache_read": 0.2}
    f_tools._OR_PRICES.update(at=0.0, models={})
