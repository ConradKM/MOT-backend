from marshmallow import Schema, fields, validate


class CustomerSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    first_name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    last_name = fields.Str(required=True, validate=validate.Length(min=1, max=100))

    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))

    # Archived (soft-deleted) rather than hard-deleted once they have
    # vehicles/appointments - see app/customers/routes.py::CustomerResource.delete.
    is_active = fields.Bool(dump_only=True)
    # Not yet actionable anywhere (no SMS sends today) - see
    # app/models/customer.py. Read-only for now; a future Twilio-phase
    # endpoint can let a customer or staff member set it.
    sms_opt_out = fields.Bool(dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class CustomerUpdateSchema(Schema):
    first_name = fields.Str(validate=validate.Length(min=1, max=100))
    last_name = fields.Str(validate=validate.Length(min=1, max=100))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))


class CustomerQueryArgsSchema(Schema):
    search = fields.Str(load_default=None)
    # False (default): only active customers - the normal, everyday view.
    # True: also include archived ones (used by a customer's own detail page
    # so an archived-but-historical customer stays reachable by id).
    include_inactive = fields.Bool(load_default=False)


class CustomerDeleteResultSchema(Schema):
    """Body of DELETE /api/customers/<id> - it doesn't always delete."""

    archived = fields.Bool(dump_only=True)
    deleted = fields.Bool(dump_only=True)
