# KQ-Propel

**An Agentic Retrieval-Augmented Generation Framework for Aviation Support and
Localized Transaction Execution**

Implements the system described in the project proposal
(`KanjaDelight_166584_DrEstherKhakata.pdf`) and defense deck (`166584-Defense.pptx`),
extended with the enhancements required by `Corrections.docx`. UI theme: **red & white**.

## What was built

- **Agentic RAG pipeline**: policy manuals are chunked (500 tokens, 50-token overlap),
  embedded with `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU) — or OpenAI
  `text-embedding-3-small` when a key is present — and indexed in a cosine-similarity
  vector store (`vectorstore.py`). Uses MongoDB Atlas Vector Search when `MONGODB_URI`
  is set, otherwise an equivalent local JSON-backed index — same interface either way.
  If no embedding model is available the pipeline still runs on a hashed bag-of-words
  vector, but it reports that it has degraded rather than presenting lexical matching
  as semantic retrieval.
- **Section-aware chunking + hybrid retrieval with MMR** (`retrieval.py`,
  `vectorstore.py`): each policy is split on its numbered section headings, then ranked
  by a blend of BM25 lexical scoring (with aviation synonym expansion) and dense cosine
  similarity. Selection is diversified by Maximal Marginal Relevance, because a trained
  encoder scores every paragraph of the closest document highly and would otherwise fill
  all three context slots with near-duplicates from one policy. This is what lets the
  assistant answer with the *one* relevant clause instead of dumping a whole document.
- **Grounded answer composition** (`composer.py`): the answer is assembled from the
  specific policy sentences that address the question, with numeric-band matching (a
  passenger saying "my bag is 5kg over" is matched to the 1–10kg fee band) and a
  section citation on every reply. Nothing is invented.
- **ReAct agent loop** (`agent.py`): Thought → Action → Observation. Tools: policy
  retrieval, live flight telemetry (AviationStack), and payment by card (PayPal Orders
  API) or M-Pesa (Safaricom Daraja STK Push). Falls back to sandbox/simulated responses
  without API keys. A payment is
  only initiated on explicit transactional intent *and* a policy-grounded amount, so
  asking "how do I pay?" never charges anyone.
- **Localized transaction execution (card and M-Pesa)**: when an answer quotes a specific,
  policy-grounded fee, the agent offers to settle it there and then. The offer is driven
  by what the *answer* says, not by how the question was worded — "I have an overweight
  baggage by 10kg" contains no fee vocabulary yet still gets an offer. Money owed *to*
  the passenger (refunds, meal vouchers, delayed-baggage allowances) is never offered for
  payment. The passenger chooses a rail: a **card**, charged in the currency the fee is
  published in, or **M-Pesa**, which reaches Kenyan Safaricom lines only and settles in
  shillings. The conversation is
  stateful — the passenger can reply "yes", then choose a rail on a later turn, and
  the agent still knows what it was charging for. Each is rendered in the chat as a
  receipt card (amount, currency, reference, and the rail used) and written
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
| Purpose | Ask questions, check flights, pay fees | Sentiment metrics, RAG-Triad scores, hallucination rate, payment audit trail (card and M-Pesa), dual-model comparison |

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

### Fees, and paying by card or M-Pesa

When the agent quotes a fee the passenger owes, it offers two settlement rails, because
they have genuinely different reach:

| | Card (PayPal Orders API) | M-Pesa (Daraja STK push) |
|---|---|---|
| Who it reaches | anyone with a Visa/Mastercard/Amex | Kenyan Safaricom lines only |
| Currency charged | the currency the fee is **published** in | shillings only |
| Exchange-rate assumption | **none** | `USD_TO_KES_RATE`, disclosed every time |
| Grounding | Kenya Airways publishes that it accepts local and international credit and debit cards (`payment_methods_policy.txt` §4) | Kenya Airways publishes **no** paybill; the in-chat push is a disclosed prototype (`synthetic_inchat_payment_flow.txt`) |

The card rail therefore does more than widen access: it removes the FX assumption rather
than managing it. Kenya Airways publishes most ancillary fees in US dollars, and a card
can simply be charged in dollars. Only the M-Pesa rail needs a rate, and when it uses one
it says so — the converted figure is never presented as an airline-published amount:

```
You:  I need to correct a typo in my name on my international booking
Bot:  Correcting up to three characters of a name costs 15 US dollars on a domestic
      booking and 75 US dollars on an international booking...
      Source: Refund Policy - Section 5: Name Corrections and Name Changes

      Would you like to settle the USD 75.00 now? There are two ways:
      - Card - I'll generate a secure checkout link and you pay the USD 75.00 directly.
      - M-Pesa - available on a Kenyan mobile number. M-Pesa settles in shillings, so
        this would be Ksh 9,675 at the indicative rate below.

      The published charge is USD 75. Kenya Airways does not publish a shilling
      equivalent, so I've converted it at this assistant's configured indicative rate
      of 129.00 KES per USD... That rate is a prototype setting, not a live exchange
      rate.

You:  I don't have a Kenyan number
Bot:  [CARD card: USD 75.00, checkout link, "charged in USD - no conversion on our side"]
```

Saying *"I'm outside Kenya"* or *"my number is international"* routes to the card rail
without the passenger having to name a payment instrument. Giving a phone number routes
to M-Pesa. The pending-payment record stores the fee in its **published** currency;
shillings are derived at the moment M-Pesa is chosen and never stored as the canonical
amount.

Fee sentences usually quote several figures, so the agent selects the one whose
qualifying words match the passenger's own situation (`international` → 75, `domestic`
→ 15) and **declines rather than guesses** when the request is unspecific. Figures the
source publishes with no currency label — the heavy-bag table — are never treated as
money. `tools/test_fee_settlement.py` and `tools/test_card_payment.py` lock all of this
down.

Everything can also go in one message — *"I want to pay now, my bag is 5kg over, number
0733445566"*. Without credentials both rails are simulated end-to-end and clearly
labelled as such ("Daraja (simulated)", "Card gateway (simulated)"); the simulated
checkout link is deliberately **not** a `paypal.com` URL, so it cannot be mistaken for a
live one in a screenshot. Set `DARAJA_*` or `PAYPAL_*` in `.env` to hit the real
sandboxes. Either way the transaction appears in the dashboard's audit trail, tagged
with its rail and currency — the dashboard totals per currency rather than summing
dollars into shillings.

## Configuration (all optional — sensible offline fallbacks are built in)

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Enables real GPT-4o-mini + text-embedding-3-small |
| `OPEN_SOURCE_MODEL_NAME`, `OLLAMA_BASE_URL` | Second model for comparison via Ollama |
| `AVIATIONSTACK_API_KEY` | Live flight telemetry |
| `DARAJA_CONSUMER_KEY/SECRET/PASSKEY/SHORTCODE` | Live Safaricom Daraja STK push |
| `PAYPAL_CLIENT_ID/SECRET`, `PAYPAL_SANDBOX` | Live PayPal card checkout; without them the card rail returns a labelled simulated link |
| `USD_TO_KES_RATE` | Indicative rate for settling dollar-published fees over M-Pesa (default 129.0; disclosed to the passenger, not a live FX feed). Not used on the card rail |
| `LOCAL_EMBEDDING_MODEL`, `LOCAL_EMBEDDING_ENABLED` | Local sentence-transformer embeddings (default `all-MiniLM-L6-v2`); disable to force the hashed fallback |
| `RETRIEVAL_MMR_LAMBDA` | Relevance/diversity trade-off in retrieval (default 0.8; 1.0 is plain top-k) |
| `MONGODB_URI`, `MONGODB_DB_NAME` | MongoDB Atlas Vector Search backend |

Values are read from a `.env` file at the repository root (see `.env.example`), falling
back to real environment variables, which take precedence.

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
    data/policies/  RAG corpus - 8 documents from Kenya Airways' published
                    pages plus 2 marked synthetic; provenance.json records
                    the source URL and retrieval date for every one
    data/eval/      Evaluation dataset for RAG-Triad + model comparison
    data/sentiment_dataset/  Labelled frustration dataset
  frontend/
    index.html, chat.css,  chat.js    Standalone passenger chatbot (responsive)
    admin.html, admin.css, admin.js   Separate admin dashboard (responsive)
```
