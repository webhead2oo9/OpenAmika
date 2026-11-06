# Architecture

Interactive version: open **/flow** in the running app (pan/zoom node map with five views).

```
            browser (ui/: Setup · Tools · Use  ·  /flow architecture map)
                 │  WebSocket /ws/dialog (JSON + binary PCM)  ·  HTTP /api/*
┌────────────────▼──────────────────────────────────────────────────────────┐
│  SPEECH LAYER  speech/api.py        FastAPI · default port 53000          │
│   SenseVoiceSmall STT (local GPU) + Silero VAD + barge-in + carry         │
│   streaming parsers (hane_parser) · sentence splitter · word replacer     │
│   TTS client (emotion tag + SFX + pauses) · voice adapter                 │
│   avatar registry/banks (her model, the paper doll) · pages · proxies     │
└───────┬──────────────────────────────────────────────┬───────────────────┘
        │ Socket.IO client                              │ Socket.IO client
        ▼                                               ▼
┌───────────────────────────────┐        ┌──────────────────────────────────┐
│  BRAIN  brain/webui.py        │        │  TTS: her voice server (:8002)   │
│  Flask-SocketIO · port 5202   │        │  vendor/tts-engine (GPT-SoVITS)  │
│  surface/deep/reflection      │        │  tts_request_gateway→audio_ready │
│  prompts (cache-first)        │        └──────────────────────────────────┘
│  evidence memory (memory_f/)  │
│  usage ledger + settlement    │──► OpenRouter / Alibaba Model Studio / Ollama (LLMs)
│  tools layer (f_tools, MCP) ──┼──► MCP servers over stdio/HTTP:
└───────┬───────────────────────┘      · servers/openamika-agents ─HTTP─► Hermes Agent :8642
        │                              │                         └HTTP─► nanoMuse :8787
        │                              · servers/openamika-iot ─MQTT─► mosquitto · zigbee2mqtt
        │                              · Home Assistant, any other MCP server
        ▼ POST /embed
┌───────────────────────────────┐
│  EMBEDDER  embed_server.py    │  EmbeddingGemma 2 · 768 dims · port 8011 · local GPU
└───────────────────────────────┘
```

**Turn flow (voice):** mic PCM → VAD/partials → commit → brain `message` → surface 4-liner → deep stream →
`hane_parser` splits `[emotion]"sentences"` → TTS per sentence (voice adapter may remap the tag) →
`tts_audio {audio_b64, emotion, pause_after}` to the browser → the avatar engine plays audio + fires
expression/motion at playback start → `tts_playing/finished` acks feed barge-in anchoring and the latency
timeline → memory writer archives the protected turn; reflection watches and may open the next turn
proactively. With tools on, the post-speech clerk runs after the turn's text is final (below): she always
speaks first, nothing runs before she has.

**Processes** are started by `launch.py` (the services in `settings.json` and the agents' `autostart` from
`config/tools.json` first, in parallel; then embedder → brain → vision → speech → messengers, each
health-gated, logs in `logs/`), stopped by `Stop_OpenAmika.ps1` strictly by script path and
`logs/instance.json`. Ports, interpreters, TTS URL and the embedder block live in `settings.json`; the
helpers' URLs, keys and start commands in `config/tools.json`.

## Her helpers and the paper doll

Personal task agents (Hermes Agent, nanoMuse) are separate programs on the same PC. OpenAmika never embeds them:
it starts them (optionally), talks to them over HTTP through a small MCP bridge, and draws their side of the
exchange on her stage with a second Live2D model, the paper doll. Setup: [TOOLS.md](TOOLS.md), *Her helpers*.

```
 config/tools.json  {"enabled": true, "mcpServers": {"hermes": {"kind": "agent", "autostart": …, "env": {HERMES_URL,
       │                                                         HERMES_API_KEY, …}}, "nanomuse": {…}}}
       │ read by
       ├──► launch.py ── starts autostart (hermes.exe gateway / nanomuse.exe serve), waits for /health ──┐
       │                 pre-flight: agent rows, Hermes's key vs its home's .env, the paper doll row      │
       │                                                                                                  ▼
       ├──► brain/f_tools.py ── spawns `python -m openamika_agents <hermes|nanomuse>` (MCP over stdio,   Hermes Agent
       │      │                 cwd servers/openamika-agents), probes `health` every 30 s,              127.0.0.1:8642
       │      │                 restarts a helper that is down (detached)                               (API_SERVER_KEY
       │      │                                                                                          as bearer)
       │      ├─ capability block: she knows her helpers and whether they can work                           ▲
       │      ├─ the clerk (after she spoke): quote-checked hermes__ask {task}  ──► bridge ── POST /v1/runs ─┘
       │      │                                                                       └── /api/threads/{id}/send ──► nanoMuse
       │      └─ the poller: status every 2.5 s → progress / question / approval / result     127.0.0.1:8787 (token file)
       │            → the tool dialogue + her relays (injected turns) + the ledger (agent:hermes)
       │
       └──► speech/api.py ── GET /api/build_f/tools (proxied) ──► ui/use.js polls it every 2 s:
                 agents on?  → live2d-engine.js seats the paper doll (first avatar with "role": "agent")
                 jobs running? → the doll thinks · each new dialogue line → a speech bubble (hers or the doll's)
```

- **The bridge** (`servers/openamika-agents`): one MCP server process per agent, started by the brain with its own
  interpreter (nothing to install). Every agent looks the same to her: `ask / status / answer / stop`, plus
  `health` for the brain. Jobs run in the bridge; the brain polls them.
- **Consent**: she decides in character; the clerk only transcribes what she agreed to, and every call quotes your
  words (verified in code). Approvals and questions come back through her; only your words or a click on the Use
  page answer them.
- **The paper doll** (`avatar/models/Host/`, from `scripts/setup_tts.py`; Hugging Face `live2d/Host/`): a Live2D
  model whose `banks.json` says `"role": "agent"`. The gateway's avatar registry marks it, her model picker never
  offers it, and the Use page seats it bottom-right (placement from its `banks.json`) whenever the tool layer is on
  and an agent server is enabled. Its motions follow the dialogue, picked by its own state loop only: `think*`
  while a job runs, `talk*` / `listen*` with the bubbles, `happy*` on a result, `worried*` on an error,
  `surprise*` on a question or approval, `idle*` otherwise. Steps the bridge makes itself (`sent`, `started`…,
  marked `internal`) stay in the transcript, off the doll. Nothing in Hermes or nanoMuse knows about it.
- **Health**: a helper that is down, refuses tasks, rejects the key or has no model shows red on the Tools page,
  goes into her prompt (she does not promise it anything), and an `ask` to it is not sent. The launcher's
  pre-flight checks the same before anything starts.

**Key contracts**
- `po_contract.py` — one module installed identically in `brain/` and `speech/`: emotion vocabulary
  (69 keys), SFX names, deep/surface parsing, template self-check. Parity is test-enforced.
- Socket protocol (brain↔speech): `message/inject/protect/commit/speech_done` in;
  `begin/surface_chunk/surface_done/deep_chunk/deep_done/end/error` + `call_receipt` out.
- Browser protocol (speech↔UI): `ready/partial/commit/ai_begin/ai_surface_done/ai_deep_done/ai_end/`
  `tts_audio/tts_sfx/ai_call_receipt/ai_timeline/...` (see `brain/PROTOCOL.md` and `/flow`).
- Avatar feed: one `{audio, emotion, pause_after}` message per sentence — the atomic coupling that
  keeps face and voice in sync.
- Agent bridge (brain↔bridge, MCP tools): `ask {task, context}` → `{job, state}`; `status {job, cursor}` →
  `{state, events, pending, result|error, usage}`; `answer {job, text | approval_id, approved}`; `stop {job}`;
  `health` → `{ok, reason, detail, version}` (`servers/openamika-agents/README.md`).

**Data on disk (all gitignored):** `memory_f/` (evidence store: `state.json` with its vector sidecar
`state.vectors-*.f32`, pending jobs in `jobs/` and finished ones under `jobs/completed/`, quarantine,
receipts, ledger; recall scores the vectors with numpy, `memory_numpy_recall: false` in `config/build_f.json`
falls back to the per-item Python cosine),
`brain/memories/` (rolling transcript), `logs/` (with `agent-<name>.log` for each helper the launcher or the
brain started), `brain/.env`, `settings.json`, `config/tools.json`, `avatar/models/*` (her model, the paper doll).
The helpers keep their own data outside: Hermes in its home (`%LOCALAPPDATA%\hermes`), nanoMuse in its
`data_dir` (`Nanomuse\data`).
