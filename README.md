# KQ-Propel

An agentic retrieval-augmented generation (RAG) assistant for Kenya Airways customer support. It answers passenger questions from airline policy with a citation, checks flight status, looks up a booking, takes payment by M-Pesa or card, and hands a conversation to a member of staff when asked. Built as a final-year project at Strathmore University.

**Live demonstration:** https://kq-propel.onrender.com (chat) and https://kq-propel.onrender.com/admin (staff dashboard). It runs on Render's free plan, so the first visit after a quiet spell takes about a minute to wake, and its data is reset whenever it restarts.

> This is a research prototype, not a Kenya Airways service. Bookings and flights shown without a live source are labelled as demonstration data, and M-Pesa runs in Safaricom's sandbox.

## What it does

| For passengers | |
|---|---|
| Policy answers | Retrieves the relevant policy section and answers from it, citing the section and edition. Declines when nothing it retrieved matches the question. |
| Follow-up questions | "What about Business class?" or "Is that flight delayed?" keep the context of the previous turn for 30 minutes. |
| Flight status | Kenya Airways' own feed when configured, then AviationStack. A status is never invented. |
| Booking lookup | Booking reference and surname give answers for the passenger's own trip. Five failed lookups in an hour lock the conversation. |
| Name corrections | Shows the name on the booking, asks for the new one, counts the characters changed, quotes the fee from the refund policy and, once paid, sends the request to staff. |
| Payments | M-Pesa (Daraja STK Push) or card (PayPal), only for a fee quoted from policy and only after the passenger confirms. A payment counts as paid only when the provider confirms it. |
| Receipts | A PDF receipt for every paid payment. |
| A person | Opens a case that staff answer from the dashboard (when handover is switched on). |
| Privacy | A passenger can delete their conversation from the chat. See [the privacy notice](frontend/privacy.html). |

| For staff (`/admin`) | |
|---|---|
| Individual accounts and roles | **Administrator** (everything), **customer service agent** (handovers, name corrections, payments) or **analyst** (quality metrics only, no passenger records). New accounts receive a one-time password that must be changed at first sign-in. |
| Work queues | Passengers waiting for a person, and paid name corrections to carry out and mark done. |
| Monitoring | RAG-Triad scores, frustration levels, classifier performance, payment ledger and an access log of who read passenger data. |
| Data protection | Phone numbers shown masked; erasure on request; configurable retention. |

Behind the scenes:

- **Hybrid retrieval.** BM25 combined with all-MiniLM-L6-v2 embeddings, Maximal Marginal Relevance and policy-aware ranking (route exceptions; checked versus hand baggage).
- **ReAct agent.** Each answer shows the agent's reasoning trace.
- **Frustration classifier.** A self-trained TF-IDF and logistic-regression classifier. It changes only the tone of a reply, never the policy answer.
- **Rate limits.** Per conversation, per network address and per phone number.
- **Accessible interfaces.** Both pass the WCAG 2.1 A/AA rules checked by axe-core.

All API keys are optional. Without them the assistant uses its offline answer composer, local embeddings and labelled demonstration data.

## Running it locally

```powershell
cd kq-propel\backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| What | URL |
|---|---|
| Chat | http://127.0.0.1:8000/ |
| Staff dashboard | http://127.0.0.1:8000/admin |
| API documentation | http://127.0.0.1:8000/docs |

The policy index and the classifier are built automatically on first start.

**Staff sign-in.** Create a password hash and put it in `.env` at the repository root:

```powershell
python -m app.auth "your-password"      # prints ADMIN_PASSWORD_HASH=scrypt$...
```

That account (`ADMIN_USERNAME`, default `admin`) is the first administrator; other staff are added from the dashboard's Staff Accounts panel. If no credential is set, a one-time password is printed at start-up and changes on every restart. There is no default password in the source code.

**On a phone.** With `--host 0.0.0.0`, open `http://<laptop's Wi-Fi address>:8000/` on a phone on the same network (allow TCP port 8000 through Windows Firewall for Private networks). In a desktop browser, Ctrl+Shift+I and then Ctrl+Shift+M shows the phone layout.

## Hosting

The live copy is deployed from this repository with the Render Blueprint in [render.yaml](render.yaml). The server installs [backend/requirements-server.txt](backend/requirements-server.txt), which runs the same embedding model on ONNX Runtime instead of PyTorch so that it fits the free plan's 512 MB. Secrets are entered in Render's dashboard, never in the repository. Every push to the deployed branch redeploys it. Step-by-step instructions: [docs/deploy_render.md](docs/deploy_render.md).

## Payments

| | M-Pesa (Daraja STK Push) | Card (PayPal Orders) |
|---|---|---|
| Currency | Kenya shillings, converted at `USD_TO_KES_RATE` and disclosed every time | The currency the fee is published in |
| Confirmation | Callback to a secret URL, double-checked with Safaricom's status query | Order captured on the server |

Safaricom's sandbox takes real money when the PIN is entered. In sandbox mode, prompts therefore go only to numbers listed in `DARAJA_SANDBOX_TEST_NUMBERS`, always for Ksh 1. Any other number receives a labelled demonstration. Money owed to the passenger, such as refunds, is never offered as a payment.

## Testing

```powershell
cd kq-propel\backend
python tools/quality_gate.py             # release gate: groundedness, relevance, conversation cases, fairness
python tools/test_priority5.py           # each suite in tools/ runs on its own
```

There are 19 suites with 340 checks. `test_auth.py` and `test_payment_session.py` need a running server; the others use a temporary database and stubbed providers.

## Configuration

Copy [.env.example](.env.example) to `.env`; it documents every setting. The main ones:

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | First administrator account |
| `OPENAI_API_KEY`, `OPEN_SOURCE_MODEL_NAME`, `OLLAMA_BASE_URL` | Live language models for answers and the model comparison |
| `AVIATIONSTACK_API_KEY` | Live flight status |
| `KQ_BOOKING_API_URL`, `KQ_FLIGHT_API_URL` (+ tokens) | Kenya Airways booking and flight services, when provided ([specification](docs/kq_integration_spec.md)) |
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY`, `DARAJA_CALLBACK_URL`, `DARAJA_CALLBACK_TOKEN` | M-Pesa |
| `DARAJA_SANDBOX_TEST_NUMBERS` | The only numbers sandbox prompts may go to |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Card checkout |
| `HANDOVER_ENABLED` | Lets passengers reach a person (off by default) |
| `PRIVACY_RETENTION_ENABLED` | Automatic deletion by age (off until retention periods are agreed) |
| `RATE_LIMIT_*`, `TRUSTED_PROXY_HOPS` | Rate limits, and how many proxies sit in front of the app |
| `SECURE_COOKIES`, `ALLOWED_ORIGINS` | Cookie and browser-origin security for HTTPS deployments |

## Project layout

```
backend/app/        FastAPI app: agent, retrieval, payments, staff, privacy, rate limits
backend/data/       Policy corpus, evaluation sets, classifier data
backend/tools/      Test suites, quality gate and evaluation tools
frontend/           Chat, staff dashboard and privacy notice
docs/               Deployment guide, Kenya Airways integration spec, draft DPIA
docgen/             How the synthetic datasets were generated
```
