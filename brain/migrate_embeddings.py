"""One-pass re-embedding of a Build F memory profile with the local embedding service.

Query and document vectors from different models must never be mixed (Amika_Embedding_Experiment
README), so switching the embedder means re-embedding every stored document vector in one pass while the
brain is stopped. This script does that for one profile folder (memory_f/<profile>):

  python -X utf8 migrate_embeddings.py --profile ..\\memory_f\\live --url http://127.0.0.1:8011 --model embeddinggemma-2-d768
  python -X utf8 migrate_embeddings.py --profile ... --apply          # write; dry run otherwise

What --apply does, in order:
  1. backs up state.json to state.json.bak_<old model>_<timestamp>
  2. re-embeds every event and fact document (f_memory.event_document / record_document, the same text the
     brain sends) through POST <url>/embed with task_type "document", in batches, and replaces
     vectors.documents; vectors.queries is emptied (they were query vectors of the old model)
  3. moves query_cache/*.json aside (query_cache_bak_<timestamp>): every entry is keyed by the old model
  4. writes embedding.json {model, dimensions} so MemoryRuntime refuses to start with another model
Jobs under jobs/ are untouched. Events whose job is not complete (queued, blocked, ...) are NOT re-embedded:
their job adds the event vector itself when it finishes (a second copy encoded in a batch differs in the
last bits and CandidateF.ingest refuses conflicting vectors). --release-pending drops such vectors from a
profile that already has them (repair after an earlier migration), so the jobs can complete.

Run it with the brain's Python (it imports f_memory / f_method from this folder); it needs no torch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import f_memory  # noqa: E402
from f_method import CandidateF  # noqa: E402


def http_embed(url: str, texts, task_type: str, timeout: float = 120.0):
    body = json.dumps({"texts": list(texts), "task_type": task_type}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/embed", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.load(response)
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ValueError("embedding count mismatch")
    out = []
    for vec in vectors:
        if (not isinstance(vec, list) or len(vec) != f_memory.DIMENSIONS
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vec)):
            raise ValueError(f"embedding must contain {f_memory.DIMENSIONS} finite numbers")
        norm = math.sqrt(sum(v * v for v in vec))
        if not norm:
            raise ValueError("zero embedding")
        out.append([v / norm for v in vec])
    return out, data.get("model")


def documents_of(model: CandidateF) -> dict:
    docs = {}
    for event in model.events:
        docs[event["id"]] = f_memory.event_document(event)
    for fact in model.records.get("facts", []):
        docs[fact["id"]] = f_memory.record_document(fact)
    return docs


def pending_job_event_ids(profile: Path) -> set:
    """Event ids of jobs that have not completed: their vectors belong to the job, not to a migration."""
    ids = set()
    for path in (profile / "jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("state") != "complete":
            ids.add((job.get("event") or {}).get("id"))
    ids.discard(None)
    return ids


def release_pending(profile: Path, apply: bool, log=print) -> dict:
    """Drop the document vectors of unfinished jobs' events so those jobs can add their own."""
    profile = Path(profile).resolve()
    state_path = profile / "state.json"
    model = CandidateF.reload(state_path)
    payload = CandidateF._vector_payload(model.vectors)
    pending = pending_job_event_ids(profile)
    held = sorted(i for i in pending if i in payload["documents"])
    log(f"pending jobs: {len(pending)} events; {len(held)} of them hold a document vector that must go")
    if not held or not apply:
        return {"pending": len(pending), "released": 0, "applied": False}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = profile / f"state.json.bak_release_{stamp}"
    model.persist(backup, inline=True)      # self-contained: the live vector sidecar is replaced below
    documents = {k: v for k, v in payload["documents"].items() if k not in pending}
    CandidateF(model.events, model.records, {"documents": documents, "queries": payload["queries"]}).persist(state_path)
    log(f"released {len(held)} vectors; backup {backup.name}")
    return {"pending": len(pending), "released": len(held), "applied": True, "backup": str(backup)}


def read_marker(profile: Path):
    path = profile / "embedding.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def migrate(profile: Path, url: str, model_name: str, apply: bool, batch_size: int = 32, log=print) -> dict:
    profile = Path(profile).resolve()
    state_path = profile / "state.json"
    if not state_path.exists():
        raise SystemExit(f"no state.json under {profile}")
    model = CandidateF.reload(state_path)
    payload = CandidateF._vector_payload(model.vectors)
    old_docs = payload["documents"]
    marker = read_marker(profile)
    old_model = (marker or {}).get("model") or f_memory.EMBEDDING_MODEL
    docs = documents_of(model)
    pending = pending_job_event_ids(profile)
    ids = sorted(set(old_docs) | set(docs))
    missing = [i for i in ids if i not in docs]
    left_to_jobs = sorted(i for i in ids if i in pending)
    ids = [i for i in ids if i in docs and i not in pending]
    summary = {"profile": str(profile), "events": len(model.events), "facts": len(model.records.get("facts", [])),
               "old_vectors": len(old_docs), "old_queries": len(payload["queries"]), "to_embed": len(ids),
               "left_to_pending_jobs": len(left_to_jobs),
               "ids_without_document": missing, "old_model": old_model, "new_model": model_name, "applied": False}
    log(json.dumps({k: v for k, v in summary.items() if k != "ids_without_document"}, ensure_ascii=False))
    if missing:
        log(f"warning: {len(missing)} stored vector ids have no event/fact behind them and will be dropped: {missing[:5]}")
    if left_to_jobs:
        log(f"{len(left_to_jobs)} events belong to unfinished jobs and are left to them: {left_to_jobs[:5]}")
    if old_model == model_name and marker is not None:
        log("profile already carries vectors of this model; nothing to do")
        return summary
    # probe the service before touching anything
    probe, served = http_embed(url, ["probe"], "query")
    log(f"service at {url} answers ({served}, {len(probe[0])} dims)")
    if served and served != model_name:
        raise SystemExit(f"service reports model {served!r}, expected {model_name!r}")
    new_docs = {}
    t0 = time.perf_counter()
    for offset in range(0, len(ids), batch_size):
        chunk = ids[offset:offset + batch_size]
        vectors, _ = http_embed(url, [docs[i] for i in chunk], "document")
        new_docs.update(zip(chunk, vectors))
        if (offset // batch_size) % 10 == 0:
            log(f"  embedded {min(offset + batch_size, len(ids))}/{len(ids)}")
    summary["seconds"] = round(time.perf_counter() - t0, 1)
    log(f"embedded {len(new_docs)} documents in {summary['seconds']} s")
    if not apply:
        log("dry run: nothing written (add --apply)")
        return summary
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = profile / f"state.json.bak_{old_model.replace('/', '_')}_{stamp}"
    model.persist(backup, inline=True)      # self-contained: the live vector sidecar is replaced below
    replacement = CandidateF(model.events, model.records, {"documents": new_docs, "queries": {}})
    replacement.persist(state_path)
    cache = profile / "query_cache"
    moved = 0
    if cache.exists():
        aside = profile / f"query_cache_bak_{stamp}"
        aside.mkdir(exist_ok=True)
        for item in cache.glob("*.json"):
            shutil.move(str(item), str(aside / item.name))
            moved += 1
    (profile / "embedding.json").write_text(json.dumps(
        {"model": model_name, "dimensions": f_memory.DIMENSIONS, "migrated_from": old_model,
         "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "documents": len(new_docs), "source": "migrate_embeddings.py"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    reloaded = CandidateF.reload(state_path)
    check = CandidateF._vector_payload(reloaded.vectors)
    assert len(check["documents"]) == len(new_docs) and not check["queries"], "persisted state does not match"
    summary.update(applied=True, backup=str(backup), query_cache_moved=moved, new_vectors=len(new_docs))
    log(f"written: {len(new_docs)} vectors, backup {backup.name}, {moved} query-cache entries moved aside, marker embedding.json")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="memory_f/<profile> folder")
    ap.add_argument("--url", default=os.getenv("AMIKA_EMBEDDING_URL", "http://127.0.0.1:8011"))
    ap.add_argument("--model", default="embeddinggemma-2-d768", help="model tag the service reports and the marker records")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--release-pending", action="store_true",
                    help="only drop the document vectors of unfinished jobs' events (repair); no embedding service needed")
    args = ap.parse_args()
    if args.release_pending:
        summary = release_pending(Path(args.profile), args.apply)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    summary = migrate(Path(args.profile), args.url, args.model, args.apply, args.batch_size)
    print(json.dumps({k: v for k, v in summary.items() if k != "ids_without_document"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
