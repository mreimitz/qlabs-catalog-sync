"""config store schema

Creates the six tables the console configuration schema owns from empty:
``endpoints``, ``sync_pairs``, ``selection_rules``, ``selection_overrides``,
``config_generation`` and ``config_changes`` (see
``qlabs_catalog_sync.configstore.models`` for the field-by-field rationale). This is
the only migration that may create these tables -- T10.1 is the sole owner of this
schema. It runs on top of ``0001`` (the T2.2 state-store schema) and leaves that
schema untouched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-20 16:30:20.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpoints",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("connector", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("secret_ref", sa.String(length=255), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_endpoints")),
    )
    op.create_table(
        "sync_pairs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("target_space", sa.String(length=255), nullable=False),
        sa.Column("entity_types", sa.JSON(), nullable=False),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False),
        sa.Column("jitter_seconds", sa.Float(), nullable=True),
        sa.Column("manual_edit_policy", sa.JSON(), nullable=False),
        sa.Column("activation_opt_in", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_pairs")),
        sa.ForeignKeyConstraint(
            ["source"],
            ["endpoints.name"],
            name=op.f("fk_sync_pairs_source_endpoints"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target"],
            ["endpoints.name"],
            name=op.f("fk_sync_pairs_target_endpoints"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("name", name="uq_sync_pairs_name"),
        sa.CheckConstraint(
            "cadence_seconds > 0", name=op.f("ck_sync_pairs_cadence_seconds_positive")
        ),
    )
    op.create_table(
        "selection_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pair_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("matcher_kind", sa.String(length=16), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_selection_rules")),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["sync_pairs.id"],
            name=op.f("fk_selection_rules_pair_id_sync_pairs"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "pair_id",
            "scope",
            "ordinal",
            name="uq_selection_rules_pair_id_scope_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_selection_rules_ordinal_non_negative")
        ),
    )
    op.create_table(
        "selection_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pair_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_selection_overrides")),
        sa.ForeignKeyConstraint(
            ["pair_id"],
            ["sync_pairs.id"],
            name=op.f("fk_selection_overrides_pair_id_sync_pairs"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "pair_id",
            "scope",
            "object_id",
            name="uq_selection_overrides_pair_id_scope_object_id",
        ),
    )
    op.create_table(
        "config_generation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_generation")),
        sa.CheckConstraint("id = 1", name=op.f("ck_config_generation_singleton")),
    )
    op.create_table(
        "config_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=255), nullable=True),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_changes")),
    )


def downgrade() -> None:
    op.drop_table("config_changes")
    op.drop_table("config_generation")
    op.drop_table("selection_overrides")
    op.drop_table("selection_rules")
    op.drop_table("sync_pairs")
    op.drop_table("endpoints")
