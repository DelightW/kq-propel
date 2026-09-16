# KQ-Propel

**An Agentic Retrieval-Augmented Generation Framework for Aviation Support and
Localized Transaction Execution**

Implements the system described in the project proposal
(`KanjaDelight_166584_DrEstherKhakata.pdf`) and defense deck (`166584-Defense.pptx`),
extended with the enhancements required by `Corrections.docx`. UI theme: **red & white**.

## What was built

- **Agentic RAG pipeline**: policy manuals are chunked (500 tokens, 50-token overlap),
  embedded (1,536-dim), and indexed in a cosine-similarity vector store (`vectorstore.py`).
  Uses MongoDB Atlas Vector Search when `MONGODB_URI` is set, otherwise an equivalent
  local JSON-backed index — same interface either way.
- **ReAct agent loop** (`agent.py`): Thought → Action → Observation. Tools: policy
  retrieval, live flight telemetry (AviationStack), and M-Pesa payment (Safaricom Daraja
  STK Push). Falls back to sandbox/simulated responses without API keys.
- **RAG-Triad evaluation** (`evaluation.py`): context relevance, groundedness, answer
  relevance, logged per turn and surfaced on the admin dashboard as a hallucination rate.
- **Admin dashboard**: sentiment metrics, RAG-Triad scores, M-Pesa transaction audit
  trail, and a one-click dual-model comparison.

### Corrections implemented

1. **Second LLM + controlled comparison** (`llm.py`, `/api/admin/model-comparison`):
   GPT-4o-mini (primary) is compared against an open-source model (Llama/Mistral via
   Ollama, configurable) over the *same* documents, chunking, embeddings, retrieval,
   prompts and evaluation dataset (`data/eval/evaluation_dataset.json`), scored on the
   same context-relevance / groundedness / answer-relevance criteria.
2. **Custom-trained ML sentiment/frustration classifier** (`sentiment.py`): a
   TF-IDF + Logistic Regression model trained on a labelled dataset
   (`data/sentiment_dataset/frustration_dataset.csv`), evaluated with accuracy,
   precision, recall and F1-score (shown on the dashboard). Its output feeds the agent
   so highly frustrated passengers get a more empathetic response and an escalation path.

## Running it

```powershell
cd kq-propel\backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/ for the chat assistant and
http://127.0.0.1:8000/admin for the dashboard.

## Configuration (all optional — sensible offline fallbacks are built in)

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Enables real GPT-4o-mini + text-embedding-3-small |
| `OPEN_SOURCE_MODEL_NAME`, `OLLAMA_BASE_URL` | Second model for comparison via Ollama |
| `AVIATIONSTACK_API_KEY` | Live flight telemetry |
| `DARAJA_CONSUMER_KEY/SECRET/PASSKEY/SHORTCODE` | Live Safaricom Daraja STK push |
| `MONGODB_URI`, `MONGODB_DB_NAME` | MongoDB Atlas Vector Search backend |

## Project layout

```
kq-propel/
  backend/
    app/            FastAPI app, RAG pipeline, agent, integrations, admin API
    data/policies/  Sample KQ policy manuals (RAG corpus)
    data/eval/      Evaluation dataset for RAG-Triad + model comparison
    data/sentiment_dataset/  Labelled frustration dataset
  frontend/         Red & white themed chat UI + admin dashboard (static HTML/CSS/JS)
```
