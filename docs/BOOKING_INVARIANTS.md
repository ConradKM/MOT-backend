# Booking invariants

This is the safety contract for public, staff, and conversation-created
bookings.  Changes to booking code must preserve these rules.

## Authority and tenancy

- The server owns booking requests, payment attempts, holds, availability and
  state transitions. Browser state is a convenience only.
- Every read and mutation is scoped to the authenticated business, or to the
  public business slug plus a high-entropy, one-purpose recovery token. Never
  use an identifier from one business to recover or mutate another business's
  booking, customer, vehicle, payment or appointment.
- A vehicle belongs to both its garage and customer. Appointment changes must
  preserve that relationship; changing a customer requires explicitly clearing
  or replacing an incompatible vehicle.

## Capacity and attempts

- A slot is validated at mutation time under the garage-level serialization
  used by public booking, request approval and staff scheduling. Calendar
  availability is advisory, never authority.
- `AWAITING_PAYMENT` requests reserve capacity only until their payment hold
  expires. `PENDING` requests reserve capacity until they are approved,
  rejected, or expired. Approved appointments reserve their own capacity.
- A deposit attempt has one server-owned request and one provider payment
  association. Retries for the same attempt reuse it; a retry must never make
  another request, hold, PaymentIntent, or charge.
- Payment recovery uses an opaque token stored only as a hash server-side.
  It can recover the same valid attempt after a browser refresh, but it cannot
  revive an expired attempt or disclose payment credentials.
- Provider success is reconciled authoritatively. A customer who paid while
  offline must recover a `PENDING` request, never be asked to pay again.

## Lifecycle and history

- Payment success moves the same request from `AWAITING_PAYMENT` to `PENDING`.
  Expiry, failure and cancellation release only their own reservation.
- Approval locks and revalidates the request and capacity, then creates exactly
  one appointment and links it to the request. Repeated approval cannot create
  another appointment.
- Rejection is terminal for the request and invokes the existing idempotent
  refund abstraction only for a successful deposit. Retried rejection must not
  duplicate a refund.
- Appointment cancellation is historical, not deletion. A cancellation frees
  capacity; completion, notes and checklist results must preserve the
  appointment's history.
- Service name, requested duration, price, answers and checklist data are
  snapshots where stored. Later edits, hiding, or archiving a service must not
  rewrite an existing request or appointment.

## Rescheduling and time

- A reschedule validates the target slot while the relevant garage is locked,
  excludes only the appointment being moved, and commits the replacement
  schedule atomically. It must not temporarily release the old slot or reserve
  the new slot twice.
- Persisted appointment instants are timezone-aware. Public dates and times are
  interpreted in the business timezone; opening boundaries, duration and
  capacity checks must remain correct across GMT/BST and midnight.

## Client and operational behavior

- Mutation UIs disable duplicate submissions while in flight and reconcile the
  authoritative response before showing success.
- Capacity conflicts, expired attempts and provider failures remain retryable
  where safe and must not be rendered as success.
- Staff scheduling uses the same employee-conflict and garage-capacity model as
  customer booking. Any deliberate out-of-hours override must be explicit and
  separately authorized; it must not arise from a client-side bypass.
