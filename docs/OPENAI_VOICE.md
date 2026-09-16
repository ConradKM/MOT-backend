# OpenAI Realtime voice assistant

An alternative to the existing ConversationRelay voice assistant
(`docs/CONVERSATION_ENGINE.md`, `app/communications/voice_relay.py`,
`app/ws/twilio_voice.py`) - not layered on top of it. Twilio still owns the
phone number and PSTN connection; OpenAI's Realtime API does the actual
speech understanding, conversation, and speech synthesis, using CoMaz's own
tenant-scoped data via tool calls.

## Architecture

```
Customer dials a business's number
  -> Twilio Voice webhook: POST /api/webhooks/twilio/voice/incoming
     (app/communications/voice_webhooks.py)
     - resolves the garage from the dialled number (unchanged)
     - logs the inbound CommunicationLog row (unchanged)
     - if OPENAI_VOICE_ENABLED + OPENAI_API_KEY: returns
       <Connect><Stream> TwiML (app/ai_voice/twiml.py) instead of
       ConversationRelay/static
  -> Twilio opens a Media Streams WebSocket to
     /api/ws/twilio/openai-voice (app/ws/openai_voice.py)
     - re-resolves + verifies the garage (see "Tenant safety" below)
     - opens a second WebSocket to OpenAI's Realtime API
       (app/ai_voice/realtime_bridge.py), configured with this
       business's own instructions + tools (app/ai_voice/instructions.py,
       app/ai_voice/tools.py)
     - relays caller audio to OpenAI, and OpenAI's spoken replies back to
       Twilio, essentially byte-for-byte (no transcoding - see "Audio
       format" below)
     - executes any tool call OpenAI makes (app/ai_voice/tools.py),
       tenant-scoped to the resolved garage, and sends the result back
  -> A booking created by the create_booking tool goes through the exact
     same PENDING BookingRequest path the WhatsApp/voice ConversationRelay
     engine already uses (app/conversation/actions.py::create_booking_request)
  -> request_human_handoff creates a CallbackRequest (same table the
     existing engine uses) and ends the call gracefully
```

## Why a separate path from ConversationRelay

ConversationRelay does its own STT/TTS and only ever exchanges *text* with
our backend (see `app/ws/twilio_voice.py`'s module docstring) - it was built
to plug a rule-based intent resolver into a real phone call cheaply. OpenAI's
Realtime API is a genuinely different kind of integration: it wants raw
audio and does its own speech understanding, turn-taking, and voice
synthesis. Building it as a second, independently-gated inbound-call path
(rather than trying to feed ConversationRelay's transcript text into an LLM)
keeps this feature genuinely optional and inert until explicitly turned on,
and keeps `app/conversation/`'s existing rule-based engine completely
untouched.

## Tenant safety

Twilio Media Streams' `start` message (unlike ConversationRelay's `setup`
message) carries no `to`/`from` fields, so the WebSocket bridge can't
cross-check a client-supplied `garage_id` against the dialled number the way
`app/ws/twilio_voice.py` does. Tenant safety here instead rests on two
things:

1. The WebSocket handshake's own Twilio signature
   (`app/communications/security.py::validate_twilio_websocket`) - the exact
   same check `twilio_voice.py` relies on as its first line of defence.
2. The claimed `garage_id` must match a `CommunicationLog` row **already
   created, for that exact garage, by the `/incoming` webhook** that built
   this call's TwiML in the first place. A bare WebSocket connection can
   never manufacture that row itself - see
   `app/ws/openai_voice.py::_resolve_call_garage`.

## Audio format - **must verify before any real call**

Twilio Media Streams' native codec is 8kHz mu-law (`audio/x-mulaw`). To avoid
any transcoding in this codebase, the bridge configures OpenAI's Realtime
session to use the same codec on both ends (`OPENAI_REALTIME_AUDIO_FORMAT`,
default `"g711_ulaw"`) and passes Twilio's base64 audio straight through
unchanged.

**As of writing, the exact accepted value for this field on OpenAI's current
GA session configuration is not clearly documented**, and existing samples
disagree slightly on shape (see OpenAI's `openai-realtime-twilio-demo` and
Twilio's own sample repos, which still work as of writing but may be using
an older/compatibility session shape). `OPENAI_REALTIME_AUDIO_FORMAT` is an
environment variable specifically so this can be corrected without a code
change if `"g711_ulaw"` turns out to be wrong. **Confirm this against a real
test call (see "Manual test call" below) before relying on it.**

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OPENAI_VOICE_ENABLED` | No | `false` | Master switch. Off means every call behaves exactly as before this feature existed. |
| `OPENAI_API_KEY` | Yes, to actually use this | `""` | Backend-only. Never sent to any frontend, never logged. |
| `OPENAI_REALTIME_MODEL` | No | `gpt-realtime` | Pin an exact dated snapshot in production once one is chosen. |
| `OPENAI_REALTIME_VOICE` | No | `marin` | One of OpenAI's current Realtime voices. |
| `OPENAI_REALTIME_AUDIO_FORMAT` | No | `g711_ulaw` | See "Audio format" above - verify before go-live. |
| `OPENAI_REALTIME_WS_URL` | No | `wss://api.openai.com/v1/realtime` | Only needed to override for testing against a proxy/mock. |

No new Twilio-side environment variables - this reuses the existing
`TWILIO_*` configuration and the existing Voice number setup entirely.
`PUBLIC_API_BASE_URL` (already required for ConversationRelay) is what
derives the `wss://.../api/ws/twilio/openai-voice` URL Twilio is told to
connect to.

## Tools implemented

- `get_business_info` - name, phone, email, address, opening hours.
- `get_appointment_types` - this business's active services.
- `get_available_slots` - real slots for one date + service, plus the next
  available dates if that date has nothing free.
- `create_booking` - submits a `PENDING` `BookingRequest` exactly like the
  existing conversation engine does; never an instant confirmation.
- `request_human_handoff` - logs a `CallbackRequest` and ends the call. This
  is the only fallback implemented in this pass.

## Investigated but not implemented: warm transfer to a human

Twilio does support redirecting a *live* call mid-conversation (e.g. via the
REST API's `Call.update(twiml=...)` to `<Dial>` a human agent, or Twilio
Programmable SIP for a true warm transfer with context). This was
deliberately **not implemented** here - it needs its own tested TwiML/REST
control path, a decision about which number/queue to transfer to per
business, and real-call validation, none of which fits "no live calls yet."
`request_human_handoff` (end the call + log a callback) is the safe fallback
for this pass; a future `transfer_to_human` tool could build on Twilio's
`Call.update()` once that's been tried against a real test call.

## Manual OpenAI/Twilio dashboard setup required

1. Set `OPENAI_API_KEY` on the backend deployment (from an OpenAI project
   with Realtime API access).
2. Confirm the OpenAI account/project has Realtime API + the chosen model
   enabled (check the OpenAI dashboard's Limits/Models pages).
3. No Twilio Console changes are needed beyond what ConversationRelay
   already required for a business's Voice number - the same number/Voice
   URL setup is reused; only the TwiML this backend returns changes.
4. Set `OPENAI_VOICE_ENABLED=true` only once a manual test call (below) has
   been run successfully.

## Manual test-call walkthrough (do this before any real customer call)

1. Deploy with `OPENAI_VOICE_ENABLED=true` and a real `OPENAI_API_KEY` to a
   **staging** environment with its own Twilio test number (never a live
   business's number).
2. Call that number from a real phone.
3. Confirm: the AI greets you as the correct (test) business, can answer
   "what are your opening hours" / "what services do you offer" correctly,
   can offer a real available slot, and can complete a test booking that
   shows up as a `PENDING` `BookingRequest`.
4. If the call is silent, garbled, or immediately drops, the most likely
   cause is `OPENAI_REALTIME_AUDIO_FORMAT` - check the platform logs for
   `AI_VOICE_OPENAI_ERROR` lines and consult OpenAI's current Realtime docs
   for the exact accepted value.
5. Say something ambiguous/out of scope and confirm `request_human_handoff`
   ends the call gracefully and creates a `CallbackRequest`.

Only after this succeeds on a test number should `OPENAI_VOICE_ENABLED` be
turned on for any real business's number.
