# Breeze voice bridge

An optional OpenAmika voice server backed by **Breeze-TTS-2.cpp** (`breeze-server`, the C++ port of
BreezeBlue/Breeze-TTS-2). This small Python server translates OpenAmika's Socket.IO requests into
Breeze's HTTP speech API and wraps its raw PCM in WAV. It does not install or start Breeze, download
models, or replace the default GPT-SoVITS server. English only.

## Run

Start `breeze-server` separately, bound to loopback, with its HTTP API at `http://127.0.0.1:8080`.
Save a voice named `main_voice` in its web UI. The bridge uses HTTP, not Breeze's WebSocket port 8081.
Both servers are unauthenticated: **bind and connect only to loopback**. The bridge rejects non-loopback
`--host` and `--breeze` addresses and does not follow upstream redirects. Like OpenAmika's other local
services it applies `local_guard.py` (an identical copy of `brain/local_guard.py`) to every route and the
Socket.IO handshake: a non-loopback `Host` (DNS rebinding) and a page from another origin get 403.

From the OpenAmika root, using the existing Python 3.11 speech environment:

```powershell
envs\speech\Scripts\python.exe vendor\breeze-bridge\main.py
```

The two dependencies are already in `speech/requirements.txt`, with the same minimum versions. To use
another Python 3.11 environment, install them there first:

```powershell
python -m pip install -r vendor\breeze-bridge\requirements.txt
python vendor\breeze-bridge\main.py --port 8002 --breeze http://127.0.0.1:8080
```

On Linux/macOS the existing interpreter is `envs/speech/bin/python`. Ctrl+C stops the bridge and closes
its active Breeze HTTP stream. Only one voice server can own port 8002; stop the GPT-SoVITS server
before using that port, or give this bridge a different port and change the URLs below to match.

### OpenAmika settings

Merge these values into `settings.json`. **Replace** `services.tts`, rather than adding a second TTS
service on the same port; leave your other services unchanged. Launcher command/interpreter paths
are relative to the OpenAmika root, while `main.py` is relative to the service's `cwd`:

```json
{
  "tts_url": "http://127.0.0.1:8002",
  "tts_character": "main_voice",
  "services": {
    "tts": {
      "enabled": true,
      "command": [
        "envs\\speech\\Scripts\\python.exe",
        "main.py",
        "--host", "127.0.0.1",
        "--port", "8002",
        "--breeze", "http://127.0.0.1:8080"
      ],
      "cwd": "vendor\\breeze-bridge",
      "health": "http://127.0.0.1:8002/ping",
      "timeout_s": 240
    }
  }
}
```

On Linux/macOS use `envs/speech/bin/python` and `vendor/breeze-bridge` in that entry. Breeze must already
be running: the launcher's health check stays unavailable until Breeze answers `/health` successfully.
Then set Setup → Voice → Voice adapter to **Written directions**: each sentence then carries its register's
`instruction` and the one `cfg_scale`, and the speaker is `tts_character` (`main_voice`). In the *V1 tags*
mode the adapter would swap the speaker to `MekaHime_Amika`, which Breeze does not have; in *Off* the
bridge strips the 69-key tags, Breeze speaks with its default instruction, and the speech layer's leading
`.` start anchor reaches Breeze.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Loopback bind address (`localhost` and loopback IPv6 are also accepted). |
| `--port` | `8002` | Bridge HTTP + Socket.IO port. |
| `--breeze` | `http://127.0.0.1:8080` | Breeze's loopback HTTP origin, without a path or credentials. |
| `--seed` | `42` | Fallback seed. At 42, absent request seeds are omitted so Breeze uses its own documented default; another value is sent when a request omits `seed`. A request seed always wins. |
| `--cors` | off | Admit browser pages from any origin (local testing only); a non-loopback `Host` is still refused. |
| `--request-timeout` | `120` | Positive seconds for one synthesis, including busy retries and PCM streaming, excluding time in the bridge queue. |

The console logs one line per request: voice, CFG (or `default`), normalized character count, audio
seconds, elapsed seconds including queue time, and synthesis real-time factor (RTF). Cancelled, dropped,
and failed requests are identified on that line. Non-English `language` is ignored, with one warning
per bridge process.

## Contract and differences from GPT-SoVITS

The base contract is in [docs/TTS.md](../../docs/TTS.md) and the
[GPT-SoVITS server README](../tts-engine/README.md#contract-any-server-that-speaks-this-works).

```text
emit tts_request_gateway {task_id, turn_id, text, character_id, audio_format,
                          language?, instruction?, cfg_scale?, seed?}
on   task_queued         {task_id, turn_id, sentence_count: 1, queue_position}
on   audio_ready         {task_id, turn_id, audio: <WAV bytes>, audio_format: "wav",
                          audio_duration, sample_rate, is_final: true, text, character_id,
                          elapsed_ms, warning?}
on   error               {task_id, turn_id, error}
```

- **Direction fields:** `instruction` is an optional delivery-direction string, forwarded unchanged;
  `cfg_scale` is an optional number in the inclusive range **1.0–5.0**. Invalid CFG, including booleans,
  numeric strings and non-finite numbers, returns `error: "bad request: ..."` before queueing. `seed`
  is an optional whole number. Null optional fields count as omitted. Missing `instruction` and
  `cfg_scale` are not sent to Breeze. Other voice servers may ignore these new fields.
- **Voice:** `character_id` becomes Breeze's `voice_id`, normally `main_voice`. The bridge does not
  modify, upload or delete voices. Breeze owns model/sampling defaults beyond the forwarded fields.
- **Text:** every `【...】` tag with 1–24 interior characters and no newline is removed, including tags
  in the middle of a sentence, and whitespace is collapsed. The speech layer's leading `.` start anchor is
  removed by the voice adapter in direction mode, not here, so a sentence's own leading dot (`.5 litres`,
  `...I mean`) reaches Breeze. Parenthesized vocal events such as `(sigh)`, `(laugh)`, `(cough)` and
  `(clears throat)` are preserved. The returned `text` is normalized.
- **No words:** blank input is `bad request: empty text`. Nonblank text containing only tags,
  punctuation, symbols or emoji gets 0.3 seconds of silence with a `warning`, without calling Breeze.
- **WAV only:** each request gets exactly one complete final WAV, even without `turn_id`. Unsupported
  `audio_format` values get WAV plus a `warning`. No sentence splitting, Opus encoding, or audio chunk
  events are performed. Successful Breeze output must be mono s16le PCM with `X-Sample-Rate` and
  `X-Sample-Format: s16le`; the WAV preserves samples and sample rate. Duration is sample count / rate.
- **Queue:** a single global FIFO worker runs one Breeze speech request at a time. `queue_position` is
  the number of requests ahead, including the active request. Only the requesting Socket.IO client
  receives its events. HTTP 409 (Breeze busy, e.g. its web UI generating) is retried every 0.25 seconds for
  at most 1 second, then answered with an error: OpenAmika's dialog discards audio that starts later than
  4 s (a turn's first sentence) or 10 s (any sentence) in direction mode, so it skips the sentence instead.
  Other upstream failures return an error.
- **Disconnect:** queued requests for that client are removed, and its active HTTP response is closed
  immediately (also cancelling connection/header waits and retry sleeps). The next remaining client
  keeps its FIFO place. This aborts the bridge's stream; whether Breeze stops model computation upon
  socket closure is an upstream behavior, not something the bridge can guarantee.

### HTTP endpoints

- `GET /ping`: `200 ok` only when Breeze `/health` reports ready; otherwise `503` with a reason.
- `GET /api/characters`: `{"characters": {"<id>": {"id", "saved", "seconds"}}, "current": ...}`,
  freshly projected from Breeze `/v1/voices`. `current` is the first saved voice, or null. Reference
  transcripts (`ref_text`) and other voice metadata are never exposed. Upstream failure returns 503.
- `GET /api/health`: JSON readiness, upstream health, sample rate, queue length and active task id.

## Offline tests

With `requirements-dev.txt` and the bridge dependencies installed:

```powershell
python -m pytest tests/test_breeze_bridge.py -q
```

Tests use synthetic in-process loopback HTTP servers, not a running Breeze or downloaded model. They
cover normalization, CFG validation, PCM/WAV integrity, privacy, queue order, bounded busy retries,
timeouts, and disconnects before and after response headers.
