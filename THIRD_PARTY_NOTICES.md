# Third-party notices

OpenAmika's own code is MIT (`LICENSE`). These components are bundled in, or fetched by, this repository and
keep their own licenses.

## Bundled in this repository

| Component | Where | License |
|---|---|---|
| SenseVoice model code (FunAudioLLM / FunASR) | `speech/model.py`, `speech/utils/ctc_alignment.py` | MIT, see `speech/LICENSE-SenseVoice` |
| PixiJS 6.5.8 (legacy build) | `ui/vendor/pixi-legacy.min.js` | MIT, (c) 2013-2018 Mathew Groves, Chad Engler |
| pixi-live2d-display 0.4.0 (Cubism 4 build) | `ui/vendor/cubism4.min.js` | MIT, (c) 2020 guansss |
| Live2D Cubism Core for Web | `ui/vendor/live2dcubismcore.min.js` | **Live2D Proprietary Software License Agreement** (the file is marked "Redistributable Code"), (c) Live2D Inc. Not MIT. |

The Live2D Cubism Core terms: https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html.
If you publish your own app or content built with the Cubism SDK, read Live2D's SDK release license
(https://www.live2d.com/en/sdk/license/) first; whether it applies depends on who you are and what you publish.

## Downloaded at setup or run time (not in this repository)

| What | Fetched by | License |
|---|---|---|
| SenseVoiceSmall model files (`iic/SenseVoiceSmall`) | `scripts/download_sensevoice.py` → `speech/models/` | FunASR Model Open Source License Agreement |
| Silero VAD | `pip install silero-vad` (speech env) | MIT |
| FunASR, ModelScope | pip (speech env) | MIT / Apache-2.0 |
| google/embeddinggemma-2 (memory embeddings) | `brain/embed_server.py`, Hugging Face cache | Apache-2.0 |
| Qwen/Qwen3-VL-2B-Instruct, Qwen3-VL-4B-Instruct (optional vision) | `vision/vlm_service.py`, Hugging Face cache | Apache-2.0 |
| Qwen/Qwen2.5-VL-3B-Instruct (optional vision) | same | Qwen Research License (non-commercial); check the model card |
| HuggingFaceTB/SmolVLM2 (optional vision) | same | Apache-2.0 |
| Google Fonts: Fraunces, Noto Serif JP, Rajdhani | loaded by `ui/openamika.css` from fonts.googleapis.com | SIL Open Font License 1.1 |
| Any LLM you pick (OpenRouter, Alibaba Model Studio, Ollama) | at run time | the provider's / model's own terms |

Model licenses can change; the model card on Hugging Face (or ModelScope) is authoritative.

## Not included

- Amika's Live2D model, her voice and the paper doll that stands in for her helpers are not in this
  repository: `scripts/setup_tts.py` downloads them from the Hugging Face repository `MekaHime/MekaHime_Amika`
  after you accept their license, (c) MekaHime Inc., personal use only plus its creator rules (social media, fan
  merch up to US$1,000), 18+, no redistribution, no training other models on them. Any other Live2D model: see `avatar/models/README.md`.
- Hermes Agent and nanoMuse (task agents) are not part of OpenAmika either: you install them yourself under their
  own licenses (Hermes Agent: MIT; nanoMuse: GPL-3.0-or-later), and OpenAmika only talks to them over HTTP
  (`docs/TOOLS.md`).
- Her voice server's code is in `vendor/tts-engine/` (MIT). Upstream GPT-SoVITS and its pretrained models are
  downloaded by `scripts/setup_tts.py` under their own licenses (`vendor/tts-engine/THIRD_PARTY_NOTICES.md`).
