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
- **Section-aware chunking + hybrid retrieval** (`retrieval.py`, `vectorstore.py`): each
  policy is split on its numbered section headings, then ranked by a blend of BM25
  lexical scoring (with aviation synonym expansion) and dense cosine similarity. This
  is what lets the assistant answer with the *one* relevant clause instead of dumping a
  whole document.
- **Grounded answer composition** (`composer.py`): the answer is assembled from the
  specific policy sentences that address the question, with numeric-band matching (a
  passenger saying "my bag is 5kg over" is matched to the 1–10kg fee band) and a
  section citation on every reply. Nothing is invented.
- **ReAct agent loop** (`agent.py`): Thought → Action → Observation. Tools: policy
  retrieval, live flight telemetry (AviationStack), and M-Pesa payment (Safaricom Daraja
  STK Push). Falls back to sandbox/simulated responses without API keys. A payment is
  only initiated on explicit transactional intent *and* a policy-grounded amount, so
  asking "how do I pay?" never charges anyone.
- **Localized transaction execution (M-Pesa)**: when an answer quotes a specific,
  policy-grounded fee, the agent offers to settle it there and then. The offer is driven
  by what the *answer* says, not by how the question was worded — "I have an overweight
  baggage by 10kg" contains no fee vocabulary yet still gets an offer. Money owed *to*
  the passenger (refunds, meal vouchers, delayed-baggage allowances) is never offered for
  payment. The conversation is
  stateful — the passenger can reply "yes", then send their number on a later turn, and
  the agent still knows what it was charging for. The STK push is rendered in the chat as
  an M-Pesa receipt card (amount, destination number, reference, checkout ID) and written
  to the dashboard's audit trail. A payment is **only** initiated for an amount that came
  from a policy section, so nothing is ever invented — and asking "how do I pay?" never
  charges anyone.
- **Scope and capability guards**: retrieval always returns *something*, so the agent
  additionally checks that the retrieved sections share subject matter with the question.
  If they don't, it says so instead of answering from the closest-ranked policy. Likewise,
  departure and arrival *times* are live operational data rather than policy — if no
  flight number is supplied, the agent asks for one instead of quoting an unrelated
  section.
- **RAG-Triad evaluation** (`evaluation.py`): context relevance, groundedness, answer
  relevance, logged per turn and surfaced on the admin dashboard as a hallucination rate.
- **Two separate front-ends**: a standalone passenger chatbot and an independent admin
  dashboard (see below). They share no assets and are used by different audiences.

### The two applications

| | Chat assistant | Admin dashboard |
|---|---|---|
| URL | `/` | `/admin` |
| Assets | `index.html` → `chat.css`, `chat.js` | `admin.html` → `admin.css`, `admin.js` |
| Audience | Passengers | Airline support supervisors |
| Purpose | Ask questions, check flights, pay fees | Sentiment metrics, RAG-Triad scores, hallucination rate, M-Pesa audit trail, dual-model comparison |

The chatbot is fully **responsive**: a single-column, full-height layout with a sticky
composer, horizontally scrolling suggestion chips and a slide-in reasoning panel on
phones; a two-column layout with a permanently docked reasoning sidebar on laptops.
Touch targets are ≥44px, the input uses a 16px font so iOS does not auto-zoom, and
safe-area insets are respected on notched devices.

### Corrections implemented

1. **Second LLM + controlled comparison** (`llm.py`, `/api/admin/model-comparison`):
   GPT-4o-mini (primary) is compared against an open-source model (Llama/Mistral via
   Ollama, configurable) over the *same* documents, chunking, embeddings, retrieval,
   prompts and evaluation dataset (`data/eval/evaluation_dataset.json`), scored on the
   same context-relevance / groundedness / answer-relevance criteria, plus per-model
   response time. Run it from the dashboard's "Run comparison" button.
2. **Custom-trained ML sentiment/frustration classifier** (`sentiment.py`): a
   TF-IDF + Logistic Regression model trained on a labelled dataset of 100 passenger
   messages (`data/sentiment_dataset/frustration_dataset.csv`), evaluated on a held-out
   25-message test split with accuracy, precision, recall and F1-score (shown on the
   dashboard). Because TF-IDF lower-cases its input and would discard the strongest
   real-world cues, the feature space unions the lexical vectors with hand-engineered
   **stylistic features** — capitalisation ratio, shouted words, exclamation and question
   bursts, "?!" combinations, elongated characters and intensifier counts — so
   "I HAVE WAITED FOR 2 HOURS?!" is correctly scored as frustrated. The model is
   fingerprinted against the dataset and feature version, and retrains automatically when
   either changes. Its output feeds the agent so frustrated passengers get an empathetic
   acknowledgement first and an explicit escalation path last.

## Running it

```powershell
cd kq-propel\backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| What | URL |
|---|---|
| Chat assistant (passengers) | http://127.0.0.1:8000/ |
| Admin dashboard | http://127.0.0.1:8000/admin |
| API docs | http://127.0.0.1:8000/docs |

### Opening the chatbot on your phone

Binding to `0.0.0.0` (as above) makes the assistant reachable from any device on the
same Wi-Fi network.

1. Find the laptop's Wi-Fi address:
   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -eq "Wi-Fi"
   ```
2. Allow the port through Windows Firewall — run **once**, in an *Administrator*
   PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName "KQ-Propel (TCP 8000)" -Direction Inbound `
     -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private
   ```
3. On the phone, browse to `http://<that-address>:8000/` — for example
   `http://192.168.100.70:8000/`.

Both the laptop and the phone must be on the same network, and the network must be
marked **Private** in Windows.

### Paying a fee by M-Pesa

Ask about a fee, then settle it without leaving the chat:

```
You:  I have an overweight baggage by 15kg
Bot:  Bags between 11kg and 20kg over the limit incur a flat fee of Ksh 9,000.
      Source: Baggage Policy - Section 2: Overweight Baggage Fees
      Would you like me to send an M-Pesa payment prompt for Ksh 9,000?

You:  can i pay it
Bot:  The amount due is Ksh 9,000. What's the M-Pesa number?

You:  0722334455
Bot:  [M-PESA receipt card: Ksh 9,000 -> 254722334455, reference, checkout ID]
```

Everything can also go in one message — *"I want to pay now, my bag is 5kg over, number
0733445566"*. Without Daraja credentials the STK push is simulated end-to-end (clearly
labelled "Daraja (simulated)" on the card); set `DARAJA_*` in `.env` to hit the real
Safaricom sandbox. Either way the transaction appears in the dashboard's audit trail.

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
    app/
      agent.py        ReAct loop, intent handling, tool gating, escalation
      retrieval.py    BM25 lexical scoring, synonym expansion, score blending
      vectorstore.py  Section-aware chunking + hybrid (lexical/dense) index
      composer.py     Grounded answer composition with numeric-band matching
      llm.py          Dual-model abstraction + controlled comparison
      sentiment.py    Self-trained frustration classifier
      evaluation.py   RAG-Triad scoring
      routers/        /api/chat and /api/admin
    data/policies/  Sample KQ policy manuals (RAG corpus)
    data/eval/      Evaluation dataset for RAG-Triad + model comparison
    data/sentiment_dataset/  Labelled frustration dataset
  frontend/
    index.html, chat.css,  chat.js    Standalone passenger chatbot (responsive)
    admin.html, admin.css, admin.js   Separate admin dashboard (responsive)
```
