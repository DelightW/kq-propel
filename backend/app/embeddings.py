"""
Embedding provider abstraction.

Three backends, tried in order of fidelity:

1. **OpenAI `text-embedding-3-small`** (1536d) when `OPENAI_API_KEY` is set -
   the design described in the proposal.
2. **`sentence-transformers/all-MiniLM-L6-v2`** (384d), a real transformer
   encoder running locally on CPU. This is the offline semantic backend.
3. **A hashed bag-of-words signature** (1536d) as a last resort so the
   pipeline still runs with no packages, no weights and no network.

Tier 3 deserves a warning, because it is easy to mistake for a real embedding.
It is a *feature-hashing* vector: each token lands in a bucket by SHA-256, so
two texts are close only when they literally share tokens. It cannot place
"luggage" near "baggage". Retrieval using it is lexical matching wearing a
cosine similarity as a disguise, and it is the diagnosed cause of the weak
context-relevance score reported before Phase 3b.

Degradation between tiers is *reported*, never silent: `describe()` names the
backend actually in use and `last_error` carries the reason a preferred tier
was abandoned. The previous version of this module swallowed failures, which
is how a misconfigured deployment could look healthy while serving the weakest
possible retrieval.
"""
import hashlib
import math
import re
import threading
from typing import Dict, List, Optional

from app import config

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")

# Populated on first use; describes whichever backend actually answered.
_active_backend: Optional[str] = None
_active_dimension: Optional[int] = None
last_error: Optional[str] = None

_model = None
_model_load_attempted = False
_model_lock = threading.Lock()


def _tokenize(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


# --------------------------------------------------------------------------
# Tier 3 - hashed fallback
# --------------------------------------------------------------------------

def _hashed_embedding(text: str,
                      dims: int = config.HASHED_EMBEDDING_DIMENSIONS) -> List[float]:
    """Deterministic feature-hashing vector. Shares no semantics with a trained
    encoder: similarity is nonzero only where the two texts share literal
    tokens. Retained as a dependency-free floor, not as a serious embedding."""
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


# --------------------------------------------------------------------------
# Tier 2 - local sentence-transformer
# --------------------------------------------------------------------------

def _load_local_model():
    """Loads the sentence-transformer once, under a lock.

    Import and weight-loading are both deferred to first use: importing torch
    costs seconds, and a marking machine or CI runner may have neither the
    package nor the cached weights. A failure here is recorded, and the caller
    falls through to the hashed tier.
    """
    global _model, _model_load_attempted, last_error
    with _model_lock:
        if _model is not None or _model_load_attempted:
            return _model
        _model_load_attempted = True
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            last_error = f"sentence-transformers unavailable: {exc}"
            return None
        try:
            _model = SentenceTransformer(config.LOCAL_EMBEDDING_MODEL)
        except Exception as exc:
            last_error = (f"could not load {config.LOCAL_EMBEDDING_MODEL}: {exc} "
                          "(the first run needs network access to fetch the weights)")
            _model = None
        return _model


def _local_embeddings(texts: List[str]) -> Optional[List[List[float]]]:
    global last_error
    model = _load_local_model()
    if model is None:
        return None
    try:
        vectors = model.encode(texts, normalize_embeddings=True,
                               convert_to_numpy=True, show_progress_bar=False)
    except Exception as exc:
        last_error = f"local encode failed: {exc}"
        return None
    return [[float(v) for v in row] for row in vectors]


# --------------------------------------------------------------------------
# Tier 1 - OpenAI
# --------------------------------------------------------------------------

def _openai_embeddings(texts: List[str]) -> List[List[float]]:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    resp = client.embeddings.create(model=config.OPENAI_EMBEDDING_MODEL, input=texts)
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _record_backend(name: str, vectors: List[List[float]]):
    global _active_backend, _active_dimension
    _active_backend = name
    if vectors and vectors[0]:
        _active_dimension = len(vectors[0])


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embeds a batch. Batching matters for tier 2: encoding the whole corpus
    in one call is far cheaper than one forward pass per chunk."""
    global last_error
    if not texts:
        return []

    if config.OPENAI_API_KEY:
        try:
            vectors = _openai_embeddings(texts)
            _record_backend(config.OPENAI_EMBEDDING_MODEL, vectors)
            return vectors
        except Exception as exc:
            last_error = f"OpenAI embeddings failed: {exc}"

    if config.LOCAL_EMBEDDING_ENABLED:
        vectors = _local_embeddings(texts)
        if vectors is not None:
            _record_backend(config.LOCAL_EMBEDDING_MODEL, vectors)
            return vectors

    vectors = [_hashed_embedding(t) for t in texts]
    _record_backend("hashed-bag-of-words (no semantic model)", vectors)
    return vectors


def embed_text(text: str) -> List[float]:
    return embed_texts([text])[0]


def warm_up() -> str:
    """Forces backend selection, so the first user query does not pay the model
    load and the dashboard can report the truth before any search runs."""
    embed_text("warm up")
    return _active_backend or "unknown"


def provider_name() -> str:
    """The backend actually in use - resolved by probing, not by assuming that
    a configured key means a working one."""
    if _active_backend is None:
        warm_up()
    return _active_backend or "unknown"


def dimension() -> int:
    if _active_dimension is None:
        warm_up()
    return _active_dimension or config.HASHED_EMBEDDING_DIMENSIONS


def is_semantic() -> bool:
    return "hashed" not in provider_name()


def describe() -> Dict[str, object]:
    """Full, honest posture for the admin dashboard and the defense."""
    if config.OPENAI_API_KEY:
        preference = config.OPENAI_EMBEDDING_MODEL
    elif config.LOCAL_EMBEDDING_ENABLED:
        preference = config.LOCAL_EMBEDDING_MODEL
    else:
        preference = "hashed-bag-of-words (semantic model disabled by configuration)"
    info: Dict[str, object] = {
        "provider": provider_name(),
        "dimension": dimension(),
        "semantic": is_semantic(),
        "configured_preference": preference,
    }
    if last_error:
        info["degraded_reason"] = last_error
    return info


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
