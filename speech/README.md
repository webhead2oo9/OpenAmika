# speech/ — the speech gateway

`speech/api.py` (FastAPI) is the part of OpenAmika your browser talks to. It runs the local speech recognition
(**SenseVoiceSmall** with Silero VAD and barge-in), streams the brain's replies through the shared output parser,
splits them into sentences, sends each sentence to her voice server (`vendor/tts-engine`, or any server that
speaks `docs/TTS.md`), and serves every web page. `launch.py` starts
it in the speech environment; it binds to 127.0.0.1.

| file | what |
|---|---|
| `api.py` | the gateway: pages, `/ws/dialog` (audio in, replies + audio out), STT, TTS client, control proxies to the brain |
| `model.py`, `utils/ctc_alignment.py` | the SenseVoice model code (FunAudioLLM, MIT, see `LICENSE-SenseVoice`) |
| `models/` | the SenseVoiceSmall files, not in git: `scripts/download_sensevoice.py` (see `models/DOWNLOAD.md`) |
| `hane_parser.py`, `po_contract.py` | streaming parsers for the surface/deep output (`po_contract.py` is identical to `brain/po_contract.py`) |
| `sentence_splitter.py`, `tts_emotion.py`, `tts_pause.py`, `tts_sfx.py` | sentence splitting, emotion tags, pauses, sound-effect tags |
| `voice_adapter.py` | fits her 69 emotion keys to the voice: old-style voice ids, written directions or raw (see `docs/TTS.md`) |
| `stt_carry.py` | carries a force-committed STT segment into the next turn |
| `static/flow.html` | the interactive architecture map at `/flow` |

## Environment (set by launch.py from settings.json)

| variable | default | |
|---|---|---|
| `YUMEHA_HOST` / `YUMEHA_PORT` | `127.0.0.1` / `53000` | bind address and port (`chat_port`) |
| `YUMEHA_DEVICE` | `cuda:0` | STT device (`speech_device`); `cpu` works, slower |
| `YUMEHA_MODEL` | `speech/models` | SenseVoiceSmall folder |
| `HANE_URL` | `http://127.0.0.1:5202` | the brain |
| `AMIKA_TTS_URL` / `AMIKA_TTS_CHARACTER` | `http://127.0.0.1:8002` / `MekaHime_Amika` | the voice server and voice id |
| `AMIKA_VLM_URL` | empty (vision off) | the optional vision service |
| `AMIKA_MESSENGERS_URL` | `http://127.0.0.1:5215` | the Discord/Telegram service (the launcher sets it from `messengers_port`) |

## Endpoints

- Pages: `/use`, `/setup`, `/tools`, `/flow`; `GET /health` (device, GPU name, model state).
- `WS /ws/dialog`: the conversation (binary PCM + JSON both ways; see `brain/PROTOCOL.md` and `/flow`).
- `WS /ws/camera`, `WS /ws/vlm_feed`, `GET /api/vision`: the optional camera.
- `/api/build_f/*`: proxies to the brain (status, models, persona, tools, memory wipe/reset); changes require the
  page's own origin.
- `/api/avatar/*`, `/api/stage*`: Live2D models from `avatar/models/` and the stage backdrop.
- `/api/voice_map*`, `/api/tts_probe`, `/api/tts_say`, `/api/name_pronunciation`: voice settings.

### Plain speech recognition: `POST /api/v1/asr`

The gateway also exposes SenseVoiceSmall as a simple API (multipart/form-data):

| field | required | default | |
|---|---|---|---|
| `files` | yes | | one or more audio files (wav, mp3, flac, ogg, …) |
| `lang` | no | `auto` | `auto`, `zh`, `en`, `yue`, `ja`, `ko` |
| `use_itn` | no | `true` | inverse text normalisation (punctuation) |
| `keys` | no | file names | comma-separated ids for the results |

```bash
curl -X POST http://127.0.0.1:53000/api/v1/asr -F "files=@audio.wav" -F "lang=auto"
```

```json
{
  "result": [{"key": "audio.wav", "text": "…", "clean_text": "…", "raw_text": "<|en|><|NEUTRAL|><|Speech|>…"}],
  "meta": {"inference_time": 0.058, "total_time": 0.072, "audio_duration": 10.24}
}
```

Tags in `raw_text`: language `<|zh|> <|en|> <|yue|> <|ja|> <|ko|>`; emotion `<|HAPPY|> <|SAD|> <|ANGRY|>
<|NEUTRAL|> <|FEARFUL|> <|DISGUSTED|> <|SURPRISED|>`; events `<|BGM|> <|Speech|> <|Applause|> <|Laughter|>
<|Cry|> <|Sneeze|> <|Breath|> <|Cough|>`. On a GPU a 10-second clip takes about 0.06 s.
