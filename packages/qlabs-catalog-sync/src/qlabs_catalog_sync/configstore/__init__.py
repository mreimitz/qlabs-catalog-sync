"""Configuration store: the console's writable, audited source of truth (C1, C2).

WP10 / T10.1. Six tables added to the existing state-store schema (``Base`` from
``qlabs_catalog_sync.state.models``, shared rather than duplicated -- see
``configstore/models.py`` for why): ``endpoints``, ``sync_pairs``, ``selection_rules``,
``selection_overrides``, ``config_generation`` and ``config_changes``, migrated by
``packages/qlabs-catalog-sync/src/qlabs_catalog_sync/alembic/versions/0002_config_store.py``
(``down_revision = "0001"``, T2.2's schema).

This task owns the schema only: the models, the small value types
(``configstore/types.py``) they are built from, and the migration. Secret-reference
parsing and resolution (T10.2), the CRUD/audit service (T10.3), and the
environment-bootstrap importer (T10.4) build on top of this module without editing it.
"""

from qlabs_catalog_sync.configstore.models import (
    ConfigChangeRow,
    ConfigGenerationRow,
    EndpointRow,
    SelectionOverrideRow,
    SelectionRuleRow,
    SyncPairRow,
)
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

__all__ = [
    "ChangeAction",
    "ChangeEntityKind",
    "ConfigChangeRow",
    "ConfigGenerationRow",
    "EndpointRole",
    "EndpointRow",
    "ManualEditPolicyType",
    "MatcherKind",
    "RuleScope",
    "SelectionDecision",
    "SelectionOverrideRow",
    "SelectionRuleRow",
    "StrEnumListType",
    "StrEnumType",
    "SyncPairRow",
]
