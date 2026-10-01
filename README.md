# KQ-Propel

An agentic retrieval-augmented generation (RAG) assistant for Kenya Airways customer support. It answers passenger questions from airline policy documents, checks live flight status, and can collect fees by card or M-Pesa. Built as a final-year project at Strathmore University.

## Features

| Feature | What it does |
|---|---|
| Policy answers | Retrieves the relevant policy section and answers from it, with a section citation on every reply. |
| Hybrid retrieval | Combines BM25 keyword scoring with embedding similarity. Maximal Marginal Relevance (MMR) stops results from repeating the same document. |
| Agent loop | A ReAct loop (thought, action, observation) with tools for policy search, flight status and payments. |
| Payments | Offers card (PayPal) or M-Pesa (Daraja STK Push) when an answer quotes a fee. A payment starts only after the passenger confirms, and only for an amount found in a policy. |
| Frustration detection | A TF-IDF and logistic regression classifier flags frustrated passengers so the agent responds with empathy and offers escalation. |
| Evaluation | RAG-Triad scores (context relevance, groundedness, answer relevance) are logged for every turn. |
| Model comparison | GPT-4o-mini is compared with an open-source model served through Ollama, using the same documents, retrieval and evaluation set. |
| Scope guards | If the retrieved sections do not match the question, the agent says so. Flight times need a flight number. |

All API keys are optional. Without them the system uses local embeddings and simulated flight and payment responses, which are labelled as simulated in the chat.

## Applications

| | Chat assistant | Admin dashboard |
|---|---|---|
| URL | `/` | `/admin` |
| Files | `index.html`, `chat.css`, `chat.js` | `admin.html`, `admin.css`, `admin.js` |
| Users | Passengers | Support supervisors |
| Purpose | Ask questions, check flights, pay fees | Sentiment metrics, RAG-Triad scores, hallucination rate, payment audit trail, model comparison |

Both are responsive and work on phones and laptops.

## Running it

```powershell
cd kq-propel\backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| What | URL |
|---|---|
| Chat assistant | http://127.0.0.1:8000/ |
| Admin dashboard | http://127.0.0.1:8000/admin |
| API docs | http://127.0.0.1:8000/docs |

### Signing in to the admin dashboard

The dashboard shows the payment audit trail, which contains passenger phone numbers, so every `/api/admin/*` route needs a staff session. There is no default password in the source code.

Set a permanent one:

```powershell
cd kq-propel\backend
python -m app.auth "your-password"      # prints ADMIN_PASSWORD_HASH=scrypt$...
```

Paste the printed line into `.env` next to `ADMIN_USERNAME=admin`. Only the hash is stored; the password itself never enters the repository.

If no credential is set, a one-time password is generated and printed to the console at startup, and it changes on every restart:

```
[auth] No ADMIN_PASSWORD_HASH or ADMIN_PASSWORD was configured.
       A one-time password has been generated for THIS RUN ONLY.

         username: admin
         password: 8Kd2mQx7bLpR
```

| Control | Behaviour |
|---|---|
| Unauthenticated request | `401`, before any data is read |
| Password storage | scrypt, standard library, never plaintext |
| Session token | Random 32 bytes; only its SHA-256 digest is stored |
| Cookie | `HttpOnly`, `SameSite=Lax`, `Secure` when `SECURE_COOKIES=true` |
| Sign out | Revokes the session on the server, not just in the browser |
| Repeated failures | Locks that username and IP for 5 minutes after 5 attempts |
| Error message | The same for a wrong username as a wrong password, so accounts cannot be guessed |
| Access log | Sign-ins and reads of passenger data are recorded and shown in the dashboard |

### Passenger sessions

The server issues the chat session (`POST /api/chat/session`) and binds it to an `HttpOnly` cookie. The browser used to invent its own identifier and the server trusted it, which meant anyone supplying another passenger's identifier inherited that conversation and its pending payment. A `session_id` that disagrees with the cookie is now rejected with `403`.

`POST /api/chat/reset` starts a new session and clears any pending payment, which matters on a shared device.

### Opening the chatbot on your phone

Running with `--host 0.0.0.0` makes the assistant reachable from other devices on the same Wi-Fi network.

1. Find the laptop's Wi-Fi address:
```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -eq "Wi-Fi"
```
2. Allow the port through Windows Firewall. Run this once in an Administrator PowerShell:
```powershell
   New-NetFirewallRule -DisplayName "KQ-Propel (TCP 8000)" -Direction Inbound `
     -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private
```
3. On the phone, open `http://<that-address>:8000/`, for example `http://192.168.100.70:8000/`.

Both devices must be on the same network, and Windows must mark it as Private.

## Payments

When the agent quotes a fee the passenger owes, it offers two ways to pay.

| | Card (PayPal Orders API) | M-Pesa (Daraja STK Push) |
|---|---|---|
| Reaches | Anyone with a Visa, Mastercard or Amex | Kenyan Safaricom lines only |
| Currency | The currency the fee is published in | Kenya shillings |
| Exchange rate | None needed | `USD_TO_KES_RATE`, shown to the passenger every time |

Money owed to the passenger, such as refunds and meal vouchers, is never offered for payment. Without credentials both rails run in simulated mode. To test the M-Pesa rail against the Safaricom sandbox, use the operator tool instead of the chat:

```
python tools/send_test_stk.py --phone 0712345678 --amount 1
```

## Configuration

Set values in a `.env` file at the repository root (see `.env.example`). Real environment variables take precedence.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Enables GPT-4o-mini and `text-embedding-3-small` |
| `OPEN_SOURCE_MODEL_NAME`, `OLLAMA_BASE_URL` | Second model for the comparison, served through Ollama |
| `AVIATIONSTACK_API_KEY` | Live flight data |
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY`, `DARAJA_SHORTCODE` | Safaricom Daraja credentials |
| `DARAJA_SANDBOX`, `DARAJA_CALLBACK_URL` | Sandbox or production host, and where Safaricom posts the result |
| `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, `PAYPAL_SANDBOX` | PayPal card checkout |
| `USD_TO_KES_RATE` | Indicative rate for M-Pesa payments (default 129.0, not a live rate) |
| `LOCAL_EMBEDDING_MODEL`, `LOCAL_EMBEDDING_ENABLED` | Local sentence-transformer embeddings (default `all-MiniLM-L6-v2`) |
| `RETRIEVAL_MMR_LAMBDA` | Relevance and diversity balance in retrieval (default 0.8) |
| `MONGODB_URI`, `MONGODB_DB_NAME` | MongoDB Atlas Vector Search. A local JSON index is used if unset |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | Staff login for the admin dashboard. Generate the hash with `python -m app.auth "password"` |
| `ADMIN_SESSION_TTL_MINUTES`, `ADMIN_MAX_LOGIN_ATTEMPTS`, `ADMIN_LOCKOUT_SECONDS` | Session lifetime and brute-force lockout (defaults 120, 5, 300) |
| `SECURE_COOKIES` | Marks session cookies `Secure`. Set to true when serving over HTTPS |
| `ALLOWED_ORIGINS` | Browsers permitted to call the API. A wildcard is not allowed with cookies |
| `MAX_MESSAGE_CHARS` | Longest passenger message accepted (default 2000) |

## More detail

The sentiment classifier is documented in [docs/sentiment.md](docs/sentiment.md), and the payment flow in [docs/payments.md](docs/payments.md).
