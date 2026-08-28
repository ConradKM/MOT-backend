from marshmallow import Schema, fields, validate

RESULTS = ("PASS", "FAIL")


class MOTRecordSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    vehicle_id = fields.UUID(dump_only=True)

    mot_date = fields.Date(required=True)
    expiry_date = fields.Date(required=True)
    result = fields.Str(required=True, validate=validate.OneOf(RESULTS))
    notes = fields.Str(allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class MOTRecordUpdateSchema(Schema):
    mot_date = fields.Date()
    expiry_date = fields.Date()
    result = fields.Str(validate=validate.OneOf(RESULTS))
    notes = fields.Str(allow_none=True)
