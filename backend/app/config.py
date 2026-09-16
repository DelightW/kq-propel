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
POLICY_DIR = DATA_DIR / "policies"
EVAL_DATASET_PATH = DATA_DIR / "eval" / "evaluation_dataset.json"
SENTIMENT_DATASET_PATH = DATA_DIR / "sentiment_dataset" / "frustration_dataset.csv"
SENTIMENT_MODEL_PATH = DATA_DIR / "sentiment_dataset" / "frustration_model.joblib"
VECTOR_STORE_PATH = DATA_DIR / "vector_store.json"
APP_DB_PATH = DATA_DIR / "kq_propel.db"

# --- RAG chunking parameters (per proposal: 500 token chunks, 50 token overlap) ---
CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
EMBEDDING_DIMENSIONS = 1536

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

# --- MongoDB Atlas Vector Search (production target); falls back to a local
#     JSON-persisted cosine-similarity index when no connection string is supplied ---
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "kq_propel")

APP_TITLE = "KQ-Propel"
APP_TAGLINE = "Agentic RAG Framework for Aviation Support & Localized Transaction Execution"
