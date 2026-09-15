from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.models.booking_flow.field import (
    FIELD_BINDINGS,
    FIELD_TYPES,
    NUMERIC_BINDINGS,
    OPTION_FIELD_TYPES,
)


class BookingFlowFieldSchema(Schema):
    id = fields.UUID(dump_only=True)
    booking_flow_section_id = fields.UUID(dump_only=True)

    label = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    help_text = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=500))
    placeholder = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=200))
    field_type = fields.Str(load_default="TEXT", validate=validate.OneOf(FIELD_TYPES))
    is_required = fields.Bool(load_default=False)
    options = fields.List(fields.Str(validate=validate.Length(min=1, max=200)), load_default=list)
    order = fields.Int(load_default=0, validate=validate.Range(min=0))
    min_value = fields.Int(allow_none=True, load_default=None)
    max_value = fields.Int(allow_none=True, load_default=None)
    max_length = fields.Int(allow_none=True, load_default=None, validate=validate.Range(min=1))
    binds_to = fields.Str(
        allow_none=True, load_default=None, validate=validate.OneOf(FIELD_BINDINGS)
    )

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    @validates_schema
    def _check_combinations(self, data, **kwargs):
        errors = {}

        field_type = data.get("field_type")
        # Only enforce on a payload that actually carries the key - the update
        # schema reuses this and a PATCH may touch neither.
        if field_type in OPTION_FIELD_TYPES and not data.get("options"):
            errors["options"] = [f"A {field_type} field needs at least one option."]

        if (
            data.get("min_value") is not None
            and data.get("max_value") is not None
            and data["min_value"] > data["max_value"]
        ):
            errors["min_value"] = ["min_value cannot be greater than max_value."]

        binds_to = data.get("binds_to")
        if binds_to in NUMERIC_BINDINGS and field_type not in (None, "NUMBER"):
            # A binding writes into a real numeric column, so a TEXT field
            # bound to it would store something that column can't hold.
            errors["binds_to"] = [f"{binds_to} can only be bound to a NUMBER field."]

        if errors:
            raise ValidationError(errors)


class BookingFlowFieldUpdateSchema(BookingFlowFieldSchema):
    """Same rules, nothing required - a PATCH may touch one key."""

    label = fields.Str(validate=validate.Length(min=1, max=200))
    field_type = fields.Str(validate=validate.OneOf(FIELD_TYPES))
    is_required = fields.Bool()
    options = fields.List(fields.Str(validate=validate.Length(min=1, max=200)))
    order = fields.Int(validate=validate.Range(min=0))


class BookingFlowSectionSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    title = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    description = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=500))
    order = fields.Int(load_default=0, validate=validate.Range(min=0))
    is_active = fields.Bool(load_default=True)
    # None = part of the business's default workflow, asked for every service.
    # A value scopes this section to that one service, and a service with any
    # section of its own *replaces* the default rather than adding to it - see
    # app/booking_flow/resolve.py.
    appointment_type_id = fields.UUID(allow_none=True, load_default=None)

    fields_ = fields.List(
        fields.Nested(BookingFlowFieldSchema),
        dump_only=True,
        data_key="fields",
        attribute="fields",
    )

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class BookingFlowSectionUpdateSchema(Schema):
    title = fields.Str(validate=validate.Length(min=1, max=200))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    order = fields.Int(validate=validate.Range(min=0))
    is_active = fields.Bool()
    appointment_type_id = fields.UUID(allow_none=True)


class BookingFlowQueryArgsSchema(Schema):
    # Filter to one service's override, or (with `default=true`) to the
    # business default. Omitted = every section this business has configured,
    # which is what the settings editor lists.
    appointment_type_id = fields.UUID(load_default=None)
    default_only = fields.Bool(load_default=False)


class ApplyPresetSchema(Schema):
    preset = fields.Str(required=True)
