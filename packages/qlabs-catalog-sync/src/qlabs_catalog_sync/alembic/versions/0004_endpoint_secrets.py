"""endpoint secrets

Creates ``endpoint_secrets``, the one table in the configuration schema allowed to hold
credential material -- and only ever as AES-256-GCM ciphertext sealed under a master key
held outside the database (``qlabs_catalog_sync.configstore.crypto``). See
``configstore/models.py::EndpointSecretRow`` for the column-by-column rationale and the
amended decision C2 this implements.

Additive only: it creates one new table and touches nothing ``0001``-``0003`` created.
Existing endpoints keep whatever ``endpoints.secret_ref`` they already have, and the
environment backend keeps resolving ``env:`` references exactly as before -- a deployment
that has not adopted stored credentials sees no change from this migration at all.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24 10:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpoint_secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("field", sa.String(length=128), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoint_secrets")),
        sa.ForeignKeyConstraint(
            ["endpoint"],
            ["endpoints.name"],
            name=op.f("fk_endpoint_secrets_endpoint_endpoints"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("endpoint", "field", name="uq_endpoint_secrets_endpoint_field"),
    )


def downgrade() -> None:
    op.drop_table("endpoint_secrets")
