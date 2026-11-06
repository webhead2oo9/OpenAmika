# IoT — her hands in the room (Zigbee smart plugs)

```
plugs ── SONOFF Zigbee Dongle Plus MG24 (COM port) ── zigbee2mqtt ── mosquitto (:1883) ── servers/openamika-iot ── brain
```

Everything here is started by `Start_OpenAmika.bat` (settings.json → `services.mqtt`, `services.zigbee2mqtt`); the
smart-home MCP server itself is `servers/openamika-iot`, spawned by `config/tools.json` (`iot`). The launcher
finds the dongle's COM port on its own and hands it to zigbee2mqtt (`ZIGBEE2MQTT_CONFIG_SERIAL_PORT`). None of it
starts before you have accepted the IoT safety statement (below).

## Safety and responsibility

**Read [docs/IOT_DISCLAIMER.md](../docs/IOT_DISCLAIMER.md) (English, 日本語, 简体中文) before you connect anything.**
Amika runs on large language models, which are non-deterministic: the same words can lead to different actions.
She can mishear or misunderstand you, switch the wrong device, act at the wrong time, repeat or skip an action, or
be manipulated by text or audio she reads or hears (prompt injection), and the models, networks, mosquitto,
zigbee2mqtt, the dongle and device firmware can all fail. Nothing guarantees that she will not cause harm to your
devices, your home, your data, or people or animals. Everything you connect is at your own risk, and MekaHime Inc.
and the contributors are not responsible for any outcome, damage, loss or injury, to the maximum extent the law
allows; the software comes as is, without warranty (MIT License).

Connect only things that are harmless if they are switched at the wrong time or left on or off: never heaters,
stoves, ovens, irons or other heating appliances, gas or water valves and pumps, door locks, garage doors and
gates, alarms and security systems, medical or life-support equipment, child- or pet-safety devices, vehicles,
machinery and power tools, high-power loads, or anything else whose failure could cause a fire, flooding or injury
or let someone in. Her confirmation steps and safety checks are best-effort, never a guarantee; keep manual,
physical control of every device. You can turn IoT off at any time on the Tools page.

OpenAmika enforces the acceptance: the Tools page shows the statement when you turn IoT on and records the version
you accepted, with the time, in `config/tools.json` (`iot_disclaimer`). Until the current version is accepted, the
brain refuses to start or connect any smart-home server, the launcher does not start mosquitto or zigbee2mqtt (it
starts them as soon as you accept), and `servers/openamika-iot` refuses to start. A new version of the statement
turns IoT off again until you accept it. If you do not agree, do not enable IoT.

| Piece | Where |
|---|---|
| zigbee2mqtt, installed by you ([Windows guide](https://www.zigbee2mqtt.io/guide/installation/03_windows.html); tested with 2.9.1 built with corepack pnpm) | `zigbee2mqtt/` (git-ignored) |
| its data: `configuration.yaml`, network key, coordinator backup, paired devices; private, never commit it | `data/` (git-ignored; starter config below) |
| broker config (127.0.0.1 only, anonymous) | `mosquitto.conf` |
| one-time admin installs: CP210x driver (via Windows Update), Mosquitto (winget) | `Setup_IoT_admin.bat` → `Install_CP210x_driver.ps1`, `Setup_IoT_admin.ps1` |
| Windows fix for the dongle (see below) | `patch_herdsman_windows.py` — re-run after reinstalling zigbee2mqtt |
| nicknames she uses for the plugs | `../servers/openamika-iot/devices.json` (created on first use, git-ignored; format: `devices.example.json`) |
| zigbee2mqtt dashboard | http://localhost:8080 (pairing/renaming happens there, never through her) |

## The dongle on Windows (why `patch_herdsman_windows.py` exists)

Node's serialport opens a COM port with DTR asserted and RTS *disabled*. On the MG24 dongle the CP210x's DTR/RTS
pins drive the EFR32's reset/boot lines, and exactly that state boots the dongle into its **Gecko bootloader** —
zigbee2mqtt then waits forever for an ASH `RSTACK` ("Failed to start EZSP layer with status=HOST_FATAL_ERROR").
Probed on COM3 (2026-10-24): DTR high + RTS low → bootloader menu; DTR low + RTS low → `RSTACK`. The patch adds
`hupcl: false` to the ember adapter's serial options so DTR stays released. `rtscts` must stay `false` (the
firmware uses XON/XOFF; with RTS/CTS the host waits on CTS and nothing is ever sent).

A starter `data/configuration.yaml` (zigbee2mqtt 2.x; the launcher fills in the COM port, and `GENERATE` makes
zigbee2mqtt create your own network keys on its first start). An existing `data/configuration.yaml` is not changed:
add `host: 127.0.0.1` under `frontend:` yourself, or its dashboard (full control of your devices) is open to your LAN.

```yaml
homeassistant:
  enabled: false
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://127.0.0.1:1883
serial:
  adapter: ember
  rtscts: false
frontend:
  enabled: true
  port: 8080
  host: 127.0.0.1
advanced:
  log_level: info
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
version: 5
```

Then set `services.mqtt.enabled` and `services.zigbee2mqtt.enabled` to `true` in settings.json and enable the `iot`
server on the Tools page (it adds one from `config/tools.example.json` in Professional mode). The Tools page shows the
IoT safety statement first; IoT turns on only after you tick "I have read and accept this" and press Enable IoT.

If zigbee2mqtt ever reports the dongle missing: Device Manager must show "Silicon Labs CP210x USB to UART Bridge
(COMx)"; if it shows the dongle with an error (code 28), run `Setup_IoT_admin.bat`.
