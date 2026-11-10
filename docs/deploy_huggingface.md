# Deploying KQ-Propel to Hugging Face Spaces (free)

The free **CPU Basic** Space (2 vCPU, 16 GB RAM) runs the app at full quality; the app needs about 616 MB,
more than the 512 MB of most free plans. The Space gets a permanent HTTPS address, which gives M-Pesa a
fixed callback URL.

**Limits of the free tier.** The database and search index live inside the container and are reset when the
Space restarts or is rebuilt (they are rebuilt automatically at start-up; conversations and payment records
are lost). The Space sleeps after 48 hours without visitors, and the first visit then takes a few minutes.
Fine for demonstrations; real payment records need persistent storage or another host.

## 1. Create the Space

1. Sign up at https://huggingface.co (free, no card).
2. **New Space**: name `kq-propel`, SDK **Docker**, template **Blank**, hardware **CPU basic (free)**.
   Visibility: **Public** (the code is already public on GitHub; secrets stay hidden either way).
3. In the Space's **Files** tab, upload the two files from `deploy/huggingface/` in this repository:
   `Dockerfile` and `README.md` (replace the README the Space created).

## 2. Enter the settings (Space → Settings → Variables and secrets)

Your Space address is `https://<your-username>-kq-propel.hf.space` (shown under the Space's "Embed this
Space" menu as the direct URL). Below, `<space>` means that address.

**Secrets** (hidden after saving):

| Name | Value |
|---|---|
| `ADMIN_PASSWORD_HASH` | the same value as in your local `.env` (staff password stays the same) |
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY` | from your local `.env` |
| `DARAJA_CALLBACK_TOKEN` | a new long random string (generate one; do not reuse the tunnel's) |
| `DARAJA_CALLBACK_URL` | `<space>/api/payments/daraja/callback/<the token above>` |
| `AVIATIONSTACK_API_KEY` | from your local `.env` (optional; demo flights without it) |

**Variables** (visible):

| Name | Value |
|---|---|
| `DARAJA_SANDBOX` | `true` |
| `DARAJA_SANDBOX_TEST_NUMBERS` | `0704136123` (only these numbers ever receive a sandbox prompt) |
| `ALLOWED_ORIGINS` | `<space>` |

Do **not** set `DARAJA_VERIFY_CALLBACKS=false`. Leave `HANDOVER_ENABLED` and `PRIVACY_RETENTION_ENABLED`
unset (off) until Kenya Airways decides.

## 3. Build and check

1. The Space builds automatically (about 5-10 minutes). The log ends with `Uvicorn running on
   http://0.0.0.0:7860`.
2. Open `<space>/api/health`: it should say `{"status":"ok","app":"KQ-Propel"}`.
3. Open `<space>` directly (not the huggingface.co Space page, whose frame blocks the session cookie) and ask
   a question; open `<space>/admin` and sign in with your usual staff password.
4. M-Pesa: in the chat, ask what a name correction costs, say yes, choose M-Pesa, give 0704136123, enter the
   PIN. The payment should show as paid. (Ksh 1, real money to Safaricom's test Paybill.)

## 4. Updating

Push to the `priority-1-hardening` branch on GitHub (or change `BRANCH` in the Space's Dockerfile once the
work is merged into `main`), then Space → Settings → **Factory rebuild**.

## Security notes

- The chat is public. Sandbox payment prompts reach only `DARAJA_SANDBOX_TEST_NUMBERS`; anyone else gets the
  labelled demonstration. Public use does consume the AviationStack quota.
- The dashboard needs the staff password; failed sign-ins are locked out per client address.
- Rate limits per session and IP address are not built yet (Priority 5).
