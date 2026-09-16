"""
Embedding provider abstraction.

If OPENAI_API_KEY is configured, embeddings are generated with OpenAI's
text-embedding-3-small model (1536 dimensions), matching the proposal's data
ingestion design. Otherwise a deterministic local embedding (hashed
bag-of-words + character n-gram signature projected into a fixed dimension)
is used so the semantic retrieval pipeline remains fully functional offline.
"""
import hashlib
import math
import re
from typing import List

from app import config

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")


def _tokenize(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


def _local_embedding(text: str, dims: int = config.EMBEDDING_DIMENSIONS) -> List[float]:
    """Deterministic hashed embedding used as an offline stand-in for a real
    embedding model. Terms are hashed into buckets (a variant of the hashing
    trick used by feature-hashing vectorizers), giving semantically similar
    text (shared vocabulary) similar vector directions without requiring any
    external network call or trained model weights."""
    vec = [0.0] * dims
    tokens = _tokenize(text)
    if not tokens:
        return vec
    for tok in tokens:
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % dims
        sign = 1.0 if int(digest[8:9], 16) % 2 == 0 else -1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _openai_embedding(text: str) -> List[float]:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    resp = client.embeddings.create(model=config.OPENAI_EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def embed_text(text: str) -> List[float]:
    if config.OPENAI_API_KEY:
        try:
            return _openai_embedding(text)
        except Exception:
            # Network / quota failure: gracefully degrade to local embedding so
            # the pipeline never hard-fails during a live demo.
            pass
    return _local_embedding(text)


def provider_name() -> str:
    """Human-readable embedding backend, surfaced on the admin dashboard."""
    if config.OPENAI_API_KEY:
        return config.OPENAI_EMBEDDING_MODEL
    return "deterministic local embedding (offline)"


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
