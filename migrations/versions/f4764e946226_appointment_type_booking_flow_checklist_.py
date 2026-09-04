"""appointment type booking flow: checklist result options, customer visibility, price and duration snapshots

Revision ID: f4764e946226
Revises: f3a8c5e91d24
Create Date: 2026-09-04 18:03:04.743143

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'f4764e946226'
down_revision = 'f3a8c5e91d24'
branch_labels = None
depends_on = None

# Every checklist item created before this migration is automotive (it's the
# only kind of business this platform served until now) - backfill their
# result_options to the exact set they were implicitly using all along
# (see app/models/appointments/checklist_template_item.py::CHECKLIST_ITEM_STATUSES)
# so existing checklists behave identically after this migration. New items
# created from here on default to the smaller GENERIC_RESULT_OPTIONS instead
# (applied at the application layer, not the database).
_AUTOMOTIVE_STATUSES = [
    "PASS", "ADVISORY", "MINOR", "MAJOR", "DANGEROUS", "RECTIFIED",
    "RECOMMENDED", "CUSTOMER_DECLINED", "NOT_APPLICABLE", "NOT_CHECKED",
]


def upgrade():
    with op.batch_alter_table('checklist_template_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.String(length=500), nullable=True))
        batch_op.add_column(
            sa.Column('result_options', postgresql.ARRAY(sa.String(length=30)), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'visible_to_customer', sa.Boolean(),
                server_default=sa.text('false'), nullable=False,
            )
        )

    with op.batch_alter_table('appointment_checklist_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.String(length=500), nullable=True))
        batch_op.add_column(
            sa.Column('result_options', postgresql.ARRAY(sa.String(length=30)), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'visible_to_customer', sa.Boolean(),
                server_default=sa.text('false'), nullable=False,
            )
        )

    checklist_template_items = sa.table(
        'checklist_template_items', sa.column('result_options', postgresql.ARRAY(sa.String))
    )
    appointment_checklist_items = sa.table(
        'appointment_checklist_items', sa.column('result_options', postgresql.ARRAY(sa.String))
    )
    op.execute(checklist_template_items.update().values(result_options=_AUTOMOTIVE_STATUSES))
    op.execute(appointment_checklist_items.update().values(result_options=_AUTOMOTIVE_STATUSES))

    with op.batch_alter_table('checklist_template_items', schema=None) as batch_op:
        batch_op.alter_column('result_options', nullable=False)
    with op.batch_alter_table('appointment_checklist_items', schema=None) as batch_op:
        batch_op.alter_column('result_options', nullable=False)

    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_at_booking', sa.Numeric(precision=10, scale=2), nullable=True))

    with op.batch_alter_table('booking_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requested_duration_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('requested_price', sa.Numeric(precision=10, scale=2), nullable=True))


def downgrade():
    with op.batch_alter_table('booking_requests', schema=None) as batch_op:
        batch_op.drop_column('requested_price')
        batch_op.drop_column('requested_duration_minutes')

    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_column('price_at_booking')

    with op.batch_alter_table('appointment_checklist_items', schema=None) as batch_op:
        batch_op.drop_column('visible_to_customer')
        batch_op.drop_column('result_options')
        batch_op.drop_column('description')

    with op.batch_alter_table('checklist_template_items', schema=None) as batch_op:
        batch_op.drop_column('visible_to_customer')
        batch_op.drop_column('result_options')
        batch_op.drop_column('description')
