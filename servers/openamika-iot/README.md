# openamika-iot — smart-home MCP server

Zigbee smart-home control for OpenAmika over **mosquitto + zigbee2mqtt**. The MQTT core (dynamic
device registry, nickname resolution, 3-second confirm-on-command, 45-second watchdog reconnect) is
ported near-verbatim from the proven Amika_IoT bridge — with the REST/OpenClaw layer replaced by MCP.

## Safety and responsibility

**Read OpenAmika's IoT safety statement, [docs/IOT_DISCLAIMER.md](../../docs/IOT_DISCLAIMER.md), before you use
this server.** Amika runs on large language models, which are non-deterministic: she can mishear, misunderstand,
switch the wrong device or act at the wrong time, repeat or skip an action, or be manipulated by what she reads or
hears (prompt injection), and the models, the network, the broker, zigbee2mqtt and device firmware can fail. There
is no guarantee that she will not cause harm to your devices, your property, your data, or people or animals.
Everything you connect is at your own risk; MekaHime Inc. and the contributors are not responsible for any outcome,
to the maximum extent the law allows, and the software is provided as is, without warranty (MIT License). Never
connect anything that could cause danger if it is misused or abused: heaters and other heating appliances, gas or
water valves and pumps, locks, garage doors and gates, alarms, medical, child- or pet-safety equipment, vehicles,
machinery, high-power loads, or anything whose failure could cause a fire, flooding or injury or let someone in.
Keep manual, physical control of every device; the `confirm` policies and the 3-second confirmation are best-effort,
never a guarantee.

This server refuses to start until the current statement has been accepted. OpenAmika's Tools page shows it and
records the acceptance in `config/tools.json`; the brain then starts this server with the accepted version in
`OPENAMIKA_IOT_DISCLAIMER_ACCEPTED`. When you run it yourself, `--accept-iot-disclaimer 2026-11-03` confirms that you
have read and accept the statement (a different version means it changed: read it again). Without that it prints
the reason and exits with code 2, before it connects to anything.

## Stack

```
Zigbee devices ── Zigbee USB dongle ── zigbee2mqtt ── mosquitto (:1883) ── this server ── OpenAmika
```

1. **mosquitto** — install from mosquitto.org; run with the 2-line `mosquitto.conf` here
   (`mosquitto -c mosquitto.conf`). It listens on 127.0.0.1 only; add auth before it listens anywhere else.
2. **zigbee2mqtt** — install per zigbee2mqtt.io with your dongle's serial port;
   `base_topic: zigbee2mqtt`, `homeassistant: enabled: false`. Pair devices from its own dashboard
   (pairing is deliberately NOT a model-callable tool).
3. **this server** — `pip install -e .` (or `pip install mcp paho-mqtt`), then it's spawned by
   OpenAmika's `config/tools.json` once the IoT safety statement was accepted on the Tools page:

```jsonc
"iot": {
  "command": "python", "args": ["-m", "openamika_iot"],
  "cwd": "../servers/openamika-iot",
  "env": { "AMIKA_MQTT_HOST": "127.0.0.1", "AMIKA_MQTT_PORT": "1883" },
  "enabled": true,
  "tools": { "list_devices": "auto", "get_state": "auto", "health": "auto",
             "set_state": "confirm", "set_nickname": "confirm", "activate_scene": "confirm" }
}
```

Dongle on another machine? Run `python -m openamika_iot --http --host 0.0.0.0 --port 8010
--accept-iot-disclaimer 2026-11-03` there and register the URL instead, under the name `iot` (or with
`"kind": "iot"`), so OpenAmika holds it off like the local one until the statement is accepted on its Tools page.

## Tools

| Tool | Policy default | Notes |
|---|---|---|
| `list_devices` | auto | names, nicknames, state, availability, power metrics |
| `get_state` | auto | one device (nickname or friendly name, case-insensitive) |
| `health` | auto | broker + z2m status; **self-heals** a stale connection before reporting |
| `set_state` | confirm | on/off/toggle; waits 3 s for the device's own confirmation |
| `set_nickname` | confirm | persisted to `devices.json` |
| `activate_scene` | confirm | from `scenes.json` (copy `scenes.example.json`) |

Alternative: if you already run Home Assistant, skip all of this and register HA's official MCP
Server URL in OpenAmika instead — same Tools page, zero code. The same safety statement applies, and
OpenAmika holds a Home Assistant server off until it is accepted, like this one.
