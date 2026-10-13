"""Narrow HTTP client for the local text-only EmbeddingGemma ranker."""

from __future__ import annotations

import json
import math
import os
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener


_MAX_QUERY_COUNT = 100
_MAX_QUERY_CHARS = 1000
_MAX_JOB_CHARS = 12_000
_MAX_RESPONSE_BYTES = 16_384


def _endpoint() -> str | None:
    raw = os.environ.get("EMBEDDING_RANKER_URL", "http://embedding-ranker:8099").strip()
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "http" or parsed.hostname != "embedding-ranker" or
                parsed.port not in (None, 8099) or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return None
    except ValueError:
        return None
    return "http://embedding-ranker:8099/score"


def score_listing(queries: list[str], listing: dict[str, str]) -> dict[str, float] | None:
    """Return an advisory semantic similarity score or None on failure."""
    endpoint = _endpoint()
    if endpoint is None or not isinstance(queries, list):
        return None
    clean_queries = [
        " ".join(value.split())[:_MAX_QUERY_CHARS]
        for value in queries[:_MAX_QUERY_COUNT]
        if isinstance(value, str) and value.strip()
    ]
    if not clean_queries:
        return None
    clean_listing = {
        key: " ".join(str(listing.get(key, "")).split())[:limit]
        for key, limit in (("title", 500), ("company", 300),
                           ("location", 300), ("description", _MAX_JOB_CHARS))
    }
    request_data = json.dumps(
        {"queries": clean_queries, "job": clean_listing},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint, data=request_data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with build_opener().open(request, timeout=120) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError):
        return None
    if len(raw) > _MAX_RESPONSE_BYTES:
        return None
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if type(result) is not dict or set(result) != {"score", "cosine_similarity"}:
        return None
    score = result["score"]
    similarity = result["cosine_similarity"]
    if (type(score) not in {int, float} or type(similarity) not in {int, float} or
            not math.isfinite(float(score)) or not math.isfinite(float(similarity)) or
            not 0 <= float(score) <= 100 or not -1 <= float(similarity) <= 1):
        return None
    return {"score": round(float(score), 1),
            "cosine_similarity": round(float(similarity), 4)}
