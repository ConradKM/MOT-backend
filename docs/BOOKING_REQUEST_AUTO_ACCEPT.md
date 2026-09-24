# Booking request auto-accept — future implementation note

This is intentionally a design note only. Do not enable or implement
automatic approval until the employee/resource assignment policy is agreed.

## Proposed behaviour

- Add an owner-facing, tenant-specific **Auto-accept booking requests**
  setting, persisted on the garage/business configuration and defaulting to
  `false`. Existing businesses therefore retain the manual review workflow.
- When enabled, a newly submitted request may be approved only by the same
  `approve_booking_request` domain operation used by staff approval. It must
  not create a second booking-to-appointment path.
- That operation must continue to lock the request and garage/capacity state,
  revalidate availability at approval time, and preserve duration, buffers,
  working hours, blocked time, existing appointments, deposits/payments,
  notifications and all other approval side effects.
- If revalidation cannot approve the request, leave it `PENDING` for staff
  review. Never reject or lose a request merely because automatic approval
  failed.
- If the audit/event model supports it, record whether approval was automatic
  or manual without changing customer-visible behaviour.

## Current constraint

The authoritative approval operation requires an explicit active employee and
checks that employee for appointment conflicts. `Appointment.employee_id` is
non-nullable, and the scheduling model has garage opening hours and aggregate
slot capacity but no employee skills, per-employee working hours, or default
assignee. A customer's `preferred_employee_note` is free text, not an employee
identity, and must not be used as an assignment.

## Assignment policy choices for product decision

### Tenant-configured default employee

Add a nullable, garage-scoped default employee reference in business settings.
The owner chooses a current active employee; auto-approval passes that ID to
the existing approval operation. The operation still locks and rechecks both
garage capacity and that employee's appointments. It must leave the request
pending if the default is missing, inactive, unavailable, or conflicts with a
customer's explicit staff request. This needs a small tenant-scoped schema
migration, a settings UI, validation that the employee belongs to the garage,
and an owner decision on whether every service can use that employee.

### Service-specific default employee

Add a nullable default employee reference to each garage appointment type.
This lets the owner route different services to different staff. It has the
same locking and conflict requirements as the tenant default, plus migration,
appointment-type settings UI, and a defined fallback when a request lacks an
active type or that type has no default. It is more precise, but requires
maintaining assignments for every service.

### Deterministic available-employee selection

At approval time, select one active employee in a documented stable order only
after acquiring the existing garage/request locks, then pass that employee to
the same operation. This may avoid a settings migration, but the present model
cannot express employee skills, leave, per-employee hours, or resource
eligibility. Product would need to define a fair/stable ordering and whether
all active employees are eligible for every service. If no eligible employee
passes the conflict check, leave the request pending.

### Unassigned appointments

This is not feasible in the current domain without a separate product change:
appointments require `employee_id` and manual approval deliberately rejects a
missing assignment. Supporting it would require a nullable schema change, a
staff assignment lifecycle, and revised capacity semantics. It must not be
introduced implicitly as part of auto-accept.

## Audit actor

The current operation accepts a human `reviewer` and stores that employee in
`reviewed_by_employee_id`. Auto-accept must not silently attribute a system
decision to the assigned/default employee. Before implementation, product and
the audit model need a deliberate representation of an automated approval
(for example a distinct approval mode/event, with no human reviewer), while
preserving the existing staff-review meaning of `reviewed_by_employee_id`.
This is separate from selecting the employee who will perform the appointment.

## Migration and concurrency

The eventual auto-accept setting needs a tenant-scoped persisted boolean with a
database default of `false`. Any default-assignee option also needs its own
tenant-scoped foreign key and validation. Create no migration until product has
selected an assignment policy and the current Alembic head is reconciled.
Concurrency and capacity guarantees must remain identical to manual approval.
