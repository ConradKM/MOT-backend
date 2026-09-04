from marshmallow import Schema, fields, validate

from app.roles.schemas import RoleSchema


class EmployeeSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    email = fields.Email(required=True)
    # Optional at the API level on purpose (existing, tested behaviour - see
    # tests/api/test_employees.py::test_employee_name_is_optional) so a
    # name-less employee stays usable and scriptable creation isn't blocked.
    # The staff "Add employee" form makes both fields required in practice
    # (see EmployeesList.tsx) - that's where real garage usage happens.
    first_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    last_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    password = fields.Str(
        required=True, load_only=True, validate=validate.Length(min=8)
    )
    role_ids = fields.List(fields.UUID(), load_only=True, load_default=list)

    is_active = fields.Bool(dump_only=True)
    roles = fields.List(fields.Nested(RoleSchema), dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class EmployeeUpdateSchema(Schema):
    email = fields.Email()
    first_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    last_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    role_ids = fields.List(fields.UUID())
    is_active = fields.Bool()
