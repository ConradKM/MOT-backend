# Onboarding mutation retry/idempotency matrix

Compiled during an overnight production-readiness pass (no real provider
mutation performed). For each action a Platform Admin operator can trigger
during onboarding: is a retry (double-click, browser refresh mid-flight,
retried request after a lost response) safe, what actually happens at the
provider and in the database, and what the duplicate-resource risk is.

| Action | Retry safe? | Provider side effect | DB side effect | Reconciliation | Duplicate-resource risk |
|---|---|---|---|---|---|
| **Create tenant** (`POST /tenants`) | **Partial.** No server-side idempotency key. | None (no provider call). | New `Garage` row + owner `Employee` row, one transaction. | A second call with the **same owner email** is refused `409` (`employees.email` unique constraint) - this is what actually prevents a duplicate business from an identical retry, not a dedicated idempotency mechanism. | **Medium** if the frontend's submit-guard is ever bypassed (e.g. a scripted retry with a *different* owner email) - two real tenants would be created. Frontend test confirms the wizard disables the button after first submit (`OnboardBusiness.test.tsx::cannot be submitted twice`), but that is a client-side guard, not a server one. |
| **Send/resend owner invite** | Yes. | Sends an email (real side effect on real providers, but not a *billable resource*). | None durable - invite state lives in `Employee`'s existing fields, not a new row. | N/A - re-sending is the explicit recovery action for a lost/expired invite. | None - a re-sent invite just invalidates/replaces the previous link. |
| **Create Twilio subaccount** | Yes. | List-before-create by friendly name (`app/communications/provisioning/subaccounts.py`). | Upserts one row. | Clicking twice returns the same subaccount, never creates a second. | None. |
| **Buy a voice number** | Yes. | List-before-buy against the subaccount (`voice.py::buy_number`) - recovers an already-purchased number instead of buying a second, closing the exact "Twilio purchase succeeded, DB commit lost" gap. | Number persisted only after the provider call is confirmed (or reconciled). | Built-in - a retry after a lost response finds the number already owned and adopts it. | None (fixed this session's predecessor work, see PR #184). |
| **Adopt an existing number** | Yes. | Read-only classification call, then a state check before any write. | Same as above. | A number already claimed by another CoMaz business is refused with the owning business named, never silently reassigned. | None. |
| **Transfer a number from parent** | Yes. | Checks the number is still parent-owned immediately before moving it - refused if already moved (by this request or another). | Number/settings only cleared locally after Twilio confirms (or already reflects) the move. | A second click after a successful transfer finds the number already in the subaccount and no-ops. | None. |
| **Return number to parent** | Yes. | Same ownership-verified-before-write pattern as transfer, in reverse. | Local `voice_phone_number`/`voice_number_sid` cleared only after Twilio confirms the move. | A second click on an already-returned number finds nothing left to return - a no-op, not an error. | None. WhatsApp-sender acknowledgement gate prevents returning a number that would silently break WhatsApp. |
| **Enable OpenAI Voice** | Yes. | List-before-create at all three provider steps (trunk by friendly name, origination URL by `sip_url`, number association by SID) - `app/communications/provisioning/openai_voice_sip.py`. | `openai_voice_status` only reaches `READY` after every provider step is confirmed. | A retry after a partial failure (e.g. trunk created, origination URL step crashed) resumes from wherever it stopped rather than creating a second trunk. | None (verified by this session's own integration tests, `test_enable_openai_voice_never_creates_a_second_trunk_on_retry`). |
| **Mark voice/comms online** | Yes. | None (no provider call - a deliberate human judgement call, not inferred from a callback). | Idempotent field write. | N/A. | None. |
| **WhatsApp: register sender** | Yes, with caveats. | Calls Twilio Senders v2 with the subaccount's own credentials. | Sender state tracked through its own multi-step state machine (`SENDER_REGISTRATION_PENDING` -> `OTP_REQUIRED` -> `SENDER_REGISTERING` -> `ONLINE`). | An already-`ONLINE` sender is not re-registered; a stuck/failed attempt surfaces the provider's own error rather than retrying blindly. | Low - Twilio's own Senders API is itself idempotent per WABA/number pair. |

## What this means for tomorrow

Every **provider-mutating** onboarding action a Platform Admin operator
would click during a real TOD onboarding is safe to retry, including after
a lost response or a page refresh - the one gap is tenant creation itself,
which relies on the frontend's submit-guard plus the owner-email unique
constraint rather than a dedicated idempotency key. In practice this means:
**if a "Create business" submission appears to hang or fail, do not
resubmit with a different owner email as a workaround** - refresh
`/tenants` and check whether the business was actually created first.
