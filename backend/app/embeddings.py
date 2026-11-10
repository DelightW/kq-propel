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

def _load_torch_model():
    """The sentence-transformers (PyTorch) runtime, or None with the reason
    recorded. Imported lazily: importing torch costs seconds, and a marking
    machine or CI runner may have neither the package nor the cached weights."""
    global last_error
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        last_error = f"sentence-transformers unavailable: {exc}"
        return None
    try:
        return SentenceTransformer(config.LOCAL_EMBEDDING_MODEL)
    except Exception as exc:
        last_error = (f"could not load {config.LOCAL_EMBEDDING_MODEL}: {exc} "
                      "(the first run needs network access to fetch the weights)")
        return None


def _load_local_model():
    """Loads the local encoder once, under a lock, trying the runtimes in
    order (see _runtime_order). A failure is recorded, and the caller falls
    through to the hashed tier."""
    global _model, _model_load_attempted, _runtime
    with _model_lock:
        if _model is not None or _model_load_attempted:
            return _model
        _model_load_attempted = True
        for runtime in _runtime_order():
            _model = _load_onnx_model() if runtime == "onnx" else _load_torch_model()
            if _model is not None:
                _runtime = runtime
                break
        return _model


class _OnnxEncoder:
    """The same all-MiniLM-L6-v2 weights, run with ONNX Runtime instead of PyTorch.

    PyTorch made the server need about 616 MB, more than the 512 MB of free
    hosting plans; ONNX Runtime and the tokenizer need a fraction of that. The
    model repository publishes the ONNX export (onnx/model.onnx), and the
    sentence-transformers pipeline for this model is reproduced exactly: the
    tokenizer truncates at 256 tokens, token vectors are mean-pooled over the
    attention mask, and the result is L2-normalised.
    """

    MAX_TOKENS = 256

    def __init__(self, repo: str):
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        self._np = np
        self.tokenizer = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1      # small, predictable memory on shared CPUs
        self.session = ort.InferenceSession(hf_hub_download(repo, "onnx/model.onnx"), options,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: List[str], batch_size: int = 32):
        np = self._np
        out = []
        for start in range(0, len(texts), batch_size):
            batch = self.tokenizer.encode_batch(texts[start:start + batch_size])
            ids = np.array([e.ids for e in batch], dtype=np.int64)
            mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            tokens = self.session.run(None, feed)[0]                 # (batch, seq, 384)
            weights = mask[:, :, None].astype(np.float32)
            pooled = (tokens * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
            pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
            out.extend(pooled.tolist())
        return out


def _runtime_order() -> List[str]:
    choice = config.LOCAL_EMBEDDING_RUNTIME
    if choice in ("onnx", "torch"):
        return [choice]
    return ["onnx", "torch"]       # auto: the lighter runtime when it is installed


def _load_onnx_model():
    global last_error
    try:
        return _OnnxEncoder(config.LOCAL_EMBEDDING_MODEL)
    except Exception as exc:
        last_error = f"ONNX runtime unavailable for {config.LOCAL_EMBEDDING_MODEL}: {exc}"
        return None


_runtime: Optional[str] = None


def _local_embeddings(texts: List[str]) -> Optional[List[List[float]]]:
    global last_error
    model = _load_local_model()
    if model is None:
        return None
    if _runtime == "onnx":
        try:
            return model.encode(texts)
        except Exception as exc:
            last_error = f"local encode failed: {exc}"
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


def runtime() -> Optional[str]:
    """"onnx" or "torch" for the local model; None for the other backends."""
    provider_name()
    return _runtime if _active_backend == config.LOCAL_EMBEDDING_MODEL else None


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
        "runtime": runtime(),
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
