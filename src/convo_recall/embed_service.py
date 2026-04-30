"""
convo-recall embedding sidecar.

Loads BAAI/bge-large-en-v1.5 once, then serves POST /embed requests over a
Unix-domain socket. Keeps the model warm so `recall search` stays fast.

Protocol (v1):
  POST /embed  {"text": "...", "mode": "query"|"document"}
               → {"vector": [...1024 floats...], "dim": 1024, "protocol": 1}
  GET  /healthz → {"model": "...", "dim": 1024, "device": "...", "protocol": 1}
"""

import asyncio
import functools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

PROTOCOL_VERSION = 1
DEFAULT_MODEL = "BAAI/bge-large-en-v1.5"
# Pin model revision to mitigate supply-chain risk: a future malicious
# upload to BAAI/bge-large-en-v1.5 cannot silently replace the weights once
# this revision is locked. SHA below is the v1.5 commit pulled on first use
# during 2026 Q2 — bump deliberately when upgrading models. Override with
# CONVO_RECALL_MODEL_REVISION env var for testing.
DEFAULT_MODEL_REVISION = os.environ.get(
    "CONVO_RECALL_MODEL_REVISION",
    "d4aa6901d3a41ba39fb536a557fa166f842b0e09",
)
DEFAULT_SOCK = Path(os.environ.get("CONVO_RECALL_SOCK",
                    Path.home() / ".local" / "share" / "convo-recall" / "embed.sock"))
SEMAPHORE_SIZE = 8
QUEUE_MAX = 32

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


class _Model:
    _instance: Optional["_Model"] = None

    def __init__(self, model_name: str = DEFAULT_MODEL,
                 revision: str = DEFAULT_MODEL_REVISION):
        import torch
        from sentence_transformers import SentenceTransformer
        self.name = model_name
        self.revision = revision
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        log.info(f"Loading {model_name}@{revision[:8]} on {self.device}…")
        self.model = SentenceTransformer(
            model_name, device=self.device, revision=revision
        )
        self.dim: int = self.model.get_sentence_embedding_dimension()
        log.info(f"Model ready (dim={self.dim})")

    @classmethod
    def get(cls, model_name: str = DEFAULT_MODEL) -> "_Model":
        if cls._instance is None or cls._instance.name != model_name:
            cls._instance = cls(model_name)
        return cls._instance

    async def encode_batch(self, texts: list[str], mode: str = "document") -> list[list[float]]:
        """Token-level chunking + mean-pool. No tail truncation for long texts."""
        import numpy as np
        tokenizer = self.model.tokenizer
        CHUNK_TOKENS, OVERLAP = 450, 50
        prefixed = [f"search_{mode}: {t}" for t in texts]

        all_chunks: list[str] = []
        chunk_owner: list[int] = []
        for idx, text in enumerate(prefixed):
            tokens = tokenizer.encode(text, add_special_tokens=False)
            if len(tokens) <= CHUNK_TOKENS:
                all_chunks.append(text)
                chunk_owner.append(idx)
            else:
                step = CHUNK_TOKENS - OVERLAP
                for j in range(0, len(tokens), step):
                    all_chunks.append(tokenizer.decode(tokens[j: j + CHUNK_TOKENS]))
                    chunk_owner.append(idx)
                    if j + CHUNK_TOKENS >= len(tokens):
                        break

        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            functools.partial(self.model.encode, all_chunks,
                              batch_size=64, show_progress_bar=False,
                              normalize_embeddings=True),
        )
        raw = np.asarray(raw)
        owner_arr = np.asarray(chunk_owner)
        result = []
        for i in range(len(texts)):
            mask = owner_arr == i
            pooled = raw[mask].mean(axis=0)
            norm = np.linalg.norm(pooled)
            result.append((pooled / norm if norm > 0 else pooled).tolist())
        return result


async def _embed_handler(request):
    from aiohttp import web
    if request.app["pending"] >= QUEUE_MAX:
        return web.json_response({"error": "too many requests"}, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    text = data.get("text")
    mode = data.get("mode", "document")
    if not text:
        return web.json_response({"error": "missing text"}, status=400)
    if mode not in ("query", "document"):
        return web.json_response({"error": f"invalid mode: {mode!r}"}, status=400)

    request.app["pending"] += 1
    try:
        async with request.app["sem"]:
            t0 = time.time()
            vecs = await request.app["model"].encode_batch([text], mode=mode)
            elapsed = round((time.time() - t0) * 1000, 1)
    finally:
        request.app["pending"] -= 1

    vec = vecs[0]
    log.info(json.dumps({"ms": elapsed, "mode": mode, "len": len(text)}))
    return web.json_response({"vector": vec, "dim": len(vec), "protocol": PROTOCOL_VERSION})


async def _healthz_handler(request):
    from aiohttp import web
    m = request.app["model"]
    return web.json_response({
        "model": m.name, "dim": m.dim, "device": m.device,
        "protocol": PROTOCOL_VERSION,
    })


def _build_app(model: _Model) -> "web.Application":
    from aiohttp import web
    app = web.Application()
    app["model"] = model
    app["sem"] = asyncio.Semaphore(SEMAPHORE_SIZE)
    app["pending"] = 0

    async def _on_startup(app):
        sock = app["sock"]
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass

    app.on_startup.append(_on_startup)
    app.add_routes([web.post("/embed", _embed_handler),
                    web.get("/healthz", _healthz_handler)])
    return app


async def _serve(sock_path: Path, model_name: str) -> None:
    from aiohttp import web
    model = _Model.get(model_name)
    app = _build_app(model)
    app["sock"] = str(sock_path)

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    os.chmod(sock_path, 0o600)
    log.info(f"Listening on {sock_path}")

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (2, 15):  # SIGINT, SIGTERM
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("Shutting down…")
    await runner.cleanup()
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass


def serve(sock_path: Optional[Path] = None, model_name: str = DEFAULT_MODEL) -> None:
    """Start the embedding sidecar (blocks until SIGINT/SIGTERM)."""
    path = sock_path or DEFAULT_SOCK
    try:
        asyncio.run(_serve(path, model_name))
    except KeyboardInterrupt:
        pass
