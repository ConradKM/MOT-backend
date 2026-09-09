# Conversation engine ("the brain")

A provider-independent conversational booking/automation engine that sits
*behind* Twilio Voice/WhatsApp - built so the whole domain is capable of
automated customer interactions **before** a live Twilio account exists. See
[`TWILIO_SETUP.md`](TWILIO_SETUP.md) for the transport layer this plugs into.

**Not built here, on purpose:** a real AI/LLM chatbot, speech-to-text or
text-to-speech, Twilio Media Streams. Everything below is deterministic,
rule-based, and fully testable with plain HTTP-free function calls.

---

## Architecture

```
Voice/WhatsApp provider (Twilio, once live)
        │
        ▼
Channel adapter                  (app/communications/whatsapp_webhooks.py -
  - resolves the garage             only when a garage's
  - normalises the phone            conversation_automation_enabled = true)
        │
        ▼
Conversation engine               app/conversation/engine.py::handle_message()
  - session load/expiry/idempotency
  - interrupt-intent detection ("speak to a human" always wins mid-flow)
  - turn logging (CommunicationLog, same timeline the Communications UI reads)
        │
        ▼
Intent resolver                   app/conversation/intents.py
  - RuleBasedIntentResolver today (regex/keyword)
  - IntentResolver Protocol - a future LLMIntentResolver drops in without
    changing engine.py or workflows.py, because both only ever see one of the
    plain intent strings, never raw model output (see "Future AI" below)
        │
        ▼
Workflow handlers                 app/conversation/workflows.py
  - all the actual "what do we say / what do we ask next" decisions
        │
        ▼
Domain action ("tool") layer      app/conversation/actions.py
  - the ONLY place conversation code touches booking/customer/vehicle/
    availability models - tenant-scoped, validated, never raises HTTP aborts
        │
        ▼
Existing domain services          app/public_booking/availability.py,
                                   app/booking_requests, app/appointments, ...
```

Nothing in this stack is Flask-request-shaped: `engine.handle_message()` is a
plain function callable from a webhook, the dev simulator, or a unit test
identically. Twilio's signature validation and form-field parsing stay in
`app/communications/whatsapp_webhooks.py` - the engine never sees a raw HTTP
request.

---

## Session model

One row per (garage, channel, phone number) *episode* -
`app/models/conversation/conversation_session.py`:

| Field | Meaning |
| --- | --- |
| `status` | `ACTIVE` / `EXPIRED` / `COMPLETED` / `HUMAN_HANDOFF` |
| `intent` | The current top-level goal (a constant from `intents.py`) |
| `workflow_step` | Which question we're waiting on a reply to (e.g. `AWAITING_DATE`), or `None` |
| `context` | Explicit JSON slot-filling state (appointment type id, chosen date, vehicle id, offered slots, ...) - plain inspectable values only, **never hidden AI reasoning** |
| `handoff_reason` | Set together with `HUMAN_HANDOFF`; cleared on resume |
| `last_external_message_id` | The provider message id already processed - the idempotency key |
| `last_activity_at` | Drives the per-channel staleness timeout (Voice 30 min, WhatsApp 12 h) |

**Session continuity rule:** a **WhatsApp** `HUMAN_HANDOFF` never expires by
timeout - only a human resuming automation (staff "Resume automation", or
the dev simulator/API doing the same) ends one, so the bot never starts a
second, contradictory reply while a human owns the async thread. A **Voice**
`HUMAN_HANDOFF` has no persistent human channel (it only ever meant "we'll
arrange a callback"), so it *does* idle out on the normal timeout - otherwise
one handoff would hang up every future call from that number. Every other
status is swept to `EXPIRED` after the idle timeout
(`session_service.expire_stale_sessions`) so a customer returning after a
long gap starts a clean flow. The timeout is per-channel: **30 minutes** for
a live phone call, **12 hours** for an asynchronous WhatsApp thread (safe
because every offered slot is re-validated against real availability at
confirmation).

---

## Intents

`app/conversation/intents.py` - `RuleBasedIntentResolver` is ordered
most-specific-first so, e.g., "when is my MOT due" hits `MOT_EXPIRY_QUERY`
before the generic "mot" keyword in `CREATE_BOOKING` ever gets a chance:

`CREATE_BOOKING`, `CHECK_AVAILABILITY`, `CHECK_APPOINTMENT`,
`RESCHEDULE_APPOINTMENT`, `CANCEL_APPOINTMENT`, `APPOINTMENT_PRICE_QUERY`,
`APPOINTMENT_TYPE_QUERY`, `BUSINESS_HOURS_QUERY`, `BUSINESS_LOCATION_QUERY`,
`MOT_EXPIRY_QUERY`, `CUSTOMER_DETAILS_QUERY`, `CALLBACK_REQUEST`,
`SPEAK_TO_HUMAN`, `GREETING`, `SMALL_TALK`, `GENERAL_QUERY`, `UNKNOWN`.

`resolve()` first normalises the message (lower-case, trim punctuation,
expand common WhatsApp shorthand - `u`/`ur`/`pls`/`appt`/`tmrw`/`resched`),
then runs the ordered rules. Only if nothing actionable matches does a bare
"hi" / "help" / "menu" become `GREETING` (a short capability intro) or a
"thanks" / "that's all" become `SMALL_TALK` - a greeting *with* a request
("hi, can I book an MOT") still routes to the request.

`INTERRUPT_INTENTS = (SPEAK_TO_HUMAN, CALLBACK_REQUEST, CANCEL_APPOINTMENT)` -
a customer can always ask for a human, a callback, or to cancel outright,
even mid-booking-flow; `engine._dispatch` checks this before trying to
interpret a reply as whatever slot is currently being asked for.

---

## Domain actions (the "tool" layer)

`app/conversation/actions.py` is the **only** place any of this touches a
booking/customer/vehicle/availability model. Every function takes an
already-resolved `garage` and only reads/writes that garage's own rows -
conversational input can pick *which* record to act on, never *whose*
garage. Nothing here raises a Flask abort; failures come back as `None` /
`(ok, reason)` tuples, so the same function works from a webhook, the
simulator, or a test.

Key actions: `get_appointment_types`, `get_availability_for_day` (thin
wrapper over `app/public_booking/availability.py` - **never re-derives slot
math**), `find_customer`, `find_vehicles_for_customer`,
`find_customer_vehicle_by_registration`, `get_upcoming_appointments`,
`create_booking_request`, `cancel_appointment`, `reschedule_appointment`,
`get_business_hours`, `get_mot_expiry`, `create_callback_request`.

This is also the seam for future AI (see below): a resolver is only ever
allowed to call one of these named, validated functions with typed
arguments - never write to the database directly, never run arbitrary SQL.

---

## Booking creation and availability

`start_booking` → fuzzy-matches the appointment type
(`appointment_matching.py`) → asks for name (if unrecognised) → asks for a
date → **always calls the real `app/public_booking/availability.py` for that
exact day** and shows only genuinely open slots, offering the next open days
if the requested one is full or closed → asks for a vehicle (existing
customer: single-vehicle confirm, multi-vehicle choice, or new-vehicle
registration) → shows a final confirmation summary → on "yes",
**re-validates the slot one more time** via `create_booking_request`
(handling the case where someone else took it in the meantime) before
writing anything. A booking is always created as a `PENDING` `BookingRequest`
- the same approval queue an owner already reviews for public web bookings -
never an auto-confirmed appointment.

### Fuzzy appointment-type matching (`appointment_matching.py`)

1. An exact phrase substring (the customer's text contains the type's full
   name) always auto-selects, unconditionally.
2. Otherwise, raw word-overlap **count** (not a ratio) is scored against
   every active type; auto-selects only if there is a single strict
   top-scorer.
3. A tie, or nothing scoring at all, returns candidates for the customer to
   disambiguate between - **it never guesses**.

This was validated by hand against the two motivating cases: "MOT" alone
confidently resolves to "MOT Test" even though it's a short, generic word,
while "service" correctly asks "Full Service or Interim Service?" when a
business offers both. It works identically for any business's own service
names (a hairdresser's "Haircut" vs "Beard Trim"), because it only ever
reads `actions.get_appointment_types(garage)` - never a hard-coded MOT/
Service list.

### Date/time phrase parsing (`datetime_parsing.py`)

Deterministic, no timezone library (matches
`app/public_booking/availability.py`'s existing "plain UK wall-clock, no
tz conversion" convention). Understands "tomorrow", a bare weekday name
(today's own weekday name, or an explicit "next X", both mean the *next*
occurrence - never "later today", since literal "today" is handled
separately), vague windows ("morning", "after lunch", "around 3"), and exact
times ("10:30", "half 10", "quarter past 2"). Every result is a
*preference* only - workflows.py always re-runs it through the real
availability service before offering or accepting a slot.

---

## Check / cancel / reschedule

All three reuse `actions.get_upcoming_appointments` and existing
services - no separate "conversational" copy of appointment logic exists.
Reschedule accepts the new day either inline in the message that triggered
it ("move my appointment to Friday") or as a follow-up turn, mirroring how
booking already accepts an inline appointment type; either way it
re-validates against real availability and the assigned employee's own
conflict-free-ness (`actions.reschedule_appointment`) before committing.
Cancelling sets `status="CANCELLED"` (never deletes), preserving history and
every existing audit hook.

---

## Human handoff

`SPEAK_TO_HUMAN`, an unresolvable message repeated twice in a row, a
complaint, or any workflow that hits a genuine dead end (e.g. no appointment
types configured) sets `status=HUMAN_HANDOFF` with a `handoff_reason`. From
that point:

- The engine's own `handle_message` short-circuits on the very next inbound
  message for that phone (`needs_human=True`, no automated reply) - the
  session-continuity rule above means this keeps working across any number
  of further messages, not just the next one.
- Staff see it in the WhatsApp inbox thread header ("Assistant is
  replying" / "Take over conversation") and in a dedicated **Needs
  Attention** queue (`GET /api/communications/attention-queue`).
- Staff can also *proactively* take over an in-progress automated
  conversation (`POST /api/communications/conversations/<phone>/takeover`),
  and hand it back (`.../resume-automation`, which also clears the session's
  workflow state so a stale in-progress flow never silently keeps driving
  the conversation).

The bot and a human are never both eligible to reply to the same
conversation at the same time - this is enforced by session status alone,
not by coordinating message timestamps.

---

## Callback requests

`CALLBACK_REQUEST` intent → asks for a reason → creates a
`CallbackRequest` row (`PENDING`/`COMPLETED`/`CANCELLED`,
`app/models/conversation/callback_request.py`) linked to the customer (if
known) and the originating session. Staff work these from
`GET/POST /api/communications/callback-requests[...]` - no customer-facing
message is sent on creation (the whole point is a human calls them back).

---

## Automation rules and events

`app/conversation/automation.py` registers real handlers on the **existing**
event bus (`app/communications/events.py`, previously a no-op foundation):
`BOOKING_REQUEST_CREATED/APPROVED/REJECTED`, `APPOINTMENT_CANCELLED`,
`APPOINTMENT_RESCHEDULED`, `MISSED_CALL`, `CALLBACK_REQUESTED`. Each handler
checks the garage's `GarageCommunicationAutomationSettings` (off by default;
`conversation_automation_enabled` is the master switch for the conversation
engine itself, separate from the individual outbound-message toggles),
renders the relevant template, and sends through the existing
`send_whatsapp_message` - identical code path whether or not Twilio is
actually configured (a `SKIPPED_NOT_CONFIGURED` log row either way).
`MISSED_CALL` is now genuinely emitted from
`voice_webhooks.py`'s status callback whenever an inbound call's final
status is one of `no-answer`/`busy`/`failed`/`canceled`.

---

## Message templates

`app/conversation/templates.py` - a fixed set of named templates
(`booking_acknowledgement`, `booking_confirmation`, `booking_rejected`,
`appointment_reminder`, `appointment_cancelled`, `appointment_rescheduled`,
`missed_call_ack`) with `{{variable}}` placeholders, substituted by a plain
regex allow-list - **never** `str.format`/an actual templating engine. An
owner's edit (`GarageMessageTemplate`) overrides the built-in default per
key; an unrecognised `{{...}}` is left as literal text rather than raising,
so a typo degrades to odd-looking wording instead of a crash. There is no
way to submit anything here that executes as code.

---

## Owner settings (frontend)

Settings → **Communications Automation**
(`ConradKM/MOT-frontend#27`): toggles for every automated message +
reminder timing + the master assistant switch, and an editor per template
with a live preview against sample data and a one-click revert to default.
**Never shows or accepts a Twilio credential** - those remain
platform/CLI-only exactly as before
(`app/garages/details.py`, `app/communications/cli.py`).

---

## Development Conversation Simulator

`POST /api/conversation/simulate` (`app/conversation/routes.py`) runs a
message through the **real** `engine.handle_message()` - no Twilio, no HTTP
signing, nothing simulated about the engine itself. Gated by
`CONVERSATION_SIMULATOR_ENABLED` (defaults on; **must** be set to `false` in
a production deployment) plus a normal employee JWT, and always scoped to
the caller's own garage - there is no "simulate as any business" mode.

The frontend's Simulator tab (`/communications/simulator`) is excluded from
production builds entirely via `import.meta.env.DEV` (confirmed absent from
the production bundle output, not just hidden), so this is defended in three
independent layers: not shipped to the browser, not routed to, and 404s on
the server even if reached.

### Voice transcript simulation

There is no speech-to-text/TTS/Twilio Media Streams work in this codebase.
"Voice" support means: pass `channel: "VOICE"` to the same simulator/engine
call with plain text standing in for a transcript. The engine's logic is
100% channel-agnostic (the only channel-specific behaviour is which
`CommunicationLog.channel` value gets written and the `whatsapp:` address
prefix), so this exercises the exact same booking/query/handoff logic a real
voice transcript pipeline would eventually feed into.

---

## Idempotency and concurrency

- **Duplicate webhook delivery:** `session.last_external_message_id` is
  checked before any processing; a repeat of the same provider message id
  returns `duplicate=True` with no reply, no second booking, no second
  callback.
- **Concurrent booking:** availability is re-checked immediately before
  `create_booking_request` writes anything, relying on the same atomic
  capacity validation the public booking API already uses - a race is
  reported back as "that slot's gone" rather than double-booked.
- **Reschedule/cancel conflicts:** re-validated the same way against real
  availability and the assigned employee's own schedule.

---

## Future AI / NLU (explicitly not built)

`IntentResolver` is a `Protocol`; `RuleBasedIntentResolver` is the only
implementation today. A future `LLMIntentResolver` would only ever need to
return one of the plain intent-string constants from `intents.py` - nothing
downstream (`engine.py`, `workflows.py`) changes. Critically, **any** future
AI output - intent, or eventually a suggested action - would still have to
go through the same validated `actions.py` functions with typed arguments.
An AI is never given database or SQL access, and never writes anything
outside what `actions.py` explicitly allows. This is a design boundary, not
a TODO.

---

## Migrations

`c544e7a7e0ed_add_conversation_engine_sessions_...py` -
`garage_communication_automation_settings`, `garage_message_templates`,
`conversation_sessions`, `callback_requests`; `booking_requests.customer_email`
made nullable (WhatsApp/voice bookings never collect email);
`communication_logs.intent` column added.

---

## Tests

- `tests/test_conversation_engine.py` (21 tests) - the engine directly:
  full booking flow, staff-visible logging, known-customer identification,
  multi-vehicle disambiguation, single-vehicle confirmation, full-day
  alternatives, price/service/hours/location/MOT-expiry queries, unknown
  service escalation, cancel flow, reschedule flow (inline and follow-up
  date), human handoff (immediate, persists across messages, complaint),
  callback creation, repeated-unresolved escalation, cross-tenant isolation,
  duplicate-webhook idempotency, stale-session non-resumption, the
  `expire_stale_sessions` sweep, and safe-by-default automation settings.
- `tests/api/test_twilio_webhooks.py` - the WhatsApp webhook actually
  routing through the engine once a garage opts in (and *not* routing when
  it hasn't), plus the `MISSED_CALL` event firing (and not firing for a
  normal completed call).
- `tests/api/test_communications_automation_api.py` (22 tests) - every new
  endpoint: automation settings (defaults, partial update, tenant scoping),
  templates (list/edit/reset/preview/unknown-key), callback requests
  (list/filter/complete/cancel/tenant scoping), and takeover/resume/
  attention-queue (including driving a real engine session and confirming
  the bot stops replying after takeover).
- Frontend: `communicationsAutomationSettings.test.tsx`,
  `WhatsAppInbox.automation.test.tsx`, `CommunicationsAttentionQueue.test.tsx`,
  `CallbackRequestsList.test.tsx`.
- Full existing suite (686 backend tests before this work) still passes
  unchanged - 709 total after.

---

## Manual demo script

Prerequisite: log in as a garage owner, open **Communications → Simulator**
(only visible in a local dev build). Each scenario uses a fresh phone number
so sessions don't interfere with each other.

### 1. A new customer books an MOT

1. Phone `07111 111111`, channel WhatsApp, send `I need an MOT`.
2. If the garage offers more than one MOT-ish service, answer the
   clarification (e.g. `MOT Test`).
3. Answer the name prompt: `Jane Doe`.
4. Answer the date prompt with a weekday name, e.g. `Friday`.
5. Pick one of the real offered times exactly as shown.
6. Give a vehicle registration, e.g. `AB12 CDE`.
7. Confirm with `yes`.
8. **Check:** a `PENDING` row appears in **Requests**, with the exact name/
   phone/vehicle/date typed above, and the customer's WhatsApp thread shows
   the whole exchange plus a `SYSTEM` line ("Booking request #... created").

### 2. An existing customer checks and reschedules

1. Use a phone number that already matches a seeded `Customer` with an
   upcoming appointment.
2. Send `when is my appointment` - **check** it reads back the real
   appointment, not an invented one.
3. Send `can I move my appointment to <a weekday a week or more out>` -
   **check** it goes straight to offering real times for that day (no
   redundant "what day?" question, since the day was already in the
   message).
4. Pick a time, confirm with `yes`.
5. **Check:** the appointment's row in **Appointments** now shows the new
   date/time, and its history/checklist are untouched.

### 3. Ambiguous service name → clarification → price query

1. Fresh phone number, send `how much is a service` (works if the garage
   has more than one service-ish appointment type).
2. **Check:** the assistant asks which one, rather than guessing.
3. Answer with one of the offered names.
4. **Check:** the reply states that type's real configured price (or, if
   no price is set, offers a human callback rather than showing "None").

### 4. A frustrated customer gets a human, then a callback

1. Fresh phone number, send `I want to complain about my last visit`.
2. **Check:** the reply says a team member will help, `needs_human` is
   true, and the conversation appears in **Needs Attention**.
3. From the WhatsApp inbox, click **Take over conversation**; send a real
   reply from the staff side.
4. Click **Resume automation**.
5. From the same phone, send `can someone call me back` → answer the
   reason prompt, e.g. `my brakes are squeaking`.
6. **Check:** a `PENDING` row appears in **Callbacks** with that reason;
   mark it **Done** and confirm it moves to the Completed tab.

### 5. Turn automation on/off and customise the wording

1. Settings → **Communications Automation**: confirm the master toggle
   (**Automated WhatsApp assistant**) is off by default.
2. Edit the **Booking request received** template to something custom,
   click **Preview** (check it substitutes sample values, not `{{...}}`
   literally), then **Save**.
3. Submit a real public booking request for this garage (or approve/reject
   one) and confirm the actual outbound message uses the new wording.
4. Click **Reset to default** and confirm it reverts.
5. Turn the master toggle on, then repeat Scenario 1 by messaging the
   garage's real WhatsApp sender (only possible once Twilio is configured -
   otherwise repeat it via the simulator) and confirm the same flow runs
   through the real webhook instead of the simulator.

---

## What still depends on a real Twilio account

Everything above works today with **zero** Twilio configuration - the
simulator, the automation rules (`SKIPPED_NOT_CONFIGURED` logging), and
every test. What's genuinely blocked until Twilio credentials + a public
HTTPS URL exist (see `TWILIO_SETUP.md`):

- Real inbound WhatsApp messages reaching `whatsapp_webhooks.py` at all.
- Real outbound sends actually reaching a customer's phone (today they log
  as `SKIPPED_NOT_CONFIGURED`).
- Real voice calls, and therefore any future actual speech-to-text/TTS
  voice pipeline (still not built, and out of scope for this work).
- Production WhatsApp sender approval (a separate Meta/Twilio ISV process,
  documented in `TWILIO_SETUP.md`).

None of that blocks anything in this document - the whole conversation
engine is fully exercised today via the simulator and the test suite, so
switching on real Twilio traffic is a configuration step, not a code
change.
