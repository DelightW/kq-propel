# KQ-Propel: professional-readiness audit and work plan

**Audit date:** 20 September 2026  
**Code baseline:** `63126ee`  
**Purpose:** turn the current academic prototype into a trustworthy passenger-support product. This is an audit and proposed implementation backlog, not a claim that the changes below have been completed.

## 1. Executive decision

**Current classification: an academic/demo prototype, not ready for public passenger use or real payments.**

Keep the useful foundation: FastAPI, a separately presented passenger chat and admin dashboard, policy retrieval, a trainable sentiment classifier, provider adapters, and persistence. The main shortfall is not the framework or colour scheme. It is the gap between what the interface claims and what the system can verify.

Priorities:

1. Prevent demo data, estimated fees, and unconfirmed payment requests from being mistaken for real airline information or completed transactions.
2. Establish access control, session ownership, privacy controls, and testable API contracts.
3. Improve conversational continuity, clarification, retrieval correctness, and honest evaluation.
4. Finish accessible passenger and administrator workflows.
5. Prove recovery, observability, and deployment readiness before public release.

**Do not enable real payments simply by adding credentials. Do not expose the current admin API to the public internet.**

### Resolution log

Findings closed since the audit baseline. Each is verified by a runnable check in `backend/tools/`.

| ID | Status | What changed | Verified by |
|---|---|---|---|
| BE-01 | **Closed** | All `/api/admin/*` data routes require an authenticated staff session (`auth.require_staff`). scrypt password hashing, server-issued revocable session tokens stored as SHA-256 digests, HttpOnly/SameSite cookies, username+IP lockout after 5 failures, and an `admin_audit` trail of sign-ins and passenger-data reads. No default password exists in source; an unconfigured deployment generates a one-time password per run. | `tools/test_auth.py` (34 checks), `tools/test_auth_ui.js` (17 checks) |
| BE-02 | **Closed** | Passenger sessions are server-issued (`POST /api/chat/session`), unguessable, and bound to an HttpOnly cookie. A body `session_id` that disagrees with the cookie is rejected with 403. `POST /api/chat/reset` clears pending payment state for shared devices. | `tools/test_auth.py` §8–9, `tools/test_payment_session.py` |
| BE-09 | **Partly closed** | Message length is bounded by `MAX_MESSAGE_CHARS` (default 2000) and blank messages are rejected with 422. Per-session and per-IP request quotas are still outstanding. | `tools/test_auth.py` §10 |
| BE-18 | **Partly closed** | `allow_origins=["*"]` replaced with a configurable allowlist and `allow_credentials=True`; methods and headers narrowed. Structured logging, correlation IDs and alerting remain outstanding. | `config.ALLOWED_ORIGINS` |

Still open and unchanged: BE-03 through BE-08 (payment settlement and flight-data honesty), BE-10 through BE-17 except as noted, and all UX findings.

### Scope and limits

- Reviewed current application source, frontend assets, tracked configuration/documentation, and data-generation/evaluation implementation.
- Traced backend routes and persistence; ran isolated provider/schema/dashboard checks with mocks, without live provider requests or production database/index changes.
- Frontend source review is not a visual, device, assistive-technology, or cross-browser certification.
- This is a broad product/engineering readiness audit, not a penetration test or regulatory certification.
- Runtime environment variables were not inspected for secret values. Missing `.env` files alone would not prove that integrations are inactive.
- No production credentials, provider callbacks, flight accuracy, real payment settlement, airline authorisation, or deployment infrastructure were verified.
- The backlog separates observed defects from missing capabilities and proposed quality targets. No audit can guarantee discovery of every possible defect.

### Severity and evidence labels

| Priority | Meaning |
|---|---|
| P0 | Blocks public use or real transactions; complete before a public beta. |
| P1 | Required for a credible, supportable limited beta. |
| P2 | Important usability, operational, or scale improvement. |
| P3 | Optional expansion after measurable demand. |

**Reproduced** = exercised in isolated checks. **Source-confirmed** = demonstrated by the current implementation. **Gap** = absent capability needed for the proposed release. **Validate** = requires browser, live integration, user, or load testing.

## 2. Backend, integrations, and operational findings

Paths below are relative to the repository. Line references describe the audited baseline.

| ID / priority | Finding and evidence | Why it falls short | Required change / completion test |
|---|---|---|---|
| BE-01 / P0 | **Source-confirmed:** no authentication/role dependencies on admin routes: `backend/app/routers/admin.py:16–84`; application mounts them directly in `main.py:20–21`. | Anyone able to reach the API can request transaction data and run potentially costly comparisons. Hiding the admin navigation would not protect it. | Authenticate staff and enforce roles on the API; deny anonymous requests with 401 and insufficient roles with 403; verify every admin endpoint. |
| BE-02 / P0 | **Source-confirmed:** clients supply arbitrary `session_id` strings (`routers/chat.py:10–17`); conversation/payment state is keyed by that value (`database.py:115–120,148–169`). | A session identifier is being used without demonstrated ownership. Payment context needs an authenticated or server-issued anonymous-session boundary. | Introduce server-issued, unguessable session credentials and ownership checks. Knowing another session ID must not allow using its context or payment state. Test two independent users and expired sessions. |
| BE-03 / P0 | **Reproduced:** live flight failures silently return simulated data; unknown flights receive randomly chosen status/delay (`aviationstack.py:23–68`). | A provider outage or typo can become plausible but false operational advice. | Explicit demo/live modes; in live mode return unavailable/not found, never generated facts. Include provider source, retrieval time, flight date and timezone. Test timeout, empty data, malformed payload and unknown flight. |
| BE-04 / P0 | **Reproduced:** successful live flight responses contain nested `departure`/`arrival` but not the flat `route`, `gate`, `delay_minutes` consumer fields (`aviationstack.py:34–42`). | Live integration is not a drop-in replacement for the sandbox schema. Missing information can appear as `n/a` or zero delay. | Define one typed flight result shared by adapter and consumer. Fixture with gate B14 and delay 185 must display those values; unknown delay must remain unknown, not zero. |
| BE-05 / P0 | **Source-confirmed:** Daraja uses a placeholder callback URL (`daraja.py:54`), with no callback/status endpoint among the current application routes. `update_transaction_status()` exists but does not supply a completion workflow. | Requesting an STK prompt is not proof that money was received. Cancellation, timeout, completion and reconciliation are unfinished. | Implement a provider-reviewed callback/status design; persist pending/succeeded/failed/cancelled/expired states, validate correlation and amounts, verify ambiguous events against provider records, and reconcile missed callbacks. Do not invent a signature scheme the provider does not support. |
| BE-06 / P0 | **Reproduced:** Daraja reports `success=True` after an HTTP-success response without checking business response code or requiring checkout ID (`daraja.py:62–72`). | A rejected request may be shown as successfully initiated. | Validate business response code, required fields, and error classes. Fixture with response code `1` and no checkout ID must fail and must not produce a success card. |
| BE-07 / P0 | **Source-confirmed:** transaction schema has no unique checkout/idempotency constraint; amounts are stored as `REAL`; pending payment has no expiry or booking association (`database.py:27–37,53–60,93–101,148–169`). | Retries/concurrent turns can create ambiguous transaction records; a text-derived quote is not an authoritative airline bill. | Use integer minor units or a defined exact monetary representation; bind a versioned quote to booking/service/currency and expiry; enforce idempotency and transactional state transitions. Duplicate requests/callbacks must not double-charge or duplicate final settlement. |
| BE-08 / P1 | **Source-confirmed:** sandbox URLs are hard-coded in `daraja.py:19–20`; `DARAJA_SANDBOX` exists in `config.py:44` but is not used by the adapter. | Changing the flag does not produce a production integration. | Validated environment-specific configuration with a production release gate; test correct endpoint selection without sending a real charge. |
| BE-09 / P1 | **Reproduced:** `ChatRequest` accepts blank sessions, empty/whitespace-only messages and a 100,000-character message (`routers/chat.py:10–12`). No application rate-limit middleware is present in `main.py`. | Accidental oversized input and unbounded usage consume resources and cause poor error handling. | Enforce trimmed nonempty input, a documented size limit, bounded session format, request limits and consistent errors. Add per-session/IP quotas and provider budget limits appropriate to deployment. Test 422/413/429 paths. |
| BE-10 / P0 | **Source-confirmed:** raw conversation text and phone numbers are stored (`database.py:16–37`); no retention/delete API, masking layer or access audit is present in the current routes. | Passenger and payment-related data lacks an explicit lifecycle and controlled visibility. | Data inventory, minimisation, masked views/logs, restricted access, retention/deletion jobs and a documented privacy notice. Verify deletion covers derived records and define backup-retention limitations. Review applicable Kenya data-protection and provider obligations with qualified stakeholders. |
| BE-11 / P1 | **Reproduced:** admin sums all transaction statuses and only the latest 200 records (`routers/admin.py:23,33,46–50`); transaction listing is capped at 100 without pagination (`:55–57`). | Failed/pending attempts contribute to the displayed total; a capped recent window can be mistaken for business totals. | Separate settled revenue, pending amounts, failed attempts and simulated activity. Aggregate over an explicit date range in the database; paginate records. A failed 5,000 + pending 9,000 fixture must yield settled revenue 0, not 14,000. |
| BE-12 / P1 | **Reproduced/source-confirmed:** an empty evaluation sample produces zero-valued metrics; `1 - avg_grounded` is labelled a hallucination rate (`routers/admin.py:25–31,38–44`). | No evidence looks like perfect safety; a heuristic is presented as a measured factual-error rate. | Show “no data” for empty samples and clearly label heuristic proxies; publish sample size/window and independently adjudicated factual-error measures. |
| BE-13 / P1 | **Source-confirmed:** `.env` instructions in README are not paired with an application dotenv loader (`config.py` uses `os.getenv`; documented Uvicorn command has no `--env-file`). | Copying keys into `.env` alone may leave the application silently in fallback mode. | Choose and document one configuration-loading mechanism; validate it on clean startup and report nonsecret provider/mode status. Never display credential values. |
| BE-14 / P1 | **Source-confirmed:** startup performs ingestion and model training (`main.py:24–28`); health always returns `ok` (`:31–33`). | Readiness and model/index failures are not distinguished; workers can repeat mutable startup work. | Separate liveness/readiness; build versioned artifacts outside serving startup, load atomically, validate compatibility, and report degraded dependencies. Test a missing/corrupt artifact and multi-worker startup. |
| BE-15 / P1 | **Source-confirmed:** full model comparison runs synchronously through GET (`routers/admin.py:60–84`), with repeated model calls and no job lifecycle. | Refreshes can re-run expensive work; latency and provider failures block the request. | Staff-only POST job with idempotent creation, concurrency/budget limits, progress, cancellation and persisted results. GET should read status/results, not trigger paid evaluation. |
| BE-16 / P1 | **Gap:** no tracked automated test suite, CI workflow, dependency lock, deployment manifest, or recovery runbook in the repository inventory. Requirements use open-ended minimum versions. | Clean installs, regressions and recovery are not repeatable enough for dependable releases. | Pin/lock a supported runtime/dependency set; add focused unit/contract/browser tests and CI; build a repeatable staging deployment and exercise backup restoration/rollback. |
| BE-17 / P1 | **Gap/source-confirmed:** SQLite schema is created with `CREATE TABLE IF NOT EXISTS`, without migration versioning or query indexes; connections are per-operation (`database.py:15–77`). | Schema upgrades and concurrent state transitions need an explicit strategy. SQLite itself is not automatically unsuitable for a small pilot. | Add migrations and indexes, transactional critical sections, retention and tested backups. Measure concurrency first; adopt a managed relational database if multi-instance/load requirements warrant it. |
| BE-18 / P1 | **Gap:** current app setup has no structured request tracing, central error contract, latency/provider-cost instrumentation or incident alerts (`main.py`). CORS allows all origins (`:16–18`). | Operators cannot readily explain errors or provider degradation; deployment boundaries are undefined. | Structured redacted logs, correlation IDs, latency/error/cost metrics, alerts, explicit CORS allowlist, TLS/proxy configuration and browser response-header policy. Verify cross-origin behavior and error redaction in staging. |

### 2.1 Passenger website and admin interface

The frontend review covered all six HTML/CSS/JavaScript files. Both scripts passed `node --check`; offline DOM-stub checks reproduced payment labels, misleading transaction styling and missing-metric aggregation. No real browser or screen reader was run.

| ID / priority | Finding and evidence | Why it falls short | Required change / completion test |
|---|---|---|---|
| UX-01 / P0 | **Reproduced:** `daraja_sandbox` is labelled “Safaricom Daraja,” alongside “Payment request sent” and PIN instructions (`frontend/chat.js:105–139`). No persistent demo banner in `index.html`. | A sandbox request can look like a live financial action. | Shared explicit environment enum, persistent mode indicator and correct per-card instructions. Sandbox, simulation, live and unknown fixtures must look meaningfully different; unknown must never imply live. |
| UX-02 / P0 | **Gap:** cards cover initiation success/failure only; no final payment status/recovery workflow (`chat.js:94–150,316–354`). | A disconnected passenger cannot know whether to retry or wait. | Confirm authoritative quote details; recover transactions by ID across reloads; show pending/expired/cancelled/failed/settled states. A lost response and retry must recover one transaction, not initiate two. Depends on BE-05–07. |
| UX-03 / P0 | **Reproduced:** substring status checks treat `unsuccessful` as “success” and `not accepted` as “accept” (`frontend/admin.js:154–156`). | Failed transactions receive success styling. | Exact enum-to-label/style mapping. Accepted/pending must remain distinct from paid; test negative, unknown and missing statuses. |
| UX-04 / P1 | **Source-confirmed:** fetch requests lack timeouts/cancellation; inputs/buttons stay disabled while unresolved (`chat.js:309–354`; `admin.js:63–75,166–181`). “Reconnecting...” has no reconnect implementation. | A stalled request locks the workflow and error messages mislead. | Bounded requests, cancellation, retained drafts and accurate retry/offline/429/401/500 states. Never offer blind retry for an uncertain payment. Disable or explain unavailable suggestion chips while busy. |
| UX-05 / P1 | **Source-confirmed:** localStorage session ID persists but reload starts a visually fresh transcript; no new/reset/forget/history controls (`chat.js:24–33,329,376–382`; `index.html:29–52`). | Visible conversation and server-side context can diverge, especially on shared devices. | Decide ephemeral vs resumable sessions; implement visible history/reset/forget controls and retention explanation. Reload, another tab and blocked storage must have predictable behavior. Local clearing must not claim server deletion. |
| UX-06 / P1 | **Source-confirmed; manual validation needed:** mobile reasoning drawer is moved offscreen, not removed from focus/accessibility; no focus trap/restore or expanded state (`index.html:24,55–63`; `chat.css:355–368`; `chat.js:264–305`). | Keyboard and screen-reader users can reach hidden controls or lose context. | Mobile dialog semantics, inert/hidden closed state, focus containment/restore, Escape and background control. Preserve desktop nonmodal sidebar behavior across the 1024px breakpoint. |
| UX-07 / P1 | **Gap/source-confirmed:** authorship and typing dots lack adequate accessible text; status updates lack live semantics; completion forcibly focuses input (`chat.js:61–91,152–158,309–313`; `index.html:21,32`; `admin.html:77`). | Sender, busy/error state and navigation can be confusing without sight or a pointer. | Announce sender/status concisely, avoid duplicate live announcements, preserve deliberate focus, name scrollable table regions and test NVDA/VoiceOver plus keyboard-only use. |
| UX-08 / P1 | **Reproduced:** missing metrics become zero; incomplete comparison rows still increase denominators (`admin.js:33–42,85–98,206–221`). | One valid all-ones row plus one missing row can appear as 0.500 instead of 1.000 with one valid sample. | Per-metric valid denominators, null/no-data/failed states, provenance and sample windows. Clearly distinguish heuristic proxies from observed factual errors. |
| UX-09 / P1 | **Source-confirmed:** refresh failure updates only KPI error content, leaving old tables/meters unmarked (`admin.js:63–75,78–161`). | Stale financial/model data appears current; all errors become “Offline.” | Last-success timestamps and coherent stale/loading/error states across panels; distinguish expired login, server error and network outage. Test success → failed refresh → recovery. |
| UX-10 / P1 | **Source-confirmed:** citations rely on a literal final `Source:` text suffix, without inspectable structured links/version details (`chat.js:45–79`). | Passengers cannot readily verify important policy claims. | Typed citation objects with document, section, version, applicability and safe links. Support multiple/no sources and keyboard access; reject unsafe URL schemes. |
| UX-11 / P1 | **Source-confirmed:** frustrated sentiment receives “Priority support”; initial green status is not health-derived; raw returned traces are shown as “Agent Reasoning” (`chat.js:81–86,177–229,344`; `index.html:60–61`; `chat.css:89–96`). | Empathy can be mistaken for an actual support queue action; internal data and pseudo-reasoning undermine trust. | Show “priority” only after a confirmed handoff; meaningful provider/connection status; replace debug trace with allowlisted, redacted “Sources and actions.” Keep operational diagnostics staff-only. |
| UX-12 / P2 | **Source-confirmed CSS issue; visual validation needed:** meter fill is an empty inline span with width/height but no block display (`chat.js:245–250`; `chat.css:425–426`). | The chat-side proportional meter may not render as intended. | Use correctly sized block/native meter with accessible value. Visually verify 0%, 50% and 100%. |
| UX-13 / P2 | **Source-confirmed/validate:** rendering forces scroll-to-bottom; no streaming; responsive layout has not been browser-verified (`chat.js:39–40,326–345`; `chat.css:42–47,140–149`; `admin.css:161–203`). | Long histories interrupt reading; slower replies lack progressive feedback; mobile/zoom edge cases remain unproven. | Preserve reading position and offer new-message navigation. Consider streaming only after correct cancellation/recovery. Test narrow screens, keyboards, zoom, long identifiers, large tables and reduced motion. |
| UX-14 / P1 | **Gap:** current pages lack a complete passenger trust/support shell and a usable staff sign-in/sign-out/session-expiry workflow. | Separate page styling is not a complete professional website or operational console. | Add accurate About/capabilities, contact/handoff, privacy/retention and appropriate terms/support information; integrate staff session UX with BE-01. Do not imply Kenya Airways affiliation without authorisation. |

**Positive foundations to retain:** plain HTML/JS is adequate here; text escaping, separate passenger/admin assets, responsive grids, safe-area handling, reduced-motion styles and the existing conversation live region are useful starting points. Their presence does not replace browser/accessibility verification.

## 3. Isolated verification record

Executed during this audit with placeholder credentials and mocked HTTP functions; no real flight/payment requests were made. Schema/admin checks executed isolated AST-extracted definitions to avoid application startup and production writes.

| Check | Observed result |
|---|---|
| Flight request raises an exception | A sandbox delayed KQ100 response is returned. |
| Live flight fixture contains gate B14 and delay 185 | Adapter result lacks flat `route`, `gate`, `delay_minutes` fields. |
| Daraja HTTP-success fixture contains rejected response code `1` | Adapter returns `success=True` and no checkout ID. |
| Inspect mocked STK request | Callback points to `https://example.com/daraja/callback`. |
| Validate blank/oversized chat payloads | Empty session, blank messages and 100,000-character message are accepted. |
| Admin receives failed 5,000 and pending 9,000 fixtures | Total amount is 14,000, without settlement filtering. |
| Admin has no evaluation records | Hallucination rate displays as 0, with sample size 0. |
| Frontend sandbox payment fixture | Card uses “Safaricom Daraja” and payment/PIN instructions without an explicit sandbox label. |
| Frontend negative transaction statuses | `unsuccessful` and `not accepted` receive success-style classification. |
| Frontend complete + incomplete comparison rows | Missing observations depress displayed means instead of reducing the valid denominator. |

These checks reproduce current defects; they are not passing production-readiness tests. They should become regression tests during implementation.

## 4. Delivery strategy

### Decisions to agree before implementing

These are proposed defaults, not decisions already authorised:

- **First release:** a clearly labelled, read-only support beta. Payments remain disabled until the separate payment gate passes.
- **Authority:** use approved, versioned source documents. Synthetic policies remain available only in demo mode.
- **Accounts:** passengers may start anonymously through server-issued sessions; staff require authentication and role-based access. Access to booking-specific details needs stronger verification.
- **Human support:** agree who actually receives handoffs, their operating hours, and the escalation SLA before showing an escalation promise.
- **Languages:** explicitly support/test English first; add Swahili only with reviewed bilingual data and regression tests.
- **Hosting:** choose a staging/production platform, operating budget, expected concurrency, retention requirements and a rollback owner.
- **Metrics:** agree measurable targets based on pilot workload. Thresholds below are proposed release gates, not existing achievements.

### Phased work plan

Estimates are **focused engineering days for one developer**, excluding procurement, airline approval, provider onboarding and recruitment of reviewers. Re-estimate after Phase 0. Several tasks can run in parallel, but dependencies and release gates must still hold.

| Phase | Work and concrete deliverable | Dependencies | Estimate | Exit gate |
|---|---|---|---|---|
| 0 — Establish truth and baseline | Publish a capability matrix; correct misleading documentation; define demo/live modes; capture representative failing transcripts and the isolated tests above; decide first-release scope. | None | 2–3 days | Every passenger-facing capability marked working, simulated, unavailable or planned; no claim that a retrospective generator created the original dataset. |
| 1 — Safe service boundary | Staff authentication/RBAC, session ownership, input limits, quotas, privacy/masking/retention design, configuration validation and consistent error responses. Add regression tests alongside changes. | Phase 0 | 4–7 days | Anonymous admin access denied; cross-session actions denied; blank/oversized/rate-limited traffic handled; no secrets/complete payment identifiers in ordinary logs. |
| 2 — Knowledge and flight correctness | Approved policy registry and refresh process; typed source/flight contracts; reliable no-answer behavior; clarify missing booking/flight facts; replace silent simulation on provider failures. | Phase 0; Phase 1 before deployment | 4–7 days | Reviewed query fixtures use the correct policy/version; an unavailable provider yields an honest failure; flight numbers with multiple dated results require disambiguation. |
| 3 — Conversational reliability | General conversation state beyond payment offers; multi-turn clarification, topic changes, multi-intent answers, reliable references, concise tool summaries and real human-handoff integration. | Phases 1–2 | 5–8 days | Core end-to-end conversations pass, including pronouns, corrections, cancellation, changing subject, contradictory facts and unsupported actions. |
| 4 — Passenger and admin experience | Implement the frontend backlog, accessible components, source details, clear demo/pending states, session reset/history controls, request recovery, accurate dashboard filters/pagination and evaluation jobs. | Stable API contracts from Phases 1–3; visual work may start earlier | 5–8 days | Browser/device/accessibility acceptance matrix passes; keyboard-only passenger journey works; dashboard totals agree with server fixtures. |
| 5 — Payment lifecycle, separately gated | Booking-bound authoritative quotes, explicit consent, idempotency, callback/status reconciliation, exact amounts, environment validation and staff audit trail. | Phases 1–3; provider access and approved fee authority | 5–9 days plus provider lead time | Sandbox completion/cancel/timeout/duplicate/late-callback scenarios pass; no simulated activity presented as settled revenue. Production remains off until operational approval. |
| 6 — Evidence and quality gate | Larger independent evaluations, human factual-error review, grouped/template-aware splits, latency/cost measurements, adversarial/ambiguous inputs and regression automation. | Test design starts in Phase 0; final run after relevant features | 4–7 days | Frozen test set and report identify dataset/source/model versions, actual providers used, failures, uncertainty and limits; no heuristic marketed as measured hallucination probability. |
| 7 — Deployment and pilot | Reproducible build, CI gates, staging, migrations, readiness checks, logging/alerts, backups, restore/rollback rehearsal, limited supervised pilot and issue triage. | Phases 1–4 and 6; Phase 5 only if payments included | 4–7 days | Staging soak/load/recovery checks pass; named support owner and runbooks exist; pilot feedback reviewed before expanding traffic. |

**Full scope:** approximately 33–56 focused engineering days, not a deadline promise. A read-only beta can omit Phase 5; data approvals and genuine user evaluation may dominate elapsed time.

### Suggested first ten working days

1. Confirm scope, source authority and deployment constraints; record current limitations.
2. Add explicit demo-mode labels and provider-state reporting; prohibit silent fabricated live answers.
3. Add isolated regression tests and CI; correct environment loading documentation/behavior.
4. Protect admin endpoints and define staff roles.
5. Add session ownership, input limits, quotas and standard errors.
6. Normalize flight responses and test timeout/not-found/ambiguous-date cases.
7. Establish policy version/source metadata and abstention fixtures.
8. Add durable conversation state and clarify missing information.
9. Correct admin status-aware totals and no-data states; separate demo metrics.
10. Review progress with a live demo of passing and failing journeys; re-estimate the remaining backlog.

Days are sequencing guidance rather than a commitment that every task fits a single day. If access control or provider correctness overruns, finish it before cosmetic polish.

## 5. Release acceptance checklist

### Trust and domain accuracy

- [ ] The passenger sees whether the system is a demonstration and whether each external result is live, stale, simulated or unavailable.
- [ ] No live provider error falls back to fabricated operational facts.
- [ ] Policy answers cite approved document/version/section and applicability; unsupported questions receive a useful limitation or clarification.
- [ ] Fare class, route, dates, operating carrier and other required eligibility details are requested before a binding quote.
- [ ] Refunds, rebooking, cancellation and escalation are offered as actions only when an implemented authorised workflow exists.

### Chat behavior

- [ ] Multi-turn tests cover “what about business class?”, “that flight”, correction of a previous number, changing topic and cancelling a pending action.
- [ ] Multi-intent questions receive complete answers rather than the first matching route only.
- [ ] An ambiguous amount/phone/intent never triggers a charge.
- [ ] Timeout/retry does not duplicate messages or transactional actions.
- [ ] Sentiment does not determine entitlement or deny service; escalation is available without being classified frustrated.

### Proposed evaluation targets

Tune these with the supervisor/product owner before freezing the benchmark:

- [ ] Start with at least 100 independently reviewed support scenarios across answerable, unanswerable, ambiguous, multi-turn and tool-failure cases, including all known regression cases.
- [ ] At least 90% task success on that frozen support benchmark; report category-level results, not just an aggregate.
- [ ] No unsupported monetary entitlement or action on the designated critical-case regression set. Zero observed failures is not proof of zero real-world risk.
- [ ] Report retrieval recall@k and source-selection accuracy separately from answer correctness.
- [ ] Report human-reviewed factual-error and citation-support rates with sample size and uncertainty.
- [ ] Sentiment evaluation uses an independent holdout/grouped split, class-specific errors and realistic typo/mixed-language/neutral complaint examples.
- [ ] Report real provider/model identity, fallback counts, latency and cost; unavailable models are not compared under misleading labels.

### Website and accessibility

- [ ] Keyboard-only navigation, visible focus, programmatic names, live message/status announcements and manageable overlay focus.
- [ ] WCAG 2.2 AA is the intended target; automated scans plus manual checks are recorded, not assumed to certify compliance.
- [ ] Test at least current Chrome, Firefox, Edge and Safari, including iOS Safari and Android Chrome on representative phones.
- [ ] Test narrow screens around 320–390 CSS pixels, landscape, on-screen keyboard, 200% zoom, reduced motion and long unbroken content.
- [ ] Sources are inspectable, errors are actionable, pending requests are visible, and copy/reset/history interactions have explicit behavior.
- [ ] Trust/support/privacy/contact information is clear; no implication of airline affiliation without authorisation.

### Operations and data protection

- [ ] Unit, integration, contract and browser smoke tests run on a clean locked dependency environment in CI.
- [ ] Backup restoration and rollback have been demonstrated in staging, not merely documented.
- [ ] Startup does not unexpectedly retrain or rebuild incompatible indexes across workers.
- [ ] Proposed initial load test: 20 simultaneous chat sessions for 30 minutes, with under 1% unexpected server errors and p95 ordinary policy-answer latency under 5 seconds. Report provider-dependent tool latency separately; revise targets to match budget and workload.
- [ ] Redacted correlation-based logs explain provider outages, job failures and transaction transitions.
- [ ] Session expiry, ownership, retention/deletion and role access are covered by automated tests.
- [ ] A dedicated security/privacy review is completed before public launch; this product audit does not replace it.

### Additional gate for real payments

- [ ] Actual airline/service authority, provider production approval and a working support/refund process are confirmed.
- [ ] Quote, currency, amount, payer, booking/service and consent are bound to the transaction.
- [ ] Settlement is based on verified provider outcome, not successful STK initiation.
- [ ] Duplicate/replayed/late/out-of-order events and uncertain timeouts are safely handled.
- [ ] Reconciliation, alerting, incident response and customer-facing receipt semantics are tested.
- [ ] Production settings cannot silently use demo records or sandbox endpoints.

## 6. What not to do first

- Do not rewrite the frontend in React or replace FastAPI solely to appear professional. Existing frameworks can support a professional product.
- Do not train an LLM from scratch. This project primarily needs trustworthy orchestration, data and evaluation.
- Do not substitute “more agentic” behavior for explicit, testable transaction controls. Rules are appropriate when their limits are intentional.
- Do not switch databases before understanding deployment/concurrency requirements.
- Do not spend the first week on animations, avatars or additional dashboards while core information and payment semantics are incorrect.
- Do not inflate metrics through synthetic/template leakage or claim a newly created notebook proves earlier data provenance.

## 7. Backlog working convention

For each finding, create a tracked issue containing:

1. Finding ID and affected user journey.
2. Reproduction or source evidence.
3. Required behavior and explicitly out-of-scope behavior.
4. Dependencies and owner.
5. Regression/acceptance tests.
6. Demo/staging evidence and documentation changes.

Suggested roles: **backend/integration owner**, **frontend/accessibility owner**, **data/evaluation owner**, and **product/domain reviewer**. One student can perform multiple roles, but policy authority and independent evaluation should not be silently replaced by the developer's own assumptions.

A task is done only when its acceptance check passes, the result is reproducible, and related documentation accurately describes the delivered behavior. Update this plan as evidence changes.
