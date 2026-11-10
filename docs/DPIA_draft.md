# KQ-Propel: Data Protection Impact Assessment (draft)

**Status:** Draft prepared by the developer from the system as built. Kenya Airways, as the data controller, must review, complete and approve it before any passenger uses the system. Sections marked **[Kenya Airways to decide]** cannot be settled by the developer. References to the Data Protection Act, 2019 should be confirmed by Kenya Airways' legal team.

## 1. Why an assessment is needed

KQ-Propel is a chat assistant that answers passenger questions from Kenya Airways policy, looks up flight status, takes fee payments by M-Pesa or card, estimates whether a passenger sounds frustrated, and can pass a conversation to support staff. It processes personal data of members of the public, includes a profiling element (the frustration estimate) and sends some data to third parties. Under section 31 of the Data Protection Act, 2019, processing that is likely to result in a high risk to data subjects requires an assessment before it begins.

## 2. Personal data processed

| Data | Where it is stored | Purpose | Source |
|---|---|---|---|
| Messages typed by passengers, and replies from the assistant and staff | `conversations` table | Answering the question; handing over to staff; quality monitoring | Passenger, assistant, staff |
| Frustration score and label for each message | `conversations` table | Acknowledging difficulty and offering a person | Derived by the classifier |
| Last question and flight number (30 minutes) | `conversation_context` table | Understanding follow-up questions | Derived |
| IP address, session timestamps | `chat_sessions` table | Session security, misuse prevention | Browser |
| Fee offer in progress | `payment_state` table | Completing a payment the passenger accepted | Derived from policy |
| M-Pesa phone number, amount, status and reference | `transactions` table | Sending the payment request; financial record | Passenger, payment provider |
| Handover case, reason, frustration score | `handovers` table | Connecting the passenger with staff | Passenger |
| Booked trip (flights, route, cabin, fare, baggage) for 30 minutes; never names | `conversation_context` table | Answering for the passenger's own trip | Kenya Airways booking system |
| Count of booking lookups per session | `booking_lookups` table | Stopping booking references being guessed | Derived |
| Question text with answer-quality scores | `rag_evaluations` table | Monitoring answer quality | Passenger |
| Staff username, actions and IP address | `admin_audit`, `admin_sessions` tables | Accountability for access to passenger data | Staff |

Passengers may also type data the system does not ask for, such as names, booking references or health information. The assistant does not request it, but it is stored with the message.

## 3. Recipients and transfers

| Recipient | Data sent | When | Location |
|---|---|---|---|
| Safaricom (Daraja) | Phone number, amount, account reference | Passenger chooses M-Pesa | Kenya |
| PayPal | Amount, currency, order description | Passenger chooses card (card details are entered on PayPal's page, never in KQ-Propel) | Outside Kenya |
| AviationStack | Flight number only | Passenger asks about a flight (backup once the Kenya Airways feed is connected) | Outside Kenya |
| Kenya Airways booking system | Booking reference and surname | Passenger gives both to look up a booking | Kenya Airways |
| Language model provider (OpenAI), if enabled | Question text and retrieved policy text | Every answered question | Outside Kenya |
| Kenya Airways contact-centre system, if a webhook is configured | Case reference, reason, transcript with phone numbers masked | Passenger asks for a person | **[Kenya Airways to decide]** |

Transfers outside Kenya must meet the conditions in Part VI of the Act (section 48 onwards). Enabling a cloud language model is the largest transfer, because every question leaves the country. A locally hosted model (Ollama) avoids it. **[Kenya Airways to decide]** which model may be used, and the transfer safeguards in place for each provider.

## 4. Lawful basis **[Kenya Airways to decide]**

The developer's suggestion, for review:

- Answering questions and handover: the passenger's request (contract or pre-contractual steps), or legitimate interest in providing customer service.
- Payments and the payment record: performance of a contract, and a legal obligation to keep financial records.
- Frustration estimate: legitimate interest in acknowledging distressed passengers. It affects only the tone of a reply and the offer of a person; it never changes the policy answer, fee or entitlement. That is tested automatically (`tools/test_sentiment_fairness.py`). On that basis it is not a decision producing legal or similarly significant effects under section 35, but this should be confirmed.
- Quality monitoring: legitimate interest. Scores are kept; question text follows the conversation retention period.

## 5. Risks and controls

| Risk | Likelihood and impact before controls | Controls in the system | Remaining risk |
|---|---|---|---|
| Another person reads a passenger's conversation or payment (shared device, guessed identifier) | Medium, high | Server-issued random session identifiers in HttpOnly cookies; every passenger endpoint is scoped to the cookie; "Delete my conversation" and session reset | Low |
| Staff see more personal data than they need | High, medium | Phone numbers are masked on every staff screen and in forwarded transcripts; every read of passenger data is audit-logged | Low |
| Unauthorised access to the dashboard | Medium, high | scrypt-hashed password, revocable server-side sessions, lockout after 5 failed attempts, audit log. Single shared staff account (see open actions) | Medium until individual staff accounts exist |
| Data kept longer than necessary | High, medium | Retention by age (conversations, IP addresses, handover cases, question text; phone numbers on payment records). **Switched off until the periods below are approved** | High until switched on |
| A passenger cannot exercise the right to erasure | Medium, medium | Self-service deletion in the chat; staff erasure by case reference or phone number, with a preview and an audit entry | Low |
| Frustration estimate treats passengers unfairly | Medium, high | Tone only; automated test that calm and frustrated passengers get identical policy answers (this test found and fixed such a defect before release) | Low |
| Classifier misreads Swahili, Sheng or certain groups | High, low | It only adds an apology; all passengers can ask for a person. Retraining on consented real messages is planned (`tools/import_real_messages.py`) | Medium |
| A booking is looked up by someone who is not the passenger | Medium, high | Reference and surname both required; identical reply whichever is wrong; lookups locked after 5 failures per hour; surname not copied into the staff-visible trace | Low to medium: anyone holding an e-ticket can look the trip up, as on Manage My Booking |
| Assistant states wrong policy | Medium, high | Answers only from cited policy text; grounding check declines off-topic questions; release quality gate | Low to medium |
| Cross-border transfer to a model provider | High if enabled | Off by default; local model option | **[Kenya Airways to decide]** |
| Sensitive data typed into chat (health, ID numbers) | Medium, medium | Not requested; erased with the conversation; masked if a phone number | Medium: a notice asking passengers not to share such data is recommended |

## 6. Retention **[Kenya Airways to decide the periods]**

| Data | Proposed period (placeholder) | Setting |
|---|---|---|
| Conversations, IP addresses, handover cases, conversation memory, question text in quality records | 30 days after the last message (handover cases kept while open) | `CONVERSATION_RETENTION_DAYS` |
| Phone number on a payment record | 90 days after the payment, once it has a final status | `PAYMENT_CONTACT_RETENTION_DAYS` |
| Payment record (amount, currency, reference, status) | As required for financial records | Not deleted by the system |
| Staff access log | 365 days | `AUDIT_RETENTION_DAYS` |

Retention is enforced by `app/privacy.py` at start-up and every hour once `PRIVACY_RETENTION_ENABLED=true`.

## 7. Data subject rights (section 26)

| Right | How it is met |
|---|---|
| To be informed | Privacy notice at `/privacy`, linked under the chat box (draft, needs approval) |
| Access | Staff can open a passenger's conversation by case reference. **[Kenya Airways to decide]** the process for providing a copy |
| Rectification | Through staff, by case reference |
| Erasure | "Delete my conversation" in the chat; staff erasure on request (dashboard, Data Protection panel) |
| Object | The passenger can stop using the assistant and ask for a person; they can also object to the frustration estimate through the contact address |

## 8. Open actions for Kenya Airways

1. Confirm registration with the Office of the Data Protection Commissioner covers this processing (section 18).
2. Name the data protection officer contact shown in the privacy notice (`PRIVACY_CONTACT`).
3. Decide the lawful bases (section 4) and the retention periods (section 6), then switch on `PRIVACY_RETENTION_ENABLED`.
4. Decide whether a cloud language model may be used, and record the transfer safeguards.
5. Replace the single staff login with individual staff accounts before go-live.
6. Approve the privacy notice and add a line asking passengers not to share sensitive personal data in the chat.
7. Approve consent wording before any real passenger messages are used to retrain the classifier.
8. Sign off this assessment and set a date to review it.
