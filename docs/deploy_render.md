# Deploying KQ-Propel to Render (free)

The free web service has 512 MB of memory. The app runs its embedding model on ONNX Runtime
(`backend/requirements-server.txt`) and uses about 285 MB, measured in a container capped at 512 MB while it
answered all 30 evaluation questions. The ONNX vectors match the PyTorch ones to 1e-7 and the quality gate gives
the same scores.

**Limits of the free plan.** The service sleeps after about 15 minutes without visitors; the next visit wakes it
in about a minute. Files are not kept between restarts, so conversations and payment records are lost on every
restart or redeploy (the search index and classifier are rebuilt automatically). Fine for demonstrations, not
for real payment records.

## 1. Create the service

1. Sign up at https://render.com with **GitHub** (no card needed for free services) and allow Render to see the
   `kq-propel` repository.
2. In the Render dashboard: **New → Blueprint**, choose the `kq-propel` repository. Render reads `render.yaml`
   (service `kq-propel`, free plan, branch `priority-1-hardening`).
3. Render asks for the values marked `sync: false`. Your address will be `https://kq-propel.onrender.com` if that
   name is free (Render shows the actual address after creation; if it differs, update the two values that
   contain it).

| Name | Value |
|---|---|
| `ADMIN_PASSWORD_HASH` | the same value as in your local `.env` |
| `ALLOWED_ORIGINS` | `https://kq-propel.onrender.com` |
| `DARAJA_SANDBOX_TEST_NUMBERS` | the phone number you test with (only listed numbers get a real prompt) |
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY` | from your local `.env` |
| `DARAJA_CALLBACK_TOKEN` | a new long random string |
| `DARAJA_CALLBACK_URL` | `https://kq-propel.onrender.com/api/payments/daraja/callback/<the token above>` |
| `AVIATIONSTACK_API_KEY` | from your local `.env` (optional) |

4. **Apply**. The first build takes several minutes; the log ends with `Uvicorn running on http://0.0.0.0:...`.

## 2. Check

1. `https://kq-propel.onrender.com/api/health` shows `{"status":"ok","app":"KQ-Propel"}`.
2. Ask a question in the chat; sign in at `/admin` with your usual staff password.
3. M-Pesa: ask what a name correction costs, say yes, choose M-Pesa, give your listed test number, enter the PIN. It should
   show as paid (Ksh 1, real money to Safaricom's test Paybill).

## 3. Updating

Every push to the `priority-1-hardening` branch redeploys automatically.

## Security notes

- The chat is public. Sandbox payment prompts reach only `DARAJA_SANDBOX_TEST_NUMBERS`.
- Do not set `DARAJA_VERIFY_CALLBACKS=false`. Leave `HANDOVER_ENABLED` and `PRIVACY_RETENTION_ENABLED` off until
  Kenya Airways decides.
- Rate limits (Priority 5) cap messages per conversation and per address, new sessions per address, and M-Pesa
  prompts per conversation and per phone number. Counters are in memory and reset when the service restarts.
- The limits and the sign-in lockout need the caller's real address. Behind Render's proxy it comes from
  `X-Forwarded-For`, whose left-hand entries the caller can forge. Set `TRUSTED_PROXY_HOPS` to the number of
  entries Render's proxies add (the address used is that many places from the right). To check it, make one
  failed sign-in and read the "login failed" row in the access log: its detail shows the forwarded chain.
  Measured on 11 November 2026 with a forged header: `203.0.113.77, 41.90.137.104, 172.71.141.37, 10.24.59.156`,
  that is, the forged entry, the real caller (added by Cloudflare), a Cloudflare address and Render's internal
  network. `render.yaml` therefore sets `TRUSTED_PROXY_HOPS=3`.
- Staff accounts other than the first administrator live in the database, so on the free plan they are lost when
  the service restarts; the administrator from `ADMIN_PASSWORD_HASH` is recreated at every start.
