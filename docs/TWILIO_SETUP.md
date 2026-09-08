# Twilio communications

The foundation for CoMaz OS's phone-call and WhatsApp features: multi-tenant
config, webhook endpoints, and a service layer. **No AI voice agent or IVR is
built** - see [What's not built yet](#whats-not-built-yet). A rule-based (not
AI) WhatsApp conversational booking engine *is* built and fully usable
without a live Twilio account - see
[`CONVERSATION_ENGINE.md`](CONVERSATION_ENGINE.md). This document explains
the transport-layer architecture that exists today and exactly what you (the
platform operator) still need to do outside this codebase.

---

## Architecture

```
CoMaz OS master Twilio account   (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)
        │
        ▼
  Garage / business            (GarageCommunicationSettings, one row per garage)
        │
        ├── twilio_subaccount_sid    - which subaccount this garage runs from (NULL today)
        ├── voice_phone_number       - the E.164 number Twilio routes calls to
        ├── whatsapp_sender          - "whatsapp:+1415…" this garage sends/receives from
        └── messaging_service_sid    - optional, if using a Messaging Service
```

Every garage runs from the **master account** until it has its own
`twilio_subaccount_sid` - subaccounts are a resolution target this codebase is
already shaped for (`app/communications/tenant_resolution.py`,
`app/communications/client.py::get_twilio_client_for_garage`), not something
it creates automatically. See [Subaccounts](#subaccounts-not-yet-provisioned).

Nothing here requires a Twilio account to exist. With
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` unset:

* the app starts normally,
* the booking flow is completely unaffected,
* any code that tries to send a WhatsApp message or place a call gets back a
  `CommunicationLog` row with `status="SKIPPED_NOT_CONFIGURED"` instead of an
  error or a silent no-op, and
* the four webhook endpoints below respond `503` instead of processing a
  request they can't cryptographically verify.

`app/communications/config.py::is_twilio_configured()` is the single gate
everything else checks.

---

## Environment variables

Set these in the deployment's environment (never commit real values -
`.env.example` documents them with safe placeholders/defaults):

| Variable | Required | Purpose |
| --- | --- | --- |
| `TWILIO_ACCOUNT_SID` | to enable communications | Platform master account SID |
| `TWILIO_AUTH_TOKEN` | to enable communications | Platform master account Auth Token. Verifies inbound webhook signatures (no API-key equivalent); also authenticates the REST client when no API key is set. |
| `TWILIO_API_KEY_SID` | no (recommended for production) | Standard API Key SID (`SK…`). When set with its secret, the **outbound REST client** authenticates with the key + Account SID instead of the Auth Token. |
| `TWILIO_API_KEY_SECRET` | no (with `TWILIO_API_KEY_SID`) | The API Key's secret. Both must be set for key auth to take effect; otherwise the client falls back to Account SID + Auth Token. |
| `TWILIO_WEBHOOK_VALIDATE` | no (default `true`) | Set `false` only for local/manual testing with a client that can't sign requests |
| `TWILIO_WHATSAPP_AUTO_ACK` | no (default `false`) | Send a generic acknowledgement reply to inbound WhatsApp messages |
| `PUBLIC_API_BASE_URL` | to receive webhooks | This deployment's public HTTPS origin, e.g. `https://api.comaz.example`. Also the origin of the ConversationRelay `wss://…/api/ws/twilio/voice` URL and the URL its handshake signature is checked against. |
| `TWILIO_CONVERSATIONRELAY_ENABLED` | no (default `false`) | `true` => a resolved inbound call returns `<Connect><ConversationRelay>` (the voice assistant) instead of the static `<Say>` |
| `CONVERSATIONRELAY_LANGUAGE` | no (default `en-GB`) | STT + TTS language for the assistant |
| `CONVERSATIONRELAY_TTS_PROVIDER` / `CONVERSATIONRELAY_VOICE` | no | Optional explicit TTS provider (`Google`/`Amazon`/`ElevenLabs`) + voice id; unset => the provider default for the language |

### Why there's no `TWILIO_AUTH_TOKEN` column in the database

`GarageCommunicationSettings` only stores **non-secret resource identifiers**
(SIDs, phone numbers, sender addresses). The platform's own Auth Token lives
only in `TWILIO_AUTH_TOKEN`. Once a garage has its own subaccount, that
subaccount has its **own** Auth Token - which must go into a secrets manager
or an encrypted store when that day comes, never a plain database column.
That's future work; today's schema simply has nowhere a plaintext token could
accidentally end up.

---

## Service layer

Everything Twilio-related is under `app/communications/` - nothing outside it
imports the `twilio` package directly:

| Module | Responsibility |
| --- | --- |
| `config.py` | `is_twilio_configured()`, `garage_communications_enabled()` |
| `client.py` | Lazy, cached Twilio REST client; `get_twilio_client_for_garage()` (the subaccount seam) |
| `tenant_resolution.py` | Garage lookup by voice number / WhatsApp sender |
| `security.py` | Twilio webhook signature verification |
| `service.py` | `send_whatsapp_message()`, `initiate_voice_call()`, inbound logging, status-callback handling - every `CommunicationLog` write goes through here |
| `events.py` | Booking-lifecycle event hooks (see below) |
| `voice_webhooks.py` / `whatsapp_webhooks.py` | The four webhook endpoints |
| `cli.py` | `flask configure-garage-communications`, `flask twilio-webhook-urls` |

### Booking-lifecycle event hooks

`app/communications/events.py` defines named events
(`BOOKING_REQUEST_CREATED/APPROVED/REJECTED`, `APPOINTMENT_CANCELLED`,
`APPOINTMENT_RESCHEDULED`, `MOT_REMINDER_DUE`, `APPOINTMENT_REMINDER_DUE`,
`MISSED_CALL`, `CALLBACK_REQUESTED`). The booking/appointment/reminder/voice
code calls `emit_event(...)` at the relevant point - it does not know or
care whether anything is listening. `app/conversation/automation.py` now
registers the real subscribers (see
[`CONVERSATION_ENGINE.md`](CONVERSATION_ENGINE.md#automation-rules-and-events)):
each one checks the garage's own automation settings (off by default) before
sending anything, through the same `send_whatsapp_message` path as
everything else. `APPOINTMENT_REMINDER_DUE` still has no producer - there's
no generic appointment-reminder feature in the codebase today (only MOT
reminders) - the constant exists for when there is one.

---

## Webhook endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /api/webhooks/twilio/voice/incoming` | Inbound call to a garage's number - resolves the tenant, logs the call, replies with TwiML |
| `POST /api/webhooks/twilio/voice/status` | Call progress/completion callback |
| `POST /api/webhooks/twilio/whatsapp/incoming` | Inbound WhatsApp message - resolves the tenant, matches a customer if possible, logs the message |
| `POST /api/webhooks/twilio/whatsapp/status` | Message delivery status callback (queued/sent/delivered/read/failed/undelivered) |

Every one of these:

1. Rejects the request with `503` if Twilio isn't configured for this
   deployment.
2. Verifies `X-Twilio-Signature` (see below) and rejects with `403` if it's
   missing or wrong.
3. Resolves the tenant from the Twilio-provided destination number/sender -
   never from anything the caller supplies directly - so a request for one
   garage's number can never be attributed to another garage.
4. Answers an *unrecognised* number/sender safely (valid empty TwiML, `200`)
   rather than erroring - Twilio always gets a clean response even for a
   stale or mistyped number.

### ConversationRelay voice assistant

When `TWILIO_CONVERSATIONRELAY_ENABLED=true`, a **resolved** inbound call to
`/api/webhooks/twilio/voice/incoming` returns
`<Connect><ConversationRelay url="wss://{PUBLIC_API_BASE_URL}/api/ws/twilio/voice" …/>`
instead of the static `<Say>`. **The number's webhook URL does not change** -
the change is purely in the TwiML that URL returns.

- **`GET /api/ws/twilio/voice`** (`app/ws/twilio_voice.py`) - the WebSocket
  ConversationRelay connects for the life of the call. It validates the
  `X-Twilio-Signature` on the handshake (over the `wss://` URL, no params -
  `TWILIO_WEBHOOK_VALIDATE=false` skips it), re-resolves the tenant from the
  dialled number in the `setup` message and cross-checks it against the
  `garage_id` `<Parameter>` baked into the TwiML, then feeds each completed
  `prompt` transcript into the **existing** `engine.handle_message(…, channel="VOICE", …)` -
  the same engine WhatsApp uses. Its `response_text` is sent back as a
  `{"type":"text","token":…}` frame for TTS. Requires the gunicorn **gevent**
  worker (the production Docker CMD); a plain sync WSGI server can't hold the
  connection.
- **Fallback is preserved:** if `TWILIO_CONVERSATIONRELAY_ENABLED` is off, or
  the TwiML build raises, the call gets the original static greeting. If the
  engine raises mid-call the caller hears an apology and a `CallbackRequest`
  is created.
- `CONVERSATIONRELAY_LANGUAGE` (default `en-GB`), optional
  `CONVERSATIONRELAY_TTS_PROVIDER` / `CONVERSATIONRELAY_VOICE`.

### Webhook signature verification

Twilio signs every webhook request (HMAC-SHA1 over the exact URL plus the
POSTed form fields, keyed by the Auth Token) and sends it as
`X-Twilio-Signature`. `app/communications/security.py::validate_twilio_request`
recomputes that signature with the official SDK's `RequestValidator` and
compares it before anything is trusted.

The URL used in that comparison is rebuilt from `PUBLIC_API_BASE_URL`, not
taken from Flask's own view of the request - behind a reverse proxy or tunnel,
Flask can report the wrong scheme/host unless proxy headers are configured,
which would make verification unreliable. `PUBLIC_API_BASE_URL` already has
to be correct for another reason (it's what you give Twilio when configuring
a number), so both agree by construction.

`TWILIO_WEBHOOK_VALIDATE=false` disables this check entirely - only ever use
it for local/manual testing with a tool that can't produce a real Twilio
signature (`TestConfig` sets this automatically so the test suite doesn't
need real signatures; the one test file that specifically exercises
verification turns it back on for that test).

---

## Subaccounts (not yet provisioned)

`GarageCommunicationSettings.twilio_subaccount_sid` exists so a garage's
number/sender ownership, usage, and logs can eventually be fully isolated at
the Twilio-account level, not just in CoMaz OS's own database. Creating a
subaccount and storing/using its own Auth Token is deliberately **not**
implemented - `get_twilio_client_for_garage()` always returns the master
client today, with a docstring pointing at this as the one place that
changes when subaccounts are real. Automating subaccount creation and number
purchasing is out of scope for this foundation (see below).

---

## What's not built yet

By design, so the foundation lands without also committing to unreviewed
product decisions:

* AI voice assistant / speech-to-text / LLM-driven booking conversations
* IVR menus beyond the single static greeting
* Automated Twilio subaccount provisioning or phone-number purchasing
* Production WhatsApp message templates
* Twilio usage/billing integration

---

## External setup you still need to do

Nothing above requires a Twilio account. Do this when you're ready to test
against real Twilio traffic:

### 1. Twilio account

- Create (or upgrade to a paid) Twilio account.
- Copy the **Account SID** and **Auth Token** from the Twilio Console.
- Put them in the deployment's environment as `TWILIO_ACCOUNT_SID` /
  `TWILIO_AUTH_TOKEN` (a secrets manager / platform env config - never a
  committed file).
- Set `PUBLIC_API_BASE_URL` to this deployment's real public HTTPS origin.

### 2. Voice

- Buy a Twilio phone number (Console → Phone Numbers).
- On that number's configuration page, set:
  - **A call comes in** → Webhook → `POST` →
    `{PUBLIC_API_BASE_URL}/api/webhooks/twilio/voice/incoming`
  - **Call status changes** → `POST` →
    `{PUBLIC_API_BASE_URL}/api/webhooks/twilio/voice/status`
- Run `flask twilio-webhook-urls` to print these with your actual
  `PUBLIC_API_BASE_URL` filled in.
- Assign the number to a garage:
  `flask configure-garage-communications --garage <slug> --enable --voice-number +44…`

### 3. WhatsApp - development (Sandbox)

- Activate the Twilio Sandbox for WhatsApp (Console → Messaging → Try it out).
- Set the Sandbox's **"When a message comes in"** webhook to
  `{PUBLIC_API_BASE_URL}/api/webhooks/twilio/whatsapp/incoming`, and its
  status callback to `.../whatsapp/status`.
- Assign the Sandbox number to a garage for testing:
  `flask configure-garage-communications --garage <slug> --enable --whatsapp-sender "whatsapp:+14155238886"`
- Local development: point `PUBLIC_API_BASE_URL` at a tunnel (e.g. `ngrok
  http 5001`) so Twilio's servers can reach your machine. Never give Twilio a
  `localhost` URL.

### 4. WhatsApp - production

- Production sender registration is a **separate process** from the Sandbox -
  it requires a Meta-approved WhatsApp Business sender, not just a Twilio
  number.
- Because CoMaz OS is a multi-business SaaS/ISV platform (one Twilio
  relationship serving many independent garages), production WhatsApp
  onboarding should go through Twilio's ISV/reseller path so each garage's
  sender is provisioned and approved under that model, rather than treating
  every garage as an ad-hoc individual WhatsApp Business account. Plan this
  with Twilio directly before onboarding a real garage onto production
  WhatsApp.

### 5. Hosting

- Every webhook above needs a public HTTPS URL - Twilio cannot call
  `localhost`.
- Production: your real deployment domain.
- Local development / staging without a public deployment yet: a secure
  tunnel (ngrok or equivalent), with `PUBLIC_API_BASE_URL` updated to match
  whenever the tunnel URL changes.

---

## Genuine blockers before live Twilio testing

- No Twilio account/credentials exist yet (expected - this task deliberately
  did not create one).
- No public HTTPS URL exists for this deployment yet, so Twilio has nothing
  to call until either a tunnel (local) or a real deployment domain
  (production) is in place.

Everything else - models, migrations, service layer, webhook routing,
signature verification, tenant isolation, and tests - is in place and does
not block on either of those.
