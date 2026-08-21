"""run history

Creates the three tables run history owns from empty: ``runs``, ``run_items`` and
``run_item_unresolved_fields``, plus ``run_errors`` (see
``qlabs_catalog_sync.runs.models`` for the field-by-field rationale). This is the only
migration that may create these tables -- T11.4 is the sole owner of this schema. It
runs on top of ``0002`` (T10.1's configuration schema) and leaves that schema, and
``0001``'s state-store schema, untouched.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20 17:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pair", sa.String(length=128), nullable=False),
        sa.Column("source_endpoint", sa.String(length=128), nullable=False),
        sa.Column("target_endpoint", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("committed", sa.Boolean(), nullable=False),
        sa.Column("create_enabled", sa.Boolean(), nullable=False),
        sa.Column("watermark_before", sa.Text(), nullable=True),
        sa.Column("watermark_after", sa.Text(), nullable=True),
        sa.Column("watermark_advanced", sa.Boolean(), nullable=False),
        sa.Column("has_more", sa.Boolean(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("read_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("written_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("no_op_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("orphaned_count", sa.Integer(), nullable=False),
        sa.Column("filtered_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_endpoints", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(
        "ix_runs_pair_entity_type_started_at",
        "runs",
        ["pair", "entity_type", "started_at"],
    )
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "run_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("native_key", sa.Text(), nullable=False),
        sa.Column("neutral_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("target_native_key", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("endpoint", sa.String(length=128), nullable=True),
        sa.Column("held_watermark", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_items")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_items_run_id_runs"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_run_items_run_id", "run_items", ["run_id"])
    op.create_index("ix_run_items_neutral_id_endpoint", "run_items", ["neutral_id", "endpoint"])

    op.create_table(
        "run_item_unresolved_fields",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_item_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_item_unresolved_fields")),
        sa.ForeignKeyConstraint(
            ["run_item_id"],
            ["run_items.id"],
            name=op.f("fk_run_item_unresolved_fields_run_item_id_run_items"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_item_id",
            "field",
            name="uq_run_item_unresolved_fields_run_item_id_field",
        ),
    )

    op.create_table(
        "run_errors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=True),
        sa.Column("native_key", sa.Text(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("fatal", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_errors")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_errors_run_id_runs"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_run_errors_run_id", "run_errors", ["run_id"])


def downgrade() -> None:
    op.drop_table("run_errors")
    op.drop_table("run_item_unresolved_fields")
    op.drop_table("run_items")
    op.drop_table("runs")
