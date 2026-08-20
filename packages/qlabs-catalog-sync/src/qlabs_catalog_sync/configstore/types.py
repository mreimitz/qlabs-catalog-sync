"""Constrained-string value types and small ``TypeDecorator``s for the config schema.

WP10 / T10.1. Every column that would otherwise need a native database enum is a plain,
length-bounded ``String`` (or ``JSON``) column carrying one of the small vocabularies
below, never a ``sqlalchemy.Enum`` -- native DB enums are a migration hazard on
PostgreSQL (adding a member is a schema migration in most drivers) and do not exist on
SQLite at all (SQLAlchemy fakes them there with a CHECK constraint), so a plain
constrained string is the portable choice on both dialects the state store targets.

:class:`StrEnumType` and :class:`StrEnumListType` are generic ``TypeDecorator``s that
make such a column round-trip as a real ``StrEnum`` (or a list of one) on the Python
side, the same way ``qlabs_catalog_sync.state.types.UTCDateTime`` makes a portable
``DateTime`` column round-trip as an aware ``datetime``. Binding a value outside the
enum raises ``ValueError`` before it ever reaches the database.

:class:`ManualEditPolicyType` and the ``entity_types`` column (``StrEnumListType(
EntityType)``) deliberately **reuse** ``qlabs_catalog_sync.config.ManualEditPolicy`` and
``qlabs_catalog_sync_sdk.models.EntityType`` rather than inventing a parallel shape for
either: T2.3 already defines the manual-edit-policy vocabulary and the SDK already
defines the supported entity types (no ``Principal``/``AccessBinding``, per the v1
no-access-control-sync guardrail), and the console's job is to make those *editable*
from the state store, not to redescribe them.

The small enums:

* :class:`EndpointRole` -- whether a configured endpoint is used as a pair's source or
  target. This does not exist on ``qlabs_catalog_sync.config.EndpointConfig`` -- that
  model never needed to ask, since a pair's direction is validated cross-referentially
  at load time (see ``config.py``'s direction guardrails). The console does need to ask,
  so an endpoint declares its role when it is registered (C6).
* :class:`RuleScope` -- object scope (``catalog.schema``, becomes a data product) or
  dataset scope (tables/views inside a selected schema), per decision C5. Shared by
  ``selection_rules.scope`` and ``selection_overrides.scope``.
* :class:`SelectionDecision` -- include or exclude. Shared by ``selection_rules.decision``
  and ``selection_overrides.decision`` rather than two near-identical enums: both express
  the same two-valued concept (C3), and separate types would only invite the two to
  drift apart for no reason.
* :class:`MatcherKind` -- what a rule's ``pattern`` is matched against: a glob over the
  fully-qualified name, a source tag, or an owner email -- the three matchers T11.1's
  evaluator implements, which this schema is shaped to feed.
* :class:`ChangeAction` / :class:`ChangeEntityKind` -- what one ``config_changes`` row
  records: a create/update/delete of an endpoint/pair/rule/override.

No column here holds a credential value: every type in this module is shape/scope
vocabulary or a reused non-secret settings model. The sole credential-*adjacent* column
in the whole schema is ``EndpointRow.secret_ref`` (``configstore/models.py``), a plain
string *reference* (e.g. ``env:QLIK_ACME``), never a value (C2) -- see
``tests/configstore/test_credentials.py`` for the schema-reflection test that makes this
an enforced property of the schema, not just a convention.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from sqlalchemy import JSON, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from qlabs_catalog_sync.config import ManualEditPolicy

__all__ = [
    "ChangeAction",
    "ChangeEntityKind",
    "EndpointRole",
    "ManualEditPolicyType",
    "MatcherKind",
    "RuleScope",
    "SelectionDecision",
    "StrEnumListType",
    "StrEnumType",
]


class EndpointRole(StrEnum):
    """Whether a configured endpoint is used as a pair's source or its target."""

    SOURCE = "source"
    TARGET = "target"


class RuleScope(StrEnum):
    """Which layer of the source tree a selection rule or override applies to (C5)."""

    OBJECT = "object"
    """``catalog.schema`` -- becomes a Qlik data product."""

    DATASET = "dataset"
    """A table or view inside a selected schema -- becomes a data product member."""


class SelectionDecision(StrEnum):
    """Include or exclude. Shared by ``selection_rules`` and ``selection_overrides``."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class MatcherKind(StrEnum):
    """What a selection rule's ``pattern`` is evaluated against (T11.1's evaluator)."""

    GLOB = "glob"
    """A glob over the object's fully-qualified name."""

    TAG = "tag"
    """A source-reported tag (unavailable when the source manifest offers none)."""

    OWNER = "owner"
    """The object's owner email."""


class ChangeAction(StrEnum):
    """What kind of write one ``config_changes`` row records."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ChangeEntityKind(StrEnum):
    """Which configuration table one ``config_changes`` row is about."""

    ENDPOINT = "endpoint"
    SYNC_PAIR = "sync_pair"
    SELECTION_RULE = "selection_rule"
    SELECTION_OVERRIDE = "selection_override"


# --------------------------------------------------------------------------------------
# Generic TypeDecorators: a constrained-string (or JSON) column that round-trips as a
# real Python value instead of a bare ``str``/``dict``.
# --------------------------------------------------------------------------------------

E = TypeVar("E", bound=StrEnum)


class StrEnumType(TypeDecorator[E], Generic[E]):
    """A length-bounded ``String`` column that round-trips as ``enum_cls``.

    Binding any value that ``enum_cls`` does not accept raises ``ValueError`` before the
    row is written -- e.g. binding an entity type outside the SDK's ``EntityType`` (no
    ``"principal"``) fails at the ORM boundary rather than silently storing a string the
    rest of the system cannot interpret.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[E], length: int = 32) -> None:
        super().__init__(length=length)
        self._enum_cls = enum_cls

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self._enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self._enum_cls(value)


class StrEnumListType(TypeDecorator[list[E]], Generic[E]):
    """A ``JSON`` column of strings that round-trips as ``list[enum_cls]``.

    Used for ``sync_pairs.entity_types``: stored as a JSON array of plain strings (so it
    reads naturally outside Python, e.g. from the API/console), bound and read back as
    ``list[EntityType]``. Binding an unsupported entity type raises ``ValueError`` before
    the row is written -- the same defense-in-depth :class:`StrEnumType` gives a scalar
    column, for a list.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self._enum_cls = enum_cls

    def process_bind_param(
        self, value: list[E] | list[str] | None, dialect: Dialect
    ) -> list[str] | None:
        if value is None:
            return None
        return [self._enum_cls(item).value for item in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[E] | None:
        if value is None:
            return None
        return [self._enum_cls(item) for item in value]


class ManualEditPolicyType(TypeDecorator[ManualEditPolicy]):
    """A ``JSON`` column that round-trips as ``qlabs_catalog_sync.config.ManualEditPolicy``.

    Stores ``ManualEditPolicy.model_dump(mode="json")`` verbatim -- ``{"default": ...,
    "per_entity": {...}, "per_field": {...}}`` using exactly the field names and
    ``ManualEditMode`` values (``"source_wins"``/``"preserve_local"``) T2.3 already
    defines, so a row read out of this column and a ``SyncPairConfig.manual_edit_policy``
    built from the same values are the same object, not two representations of one idea
    that can drift apart.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(
        self, value: ManualEditPolicy | None, dialect: Dialect
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return value.model_dump(mode="json")

    def process_result_value(
        self, value: dict[str, Any] | None, dialect: Dialect
    ) -> ManualEditPolicy | None:
        if value is None:
            return None
        return ManualEditPolicy.model_validate(value)
