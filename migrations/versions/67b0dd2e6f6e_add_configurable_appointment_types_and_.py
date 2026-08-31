"""Add configurable appointment types and checklist system

Revision ID: 67b0dd2e6f6e
Revises: 46c9ee69459d
Create Date: 2026-08-31 13:27:28.901293

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '67b0dd2e6f6e'
down_revision = '46c9ee69459d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('garage_appointment_types',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('base_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('garage_appointment_types', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_garage_appointment_types_garage_id'), ['garage_id'], unique=False)

    op.create_table('checklist_templates',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_type_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['appointment_type_id'], ['garage_appointment_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('appointment_type_id')
    )
    with op.batch_alter_table('checklist_templates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_checklist_templates_garage_id'), ['garage_id'], unique=False)

    op.create_table('checklist_template_items',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('checklist_template_id', sa.Uuid(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=300), nullable=False),
    sa.Column('is_compulsory', sa.Boolean(), nullable=False),
    sa.Column('media_type', sa.String(length=10), nullable=False),
    sa.Column('media_required_for_statuses', postgresql.ARRAY(sa.String(length=20)), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['checklist_template_id'], ['checklist_templates.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('checklist_template_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_checklist_template_items_checklist_template_id'), ['checklist_template_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_checklist_template_items_garage_id'), ['garage_id'], unique=False)

    op.create_table('appointment_checklists',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_id', sa.Uuid(), nullable=False),
    sa.Column('checklist_template_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['appointment_id'], ['appointments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['checklist_template_id'], ['checklist_templates.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('appointment_id')
    )
    with op.batch_alter_table('appointment_checklists', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointment_checklists_garage_id'), ['garage_id'], unique=False)

    op.create_table('appointment_checklist_items',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_checklist_id', sa.Uuid(), nullable=False),
    sa.Column('checklist_template_item_id', sa.Uuid(), nullable=True),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=300), nullable=False),
    sa.Column('is_compulsory', sa.Boolean(), nullable=False),
    sa.Column('media_type', sa.String(length=10), nullable=False),
    sa.Column('media_required_for_statuses', postgresql.ARRAY(sa.String(length=20)), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('completed_by_employee_id', sa.Uuid(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['appointment_checklist_id'], ['appointment_checklists.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['checklist_template_item_id'], ['checklist_template_items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['completed_by_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('appointment_checklist_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_appointment_checklist_items_appointment_checklist_id'), ['appointment_checklist_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_appointment_checklist_items_garage_id'), ['garage_id'], unique=False)

    op.create_table('checklist_item_media',
    sa.Column('garage_id', sa.Uuid(), nullable=False),
    sa.Column('appointment_checklist_item_id', sa.Uuid(), nullable=False),
    sa.Column('media_type', sa.String(length=10), nullable=False),
    sa.Column('storage_key', sa.String(length=500), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['appointment_checklist_item_id'], ['appointment_checklist_items.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['garage_id'], ['garages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('checklist_item_media', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_checklist_item_media_appointment_checklist_item_id'), ['appointment_checklist_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_checklist_item_media_garage_id'), ['garage_id'], unique=False)

    # --- appointment_type: enum column -> FK on the new per-garage table ---
    #
    # Temporary stopgap (see auth/routes.py and issue #8's follow-up): seed
    # the 5 old enum values as real rows for every garage that already
    # exists, then point every existing appointment at its garage's matching
    # row, before dropping the old column. New garages get the same 5 seeded
    # at registration time instead of via migration.
    op.add_column('appointments', sa.Column('appointment_type_id', sa.Uuid(), nullable=True))

    op.execute(
        """
        INSERT INTO garage_appointment_types (id, garage_id, name, created_at, updated_at)
        SELECT gen_random_uuid(), g.id, t.name, now(), now()
        FROM garages g
        CROSS JOIN (VALUES ('MOT'), ('Service'), ('MOT + Service'), ('Repair'), ('Other')) AS t(name)
        """
    )

    op.execute(
        """
        UPDATE appointments a
        SET appointment_type_id = gat.id
        FROM garage_appointment_types gat
        WHERE gat.garage_id = a.garage_id
          AND gat.name = CASE a.appointment_type
              WHEN 'MOT' THEN 'MOT'
              WHEN 'SERVICE' THEN 'Service'
              WHEN 'MOT_AND_SERVICE' THEN 'MOT + Service'
              WHEN 'REPAIR' THEN 'Repair'
              WHEN 'OTHER' THEN 'Other'
              ELSE 'Other'
          END
        """
    )

    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.alter_column('appointment_type_id', existing_type=sa.Uuid(), nullable=False)
        batch_op.create_index(batch_op.f('ix_appointments_appointment_type_id'), ['appointment_type_id'], unique=False)
        batch_op.create_foreign_key(None, 'garage_appointment_types', ['appointment_type_id'], ['id'])
        batch_op.drop_column('appointment_type')


def downgrade():
    # Not supported, for the same reason 46c9ee69459d's downgrade isn't:
    # this seeds new rows and repoints appointments at them, there's no way
    # back to "the enum value this used to be" without a pre-migration
    # backup.
    raise NotImplementedError(
        "This migration is not reversible: appointment_type_id points at "
        "newly-seeded garage_appointment_types rows, and the original enum "
        "value isn't recoverable from that. Restore from a backup taken "
        "before this migration ran instead of downgrading."
    )
