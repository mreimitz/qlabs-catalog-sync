"""initial state store schema

Creates the four tables the state store owns from empty: ``identity_map``,
``watermarks``, ``field_envelopes`` and ``orphan_log`` (see
``qlabs_catalog_sync.state.models`` for the field-by-field rationale). This is the
only migration that may create these tables -- T2.2 is the sole owner of this
schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-20 13:29:07.448536
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_envelopes",
        sa.Column("neutral_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("source_endpoint", sa.String(length=128), nullable=True),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("last_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "neutral_id", "endpoint", "entity_type", "field", name=op.f("pk_field_envelopes")
        ),
    )
    op.create_table(
        "identity_map",
        sa.Column("neutral_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("native_key", sa.Text(), nullable=False),
        sa.Column("secondary_keys", sa.JSON(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "neutral_id", "endpoint", "entity_type", name=op.f("pk_identity_map")
        ),
        sa.UniqueConstraint(
            "endpoint",
            "entity_type",
            "tenant_id",
            "native_key",
            name="uq_identity_map_endpoint_entity_type_tenant_id_native_key",
        ),
    )
    op.create_table(
        "orphan_log",
        sa.Column("neutral_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("native_key", sa.Text(), nullable=True),
        sa.Column("first_missing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_missing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "neutral_id", "endpoint", "entity_type", name=op.f("pk_orphan_log")
        ),
    )
    op.create_table(
        "watermarks",
        sa.Column("sync_pair", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("watermark_token", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "sync_pair", "endpoint", "entity_type", name=op.f("pk_watermarks")
        ),
    )


def downgrade() -> None:
    op.drop_table("watermarks")
    op.drop_table("orphan_log")
    op.drop_table("identity_map")
    op.drop_table("field_envelopes")
