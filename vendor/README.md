# vendor/: the voice server

OpenAmika speaks through **`vendor/tts-engine/main.py`, the OpenAmika voice server: a MekaHime build of
GPT-SoVITS (v2ProPlus)**, derived from the OpenSakura voice server. The server code is in this repository (MIT);
the upstream GPT-SoVITS checkout, its pretrained models, the Python venv, ffmpeg and Amika's voice are assembled
from public sources by

```powershell
envs\brain\Scripts\python.exe scripts\setup_tts.py      # after scripts\setup_windows.ps1; any Python 3.11 runs it
```

Amika's voice is downloaded from the Hugging Face repository
[MekaHime/MekaHime_Amika](https://huggingface.co/MekaHime/MekaHime_Amika) after you accept its personal-use
license when the script asks (no account is needed once the repository is public; until then, a read token in
`HF_TOKEN`), and every file is checked against `scripts/MekaHime_Amika.manifest.json`. The same repository, under the same license, holds her
Live2D model and the paper doll that stands in for her helpers; the script puts them into `avatar/models/Amika/` and
`avatar/models/Host/`.
Install details, flags, layout and the package list are in [`tts-engine/README.md`](tts-engine/README.md).

OpenAmika's launcher starts the server as one process on port 8002 from `settings.json` → `services.tts`
(`settings.example.json` has the block; `tts_url` points at it and `tts_character` names the voice). Without a
GPU, install it with `--cpu`, or point `tts_url` at any machine that runs a server speaking the contract below;
without any voice server she answers in text only.

## Contract (any server that speaks this works)

```
emit  tts_request_gateway  {task_id, turn_id, text: "【happy2】.Hello!", character_id, language, audio_format}
on    task_queued          {task_id, sentence_count}
on    audio_ready          {task_id, audio: <bytes>, audio_format, audio_duration, is_final}
on    error                {task_id, error}
GET   /ping  -> ok         GET /api/characters -> {characters: {...}}
```

Emotion tags are `【<name><intensity>】` (`happy1-4`, `sad1-4`, `angry1-4`, `shy1-3`, `tsun1-3`,
`whisper1-3`, ...), the keys of the speaker's `references/emotions.json`; a tag without a recording falls back
to `neutral1`. Amika's voice has 69 tags, each with a recorded clip. `speech/voice_adapter.py` turns her 69
internal `register_emotion` keys into these tags. A non-empty `turn_id` asks for one audio for the whole text
(OpenAmika sends one sentence per request). `audio_format` is
`wav` (default) or `opus`. Optional request fields: `speed` (0.5–2.0), `top_k` (1–100), `top_p` (0–1],
`temperature` (0–2], `seed`, and, from the voice adapter's `direction` mode, `instruction` (a written delivery
direction) and `cfg_scale` (1–5); servers that do not use a field ignore it. `audio_ready` may carry a `warning` (for
example when words may be missing, or when a text had nothing to speak, which returns 0.3 s of silence instead of an
error).

## Optional: Breeze TTS 2 (`breeze-bridge/`)

[`breeze-bridge/`](breeze-bridge/README.md) speaks this contract in front of a Breeze-TTS-2.cpp `breeze-server`
that you install and run yourself, cloning a saved voice and following the written direction of each sentence
(Setup → Voice → Voice adapter: *Written directions*). English and Chinese only; the Breeze weights are for research
and non-commercial use.

License: the server code is MIT (upstream GPT-SoVITS is MIT); the voice model is a MekaHime asset for
personal, hobbyist use only.
