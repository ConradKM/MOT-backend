# Bringing an existing business number onto CoMaz Voice

How to connect a business's *existing* public phone number to CoMaz's IVR/AI
voice stack. This is the decision tree Platform Admin support should walk
through with an owner, and the runbook for actually doing it.

**CoMaz never ports numbers.** Number porting (moving carrier ownership of a
number to Twilio) is permanently out of scope - the operational risk (LOA
paperwork, carrier release windows, a number that can go dead mid-port) and
onboarding complexity are not a fit for a garage's live phone line. Nothing
in this codebase implements it, and nothing here should ever recommend it.

## The decision tree

```
Does the business's existing provider support inbound SIP trunking to a
carrier of the business's choice (BYOC/SIP trunk/PBX with SIP egress)?

  YES -> SIP_BYOC (recommended)
  NO  |
      v
Can the business's existing number forward calls (Call Forwarding Always,
or the provider's equivalent) reliably, indefinitely, for the life of the
integration?

  YES -> PSTN_FORWARD (fallback)
  NO  |
      v
Offer NEW_COMAZ_NUMBER - a dedicated CoMaz number instead.

STOP. There is no fourth option and no porting branch.
```

Ask the owner (or check their provider's own dashboard/docs) which of these
their existing setup actually supports - don't assume from the provider's
name alone. Many "cloud PBX" products support both SIP and forwarding; some
only forward.

## The three supported modes

### 1. `SIP_BYOC` - standard and recommended

The business keeps its number with its own carrier/PBX. The carrier sends
inbound calls for that number to Twilio over SIP (a **BYOC Trunk** - see
[Twilio's BYOC docs](https://www.twilio.com/docs/voice/bring-your-own-carrier-byoc)).
Twilio never "owns" the number in this mode.

```
caller
  -> business's existing public number
  -> business's existing carrier/PBX
  -> SIP (BYOC Trunk, carrier's own SourceIpMapping/SIP Domain)
  -> Twilio -> CoMaz's /api/webhooks/twilio/voice/incoming
  -> CoMaz IVR / AI
  -> human needed? SIP back out through the same BYOC Trunk to the
     business's own PBX/hunt group/extension (preferred), or a PSTN number
     the business gives CoMaz
```

What CoMaz needs recorded per business (Platform Admin > Communications >
Voice > telephony, `PUT .../voice/telephony`):

- `public_business_number` - the number customers know (E.164). This is
  also the tenant key for inbound SIP calls: CoMaz only accepts a SIP call
  for this business when the `To` matches this number **and** it arrived on
  this business's own `byoc_sip_domain_sid`.
- `byoc_sip_domain_sid` (`SD...`) - the Twilio SIP Domain the carrier
  delivers inbound calls to for this business.
- `byoc_trunk_sid` (`BY...`) - the BYOC Trunk used to send a PSTN human
  transfer back out through the business's own carrier (`<Number byoc="...">`),
  so the transfer looks like it came from the business, not from Twilio's
  own numbers.
- A primary (and optionally secondary) human destination - see below.

Twilio primitives used: a **BYOC Trunk** with its `voice_url` pointed at
CoMaz's `/incoming` webhook (same webhook as everything else - see
`app/communications/voice_webhooks.py`), TwiML `<Dial><Sip>` for the AI
handoff (unchanged from the direct-trunk path), and `<Dial><Number byoc="...">`
for a PSTN human transfer that should leave through the business's own
carrier. No SIP signalling is hand-rolled; everything is standard TwiML.

### 2. `PSTN_FORWARD` - the fallback

The business's existing number forwards (unconditionally - "Call Forwarding
Always") to a CoMaz-owned ingress number.

```
caller
  -> business's existing public number
  -> provider's Call Forwarding Always
  -> CoMaz ingress number (PSTN, Twilio-owned)
  -> CoMaz IVR / AI
  -> human needed? CoMaz dials the configured human destination directly
     (ordinary PSTN <Dial><Number>, no BYOC egress)
```

What CoMaz needs recorded: `public_business_number` (the number being
forwarded - for identification/records only, since Twilio can't route on it
directly), a CoMaz `voice_phone_number` (the ingress number the business's
provider forwards to - bought/assigned the normal way, see
`docs/TWILIO_SETUP.md`), and a primary human destination.

Twilio can't route inbound calls by the *original* number in this mode - by
the time it reaches CoMaz's number, standard PSTN forwarding carries no
reliable machine-readable trace of the original dialled number across
carriers (`ForwardedFrom` is reported only when the caller's own carrier
supports and forwards it, per Twilio's docs - it is logged for diagnostics,
never used to pick the tenant). Tenant resolution is by the CoMaz ingress
number, exactly like a `NEW_COMAZ_NUMBER` business - the difference is
purely what number the *customer* dials and what happens to their calls if
they don't need CoMaz features anymore (forwarding off, they're back to
their own phone; a new number would need customers to be told a new number).

### 3. `NEW_COMAZ_NUMBER`

No existing number involved. Customers call a CoMaz number directly. This
is what every business had before this feature existed, and remains the
default for a business set up before telephony modes existed
(`telephony_mode IS NULL` behaves exactly like this mode).

## Human handoff - mandatory, in every mode

The AI must be able to reach a real person. It asks for one
(`request_human_handoff`) whenever it can't safely continue - the caller
asks for a person, it can't understand them after a couple of tries, a
booking/lookup/payment step fails, the request is out of scope, or it's
just not sure of the right answer. It never guesses instead of asking.

**Destinations** (`app/communications/telephony.py::human_destinations`):
ordered `primary`, then optional `secondary`, then a menu option's own
number, the phone menu's own fallback number, and finally the legacy
`voice_escalation_number`/`voice_fallback_number` for a business set up
before this existed. Each is `SIP_URI` (a PBX extension/hunt group -
preferred for `SIP_BYOC`) or `PSTN_NUMBER`. At most 3 are ever tried; the
chain always ends at a logged callback request, never a dropped call.

**Loop prevention**: a destination is refused - both when it's saved
(Platform Admin validation) and again every time before it's dialled - if
it is any business's CoMaz ingress number or public business number. A
transfer to such a number would ring CoMaz again instead of a person. A
second, independent guard watches for the call itself looping: if an
inbound call arrives while one of this business's own transfers is still
ringing, and the caller matches that transfer's caller or destination,
CoMaz rejects it (`486 Busy`) instead of starting the loop over - the
transfer then sees "busy" and moves on to its own next destination.

**Failure handling**: each transfer reports back to CoMaz (never fire-and-
forget) - busy, no-answer, timeout, SIP rejection and provider errors all
move to the next destination in the chain. The AI is never told a
destination succeeded until it actually did, and it never says a caller
"is connected" - only that it's *trying* to put them through.

## Migration/rollback safety

Every step here is either (a) a config change on `GarageCommunicationSettings`
via Platform Admin - reversible by setting the fields back - or (b) a
provider-side routing change (moving the number's SIP/forwarding
destination) that the business's own provider console reverts just as
easily. Nothing is destructive:

- **Rolling back `SIP_BYOC`**: point the carrier's SIP trunk back at
  whatever it dialled before (or leave the BYOC Trunk in place and just stop
  routing to it) - CoMaz's own config can be cleared independently and
  costs nothing to redo.
- **Rolling back `PSTN_FORWARD`**: turn off Call Forwarding Always on the
  business's existing number. Calls go back to ringing wherever they did
  before CoMaz.
- **Rolling back `NEW_COMAZ_NUMBER`**: nothing to roll back - it's the
  default state.

## Saturday: Tints on Demand (TOD)

TOD's owner can manage call forwarding from a BT app - the exact BT voice
product isn't confirmed yet (BT Cloud Voice, Cloud Voice Express, Cloud
Voice SIP/SIP-T, or another product all behave differently for this
purpose). **First objective: identify the exact product and whether it can
do SIP/BYOC**, before changing anything.

### Discovery checklist (do this before touching any configuration)

- [ ] Exact BT product name (ask the owner to check their BT portal/app, or
      their last BT invoice/contract)
- [ ] TOD's existing public number
- [ ] Current inbound routing (where calls go today - a person, a hunt
      group, voicemail, another forwarding target)
- [ ] Any existing users/extensions or hunt groups on the BT service
- [ ] Whether an auto-attendant is already in front of it
- [ ] Whether the product supports SIP trunking to a third-party SIP
      destination (Twilio's BYOC Trunk) - and if so, its SIP
      authentication requirements (IP ACL vs. credentials - Twilio BYOC
      needs at least one)
- [ ] A real human destination to hand off to - a phone or hunt group TOD's
      team will actually answer, never their own public number
- [ ] Exactly what would need to be reverted to restore today's behaviour
      (the current forwarding target/routing configuration, written down
      before any change)

### If SIP/BYOC is available

```
TOD's existing number -> BT -> SIP/BYOC -> CoMaz IVR/AI
                                  -> SIP back to TOD's human destination
```

1. In Twilio: create a BYOC Trunk (if one doesn't already exist for TOD),
   set its `voice_url` to `https://mot-backend.onrender.com/api/webhooks/twilio/voice/incoming`,
   set up the SIP Domain/SourceIpMapping BT's SIP traffic will arrive on,
   and configure authentication (credential list and/or IP ACL for BT's
   SIP endpoints - never leave a BYOC trunk unauthenticated).
2. In BT's own console: point TOD's number's SIP trunk/egress at the BYOC
   Trunk's SIP Domain.
3. In CoMaz Platform Admin (Communications > Voice > telephony):
   `telephony_mode=SIP_BYOC`, `public_business_number=<TOD's number>`,
   `byoc_sip_domain_sid=<the SIP Domain SID>`,
   `byoc_trunk_sid=<the BYOC Trunk SID>`, and a real `human_primary_*`
   destination (TOD's own PBX/hunt group over SIP if BT exposes one,
   otherwise a PSTN number TOD's team answers).
4. **Do not** touch TOD's number's *existing* configuration in BT until
   this is verified - test first (below), cut over only once it works.

### If SIP/BYOC is not available - use Call Forwarding Always

```
TOD's existing number -> BT forwarding -> CoMaz ingress number -> IVR/AI
                                             -> PSTN human destination
```

1. Buy/assign a CoMaz ingress number for TOD the normal way
   (`docs/TWILIO_SETUP.md`), with its webhooks pointed at CoMaz.
2. In CoMaz Platform Admin: `telephony_mode=PSTN_FORWARD`,
   `public_business_number=<TOD's existing number>`, and a real
   `human_primary_*` PSTN destination.
3. In BT's app: set Call Forwarding Always on TOD's number to the new CoMaz
   ingress number. **This is the one live cutover step** - do it last, and
   only once steps 1-2 are verified by calling the CoMaz ingress number
   directly first.

### Testing before calling it done

1. Call the CoMaz ingress number directly (bypassing BT) first, to confirm
   the IVR/AI side works in isolation.
2. Once cutover (SIP routing or forwarding) is live, call TOD's real public
   number and confirm: the menu/greeting plays, the AI option works, and a
   human-transfer test actually rings the configured destination and reports
   back correctly (answer it, then separately let it go unanswered and
   confirm the next destination/callback behaviour).
3. Confirm rollback: revert the one live change from the discovery
   checklist and confirm the number behaves as it did before.

### Do not

- Port TOD's number, under any circumstance.
- Touch +447402220792 (a separate, already-configured CoMaz test number) or
  its WhatsApp configuration.
- Change +443330382135's live routing as part of this work - it is a
  separate dedicated test number with its own already-verified cutover.
- Buy or provision anything without confirming it's actually needed first.
