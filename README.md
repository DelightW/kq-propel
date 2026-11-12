# KQ-Propel

KQ-Propel is a customer-support chat assistant for Kenya Airways, built as my final-year project at Strathmore University. Passengers can ask about baggage, refunds and other policies and get an answer taken straight from the airline's published policy, with the section it came from. They can also check a flight, look up a booking, pay for a name correction, or ask to speak to a person.

**Try it:** [kq-propel.onrender.com](https://kq-propel.onrender.com). The staff dashboard is at [/admin](https://kq-propel.onrender.com/admin).

It runs on Render's free plan, so the first visit after a quiet period can take about a minute while the server wakes up. Its data is wiped whenever it restarts.

> This is a student research prototype. It is not affiliated with or run by Kenya Airways. Bookings and flights that don't come from a live source are marked as demo data, and M-Pesa payments go through Safaricom's sandbox.

## Things to try in the chat

- "How many bags can I check in Economy to London?" and then "What about Business class?" (the follow-up keeps the context)
- "Check my booking KQDEMO, surname Wanjiru" (a demo booking)
- "I need to correct the spelling of my name", then "booking KQDLW1, surname Wangui", then the corrected name "Delight Wambui". The assistant counts the characters changed and quotes the fee.

Each reply has a **reasoning trace** you can open to see what the assistant checked, what it retrieved and why it answered the way it did.

## How it works

When a message comes in, the assistant goes through a fixed sequence of checks. It doesn't let a language model decide which tools to call. First it reads the tone of the message. Then it checks whether the passenger wants a person, a booking or a service request. Only after that does it fall back to answering from policy.

For policy questions:

1. **Retrieval.** The question is matched against 53 chunks from 10 policy documents. Ranking combines BM25 keyword matching with all-MiniLM-L6-v2 sentence embeddings. On top of that, it boosts results whose headings match and down-ranks rules for the wrong route or the wrong kind of baggage (checked, hand, delayed or special). It then keeps the top four, chosen with MMR so they don't repeat each other.
2. **Grounding check.** If nothing retrieved actually covers the question, the assistant says so instead of guessing.
3. **Answer.** By default an extractive composer picks sentences from the retrieved policy, so it can't make anything up. A live model (OpenAI or Ollama) can be switched on. Its answer is used only if every number in it appears in the source text.

A small frustration classifier (TF-IDF features and logistic regression, trained on 499 messages) softens the tone when someone is clearly upset. A fairness test checks that tone never changes what the policy answer says.

Eight of the policy documents are Kenya Airways' published information. Two are synthetic and labelled as such. The details are in [backend/data/policies/provenance.json](backend/data/policies/provenance.json).

### Payments

Name corrections carry a fee, which is read from Section 5 of the refund policy. The fee depends on how many characters change and whether the route is domestic or international. The passenger can pay by M-Pesa (STK Push through Safaricom's Daraja API) or by card (PayPal). A payment counts as paid only once the provider confirms it. A PDF receipt is then available, and the request goes to staff.

The amount always comes from the policy, never from anything the passenger types. Money owed to the passenger, such as a refund, is never offered as a payment. Safaricom's sandbox still takes real money, so in sandbox mode prompts go only to whitelisted test numbers, for Ksh 1.

### Staff dashboard

Staff sign in with individual accounts. There are three roles:

- **Admin:** everything.
- **Agent:** handovers, name corrections and payments.
- **Analyst:** quality metrics only, with no access to passenger records.

The dashboard holds:

- the queues of passengers waiting for a person and of paid corrections to carry out, with optional sound and browser alerts;
- answer-quality scores (RAG Triad), frustration levels and 14-day trends;
- a conversation review that finds answers passengers rated unhelpful, declined questions and weakly grounded answers;
- the payment ledger, filterable and exportable as CSV;
- the policy documents exactly as the assistant searches them;
- an access log of who viewed passenger data, also exportable.

Passengers can rate each answer, and a reloaded chat keeps the conversation. Phone numbers are masked, and a passenger can erase their own conversation.

## Results

These figures come from the 30-question evaluation set, using the offline composer:

| Metric | Score |
|---|---|
| Groundedness | 1.000 |
| Answer relevance | 0.633 |
| Context relevance | 0.405 |
| Right document in the top results (any@k) | 1.000 |
| All needed documents in the top results (all@k) | 0.967 |

Every follow-up, unanswerable and handover case in the conversation test set passes, as do the two Swahili and two vague-question cases. So do all 49 sentiment-fairness pairs. Both interfaces pass axe-core's WCAG 2.1 A/AA checks and can be used with the keyboard alone.

### Known limitations

- Swahili questions are read through a small glossary of travel terms and answered in English. Anything outside the glossary is declined.
- Vague questions ("How much is the fee?") get a clarifying question chosen by keywords, which is a simple rule rather than real understanding.
- No live language model has been evaluated yet. All the figures above are for the offline composer.
- Rate limits are kept in memory. On the free hosting plan, all data resets on restart.

## Running it yourself

You need Python 3.12.

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Then open `localhost:8000` for the chat, `/admin` for the dashboard or `/docs` for the API. The search index and the classifier are built on first start.

No API keys are needed. Without them, the assistant uses its offline composer, local embeddings and labelled demo data. To add keys or change settings, copy [.env.example](.env.example) to `.env`. Every option is explained in that file.

To sign in to the dashboard, generate a password hash and add it to `.env`:

```bash
python -m app.auth "your-password"   # prints ADMIN_PASSWORD_HASH=...
```

If you don't set one, a one-time password is printed when the server starts.

### Tests

```bash
python tools/quality_gate.py      # the release gate: grounding, relevance, conversation cases, fairness
python tools/test_<name>.py       # each of the 19 suites runs on its own
```

`test_auth.py` and `test_payment_session.py` need the server running, and `test_auth.py` reads the admin password from the `KQ_TEST_ADMIN_PASSWORD` environment variable. The rest use a temporary database and stubbed providers.

## Deployment

The live copy is deployed to Render from [render.yaml](render.yaml). On the server, the embedding model runs on ONNX Runtime instead of PyTorch. This gives the same vectors and keeps memory use around 285 MB, under the free plan's 512 MB limit. Setup notes are in [docs/deploy_render.md](docs/deploy_render.md).

## Repository layout

```
backend/app/     the FastAPI app: agent, retrieval, payments, staff, privacy
backend/data/    policy documents, evaluation sets, classifier training data
backend/tools/   tests, the quality gate and evaluation scripts
frontend/        chat, staff dashboard and privacy notice (plain HTML/CSS/JS)
docs/            deployment guide, Kenya Airways integration spec, draft DPIA
docgen/          how the synthetic datasets were generated
```
