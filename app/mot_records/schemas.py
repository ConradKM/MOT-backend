from marshmallow import Schema, fields, validate

RESULTS = ("PASS", "FAIL")


class MOTRecordSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    vehicle_id = fields.UUID(dump_only=True)

    mot_date = fields.Date(required=True)
    # Required for a PASS (a test that grants no new valid period doesn't
    # make sense); optional for a FAIL, where it defaults to `mot_date` itself
    # - a failed test never grants a new expiry (see routes.py::_apply_result).
    expiry_date = fields.Date(allow_none=True, load_default=None)
    result = fields.Str(required=True, validate=validate.OneOf(RESULTS))
    notes = fields.Str(allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class MOTRecordUpdateSchema(Schema):
    mot_date = fields.Date()
    expiry_date = fields.Date(allow_none=True)
    result = fields.Str(validate=validate.OneOf(RESULTS))
    notes = fields.Str(allow_none=True)
