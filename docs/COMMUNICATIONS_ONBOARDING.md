# Communications onboarding

How a CoMaz business gets a working phone number and WhatsApp line, and how
Platform Admin shows you where every business has got to.

Everything here is driven from **Platform Admin > Operations > Communications
Setup** (the cross-business worklist) and **Platform Admin > a business >
Communications** (that business's workflow). The CLI
(`flask configure-garage-communications`) still exists and still works; it now
writes the same rows the console does.

---

## The shape of it

```
CoMaz business
   └── Twilio subaccount            (one per business, never shared)
         ├── Voice number(s)        (bought into the subaccount)
         └── WhatsApp WABA/sender   (Meta WABA associated to this subaccount)
```

Two rows per business hold the state:

| Table | What it is | Read by |
| --- | --- | --- |
| `garage_communication_settings` | live resource identifiers - subaccount SID, voice number, WhatsApp sender, escalation/fallback numbers | every send and every inbound webhook (`tenant_resolution.py`) |
| `garage_communications_onboarding` | the workflow - per-channel state, Meta/Twilio ids, test results, last provider errors | Platform Admin only |

They are kept in step in **one direction only**: when a channel goes live, its
address is written across into `garage_communication_settings`, because that is
what an inbound webhook resolves a tenant against. Nothing reads back.

A third table, `twilio_subaccount_credentials`, holds one Fernet-encrypted
subaccount Auth Token per subaccount. It has no serialiser anywhere in the
codebase.

---

## The two state machines

Not booleans, on purpose: "we asked Twilio and are waiting" and "Twilio said
no" need different actions from an operator, and a boolean cannot tell them
apart. Both are defined in `app/communications/provisioning/states.py`, along
with what each state *means* - its blocker, the next CoMaz action and the next
customer action.

**Voice:** `NOT_STARTED` → `SUBACCOUNT_PENDING` → `SUBACCOUNT_READY` →
`NUMBER_PURCHASING` → `NUMBER_ASSIGNED` → `WEBHOOKS_CONFIGURED` → `TESTING` →
`ONLINE`, plus `ACTION_REQUIRED`, `FAILED`, `DISABLED`.

**WhatsApp:** `NOT_STARTED` → `NUMBER_ENTERED` → `META_SIGNUP_REQUIRED` →
`META_SIGNUP_IN_PROGRESS` → `META_SIGNUP_COMPLETED` → `WABA_RECEIVED` →
`TWILIO_SUBACCOUNT_READY` → `SENDER_REGISTRATION_PENDING` → `OTP_REQUIRED` →
`SENDER_REGISTERING` → `TESTING` → `ONLINE`, plus
`EXISTING_WHATSAPP_MIGRATION_REQUIRED`, `ACTION_REQUIRED`, `FAILED`,
`DISABLED`.

Each detailed state maps onto one of eight coarse statuses the console renders:
`ONLINE`, `SETUP_REQUIRED`, `WAITING_FOR_CUSTOMER`, `WAITING_FOR_META`,
`WAITING_FOR_TWILIO`, `ACTION_REQUIRED`, `FAILED`, `DISABLED`. The mapping is
server-side, so the list view and the detail view can never disagree about
whether a business is live.

---

## External prerequisites

### Twilio (needed for Voice, and for WhatsApp)

1. A Twilio account with `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` set.
2. Funds on the account - buying a number spends money.
3. `PUBLIC_API_BASE_URL` set to a **public HTTPS origin**. Twilio cannot reach
   localhost; use a tunnel for local testing.
4. `COMMS_SECRET_KEY` set (a Fernet key). Without it CoMaz refuses to create a
   subaccount rather than store a live Twilio credential in plaintext.

### Meta + Twilio Tech Provider (needed for WhatsApp only)

These take weeks, not minutes, and **cannot be done from this codebase**.
Platform Admin reports which of them are outstanding rather than pretending the
flow is ready.

1. **A Meta Developer account and a Meta app**, switched to **Live** mode.
2. **App Review approval** for `whatsapp_business_messaging` and
   `whatsapp_business_management`.
3. **Access Verification** completed for the app (Meta quotes ~5 business days).
4. **Business verification** of the CoMaz/MazTrad business portfolio with Meta.
5. **An Embedded Signup configuration** created in the Meta app dashboard →
   `META_EMBEDDED_SIGNUP_CONFIG_ID`.
6. **A Twilio Partner Solution**: raise a Twilio support ticket giving them your
   Meta app ID, then **accept Twilio's request in your Meta app dashboard**.
   Twilio returns a Solution ID → `TWILIO_PARTNER_SOLUTION_ID`.
7. If you intend to register **Twilio SMS-capable numbers** as WhatsApp senders,
   Twilio also requires their "SMS-Capable Number OTP Request for Tech Provider
   Program" ticket to be approved.

Twilio's own guidance puts the whole approval chain at roughly 3-4 weeks.

Until 1-6 are done, every WhatsApp step in the console short of Embedded Signup
still works (recording the number, migration handling, subaccount creation);
the "Continue Meta setup" button is disabled with a note pointing at the
outstanding prerequisite.

---

## Onboarding your first real business

Assumes the business already exists in Platform Admin (Businesses > Add
business). All of this is Superadmin-only and every step is audited.

### Voice

1. **Platform Admin > Businesses > [the business] > Communications.**
2. **Create Twilio subaccount.** Creates it under your master account, named
   `CoMaz — <name> (<slug>)`, and stores its Auth Token encrypted. Idempotent -
   clicking twice does not create two subaccounts.
3. **Buy a voice number.** Optionally give an area code, press *Search
   numbers*, then *Buy* on the one you want. The number is bought **into the
   business's subaccount** with CoMaz's webhooks already set, so it is never
   live-but-unconfigured. This spends money.
   - Already own a number in that subaccount? Use the API's `already_owned`
     flag to adopt it instead of buying.
4. **Set escalation and fallback.** Where a caller goes when automation cannot
   help, and where they go if CoMaz itself is unreachable. Both E.164. With an
   escalation number set, an inbound call that reaches the static greeting is
   forwarded to a human instead.
5. **Test voice.** Rings *your own* mobile from the business's number and
   records the result. Do not test against the customer's phone.
6. **Mark voice online.** A deliberate operator decision - "the test call
   connected" and "this business is ready for its customers" are different
   claims. This also enables communications for the business.

Verify in the Twilio console that the number's Voice URL is
`{PUBLIC_API_BASE_URL}/api/webhooks/twilio/voice/incoming` and its status
callback is `.../voice/status`. Both are shown on the Communications Setup tab.

### WhatsApp

1. **Record the business's WhatsApp number** (E.164). Meta needs the full
   number before signup starts.
   - **If that number is already on WhatsApp or the WhatsApp Business App**,
     tick the box. The state becomes
     `EXISTING_WHATSAPP_MIGRATION_REQUIRED` and the page shows the business
     what *they* must do. **CoMaz never deletes, migrates or alters an existing
     WhatsApp registration.** Once they confirm it is cleared, press *Mark
     migration complete*.
2. **Create the Twilio subaccount** if the business does not have one yet
   (shared with Voice - a business has one subaccount, not one per channel).
3. **Continue Meta setup.** Opens Meta's Embedded Signup window. The business
   owner - not you - signs in with their own Facebook account, selects or
   creates a Meta Business Portfolio, creates a WhatsApp Business Account, and
   verifies the number where Meta asks. Do this with them on a call or screen
   share; there is no API that replaces this step and none should be invented.
4. CoMaz records the **WABA ID** Meta hands back. A one-time nonce ties that
   result to this business, and a WABA already connected to another CoMaz
   business is refused.
5. **Register sender.** Set the display name (it must satisfy Meta's
   display-name guidelines or Meta will reject it) and how Meta should deliver
   the one-time code. This calls Twilio Senders v2 **with the business's own
   subaccount credentials**, passing `configuration.waba_id` - which is what
   associates that WABA with that subaccount - and CoMaz's existing WhatsApp
   webhooks.
6. **Enter verification code** if the state becomes `OTP_REQUIRED`. Ask the
   business to read out the code Meta sent. It is forwarded to Twilio and never
   stored, never logged, never in the audit trail.
7. **Check status** until Twilio reports `ONLINE`. When it does, CoMaz writes
   `whatsapp:+…` into `garage_communication_settings.whatsapp_sender`, which is
   what makes inbound messages resolve to this tenant.
8. **Run test message.** Message the sender from your own mobile *first*, then
   send the test - otherwise WhatsApp's 24-hour rule rejects it (error 63016)
   and the test looks like a failure when it is really WhatsApp policy.
9. **Enable automation** when the business is ready for the conversation engine
   to answer customers. Off by default, deliberately.

---

## Test procedure

| What | How | Pass looks like |
| --- | --- | --- |
| Subaccount | Create it, then check Communications Setup | Subaccount shows **Ready**, not *Credential missing* |
| Voice inbound | Call the business's number from your own phone | You hear the greeting (or are forwarded to the escalation number); a `VOICE`/`INBOUND` row appears in the business's communications log |
| Voice status callback | Hang up | The same row gains a final status and duration |
| Voice outbound | *Test voice* to your own mobile | Your phone rings and says "This is a CoMaz test call for …" |
| WhatsApp inbound | Message the business's WhatsApp number from your own | A `WHATSAPP`/`INBOUND` row appears; if automation is on, you get a reply |
| WhatsApp outbound | *Run test message* (after messaging in first) | Status `queued`/`sent`, then `delivered` once the status callback lands |
| Failure handling | Send a test more than 24 hours after your last inbound message | Error 63016 appears under *Recent provider errors* **with its meaning and recommended action**, and the raw code and message intact |
| Tenancy | Try to attach one business's subaccount SID to a second business | Refused with a message naming the first business |

---

## Failures

There is **one** communication log. Operations > Failures and a business's
Communications page are two questions asked of the same
`communication_logs` rows - clicking a business in the failure breakdown lands
on that business's communications state, showing the same rows.

Every provider code is rendered with what it means and what to do, from
`app/communications/provisioning/errors.py`, **without discarding the original
code or message** - that is what a support engineer searches Twilio's docs
with. An unrecognised code is labelled unrecognised rather than given an
invented cause.

The catalogue is readable at
`GET /api/platform-admin/operations/communications-setup/error-codes`.

---

## Security

* Platform Admin only. Reads are open to any platform admin; every write is
  `@superadmin_required` and audited.
* **No response carries a secret.** No schema has a field for
  `TWILIO_AUTH_TOKEN`, a subaccount Auth Token, a Meta access token or a
  one-time code. The two secrets that come *in* are `load_only`: an attached
  subaccount token (encrypted on arrival) and Meta's OTP (forwarded to Twilio,
  never stored).
* The browser receives only *public* Meta identifiers - app ID, Embedded Signup
  config ID, Twilio Solution ID - which Meta itself renders into its popup URL.
* Audit `details` record SIDs, numbers and display names, never credentials;
  `app/platform_admin/audit.py::_scrub` drops anything secret-looking as a
  backstop.
* All Twilio API calls are backend-only.

---

## Known limits

* **Sender status is polled, not pushed.** Twilio's sender-status changes are
  picked up by *Check status*, not by a webhook. A sender that goes `ONLINE`
  overnight shows as such the next time somebody looks.
* **An unmapped Twilio sender status leaves our state alone** rather than
  guessing. A new Twilio value is far more likely than a failure, and flipping
  a live business to `FAILED` would be the expensive mistake.
* **Number release is not implemented.** Disabling communications stops every
  send and releases nothing at Twilio; releasing a number is a Twilio-console
  action, on purpose.
* **Marking a channel online is a human judgement**, not something inferred
  from a callback.
