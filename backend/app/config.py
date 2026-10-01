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
# Safaricom requires a reachable https callback. The sandbox accepts a
# placeholder and still delivers the prompt to the handset, but no result
# callback can arrive at one - so a prompt that is never confirmed is expected
# behaviour on a placeholder, not a failure.
DARAJA_CALLBACK_URL = os.getenv(
    "DARAJA_CALLBACK_URL", "https://example.com/daraja/callback").strip()

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

# --- Staff authentication ---
# The administrative dashboard exposes the transaction ledger, which contains
# passenger phone numbers, so it must not be anonymous. No default password is
# defined here on purpose: a hard-coded credential in source is worse than the
# missing authentication it would be replacing. When neither variable below is
# set, auth.py generates a single-run password and prints it at startup.
#
# Generate a permanent hash with:  python -m app.auth <password>
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_TTL_MINUTES = int(os.getenv("ADMIN_SESSION_TTL_MINUTES", "120"))
ADMIN_MAX_LOGIN_ATTEMPTS = int(os.getenv("ADMIN_MAX_LOGIN_ATTEMPTS", "5"))
ADMIN_LOCKOUT_SECONDS = int(os.getenv("ADMIN_LOCKOUT_SECONDS", "300"))

# Cookies carry the staff session, so they must be inaccessible to script and
# must not travel cross-site. `secure` is configurable only because enabling it
# would break a plain-HTTP local demonstration; it must be true behind TLS.
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").strip().lower() in {"1", "true", "yes", "on"}

# --- Browser origin allowlist ---
# Previously every origin was permitted, which would have let any website on
# the internet issue credentialed requests against this API using a logged-in
# operator's cookies. Credentialed CORS cannot use a wildcard at all, so an
# explicit list is required rather than merely preferable.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",") if o.strip()
]

# Maximum accepted passenger message length. Unbounded input was accepted
# before, so a single request could carry a 100,000-character payload.
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
