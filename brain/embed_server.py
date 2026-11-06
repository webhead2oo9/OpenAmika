"""Local embedding service for the Amika memory (Build F / PromptOptimized clone).

Serves google/embeddinggemma-2 (Apache 2.0; text encoder only, 270M parameters, bf16) at its native 768
dimensions, with the model's own query / document task prompts. Any sentence-transformers model works with
--model; one without prompts (BAAI/bge-m3, the earlier default, chosen by Amika_Embedding_Experiment) is
encoded as is. Vectors: model output -> L2 normalise -> truncate to --dims -> L2 normalise again.

Runs with a Python that has torch + sentence-transformers (brain/requirements-embedder.txt).
Standard library only otherwise, so it needs no FastAPI. Started by launch.py before the brain.

    python -X utf8 embed_server.py --port 8011                       # serve
    python -X utf8 embed_server.py --check                           # imports + cached weights, no load
    python -X utf8 embed_server.py --download                        # fetch the weights now (scripts/setup_core.py)

API (JSON):
    GET  /health                -> {"ok": true, "model", "tag", "dims", "device", "loaded_s", "requests"}
    POST /embed  {"texts": [...], "task_type": "query"|"document"}
                                -> {"model": tag, "dims": 768, "embeddings": [[...], ...],
                                    "usage": {"prompt_tokens": n, "completion_tokens": 0, "total_tokens": n}, "ms": elapsed}
The brain meters each call as provider "local", priced at zero in config/pricing.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def cached_snapshot(repo: str) -> bool:
    """Is a snapshot of `repo` in the local Hugging Face cache? A plain filesystem check, made before the
    Hub libraries are imported (they read HF_HUB_OFFLINE at import time)."""
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub")
    snapshots = os.path.join(hub, "models--" + repo.replace("/", "--"), "snapshots")
    try:
        return any(os.path.isfile(os.path.join(snapshots, d, "config.json")) for d in os.listdir(snapshots))
    except OSError:
        return False

DEFAULT_MODEL = "google/embeddinggemma-2"
DEFAULT_TAG = "embeddinggemma-2-d768"
DEFAULT_DIMS = 768
DEFAULT_DTYPE = "bf16"          # EmbeddingGemma overflows in fp16 (NaN vectors): bf16 on the GPU, fp32 on the CPU
DOWNLOAD_HINT = "about 1.5 GB for google/embeddinggemma-2"
MAX_TEXTS = 256
MAX_CHARS = 20000


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[embed]", message, flush=True)


def finish(vectors, dims: int):
    """L2-normalise, truncate to `dims`, normalise again. Pure Python; rejects non-finite or zero vectors."""
    out = []
    for vec in vectors:
        vec = [float(x) for x in vec]
        if dims > len(vec):
            raise ValueError(f"dims {dims} exceeds the model's native {len(vec)}")
        norm = math.sqrt(sum(x * x for x in vec))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("non-finite or zero vector")
        vec = [x / norm for x in vec[:dims]]
        norm = math.sqrt(sum(x * x for x in vec))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("non-finite or zero vector after truncation")
        out.append([x / norm for x in vec])
    return out


def load_model(model_id: str, device: str, torch_dtype=None):
    """SentenceTransformer for `model_id`. A multimodal model (EmbeddingGemma 2) is loaded as its text encoder
    only: memory search embeds text, and the vision/audio encoders would only cost memory."""
    from sentence_transformers import SentenceTransformer
    from transformers import AutoConfig
    config = AutoConfig.from_pretrained(model_id)
    text_only = {k: None for k in ("vision_config", "audio_config") if getattr(config, k, None) is not None}
    return SentenceTransformer(model_id, device=device, config_kwargs=text_only or None,
                               model_kwargs={"torch_dtype": torch_dtype} if torch_dtype is not None else None)


def embedding_dims(model) -> int:
    dims = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return int(dims() or 0)


class LocalEncoder:
    """sentence-transformers wrapper; one GPU, so encodes are serialised."""

    def __init__(self, model_id: str, dims: int, dtype: str, device: str, max_length: int, tag: str):
        import torch
        self.torch = torch
        self.model_id, self.dims, self.tag, self.device = model_id, dims, tag, device
        td = {"fp16": torch.float16, "bf16": torch.bfloat16, "fp32": torch.float32}[dtype]
        t0 = time.perf_counter()
        self.model = load_model(model_id, device, td)
        self.model.to(td)
        self.model.eval()
        limit = self.model.max_seq_length or max_length
        self.model.max_seq_length = min(max_length, limit)
        self.native = embedding_dims(self.model)
        # the model's own task prompts (EmbeddingGemma: "task: search result | query: " / "title: none | text: ");
        # a model without them (bge-m3) is encoded as is
        prompts = getattr(self.model, "prompts", None) or {}
        self.prompt_names = {task: task for task in ("query", "document") if prompts.get(task)}
        self.loaded_s = time.perf_counter() - t0
        self.lock = threading.Lock()
        if self.native and self.native < dims:
            raise RuntimeError(f"{model_id} produces {self.native} dims, fewer than the configured {dims}")
        try:
            self.encode(["warm-up"], "query")
        except ValueError as exc:
            raise RuntimeError(f"{model_id} gives unusable vectors in {dtype} ({exc}); use --dtype bf16 on the GPU "
                               "or fp32 (EmbeddingGemma overflows in fp16)") from exc

    def encode(self, texts, task_type: str):
        with self.lock:
            with self.torch.inference_mode():
                out = self.model.encode(list(texts), prompt_name=self.prompt_names.get(task_type), batch_size=32,
                                        convert_to_tensor=True, normalize_embeddings=False, show_progress_bar=False)
            raw = out.float().cpu().tolist()
        return finish(raw, self.dims)

    def token_count(self, texts) -> int:
        ids = self.model.tokenizer(list(texts), add_special_tokens=True, truncation=True,
                                   max_length=self.model.max_seq_length)["input_ids"]
        return sum(len(x) for x in ids)


def make_server(encoder, host: str, port: int) -> ThreadingHTTPServer:
    """HTTP server bound to `encoder` (anything with .encode(texts, task_type), .token_count(texts),
    .model_id, .tag, .dims, .device, .loaded_s). Tests inject a fake encoder."""
    started = time.time()
    counter = {"requests": 0, "errors": 0}

    class Handler(BaseHTTPRequestHandler):
        server_version = "amika-embed/1"

        def log_message(self, fmt, *args):   # keep the console for our own lines
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] != "/health":
                return self._send(404, {"ok": False, "error": "unknown path"})
            self._send(200, {"ok": True, "model": encoder.model_id, "tag": encoder.tag, "dims": encoder.dims,
                             "device": encoder.device, "loaded_s": round(float(encoder.loaded_s), 2),
                             "uptime_s": round(time.time() - started, 1), **counter})

        def do_POST(self):
            if self.path.split("?")[0] != "/embed":
                return self._send(404, {"ok": False, "error": "unknown path"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, UnicodeDecodeError):
                counter["errors"] += 1
                return self._send(400, {"ok": False, "error": "body must be JSON"})
            texts = data.get("texts") if isinstance(data, dict) else None
            task_type = str((data or {}).get("task_type") or "document")
            if (not isinstance(texts, list) or not texts or len(texts) > MAX_TEXTS
                    or any(not isinstance(t, str) or not t.strip() or len(t) > MAX_CHARS for t in texts)):
                counter["errors"] += 1
                return self._send(400, {"ok": False, "error": f"texts must be 1..{MAX_TEXTS} non-empty strings of at most {MAX_CHARS} chars"})
            if task_type not in ("query", "document"):
                counter["errors"] += 1
                return self._send(400, {"ok": False, "error": "task_type must be query or document"})
            t0 = time.perf_counter()
            try:
                vectors = encoder.encode(texts, task_type)
                tokens = int(encoder.token_count(texts))
            except Exception as exc:  # the brain blocks the job and reports; never crash the server
                counter["errors"] += 1
                log(f"embed failed: {type(exc).__name__}: {exc}")
                return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            counter["requests"] += 1
            self._send(200, {"model": encoder.tag, "dims": encoder.dims, "embeddings": vectors,
                             "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens},
                             "ms": round((time.perf_counter() - t0) * 1000.0, 1)})

    return ThreadingHTTPServer((host, port), Handler)


def check(model_id: str) -> int:
    """Imports and cached weights only; no model is loaded, nothing is downloaded."""
    try:
        import torch
        import sentence_transformers
        import transformers
    except ImportError as exc:
        print(f"embed_server check failed: {exc}")
        return 1
    if os.path.isdir(model_id):
        path = model_id
    else:
        try:
            from huggingface_hub import snapshot_download
            path = snapshot_download(model_id, local_files_only=True)
        except Exception as exc:
            # not an error: the first start downloads it (online once), later starts run offline
            print(f"embed_server check: torch {torch.__version__} cuda={torch.cuda.is_available()}; "
                  f"{model_id} not cached yet ({type(exc).__name__}): the first start downloads it ({DOWNLOAD_HINT})")
            return 0
    print(f"embed_server check: torch {torch.__version__} cuda={torch.cuda.is_available()} "
          f"sentence-transformers {sentence_transformers.__version__} transformers {transformers.__version__}; "
          f"{model_id} cached at {path}")
    return 0


def download(model_id: str) -> int:
    """Put the model into the Hugging Face cache now, with the same loader the server uses (on the CPU, nothing is
    served), so the first start does not wait for it. scripts/setup_core.py runs this; a second run only re-checks."""
    t0 = time.perf_counter()
    try:
        native = embedding_dims(load_model(model_id, "cpu"))
    except Exception as exc:  # noqa: BLE001  (reported in one line for the setup summary)
        print(f"embed_server download failed: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
        return 1
    print(f"embed_server download: {model_id} is in the Hugging Face cache ({native} dims, "
          f"{time.perf_counter() - t0:.0f} s)", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--tag", default=DEFAULT_TAG, help="name recorded in the ledger / vector marker")
    ap.add_argument("--dims", type=int, default=DEFAULT_DIMS)
    ap.add_argument("--dtype", default=DEFAULT_DTYPE, choices=["fp16", "bf16", "fp32"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--max-length", type=int, default=1024)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--check", action="store_true", help="verify imports and cached weights, then exit")
    ap.add_argument("--download", action="store_true", help="download the weights into the Hugging Face cache, then exit")
    args = ap.parse_args()
    if args.check:
        return check(args.model)
    if args.download:
        return download(args.model)
    if os.path.isdir(args.model) or cached_snapshot(args.model):
        # weights are local: never ask the Hub (a reset connection during a HEAD request used to kill the load)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    else:
        log(f"{args.model} is not in the Hugging Face cache yet: downloading it once ({DOWNLOAD_HINT})")
    log(f"loading {args.model} ({args.dtype}, {args.device}, dims {args.dims}, max_length {args.max_length})")
    encoder = LocalEncoder(args.model, args.dims, args.dtype, args.device, args.max_length, args.tag)
    log(f"loaded in {encoder.loaded_s:.1f}s; native dims {encoder.native}; max_seq_length {encoder.model.max_seq_length}; "
        f"task prompts {sorted(encoder.prompt_names) or 'none'}")
    server = make_server(encoder, args.host, args.port)
    log(f"serving http://{args.host}:{args.port} (GET /health, POST /embed)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
