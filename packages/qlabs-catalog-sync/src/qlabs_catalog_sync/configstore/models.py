"""SQLAlchemy 2.0 declarative models for the console configuration schema.

WP10 / T10.1. Six tables, all dialect-portable (SQLite today, PostgreSQL later via the
same schema and the same Alembic history), that move configuration out of environment
variables and into the state store so a browser console can become its source of truth
(decision C1). They share ``Base``/``Base.metadata`` with the T2.2 state-store tables
(``identity_map``, ``watermarks``, ``field_envelopes``, ``orphan_log``) rather than
defining a second declarative base -- one ``MetaData`` object is what lets a single
Alembic migration history see, and migrate, the whole database, and what lets a future
join between, say, ``sync_pairs`` and ``watermarks`` work without wiring two engines.
This module adds tables; it does not touch the four T2.2 defines.

* ``endpoints`` -- one configured instance of a connector (C6: "install an endpoint"
  means naming an already-discovered connector, never fetching or executing one).
  ``name`` is the natural primary key, matching the vocabulary
  ``qlabs_catalog_sync.config.EndpointConfig`` already uses (endpoints are keyed by an
  arbitrary, engine-chosen string such as ``"qlik_acme"``) -- ``sync_pairs.source``/
  ``target`` reference it directly, exactly the way ``SyncPairConfig.source``/``target``
  already do. ``secret_ref`` is the endpoint's *one* named secret reference (C2, e.g.
  ``"env:QLIK_ACME"``); resolved through the SDK's ``ConnectorConfig.for_endpoint``
  prefix convention, one reference yields every credential field that endpoint's
  connector needs, so one column is enough -- T10.2 owns parsing and resolution.
  ``settings`` is plain (non-secret) connector configuration, exactly
  ``EndpointConfig.settings``'s shape. ``enabled`` defaults to ``False``: per C6,
  registering an endpoint is a multi-step flow (name it, bind a secret reference, run a
  healthcheck, read its capability manifest) that ends with an explicit enable, not an
  implicit one.

* ``sync_pairs`` -- one source-endpoint-to-target-endpoint pair (mirrors
  ``qlabs_catalog_sync.config.SyncPairConfig`` field-for-field: ``target_space``,
  ``entity_types``, ``cadence_seconds``, ``manual_edit_policy``, ``activation_opt_in``
  are the exact names and shapes T2.3 already defines -- see ``configstore/types.py``
  for why ``entity_types`` and ``manual_edit_policy`` *reuse* the SDK's ``EntityType``
  and T2.3's ``ManualEditPolicy`` rather than re-describing them). ``id`` is a surrogate
  UUID primary key -- unlike an endpoint, a pair is the target of two more tables
  (``selection_rules``, ``selection_overrides``) that must keep pointing at the same
  pair even if an operator renames it, so the FK target is opaque and stable while
  ``name`` stays the human-facing, uniquely-constrained label (matching
  ``SyncPairConfig.name``). ``jitter_seconds`` is new: T2.6's scheduler currently
  *computes* a pair's jitter window from its cadence
  (``scheduler.jitter_seconds_for``); this column is an optional per-pair override
  (``NULL`` means "use the computed default") for the console to expose later, not a
  duplicate of the computed value. ``enabled`` defaults to ``False``, the same
  "explicit, not implicit" default as ``activation_opt_in`` (decision D7) and
  ``endpoints.enabled`` above.

* ``selection_rules`` -- one ordered include/exclude rule (C3, C4): ``ordinal`` is part
  of what makes the rule *set* meaningful (last match wins), so it is a real column,
  uniquely constrained together with ``(pair_id, scope)`` -- two rules at the same
  position in the same pair's same scope is a contradiction the schema refuses to
  store, not a UI concern. Deleting a pair cascades to its rules (``ondelete="CASCADE"``
  on ``pair_id``): a rule set with no pair to select for is meaningless, unlike an audit
  entry, which must outlive the row it describes (see ``config_changes`` below).

* ``selection_overrides`` -- one per-object pin (C3: "per-object overrides pinned by
  stable identifier beat every rule"). Uniquely constrained on
  ``(pair_id, scope, object_id)`` -- at most one override decision per object per scope
  per pair, so "which override applies" is never ambiguous. Also cascades on pair
  deletion, for the same reason as ``selection_rules``.

* ``config_generation`` -- a single-row counter, modeled in the schema rather than by
  convention: the primary key is fixed to ``1`` by both the column default *and* a
  ``CHECK (id = 1)`` constraint, so inserting a second row fails the primary-key
  uniqueness check regardless of what id is attempted, and inserting the "right" id a
  second time fails it too. There is no way to end up with zero or two rows through the
  ORM; there is exactly one, always at id ``1``.

* ``config_changes`` -- the append-only audit log every configuration write produces
  (T10.3 bumps ``config_generation`` and appends here in the same transaction). It
  deliberately holds **no** foreign key to the entity it describes: an audit trail that
  vanished along with the row it explains would defeat its own purpose (its target
  question is "when did this schema start syncing", which must still be answerable
  after the pair in question is deleted). ``entity_id`` is therefore a plain string --
  the endpoint's ``name`` or the UUID (as text) of a pair/rule/override -- rather than a
  typed FK. Immutability (no ``UPDATE``, ever) is an application-layer discipline this
  task's ``configstore/service.py`` (T10.3) is responsible for, not a database trigger:
  a portable trigger that behaves identically on SQLite and PostgreSQL is disproportionate
  machinery for a guarantee the one writer already gives by construction (it only ever
  calls ``INSERT``).

No column anywhere in this schema can hold a credential value. The only
credential-*adjacent* column is ``EndpointRow.secret_ref``, and it is a reference, never
a value -- see ``tests/configstore/test_credentials.py``, which reflects the schema and
fails if a future change adds a column shaped like a password, token, or secret value.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from qlabs_catalog_sync.config import ManualEditPolicy
from qlabs_catalog_sync.configstore.types import (
    ChangeAction,
    ChangeEntityKind,
    EndpointRole,
    ManualEditPolicyType,
    MatcherKind,
    RuleScope,
    SelectionDecision,
    StrEnumListType,
    StrEnumType,
)
from qlabs_catalog_sync.state.models import Base
from qlabs_catalog_sync.state.types import UTCDateTime
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "ConfigChangeRow",
    "ConfigGenerationRow",
    "EndpointRow",
    "EndpointSecretRow",
    "SelectionOverrideRow",
    "SelectionRuleRow",
    "SyncPairRow",
]


class EndpointRow(Base):
    """One configured connector instance (C1, C6). See the module docstring."""

    __tablename__ = "endpoints"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    connector: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[EndpointRole] = mapped_column(StrEnumType(EndpointRole, 16), nullable=False)
    # A named *reference* only -- "env:QLIK_ACME" -- never a credential value (C2).
    # Nullable: an endpoint can be registered, and its non-secret settings edited, before
    # a secret reference is bound (T10.2 validates the reference's own scheme:locator
    # shape; this column just stores whatever string is given).
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    settings: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class EndpointSecretRow(Base):
    """One credential an operator entered in the console, sealed (amended C2).

    The exception to "no column in this schema may hold a credential": these columns hold
    **ciphertext**, sealed with AES-256-GCM under a master key that lives outside the
    database entirely (``configstore.crypto``). ``tests/configstore/test_credentials.py``
    allows this table by name and separately proves the stored bytes are not the
    plaintext, so the guarantee is tested rather than asserted in a comment.

    Why a table rather than another column on ``endpoints``: a connector declares *n*
    secret-typed fields, not one (Databricks has ``client_secret`` and ``token``, one per
    credential route), each is set, replaced and cleared independently, and each is sealed
    under its own nonce. One row per ``(endpoint, field)`` is what makes "replace only the
    client secret" a single-row write instead of a read-modify-write of a JSON blob.

    ``endpoint`` cascades on delete: a sealed credential for an endpoint that no longer
    exists is unopenable by construction (the endpoint name is bound into the ciphertext)
    and keeping it would be keeping a credential nobody can account for. This is the same
    reasoning as ``selection_rules``, and the opposite of ``config_changes``, which must
    outlive what it describes.

    ``key_id`` records which master key sealed the row -- see ``crypto.key_id_of``. It is
    what turns "running with the wrong key" into a message that says so, and what makes a
    future key rotation legible: re-sealed rows are the ones whose ``key_id`` has moved.
    """

    __tablename__ = "endpoint_secrets"
    __table_args__ = (
        UniqueConstraint("endpoint", "field", name="uq_endpoint_secrets_endpoint_field"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    endpoint: Mapped[str] = mapped_column(
        String(128), ForeignKey("endpoints.name", ondelete="CASCADE"), nullable=False
    )
    #: The connector config field this credential is for -- ``"client_secret"``,
    #: ``"token"``. Always one of the connector's own secret-typed field names, which is
    #: what ``configstore.secrets.secret_field_names`` already defines once for the whole
    #: system; the service validates against it rather than accepting any string.
    field: Mapped[str] = mapped_column(String(128), nullable=False)
    #: AES-256-GCM ciphertext, tag included. Never a plaintext, never a reversible encoding.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: The per-value random nonce this ciphertext was sealed under. Not secret; unique.
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which master key sealed it (``crypto.key_id_of``) -- not the key, and not derived
    #: from it in any way that can be walked back.
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SyncPairRow(Base):
    """One source-to-target sync pair (mirrors ``config.SyncPairConfig``). See module docstring."""

    __tablename__ = "sync_pairs"
    __table_args__ = (
        UniqueConstraint("name", name="uq_sync_pairs_name"),
        CheckConstraint("cadence_seconds > 0", name="cadence_seconds_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    source: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("endpoints.name", ondelete="RESTRICT"),
        nullable=False,
    )
    target: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("endpoints.name", ondelete="RESTRICT"),
        nullable=False,
    )

    target_space: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_types: Mapped[list[EntityType]] = mapped_column(
        StrEnumListType(EntityType), nullable=False, default=list
    )
    cadence_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    # NULL = use the scheduler's computed default (T2.6 `jitter_seconds_for`); a value
    # here overrides that computation for this pair only. See the module docstring.
    jitter_seconds: Mapped[float | None] = mapped_column(Float)
    manual_edit_policy: Mapped[ManualEditPolicy] = mapped_column(
        ManualEditPolicyType(), nullable=False, default=ManualEditPolicy
    )
    activation_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SelectionRuleRow(Base):
    """One ordered include/exclude rule within a pair's scope (C3, C4). See module docstring."""

    __tablename__ = "selection_rules"
    __table_args__ = (
        UniqueConstraint(
            "pair_id", "scope", "ordinal", name="uq_selection_rules_pair_id_scope_ordinal"
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pair_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sync_pairs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Position within (pair_id, scope); evaluation runs low-to-high ordinal, last match
    # wins (C3). Part of the unique constraint above, not just an orderable hint.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[RuleScope] = mapped_column(StrEnumType(RuleScope, 16), nullable=False)
    decision: Mapped[SelectionDecision] = mapped_column(
        StrEnumType(SelectionDecision, 16), nullable=False
    )
    matcher_kind: Mapped[MatcherKind] = mapped_column(StrEnumType(MatcherKind, 16), nullable=False)
    # The glob, tag key[=value], or owner-email pattern `matcher_kind` interprets it as.
    pattern: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SelectionOverrideRow(Base):
    """One per-object pin that beats every rule (C3). See module docstring."""

    __tablename__ = "selection_overrides"
    __table_args__ = (
        UniqueConstraint(
            "pair_id", "scope", "object_id", name="uq_selection_overrides_pair_id_scope_object_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pair_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sync_pairs.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope: Mapped[RuleScope] = mapped_column(StrEnumType(RuleScope, 16), nullable=False)
    # The stable identifier of the pinned object -- a "catalog.schema" for object scope,
    # a "catalog.schema.table" for dataset scope. Opaque text here; the evaluator (T11.1)
    # owns interpreting it against a candidate.
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[SelectionDecision] = mapped_column(
        StrEnumType(SelectionDecision, 16), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConfigGenerationRow(Base):
    """Single-row generation counter, bumped by every configuration write (C1).

    The single row is enforced in the schema, not by convention: ``id`` is pinned to
    ``1`` by both its default and the ``CHECK`` constraint below, so no second row --
    at id ``1`` or any other id -- can ever be inserted through this table.
    """

    __tablename__ = "config_generation"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConfigChangeRow(Base):
    """One append-only audit entry: what changed, old/new values, actor, generation.

    No foreign key to the entity described -- see the module docstring for why the audit
    trail must be able to outlive the row it is about. No ``UPDATE`` path exists in the
    service layer (T10.3): this table only ever grows.
    """

    __tablename__ = "config_changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_kind: Mapped[ChangeEntityKind] = mapped_column(
        StrEnumType(ChangeEntityKind, 32), nullable=False
    )
    # The endpoint's `name`, or the UUID (as text) of a pair/rule/override. Deliberately
    # untyped/unconstrained -- see the module docstring.
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[ChangeAction] = mapped_column(StrEnumType(ChangeAction, 16), nullable=False)
    # The single field that changed, for a field-level update; NULL for a whole-row
    # create/delete, where old_value/new_value already carry the complete row.
    field: Mapped[str | None] = mapped_column(String(255))
    old_value: Mapped[object | None] = mapped_column(JSON)
    new_value: Mapped[object | None] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # The value config_generation.generation held *after* this write -- lets a reader
    # correlate a change with the exact generation the scheduler reconciled against.
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
