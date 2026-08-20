"""Turning live connector reads into :class:`CatalogObject` s for identity bootstrap.

WP2 / T2.8. ``identity.py`` (T7.1) is deliberately connector-agnostic: :meth:`
~qlabs_catalog_sync.identity.IdentityResolver.bootstrap` takes plain ``CatalogObject``
sequences and does no I/O of its own ("The caller (the sync loop, or the CLI) builds
these from what a connector read; this module never talks to a connector."). This
module is that caller, for the CLI: it enumerates every object of a given entity type at
a connector (a full scan, not a since-watermark poll) and builds the natural key
bootstrap matches on.

Deriving a natural key from a connector read
---------------------------------------------

Every neutral entity carries a ``name`` field (RS-03); that is the natural key's name,
unchanged. The parent path is derived from the native key's dotted structure: decision
D1 makes a Unity Catalog schema (native key ``"catalog.schema"``) a data product with
natural-key parent path ``("catalog",)`` -- i.e. every segment of the native key except
the last. A native key with no dot (a Qlik id, a glossary term with an opaque handle)
gets an empty parent path.

That asymmetry is exactly why this module's bootstrap command defaults to
:attr:`~qlabs_catalog_sync.identity.ParentPathRule.IGNORE` (see ``identity_commands.py``):
a Databricks schema's derived parent path is a catalog name, but nothing about a Qlik
data product's native key expresses a comparable containment, so under
:attr:`~qlabs_catalog_sync.identity.ParentPathRule.EXACT` every source object's non-empty
parent path would fail to equal every target candidate's empty one and nothing would
ever match. ``identity.py``'s own docstring recommends ``IGNORE`` for exactly this shape:
a candidate list already scoped to one target container (one Qlik space, via the pair's
``target_space``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from qlabs_catalog_sync.identity import CatalogObject, NaturalKey
from qlabs_catalog_sync_sdk.contract import Connector, Watermark
from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity

from .errors import EXIT_ENDPOINT_UNREACHABLE, CliError

__all__ = ["enumerate_all", "to_catalog_objects"]

#: Safety bound on paged listings during one bootstrap enumeration -- mirrors
#: `SyncLoop`'s own `max_pages` default (`sync/loop.py`), for the same reason: a
#: connector bug that never sets `has_more = False` must fail loudly, not hang forever.
_MAX_PAGES: Final = 1000


async def enumerate_all(connector: Connector, entity_type: EntityType) -> list[NeutralEntity]:
    """Every current object of ``entity_type`` at ``connector`` -- a full scan.

    Bootstrap needs the *whole* catalog, not what changed since a watermark, so this
    always starts from :meth:`~qlabs_catalog_sync_sdk.contract.Watermark.initial` and
    pages through every result regardless of any cycle's stored watermark. A deletion
    reported mid-scan is simply skipped (nothing to match); a ``read`` that 404s between
    the listing and the read (:class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`) is
    skipped the same way -- both are transient scan artifacts, not bootstrap's problem.
    """
    entities: list[NeutralEntity] = []
    watermark = Watermark.initial(connector.name, entity_type)
    for _ in range(_MAX_PAGES):
        result = await connector.list_changed(entity_type, watermark)
        for change in result.changes:
            if change.is_delete:
                continue
            try:
                entities.append(await connector.read(change.ref))
            except NotFound:
                continue
        if not result.has_more:
            return entities
        watermark = result.next_watermark
    raise CliError(
        f"{connector.name!r}: listing {entity_type.value!r} for identity bootstrap did not "
        f"finish within {_MAX_PAGES} pages; this looks unbounded",
        exit_code=EXIT_ENDPOINT_UNREACHABLE,
    )


def _parent_path(native_key: str) -> tuple[str, ...]:
    """Every dotted segment of ``native_key`` except the last (decision D1). See the
    module docstring."""
    parts = native_key.split(".")
    return tuple(parts[:-1]) if len(parts) > 1 else ()


def to_catalog_objects(
    entities: Sequence[NeutralEntity], entity_type: EntityType, endpoint: str
) -> list[CatalogObject]:
    """Build one :class:`CatalogObject` per entity that has an identity at ``endpoint``.

    An entity with no identity for ``endpoint`` (should not happen for a `read` result
    from that same connector, but is not this module's contract to assume) is skipped
    rather than crashing bootstrap over one malformed record.
    """
    objects: list[CatalogObject] = []
    for entity in entities:
        ref = entity.identity_for(endpoint)
        if ref is None:
            continue
        name = getattr(entity, "name", None)
        if not isinstance(name, str) or not name:
            continue
        objects.append(
            CatalogObject(
                identity=ref,
                natural_key=NaturalKey(
                    name=name, entity_type=entity_type, parent_path=_parent_path(ref.native_key)
                ),
            )
        )
    return objects
