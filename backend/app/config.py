"""
Central configuration for KQ-Propel.

All external integrations (OpenAI, an open-source LLM, AviationStack, Safaricom Daraja,
MongoDB Atlas) are optional. When credentials are absent the system automatically falls
back to deterministic local/sandbox implementations so the full agentic pipeline can be
demonstrated and evaluated offline.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR.parent / ".env"


def _load_env_file(path: Path = ENV_PATH) -> None:
    """Loads .env before any credential is read.

    Without this, every `os.getenv` below returned the empty string no matter
    what .env contained, so the documented configuration mechanism was inert
    and a supplied API key silently produced sandbox data. Existing environment
    variables take precedence, so a real deployment can override the file.
    """
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    # Dependency-free fallback, so configuration never depends on an optional
    # package being present.
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

POLICY_DIR = DATA_DIR / "policies"
EVAL_DATASET_PATH = DATA_DIR / "eval" / "evaluation_dataset.json"
SENTIMENT_DATASET_PATH = DATA_DIR / "sentiment_dataset" / "frustration_dataset.csv"
SENTIMENT_MODEL_PATH = DATA_DIR / "sentiment_dataset" / "frustration_model.joblib"
VECTOR_STORE_PATH = DATA_DIR / "vector_store.json"
APP_DB_PATH = DATA_DIR / "kq_propel.db"

# --- RAG chunking parameters (per proposal: 500 token chunks, 50 token overlap) ---
CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50

# Dimension of the hashed fallback embedding only. The real providers declare
# their own width (OpenAI 1536, all-MiniLM-L6-v2 384), so nothing should read
# this constant to describe the active backend - ask embeddings.dimension().
HASHED_EMBEDDING_DIMENSIONS = 1536
EMBEDDING_DIMENSIONS = HASHED_EMBEDDING_DIMENSIONS  # backwards-compatible alias

# --- Local sentence-transformer embeddings ---
# Used when no OpenAI key is present. This is the offline semantic backend; if
# the package or the weights are unavailable it degrades to the hashed
# embedding, and the degradation is reported rather than hidden.
LOCAL_EMBEDDING_MODEL = os.getenv("LOCAL_EMBEDDING_MODEL",
                                  "sentence-transformers/all-MiniLM-L6-v2").strip()
LOCAL_EMBEDDING_ENABLED = os.getenv("LOCAL_EMBEDDING_ENABLED", "true").strip().lower() \
    not in {"0", "false", "no", "off"}

# --- Retrieval diversification ---
# Maximal Marginal Relevance trade-off: 1.0 is plain top-k, lower values buy
# diversity at the cost of raw relevance. 0.8 is the lightest setting that
# recovers the recall the sentence-transformer backend lost to near-duplicate
# chunks (any-expected-document@3 back from 0.867 to 0.900) and it costs 0.002
# of context relevance. Pushing further down buys nothing at k=3 until 0.4,
# where answer relevance itself starts to fall. See tools/tune_retrieval.py.
RETRIEVAL_MMR_LAMBDA = float(os.getenv("RETRIEVAL_MMR_LAMBDA", "0.8"))

# --- LLM providers ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
LLM_TEMPERATURE = 0.0

# Second, open-source model used for the comparative evaluation introduced in the
# corrections document (e.g. Llama 3 / Mistral served locally via Ollama, or a
# HuggingFace inference endpoint).
OPEN_SOURCE_MODEL_NAME = os.getenv("OPEN_SOURCE_MODEL_NAME", "llama3-8b-instruct")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# --- External API integrations ---
AVIATIONSTACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY", "").strip()
DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "").strip()
DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "").strip()
DARAJA_SHORTCODE = os.getenv("DARAJA_SHORTCODE", "174379")
DARAJA_PASSKEY = os.getenv("DARAJA_PASSKEY", "").strip()
DARAJA_SANDBOX = os.getenv("DARAJA_SANDBOX", "true").lower() != "false"

# Kenya Airways publishes most ancillary fees in US dollars, but Daraja settles
# in Kenyan shillings. To let the STK push demonstrate end-to-end settlement on
# a dollar-denominated fee, the prototype converts at this rate.
#
# This is a prototype convenience, not a live FX feed and not a Kenya Airways
# published figure. It is declared here rather than buried in the agent so the
# assumption is visible and overridable, and every converted amount the agent
# quotes states the rate it used.
USD_TO_KES_RATE = float(os.getenv("USD_TO_KES_RATE", "129.0"))

# --- Card payments (PayPal Orders API v2) ---
# M-Pesa requires a Kenyan mobile number, so it cannot serve guests outside
# Kenya and Tanzania at all. Kenya Airways publishes that it accepts local and
# international credit and debit cards, so a card rail is the grounded way to
# serve everyone else.
#
# The card rail also removes the conversion problem rather than managing it: a
# card can be charged in the currency the fee is actually published in, so no
# invented exchange rate is involved. USD_TO_KES_RATE therefore applies only to
# the M-Pesa path.
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_SANDBOX = os.getenv("PAYPAL_SANDBOX", "true").lower() != "false"
# Where PayPal returns the guest after they approve or cancel. In a deployed
# system these are routes on the airline's own site.
PAYPAL_RETURN_URL = os.getenv("PAYPAL_RETURN_URL", "https://example.com/payment/complete")
PAYPAL_CANCEL_URL = os.getenv("PAYPAL_CANCEL_URL", "https://example.com/payment/cancelled")

# --- MongoDB Atlas Vector Search (production target); falls back to a local
#     JSON-persisted cosine-similarity index when no connection string is supplied ---
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "kq_propel")

APP_TITLE = "KQ-Propel"
APP_TAGLINE = "Agentic RAG Framework for Aviation Support & Localized Transaction Execution"
