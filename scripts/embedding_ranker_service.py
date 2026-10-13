"""Private Docker HTTP service for text-only EmbeddingGemma job scoring."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
import tempfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOG = logging.getLogger("jobbot.embedding")
MODEL_ID = "google/embeddinggemma-2"
MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
MODEL_DIR = Path(os.environ.get("EMBEDDING_MODEL_DIR", "/models/embeddinggemma-2-text"))
DOWNLOAD_CACHE = Path(os.environ.get("HF_DOWNLOAD_CACHE", "/models/download-cache"))
MIN_TEXT_PARAMETERS = 250_000_000
MAX_TEXT_PARAMETERS = 300_000_000
MAX_BODY_BYTES = 256 * 1024
MAX_QUERIES = 100
MAX_QUERY_CHARS = 1000
MAX_JOB_FIELD_CHARS = {"title": 500, "company": 300, "location": 300, "description": 12_000}
_QUERY_EMBEDDINGS: OrderedDict[str, Any] = OrderedDict()
_MODEL_LOCK = threading.Lock()


def _parameter_count(model: Any) -> int:
    return sum(int(parameter.numel()) for parameter in model.parameters())


def _saved_tensor_count(directory: Path) -> int:
    from safetensors import safe_open

    weight_files = sorted(directory.rglob("*.safetensors"))
    if not weight_files:
        raise RuntimeError("saved_text_weights_missing")
    total = 0
    for weight_file in weight_files:
        with safe_open(str(weight_file), framework="pt", device="cpu") as checkpoint:
            for key in checkpoint.keys():
                total += math.prod(checkpoint.get_slice(key).get_shape())
    return total


def _load_text_model() -> Any:
    import torch
    from sentence_transformers import SentenceTransformer

    config_kwargs = {"vision_config": None, "audio_config": None}
    model_kwargs = {"torch_dtype": torch.float32}
    marker = MODEL_DIR / "jobbot_text_only.json"
    if marker.is_file():
        model = SentenceTransformer(
            str(MODEL_DIR), device="cpu", config_kwargs=config_kwargs,
            model_kwargs=model_kwargs,
        )
    else:
        DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)
        model = SentenceTransformer(
            MODEL_ID, revision=MODEL_REVISION, cache_folder=str(DOWNLOAD_CACHE),
            device="cpu", config_kwargs=config_kwargs, model_kwargs=model_kwargs,
        )
        count = _parameter_count(model)
        if not MIN_TEXT_PARAMETERS <= count <= MAX_TEXT_PARAMETERS:
            raise RuntimeError("text_only_parameter_count_out_of_range")
        MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="embeddinggemma-text-", dir=MODEL_DIR.parent))
        saved_successfully = False
        try:
            model.save_pretrained(str(temporary), safe_serialization=True,
                                  create_model_card=False)
            saved = SentenceTransformer(
                str(temporary), device="cpu", config_kwargs=config_kwargs,
                model_kwargs=model_kwargs,
            )
            saved_count = _parameter_count(saved)
            if not MIN_TEXT_PARAMETERS <= saved_count <= MAX_TEXT_PARAMETERS:
                raise RuntimeError("saved_text_only_parameter_count_out_of_range")
            saved_weight_count = _saved_tensor_count(temporary)
            if not MIN_TEXT_PARAMETERS <= saved_weight_count <= MAX_TEXT_PARAMETERS:
                raise RuntimeError("saved_checkpoint_contains_non_text_weights")
            (temporary / "jobbot_text_only.json").write_text(
                json.dumps({"model": MODEL_ID, "revision": MODEL_REVISION,
                            "parameters": saved_count,
                            "checkpoint_tensor_elements": saved_weight_count,
                            "modalities": ["text"]},
                           separators=(",", ":")),
                encoding="utf-8",
            )
            if MODEL_DIR.exists():
                shutil.rmtree(MODEL_DIR)
            os.replace(temporary, MODEL_DIR)
            saved_successfully = True
            model = saved
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        finally:
            # The full Hub checkpoint contains vision and audio weights in the
            # same 1.49 GB file. Keep only the verified text-only export.
            if saved_successfully:
                shutil.rmtree(DOWNLOAD_CACHE, ignore_errors=True)
    count = _parameter_count(model)
    if not MIN_TEXT_PARAMETERS <= count <= MAX_TEXT_PARAMETERS:
        raise RuntimeError("loaded_text_only_parameter_count_out_of_range")
    model.max_seq_length = 8192
    LOG.info("Text embedding model ready parameters=%s dimensions=256", count)
    return model


def _clean_request(payload: Any) -> tuple[list[str], dict[str, str]]:
    if type(payload) is not dict or set(payload) != {"queries", "job"}:
        raise ValueError("invalid_request")
    raw_queries = payload["queries"]
    raw_job = payload["job"]
    if type(raw_queries) is not list or not 1 <= len(raw_queries) <= MAX_QUERIES:
        raise ValueError("invalid_queries")
    queries: list[str] = []
    for value in raw_queries:
        if type(value) is not str or not value.strip() or len(value) > MAX_QUERY_CHARS:
            raise ValueError("invalid_query")
        queries.append(" ".join(value.split()))
    if type(raw_job) is not dict or set(raw_job) != set(MAX_JOB_FIELD_CHARS):
        raise ValueError("invalid_job")
    job: dict[str, str] = {}
    for key, limit in MAX_JOB_FIELD_CHARS.items():
        value = raw_job[key]
        if type(value) is not str or len(value) > limit:
            raise ValueError("invalid_job_field")
        job[key] = " ".join(value.split())
    if not job["title"]:
        raise ValueError("missing_job_title")
    return queries, job


def _score(model: Any, queries: list[str], job: dict[str, str]) -> dict[str, float]:
    import hashlib
    import torch

    document = (
        f"title: {job['title']} | text: "
        f"{job['company']} {job['location']} {job['description']}"
    )[:16_000]
    key = hashlib.sha256(json.dumps(queries, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()
    with _MODEL_LOCK:
        query_embeddings = _QUERY_EMBEDDINGS.get(key)
        if query_embeddings is None:
            query_embeddings = model.encode(
                queries, prompt_name="SearchQuery", normalize_embeddings=True,
                truncate_dim=256, convert_to_tensor=True, batch_size=16,
            )
            _QUERY_EMBEDDINGS[key] = query_embeddings
            while len(_QUERY_EMBEDDINGS) > 16:
                _QUERY_EMBEDDINGS.popitem(last=False)
        else:
            _QUERY_EMBEDDINGS.move_to_end(key)
        document_embedding = model.encode(
            document, normalize_embeddings=True, truncate_dim=256,
            convert_to_tensor=True,
        )
        similarities = torch.matmul(query_embeddings, document_embedding)
    similarity = float(torch.max(similarities).item())
    similarity = max(-1.0, min(1.0, similarity))
    return {"score": round((similarity + 1.0) * 50.0, 1),
            "cosine_similarity": round(similarity, 4)}


class Handler(BaseHTTPRequestHandler):
    model: Any = None

    def log_message(self, _format: str, *_args: object) -> None:
        # Requests contain user preferences and job text; never log them.
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"error": "not_found"})
            return
        self._send(200, {"ready": self.model is not None, "model": MODEL_ID})

    def do_POST(self) -> None:
        if self.path != "/score":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("invalid_body_size")
            payload = json.loads(self.rfile.read(length))
            queries, job = _clean_request(payload)
            self._send(200, _score(self.model, queries, job))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_request"})
        except Exception as exc:
            LOG.error("Embedding score failed: %s", type(exc).__name__)
            self._send(503, {"error": "scoring_unavailable"})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    model = _load_text_model()
    Handler.model = model
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
