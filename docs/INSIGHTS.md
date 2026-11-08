# Insights

A business's own analytics page. Code: `app/insights/` (service, due back,
CSV export, routes, schemas). Shared SQL with Platform Admin's tenant
statistics: `app/analytics/queries.py`.

Read-only and computed on request. No rollup tables, no background jobs. The
only writes in this feature are the two due-back settings on appointment
types.

## Endpoints

All are staff JWT routes, with the garage taken from the token.

| Route | Plan | What |
| --- | --- | --- |
| `GET /api/insights?start=&end=&employee_id=` | every plan (advanced sections locked without the flag) | One payload for the period and the previous period of equal length |
| `GET /api/insights/due-back?page=&per_page=&employee_id=` | `insights_advanced` | Customers due back, paginated, as of today |
| `GET /api/insights/export.csv?start=&end=&employee_id=` | `insights_advanced` | The period's appointments as CSV |
| `GET /api/garage/features` | every plan | `{plan, features: {key: bool}}` so the staff app can show "Not on your plan" |

- `start`/`end` are business-local dates, both inclusive. Pass both or
  neither. The default is the last 30 days. A period longer than 730 days
  returns 422.
- Each period runs from local midnight to local midnight in the business's
  timezone (`app/garages/timezones.py`), so a period that crosses a DST
  change still covers exactly the days it names.
- Without `insights_advanced`, the `trend`, `heatmap`, `staff`, `customers`
  and `export` sections come back as `{"locked": true, "feature":
  "insights_advanced"}`, and the advanced-only endpoints return 403.

## Who sees what (enforced server-side)

- **Owners** see the whole business and every money field, and can narrow
  the page to one employee with `employee_id`.
- **Other staff** are always scoped to their own appointments
  (`Appointment.employee_id`). Naming anyone else returns 403. Money keys
  are never computed for them, so they are missing from the payload rather
  than set to zero. They get no staff table (`staff: null`), and their
  export has no money columns and no customer email or phone. Their due-back
  list only includes customers whose last visit was with them.
- `booking_requests` is `null` in any employee-scoped view, because requests
  aren't assigned to a member of staff.

## Definitions

- **Booked value**: the sum of `price_at_booking` (add-ons and discount
  included) for `COMPLETED` appointments that start in the period. This is
  completed work, not money in the bank.
- **Deposits collected**: online deposits charged in the period
  (`PAYMENT_STATUSES_CHARGED`, by `paid_at`), minus `refunded_amount_minor`.
  Partially refunded deposits count at their net amount. Fully refunded and
  failed attempts are left out.
- **Lost value**: the price of `NO_SHOW` and `CANCELLED` appointments. It is
  always reported separately and never mixed into the two figures above.
- **Unpriced appointments**: completed or lost appointments with no price.
  They count in every booking total but not in the money.
- **Rates**: the same as Platform Admin. Completion and cancellation are
  measured against concluded appointments, and no-shows against
  completed + no-show. A rate with an empty denominator is `null`.
- **New / returning**: these look at customers with a completed visit in
  the period. A customer is returning if they also had a completed visit
  before the period started. Otherwise they are new, including a customer
  whose first and second visits both fall in the period.
- **Heatmap**: appointment starts by local ISO weekday (1 = Monday) and
  hour. Cancelled appointments are left out; no-shows are included.
- **Sources**: each appointment is counted in exactly one channel:
  `web`/`conversation` (from a booking request's `source`), `queue`
  (promoted from the walk-in queue), `import` (bulk import), and otherwise
  `staff`.
- **Trend granularity**: daily up to 31 days, weekly (starting Monday) up to
  182 days, and monthly beyond that. Empty buckets are filled in.

## Due back

This is driven by each appointment type's own settings, never by its name:

- `return_interval_days`: the due date is the local date of the customer's
  last completed visit for that service plus the interval. If it is null,
  the service never produces due-back entries.
- `due_back_uses_mot_expiry`: the due date is the vehicle's stored
  `mot_expiry_date`. There is one entry per customer, service and vehicle,
  and only for vehicles that are active and still belong to that customer.

An entry is listed once its due date is today or earlier (`due`), or within
the next 14 days (`due_soon`). It is suppressed while the customer has any
upcoming appointment. The list is sorted with the soonest due date first,
so customers who lapsed long ago end up at the bottom.

## Indexes

There is an existing index on `(garage_id, start_time)`, plus two added for
Insights: `(garage_id, employee_id, start_time)` for staff-scoped queries,
and `(garage_id, customer_id, appointment_type_id, start_time)` for
first-visit and last-visit-per-service lookups.
