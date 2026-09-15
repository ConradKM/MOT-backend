# Communications providers

## Current deployment and boundary

Voice and WhatsApp still default independently to **Twilio**. No traffic, tenant
data, credentials, numbers, sender registrations, or database schema are migrated.
Existing Twilio environment variables and webhook URLs remain accepted unchanged.

`service.py` keeps number validation and the existing skip/failure/success
`CommunicationLog` contract. Separate `VoiceProvider` and `MessagingProvider`
protocols in `providers/base.py` return `SendResult`, never Twilio SDK objects.
`providers/twilio.py` wraps the existing client selection, REST arguments, error
translation, callback URL, and browser token generator. Provisioning remains
explicitly Twilio-specific platform administration, outside the runtime contracts.

The audit found these runtime paths:

* Inbound Voice: unchanged `/api/webhooks/twilio/voice/incoming` and signature
  validation → Twilio `InboundEvent` → exact existing `To`/voice-number tenant
  lookup → internal call log → existing ConversationRelay TwiML, escalation, or
  static greeting. The authenticated WebSocket bridge still translates relay
  frames into the existing provider-neutral conversation engine arguments.
* Voice status: unchanged `/api/webhooks/twilio/voice/status` and signature
  validation → `StatusEvent` → existing log status/duration/error update and
  missed-call automation dispatch.
* Inbound WhatsApp: unchanged `/api/webhooks/twilio/whatsapp/incoming` and signature
  validation → `InboundEvent` → exact existing sender lookup → conversation engine
  (which owns turn logging), or customer matching/inbound log/optional acknowledgement
  → existing MessagingResponse XML.
* WhatsApp status: unchanged `/api/webhooks/twilio/whatsapp/status` and signature
  validation → normalized status/error explanation → existing log update.
* Outbound Voice/WhatsApp: existing business/automation callers → service → selected
  channel adapter → existing garage-scoped Twilio client → internal result/log.
* Browser: authenticated `/api/communications/voice/token` → selected Voice
  capability/configuration checks → unchanged Twilio token/identity → existing
  browser SDK → signed `/api/communications/voice/outbound` TwiML and call log.
  Tokens add a `provider` field. The browser accepts legacy tokens without that
  field as Twilio and refuses another provider's token before SDK initialization.

`security.py`, phone/sender lookup semantics, TwiML/ConversationRelay rendering,
and engine workflows are deliberately retained. Core automation imports no Twilio
SDK types. Twilio parsing stays at the adapter/webhook edge; events contain only
the fields currently consumed, with no raw payload passed to services.

## Selection without a migration

Defaults are `VOICE_PROVIDER_DEFAULT=twilio` and
`WHATSAPP_PROVIDER_DEFAULT=twilio`. Optional deployment-controlled
`COMMUNICATIONS_PROVIDER_OVERRIDES` is a JSON object keyed by immutable garage UUID:

```json
{"<garage-uuid>": {"voice": "sip", "whatsapp": "twilio"}}
```

This is an example only: **do not enable SIP for live businesses**. It is a stub;
outbound sends are logged as skipped and browser calling is unavailable. Direct
stub calls raise `ProviderNotConfigured` or `UnsupportedCapability`; no fake call
is returned. Unknown provider names raise `ProviderNotConfigured`.

Absent overrides require no tenant intervention. The override only changes the
specified channel. There is no global communications provider and no owner-facing
provider editing. Number/sender fields in `GarageCommunicationSettings` remain the
single endpoint configuration; no duplicate endpoint tables are introduced.
Twilio webhook edges remain Twilio edges regardless of outbound selection, so
callbacks and in-flight interactions remain valid during a future staged migration.
Actual traffic cutover requires provider-specific endpoint configuration and testing.

## State and compatibility limits

`CommunicationLog.id` is the internal identifier; `external_provider` and
`external_id` are external references. Existing SIDs and the legacy `call_sid`
transcript correlation column are preserved. Engine-owned rows use
`comaz_conversation_engine` even when carrying Twilio references, so the existing
Twilio status lookup by globally unique external ID is retained. Other providers'
status updates are scoped to their provider tag.

The existing external-ID uniqueness and length limits remain (100 characters for
`external_id`, 40 for `call_sid`). A future adapter must provide unique, bounded
references and retain any native-ID mapping it needs; otherwise an additive schema
extension will be required then. E.164 voice numbers and stored WhatsApp prefixes
remain unchanged. SIP URI storage/routing is not implemented by this refactor.

## Migration phases and future SIP work

1. **Now:** Voice = Twilio; WhatsApp = Twilio.
2. Implement and validate a real Voice adapter and its infrastructure; migrate
   selected Voice endpoints; Voice = SIP provider, WhatsApp = Twilio.
3. Independently implement a Meta/direct WhatsApp or another BSP messaging adapter
   and its sender registration, authentication and event mapping when required.

A production SIP provider must supply carrier credentials and endpoint/DID
mapping; inbound/outbound call control and outgoing caller ID; authenticated event
ingress; status, duration, failure and correlation handling; transfer/escalation;
and an SBC/media gateway with RTP transport. Its instruction/control mechanism
must replace TwiML (`instructions_url`, with legacy `twiml_url` accepted by the
service). SIP alone supplies none of Twilio's application features automatically.

ConversationRelay currently supplies the speech/media interface to automation.
A replacement needs media streaming, speech recognition/transcription, speech
synthesis, interruptions, DTMF, teardown and fallback handling. Recording and
recording-event integration need an explicit implementation if enabled later;
the runtime capability contract does not claim recording support today.

Browser calling requires a WebRTC gateway, browser SDK/transport adapter, secure
short-lived credentials, microphone/media handling, call-state events, mute,
hang-up and DTMF. Until implemented, `browser_calling` stays false. Provisioning
and porting DIDs, webhook/event signing, recording/transcription, and carrier
configuration are separate work. This change buys/configures none of them and
adds no SIP secrets or media implementation.

## Regression coverage

Existing service, webhook, Voice token/outbound, ConversationRelay, tenant mapping,
communications API and database tests remain the behavioural baseline. Only the
SDK mock injection path moves from the service to its adapter; existing assertions
are unchanged. Added tests cover normalized events, fake-provider orchestration,
independent channel/tenant selection, explicit SIP/capability failures, all four
invalid-signature paths, engine-owned legacy references and browser SDK selection.
Full suites and CI are required before merge; mocked tests do not represent a live
carrier or microphone smoke test.
