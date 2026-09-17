# OpenAI Realtime voice assistant (direct SIP)

Supersedes `docs/OPENAI_VOICE.md`, which documented an earlier Media
Streams design (Twilio relays raw call audio through this backend to an
OpenAI Realtime WebSocket). That design was abandoned before any of it went
live and has been deleted from the codebase - see "Why direct SIP, not
Media Streams" below. This backend never touches call audio in the current
design.

An alternative to the existing ConversationRelay voice assistant
(`docs/CONVERSATION_ENGINE.md`, `app/communications/voice_relay.py`,
`app/ws/twilio_voice.py`) - not layered on top of it, and not used
together on the same call. Twilio still owns the phone number and PSTN
leg, but instead of bridging into this backend, Twilio's Elastic SIP
Trunk dials OpenAI's Realtime SIP endpoint **directly**. OpenAI does the
speech understanding, turn-taking, and voice synthesis; this backend's only
job is telling OpenAI *which business* is being called and what it's
allowed to say and do, via a webhook and a handful of tool calls.

## Architecture

```
Customer dials a business's CoMaz voice number
  -> Twilio PSTN carrier leg
  -> Twilio Elastic SIP Trunk, Origination URI:
       sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls
     (configured once in the Twilio Console - see "Twilio SIP trunk setup")
  -> OpenAI's SIP endpoint answers the leg itself (no CoMaz involvement yet)
     and sends CoMaz a webhook:
       POST {PUBLIC_API_BASE_URL}/api/webhooks/openai/realtime
       event type: realtime.call.incoming
     (app/ai_voice/routes.py)
       - verifies the webhook signature (openai SDK's client.webhooks.unwrap)
       - reads the SIP `To` header from event.data.sip_headers, resolves it
         to a CoMaz business (app/ai_voice/tenant.py) via the SAME
         voice_phone_number mapping every other voice path uses
         (app/communications/tenant_resolution.py::resolve_garage_by_voice_number)
       - if unresolved, or the business has communications disabled:
         rejects the call (client.realtime.calls.reject) and returns - no
         CommunicationLog row is created for a call CoMaz never accepted
       - otherwise accepts the call (client.realtime.calls.accept),
         configuring the whole Realtime session in that one call: model,
         voice, audio format, and this business's own dynamic system
         instructions + tool schemas (app/ai_voice/instructions.py,
         app/ai_voice/tools.py) - nothing further needs to be sent to
         configure the session
       - logs a CommunicationLog row (channel=VOICE, direction=INBOUND,
         external_provider="openai", external_id=<call_id>) - the same
         table every other communications channel uses, and the mechanism
         idempotency is built on (a redelivered webhook for a call_id
         already logged is a no-op)
       - spawns a background greenlet (app/ai_voice/call_controller.py) to
         handle the rest of the call, and returns 200 immediately (OpenAI
         retries a slow or non-2xx webhook response)
  -> OpenAI handles the live call's audio and conversation entirely itself.
     The only thing CoMaz's backend does for the rest of the call is the
     call-control WebSocket (client.realtime.connect(call_id=...)):
       - receives response.function_call_arguments.done events when the
         model wants to call a tool
       - dispatches to app/ai_voice/tools.py::dispatch_tool, tenant-scoped
         to the garage resolved above (never re-derived from anything the
         model says)
       - sends the tool's result back (conversation.item.create +
         response.create) and lets the model keep talking
       - on request_human_handoff: after the model's closing line
         finishes (response.done), either transfers the call
         (client.realtime.calls.refer, if a fallback number is configured)
         or hangs up (client.realtime.calls.hangup)
  -> A booking created by the create_booking tool goes through the exact
     same PENDING BookingRequest path the WhatsApp/voice ConversationRelay
     engine already uses (app/conversation/actions.py::create_booking_request)
  -> request_human_handoff creates a CallbackRequest (same table the
     existing engine uses)
```

No audio, and no full transcript, is ever stored by or passed through this
backend - see "What is and isn't stored" below.

## Why direct SIP, not Media Streams

The Media Streams design this replaces would have had Twilio open a raw
audio WebSocket to this backend, which would then relay every audio frame
to a second WebSocket to OpenAI's Realtime API, and relay OpenAI's spoken
replies back the same way. That works, but this backend would be sitting
in the middle of every call's live audio for no functional benefit -
OpenAI's Realtime API supports SIP directly, which lets Twilio's Elastic
SIP Trunk hand the call straight to OpenAI. CoMaz's backend then only
needs to: (1) tell OpenAI which business is being called and what it can
say/do (a webhook + one REST call), and (2) receive tool-call events over
a lightweight control WebSocket that never carries media. This is simpler,
has one fewer network hop of audio latency, and removes an entire class of
audio-transcoding/format bugs (the Media Streams design's `g711_ulaw`
audio-format value was never confirmed against current OpenAI docs before
being abandoned - direct SIP's `audio/pcmu` value, used here, is
confirmed current).

All Media Streams-specific code has been deleted, not kept around as an
alternative path: `app/ws/openai_voice.py` (the Twilio<->OpenAI audio
relay), `app/ai_voice/twiml.py` (`<Connect><Stream>` TwiML builder),
`app/ai_voice/realtime_bridge.py` (the raw-WebSocket OpenAI client this
design replaces with the SDK's own `client.realtime.connect()`), and their
tests. `app/communications/voice_webhooks.py` and `app/ws/__init__.py`
were reverted to their pre-OpenAI state, since a direct SIP call never
reaches Twilio's `/api/webhooks/twilio/voice/incoming` webhook at all - the
SIP trunk bypasses it entirely.

## Tenant safety

This is the security boundary the whole integration rests on - see
`app/ai_voice/tenant.py` and `app/ai_voice/routes.py`.

1. **The dialled number, not the caller's claim, decides the tenant.**
   `resolve_business_for_sip_call` reads the SIP `To` header from
   `event.data.sip_headers` (a list of `{name, value}` pairs per OpenAI's
   webhook payload, not a dict), extracts the phone number from the
   `sip:`/`tel:` URI, normalises it to E.164
   (`app.phone.normalize_uk_phone`), and looks it up via the same
   `resolve_garage_by_voice_number` every other voice path uses. Every SIP
   header is untrusted caller/network-supplied metadata per OpenAI's own
   guidance - it is only ever used to look a business up, never to
   authorize one directly.
2. **An ambiguous or unrecognised number fails closed.** A number that
   doesn't parse, doesn't match a business, or matches a business with
   `communications_enabled=False` results in `reject_call` and no
   `CommunicationLog` row - never a default/fallback business, and never
   partial acceptance. `GarageCommunicationSettings.voice_phone_number`
   carries its own unique database constraint, so two businesses can never
   share a mapping (see `tests/test_ai_voice_tenant.py::
   test_duplicate_voice_number_mapping_is_rejected_at_the_database_level`).
3. **Once resolved, the tenant is fixed for the call's whole lifetime.**
   The `garage` resolved in the webhook handler is passed by value into
   `run_call_controller` (re-fetched inside the background greenlet's own
   session via `db.session.get(Garage, garage_id)` - never the original
   request-scoped ORM object). Every tool call dispatched over the
   call-control WebSocket is bound to that same `garage`
   (`app/ai_voice/tools.py::dispatch_tool(garage, ...)`) - the model has no
   way to nominate a different business, because no tool schema accepts a
   business/garage/tenant identifier as an argument at all.
4. **Every tool argument is still validated server-side**, independent of
   the tenant binding above: `create_booking` checks the appointment type
   belongs to the bound garage, the slot is still available, and required
   customer details are present, before calling the existing
   `app.conversation.actions.create_booking_request` - the AI layer never
   manipulates booking/appointment models directly.

Test coverage for all of this: `tests/test_ai_voice_tenant.py` (known,
unknown, missing, malformed, E.164-normalised, and duplicate-mapping
numbers, plus cross-tenant resolution) and `tests/api/test_ai_voice_routes.py`
(webhook-level: known/unknown/disabled number, duplicate delivery,
accept-failure fallback).

## OpenAI webhook

- **URL**: `POST {PUBLIC_API_BASE_URL}/api/webhooks/openai/realtime`
  (`app/ai_voice/routes.py`) - configured once in the OpenAI dashboard
  (Settings -> Webhooks); there is no API call that registers it.
- **Verification**: `client.webhooks.unwrap(payload, headers, secret=...)`
  from the official `openai` SDK (Standard Webhooks spec -
  `webhook-id`/`webhook-timestamp`/`webhook-signature` headers) - no
  hand-rolled HMAC code. A bad/missing signature raises
  `openai.InvalidWebhookSignatureError`, which the route catches and
  returns `400` for, processing nothing.
- **Event types**: only `realtime.call.incoming` is handled. Every other
  event type is logged and acknowledged with `200` - never a `4xx`/`5xx`,
  since an unrecognised-but-valid event isn't an error.
- **Idempotency**: a redelivered `realtime.call.incoming` for a `call_id`
  already present in `CommunicationLog.external_id` is a no-op (`200`,
  nothing re-executed) - covers OpenAI's own retry-on-slow-response
  behavior without a separate dedup table.
- **Secrets**: `OPENAI_WEBHOOK_SECRET` is never logged, never included in
  a response body, and the route returns `503` (not `500`) if it isn't
  configured, mirroring `app/payments/webhooks.py`'s existing contract for
  "this deployment doesn't have this feature turned on."

## Call acceptance and session configuration

`accept_call` (`app/ai_voice/openai_sip.py`) configures the entire Realtime
session in one call - model, voice, audio format, and:

- **Instructions** (`app/ai_voice/instructions.py::build_instructions`):
  built per-call from the resolved business's own stored info (name,
  hours, address, enabled appointment types) via the existing
  `app.conversation.actions` domain functions - not by dumping the tenant
  database into the prompt. The model is told to use tools for anything
  live (availability, booking) rather than assume it knows current data.
- **Tools** (`app/ai_voice/tools.py::TOOL_SCHEMAS`, described below).

## Tools

All four reuse existing domain logic (`app/conversation/actions.py`) rather
than reimplementing booking/availability - the AI layer orchestrates, it
doesn't become a second booking system.

| Tool | Purpose | Notes |
|---|---|---|
| `get_business_info` | Name, hours, address, contact info | Only customer-facing fields |
| `get_appointment_types` | This business's enabled services | Name/duration/description; no internal ids leaked unnecessarily |
| `get_available_slots` | Real open slots for a date + service | Never fabricated; falls back to "next available days" if the requested date has nothing free |
| `create_booking` | Submits a `PENDING` `BookingRequest` | Validates the appointment type belongs to the bound garage and the slot is still free before calling `create_booking_request` - never an instant confirmation |
| `request_human_handoff` | Ends the call, optionally with a transfer | Logs a `CallbackRequest`; call-ending (see `CALL_ENDING_TOOLS`) |

Every tool handler validates its own arguments server-side
(`app/ai_voice/tools.py::dispatch_tool`) - a malformed date, an unknown
appointment-type id, or a slot that's no longer available returns a small
JSON error object to the model (e.g. `_TOOL_ERROR`), never an unhandled
exception, and never an ORM object serialized wholesale. See
`tests/test_ai_voice_tools.py` for malformed-input and cross-tenant-id
coverage.

## Call-control WebSocket

`app/ai_voice/call_controller.py::run_call_controller` is the **only**
WebSocket this integration opens, and it never carries audio - it exists
purely to receive tool-call events for an already-accepted call and send
results back, using the official SDK's synchronous
`client.realtime.connect(call_id=...)` (not a hand-rolled client; the
`websocket-client` dependency this project briefly added for the earlier
Media Streams design has been removed - the `openai` SDK's own sync
Realtime WebSocket client covers this).

Runs in its own `gevent.spawn()` greenlet (started from
`app/ai_voice/routes.py::_spawn_call_controller`, with its own Flask app
context) so the webhook handler itself returns immediately. Handles:
`response.function_call_arguments.done` (dispatch the tool, send the
result, re-prompt the model), `response.done` (after a
`request_human_handoff` call: transfer or hang up), `error` (logged, call
continues), and the connection closing (logged, loop exits cleanly - never
crashes the greenlet). A crash anywhere in the loop is caught and logged
(`AI_VOICE_CALL_CONTROL_CRASH`) rather than left to kill the greenlet
silently.

## What is and isn't stored

- **No raw audio** is ever received by or stored in this backend - OpenAI's
  SIP endpoint terminates the media itself.
- **No full transcript** is stored. This mirrors the existing
  ConversationRelay path, which also doesn't persist full transcripts -
  if that decision changes product-wide, it should be a deliberate,
  documented change to both paths, not a one-off addition here.
- **Call metadata** is recorded in the existing `CommunicationLog` table
  (garage, caller number, called number, provider call id, status,
  timestamps) - no new parallel call-history table.

## Fallback / human handoff

`request_human_handoff` always logs a `CallbackRequest` and ends the call.
If `GarageCommunicationSettings.voice_fallback_number` is set for the
business, the call is transferred there via SIP `REFER`
(`client.realtime.calls.refer`, blind transfer only - OpenAI's Realtime
Calls API does not document an attended/warm-transfer primitive); if not,
the call is simply hung up (`client.realtime.calls.hangup`) after the
model's closing line. `voice_escalation_number` exists on the same model
for a future, more deliberate escalation feature, but is not consulted by
this integration tonight - documented here rather than wired up
speculatively.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OPENAI_VOICE_ENABLED` | No | `false` | Master switch. Off = the webhook always returns 503 and no call is ever accepted. |
| `OPENAI_API_KEY` | Yes, to use this | `""` | Backend-only; never sent to a frontend or logged. |
| `OPENAI_WEBHOOK_SECRET` | Yes, to use this | `""` | From the OpenAI dashboard's webhook configuration. Verifies `realtime.call.incoming` deliveries. |
| `OPENAI_PROJECT_ID` | Yes, to use this | `""` | The OpenAI project the Twilio SIP trunk's Origination URI dials (`sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls`) - not read by this backend's own code, but recorded here since it's part of the same setup. |
| `OPENAI_REALTIME_MODEL` | No | `gpt-realtime-2.1` | Pin an exact model in production once chosen. |
| `OPENAI_REALTIME_VOICE` | No | `marin` | One of OpenAI's current Realtime voices. Per-business voice selection is not implemented. |
| `OPENAI_REALTIME_AUDIO_FORMAT` | No | `audio/pcmu` | Confirmed current for G.711 mu-law SIP calls - see OpenAI's voice-SIP guide. |

No new Twilio-side environment variables - the existing `TWILIO_*`
configuration is untouched; this integration only needs
`PUBLIC_API_BASE_URL` (already required) to give OpenAI a webhook URL.

## Twilio Elastic SIP Trunk setup (manual, one-time per project)

1. In the Twilio Console, create a new **Elastic SIP Trunk**
   (Super Network > Elastic SIP Trunking > Trunks).
2. Under **Origination**, add an Origination URI:
   `sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls` (replace
   `$OPENAI_PROJECT_ID` with the real OpenAI project id) - this is the
   direction that matters for inbound calls (Twilio -> OpenAI). Termination
   (OpenAI calling out through Twilio) is not needed for this feature.
2a. Set the Origination URI's priority/weight as the only entry (no
   fallback origination target is configured tonight).
3. Under **Numbers**, associate the business's CoMaz voice number with this
   trunk (a number can only belong to one trunk, so this is exclusive with
   that number's existing ConversationRelay/static Voice URL config -
   see "Backward compatibility" below).
4. TLS is required end-to-end for the trunk (`transport=tls` above) -
   confirm the trunk's **Secure Trunking** setting is enabled.
5. No credential/IP ACL is needed for *origination* to OpenAI (Twilio is
   the caller, not the callee, on this leg); OpenAI's own project-level
   auth is what secures the SIP session (the project id in the URI plus
   OpenAI's account-level access controls).
6. Twilio's own status callbacks (call status, recording, etc.) are not
   configured for this trunk tonight - the call's lifecycle from CoMaz's
   point of view is driven entirely by OpenAI's webhook and the
   call-control WebSocket, not by a Twilio callback.

## OpenAI dashboard setup (manual, one-time per project)

1. An OpenAI project with Realtime API access and billing enabled.
2. **Settings -> Webhooks**: add
   `{PUBLIC_API_BASE_URL}/api/webhooks/openai/realtime`, subscribed to at
   least `realtime.call.incoming`. Copy the generated signing secret into
   `OPENAI_WEBHOOK_SECRET`.
3. Confirm the project's SIP URI is
   `sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls` - this is the
   value the Twilio trunk's Origination URI (above) must match exactly.
4. Confirm the chosen `OPENAI_REALTIME_MODEL` is enabled for the project
   (Limits/Models pages).
5. Never put `OPENAI_API_KEY` or `OPENAI_WEBHOOK_SECRET` in git - set them
   as deployment secrets only (see `.env.example` for the placeholder
   names).

## Backward compatibility

A business's Voice number is either routed through Twilio's Elastic SIP
Trunk to OpenAI (this feature, once its trunk/number association above is
done) **or** through Twilio's own Programmable Voice webhook to
ConversationRelay/the static greeting (the existing path,
`app/communications/voice_webhooks.py`) - never both, since a Twilio
number belongs to exactly one trunk/Voice-URL configuration at a time.
Businesses not migrated to a SIP trunk keep working exactly as before;
`OPENAI_VOICE_ENABLED=false` (the default) additionally guarantees this
backend never accepts a `realtime.call.incoming` webhook even if one
somehow arrived. This is an intentional two-path state during migration,
not a permanent design - moving every business to direct SIP and retiring
the Twilio Voice webhook path is future work, not done tonight.

## Local test strategy

No live Twilio/OpenAI calls are made in tests. `tests/test_ai_voice_tenant.py`
and `tests/test_ai_voice_tools.py` exercise the pure business logic directly.
`tests/api/test_ai_voice_routes.py` drives the actual Flask route, but
monkeypatches `verify_webhook`, `accept_call`, `reject_call`, and
`run_call_controller` (and forces `gevent.spawn` to run synchronously) so
no real network call to OpenAI ever happens - a regression here (see the
worked example in the codebase's own commit history) is a `401` from a
real OpenAI request inside a test that forgot to mock `reject_call`; if a
new test in this file makes a real network call, it forgot to mock one of
these four. `tests/test_ai_voice_call_controller.py` drives
`run_call_controller` against a fake connection double
(`_FakeConnection`), not a real WebSocket.

## Production setup checklist

1. Set `OPENAI_API_KEY`, `OPENAI_WEBHOOK_SECRET`, `OPENAI_PROJECT_ID` as
   deployment secrets (never in git).
2. Set `OPENAI_VOICE_ENABLED=true` only once the Twilio trunk and OpenAI
   webhook are both configured (see the two sections above).
3. Configure the OpenAI webhook URL and copy its signing secret in.
4. Create the Twilio Elastic SIP Trunk and associate exactly the business
   number(s) being migrated to this feature.
5. Make a real test call to a **test** number before migrating any live
   business number - confirm: correct business greeting, accurate
   hours/services, a real available slot offered, a booking actually
   created as `PENDING`, and `request_human_handoff` ending the call
   gracefully (with or without a configured fallback number).
6. Check `CommunicationLog` for the test call's row and confirm no other
   tenant's data was ever reachable during the call.

## Limitations / remaining work

- Per-business voice selection is not implemented (`OPENAI_REALTIME_VOICE`
  is deployment-wide).
- Warm/attended transfer is not supported by OpenAI's Realtime Calls API as
  documented - only blind transfer (SIP `REFER`) is implemented.
- `voice_escalation_number` is not yet consulted by this integration (see
  "Fallback / human handoff" above).
- No production deploy, live Twilio routing change, or real phone call has
  been made as part of this work - see the manual test plan given
  separately for exactly how to validate this before turning it on for a
  real business.
