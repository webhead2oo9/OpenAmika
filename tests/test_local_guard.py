"""local_guard Host/Origin rules, the messengers API guard, and secret masking on the Tools page. Offline."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "brain", ROOT / "messengers"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import f_tools  # noqa: E402
import local_guard  # noqa: E402
import service  # noqa: E402  (messengers/service.py)


@pytest.mark.parametrize("host, origin", [
    ("127.0.0.1:5202", None),                              # local programs send no Origin
    ("localhost:53000", "http://localhost:53000"),
    ("[::1]:53000", "http://[::1]:53000"),
    ("127.0.0.1:53000", "http://127.0.0.1:53000"),         # websocket-client (vision) sends a default Origin
    ("LOCALHOST:53000", "http://localhost:53000"),
])
def test_this_pc_passes(host, origin):
    assert local_guard.refusal(host, origin) is None


@pytest.mark.parametrize("host, origin, why", [
    ("localhost:53000", "https://evil.test", "foreign origin"),
    ("localhost:53000", "http://localhost:5202", "foreign origin"),     # another port is another origin
    ("localhost:53000", "null", "foreign origin"),
    ("evil.test:53000", "http://evil.test:53000", "unknown host"),      # DNS rebinding
    ("192.168.1.20:53000", None, "unknown host"),
    ("pc.tailnet.ts.net:8447", "https://pc.tailnet.ts.net:8447", "unknown host"),   # tailnet only with allow_tailnet
    ("", None, "unknown host"),
    (None, None, "unknown host"),
])
def test_web_pages_and_foreign_hosts_are_refused(host, origin, why):
    assert local_guard.refusal(host, origin) == why


def test_the_gateway_admits_its_tailscale_name_but_not_a_foreign_page_there():
    host = "pc.tailnet.ts.net:8447"
    assert local_guard.refusal(host, "https://pc.tailnet.ts.net:8447", allow_tailnet=True) is None
    assert local_guard.refusal(host, "https://evil.test", allow_tailnet=True) == "foreign origin"
    assert local_guard.refusal("evil.test", None, allow_tailnet=True) == "unknown host"


def test_messengers_refuse_a_page_before_it_can_save_a_bot():
    import core  # messengers/core.py
    from aiohttp.test_utils import TestClient, TestServer

    class Service:
        cfg = core.default_config()
        saved = []

        def save(self):
            Service.saved.append(self.cfg["telegram"]["token"])

        async def apply(self):
            pass

        def status(self):
            return {"ok": True}

        def health(self):
            return {"ok": True}

    body = json.dumps({"telegram": {"enabled": True, "token": "123:attacker", "allowed_users": "666"}})

    async def run():
        client = TestClient(TestServer(service.build_app(Service())))
        await client.start_server()
        try:
            page = await client.post("/api/config", data=body, headers={"Origin": "https://evil.test",
                                                                         "Content-Type": "text/plain"})
            assert page.status == 403 and Service.saved == []
            rebound = await client.post("/api/config", data=body, headers={"Host": "evil.test:5215"})
            assert rebound.status == 403 and Service.saved == []
            gateway = await client.post("/api/config", data=body, headers={"Content-Type": "application/json"})
            assert gateway.status == 200 and Service.saved == ["123:attacker"]   # the gateway's proxy: no Origin
        finally:
            await client.close()

    asyncio.run(run())


@pytest.fixture
def brain(tmp_path, monkeypatch):
    from flask import Flask, jsonify, request
    monkeypatch.setattr(f_tools, "CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr(f_tools, "_cfg_sig", None)
    monkeypatch.setattr(f_tools, "_cfg_cache", None)
    monkeypatch.setattr(f_tools, "_kick_health", lambda: None)
    monkeypatch.setattr(f_tools, "_ensure_health_thread", lambda: None)
    monkeypatch.setattr(f_tools, "_g", {})
    app = Flask("brain-under-test")
    f_tools.install_tools({"app": app, "request": request, "jsonify": jsonify})
    (tmp_path / "tools.json").write_text(json.dumps({"enabled": True, "mcpServers": {"hermes": {
        "command": "python", "enabled": False, "kind": "agent",
        "env": {"HERMES_URL": "http://127.0.0.1:8642", "HERMES_API_KEY": "sk-saved", "NANOMUSE_TOKEN_FILE": "C:/t"},
        "autostart": {"command": "hermes.exe", "args": ["gateway"], "env": {"API_SERVER_KEY": "sk-auto"}}},
        "ha": {"url": "http://homeassistant.local:8123/mcp_server/sse", "enabled": False,
               "headers": {"Authorization": "Bearer ha-saved"}}}}), "utf-8")
    client = app.test_client()

    def save(cfg):
        return client.post("/api/build_f/tools/config", json={"config": cfg}, headers={"X-Build-F-Action": "tools-config"})

    return client, save, tmp_path / "tools.json"


def test_the_tools_page_gets_masked_keys_and_a_save_keeps_them(brain):
    client, save, path = brain
    page = client.get("/api/build_f/tools").get_json()["config"]
    hermes, ha = page["mcpServers"]["hermes"], page["mcpServers"]["ha"]
    assert "sk-saved" not in json.dumps(page) and "sk-auto" not in json.dumps(page) and "ha-saved" not in json.dumps(page)
    assert hermes["env"]["HERMES_API_KEY"] == f_tools.SECRET_MASK
    assert hermes["env"]["HERMES_URL"] == "http://127.0.0.1:8642"
    assert hermes["env"]["NANOMUSE_TOKEN_FILE"] == "C:/t"               # a path, not a secret
    assert ha["headers"]["Authorization"] == f_tools.SECRET_MASK
    page["mcpServers"]["hermes"]["persona_note"] = "edited on the page"
    assert save(page).status_code == 200
    disk = json.loads(path.read_text("utf-8"))["mcpServers"]
    assert disk["hermes"]["env"]["HERMES_API_KEY"] == "sk-saved"
    assert disk["hermes"]["autostart"]["env"]["API_SERVER_KEY"] == "sk-auto"
    assert disk["ha"]["headers"]["Authorization"] == "Bearer ha-saved"
    assert disk["hermes"]["persona_note"] == "edited on the page"
    page["mcpServers"]["hermes"]["env"]["HERMES_API_KEY"] = "sk-new"
    assert save(page).status_code == 200
    assert json.loads(path.read_text("utf-8"))["mcpServers"]["hermes"]["env"]["HERMES_API_KEY"] == "sk-new"


def test_a_mask_with_nothing_saved_behind_it_is_refused_not_written(brain):
    client, save, path = brain
    page = client.get("/api/build_f/tools").get_json()["config"]
    page["mcpServers"]["hermes2"] = page["mcpServers"].pop("hermes")    # renamed in the raw editor
    before = path.read_text("utf-8")
    response = save(page)
    assert response.status_code == 400 and "HERMES_API_KEY" in response.get_json()["error"]
    assert path.read_text("utf-8") == before
