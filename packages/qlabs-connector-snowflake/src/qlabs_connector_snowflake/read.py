"""Snowflake read path: schema/listing -> ``DataProduct``, table/view -> ``Dataset``.

WP6 / T6.4 (``read()``) and T6.3 (``list_changed()``). Reads one Snowflake object into a
neutral entity with a full provenance sidecar, over the SQL REST API
(``POST /api/v2/statements``, RS-05 section 3.8) -- the "universal fallback" surface,
chosen because the typed resource REST APIs (RS-05 section 3.7) do not cover listings,
shares or tag references at all, and because everything this connector needs is
expressible as ``SELECT``/``SHOW``/``DESCRIBE``. The change feed
(:func:`list_changed_candidates`, T6.3) lands in this same module, as this docstring
originally said it would, and reuses the statement client, identifier quoting, row
helpers and identity rules below unchanged -- see "Change detection" near the end of this
docstring for the surfaces it reads and how it handles ``ACCOUNT_USAGE`` latency.

Content mapping is **not** this module's job. ``comment`` -> ``description``, owner role
-> ``owners``, tag assignments -> ``tags``/``classifications``, ``TABLE_TYPE`` ->
``asset_type``, FQN -> ``physical_ref``, and the listing ``subtitle``/``description``
split all live in ``mapping.py`` (T6.5) and are called from here, exactly as
``qlabs_connector_databricks.read`` delegates to its own ``mapping.py``. This module owns
the SQL, the HTTP, identity, containment, and envelope construction.

## The three native shapes, two neutral entity types

``manifest.py`` (T6.2) declares ``DATA_PRODUCT`` with two identity schemes and
``DATASET`` with one, and this module honors exactly that:

* **``DATASET``** -- a table or view. Read from ``INFORMATION_SCHEMA.TABLES``, falling
  back to ``INFORMATION_SCHEMA.VIEWS``, plus ``INFORMATION_SCHEMA.COLUMNS`` for the
  column list. ``INFORMATION_SCHEMA`` rather than ``ACCOUNT_USAGE`` because RS-05 section
  1.4 is explicit about the trade-off: ``INFORMATION_SCHEMA`` is "real-time... best for
  'read current truth for one database' during a sync scan", while ``ACCOUNT_USAGE``
  carries up to ~2 hours of latency. A ``read(ref)`` of a single named object is exactly
  the "current truth for one database" case, so freshness wins.
* **``DATA_PRODUCT``, schema shape** -- a Snowflake schema, read from
  ``INFORMATION_SCHEMA.SCHEMATA`` with its member tables/views read alongside.
* **``DATA_PRODUCT``, listing shape** -- Snowflake's own "data product" vocabulary
  (RS-05 section 2.1: "a data product is the thing attached to a listing"), read with
  ``SHOW LISTINGS`` + ``DESCRIBE LISTING`` (RS-05 section 3.6).

**Shares are not a fourth shape.** RS-05 section 2.1 puts a share *beneath* a listing as
the access-control substrate the listing wraps, and ``manifest.py`` deliberately declares
no separate share entity. :func:`read_share_composition` therefore exists only to
*enrich* a listing-shaped data product -- it is called when a listing row names a share,
its result is folded into that data product's ``custom_attributes``, and it is never a
read target of its own. A share is never returned as an entity and never gets an
``IdentityRef``.

## Identity: the fully-qualified name is the native key here (unlike Databricks)

RS-05 section 4.3 names the FQN ``DATABASE.SCHEMA.OBJECT`` as "the primary
matching/identity key" for structural objects, and says object ids exist only "in some
``ACCOUNT_USAGE`` views". The Databricks connector makes the opposite choice --
``native_key`` is the immutable ``table_id``/``schema_id``, because Unity Catalog returns
one on every row of every list endpoint. Snowflake does not: ``INFORMATION_SCHEMA.TABLES``
carries no id column at all, so an id-keyed identity would be unavailable on precisely the
surface this module reads. ``native_key`` is therefore the FQN, and
``secondary_keys["object_id"]`` carries the numeric id **only when a caller already has
one** (T6.3's ``ACCOUNT_USAGE``-based change feed is where one would come from).

The consequence is stated plainly rather than hidden: a Snowflake object that is renamed
looks like a different entity to the IdentityMap. That is the honest cost of the surface
Snowflake exposes, not an oversight -- see this task's report.

**Identifier case.** RS-05 section 3.8 warns that identifiers "are case-sensitive and must
match the stored (usually uppercase) identifier case". This module never case-folds an
identifier in either direction: what a caller puts in an ``IdentityRef`` is what reaches
the SQL text and the bind values. A caller holding a lower-cased name for an object stored
upper-cased will get :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`, which is the
truthful answer to "is there an object stored under this exact name".

## Identifier and value safety

Three distinct escaping rules, applied by construction rather than by convention:

* **Identifiers** (database, schema, object, listing, share names appearing as SQL
  identifiers) go through :func:`quote_identifier`, which wraps in double quotes and
  doubles any embedded double quote. They cannot be bind parameters -- SQL has no
  placeholder syntax for an identifier in a ``FROM`` clause.
* **Values** in a ``WHERE`` clause go through the SQL REST API's own ``bindings`` object
  (RS-05 section 3.8: "bind variables are supported via a ``bindings`` object") as ``?``
  placeholders, never spliced into the statement text.
* **Literals that cannot be bound** -- the arguments of the ``TAG_REFERENCES`` table
  function and of ``SHOW ... LIKE`` -- go through :func:`quote_literal`, which
  single-quotes and escapes both ``'`` and ``\\`` (Snowflake honors backslash escapes in
  string constants, so escaping only the quote would leave an injection path open).

All three refuse a value containing a NUL byte, and :func:`quote_identifier` additionally
refuses an empty name and one past Snowflake's 255-character identifier limit -- checked
*before* any HTTP call, so an unsafe name is never concatenated at all.

## Statement execution: synchronous, asynchronous, and paged

:class:`StatementClient` wraps the one endpoint (``POST /api/v2/statements``) and handles
every shape RS-05 section 3.8 describes:

* a **synchronous 200** whose body already carries ``data`` and ``resultSetMetaData``;
* an **asynchronous 202** carrying only a ``statementHandle``, polled at
  ``GET /api/v2/statements/{handle}`` until it answers 200, with exponential backoff
  through the SDK's :class:`~qlabs_catalog_sync_sdk.config.Clock` (so a test injects a
  :class:`~qlabs_catalog_sync_sdk.config.ManualClock` and asserts the wait schedule with
  no real sleeping) and a bound on the number of attempts. Exceeding the bound raises
  :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`: a warehouse still resuming
  is exactly the kind of failure a later attempt may not repeat;
* a **paged result set**, continued through ``resultSetMetaData.partitionInfo`` by
  refetching the same handle with ``?partition=N``, bounded defensively the way
  ``qlabs_connector_databricks.read._paginate_defensively`` bounds REST paging;
* ``requestId`` on every submission, per RS-05 section 4.4's idempotent-retry note. Note
  that :class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint` performs its own 429/5xx retry
  internally by replaying the identical URL, so a retried submission necessarily carries
  the same ``requestId``. Every statement this module issues is a read
  (``SELECT``/``SHOW``/``DESCRIBE``), so duplicate execution would be harmless in any
  case -- RS-05's idempotency concern is about resubmitted DDL, which a read-only
  connector never sends;
* **errors** mapped through :func:`~qlabs_connector_snowflake.auth.translate_snowflake_error`
  -- 401/403 -> ``AuthError``, 404 -> ``NotFound``, 429/5xx/transport -> ``TransientError``,
  any other 4xx -> ``CapabilityError``. No per-connector exception hierarchy is invented
  (agent-guide convention); the one non-SDK exception here, :class:`IdentifierError`,
  guards an internal invariant before any request exists, exactly as
  ``qlabs_connector_databricks.sql_tags.IdentifierError`` does.

## Enrichment degrades, the entity itself does not

Two reads are *enrichment* rather than part of the entity, and both degrade to "not read"
instead of failing the whole ``read()`` when the connector's role lacks the privilege:

* the member-dataset tag index for a schema-shaped data product, which comes from the
  separately-granted ``SNOWFLAKE.ACCOUNT_USAGE`` share (RS-05 section 1.4), and
* a listing's share composition (RS-05 section 3.5), which needs privileges on the share.

Both surface as ``tags``/``share_composition`` being **absent** rather than empty, which
``mapping.py`` and ``envelope.py`` rule 10 already give a precise meaning: absent means
"this connector has nothing to say", so the engine leaves the target's copy alone. An
empty list would have claimed "there are none", which would be a lie. The primary object's
own tag read is *not* degraded this way -- it is part of the entity, and a failure there
is a failure of the read.

## Change detection (T6.3): ``ACCOUNT_USAGE``, and the lag that comes with it

:func:`list_changed_candidates` answers "what changed since this watermark" for the two
entity types ``manifest.py`` declares -- ``DATASET`` (tables/views) and ``DATA_PRODUCT``
(schemas *and* listings, the two native shapes described above, both on the one stream).

**The surface is ``SNOWFLAKE.ACCOUNT_USAGE``, deliberately, and it lags.** RS-05 section
1.4 sets out the trade-off and section 4.4 states the conclusion outright: "Use
``INFORMATION_SCHEMA`` for fresh single-database reads and ``ACCOUNT_USAGE`` for
account-wide discovery and change detection, accounting for ``ACCOUNT_USAGE`` latency."
``INFORMATION_SCHEMA`` cannot do this job at all: it is **per-database** (a poll would
have to enumerate databases and issue one statement per database, and would still miss a
database created since the last poll) and it carries **no record of a dropped object**, so
a delete would be invisible. ``ACCOUNT_USAGE`` is account-wide in one statement, keeps
dropped objects with a ``DELETED`` timestamp, and carries the numeric object ids RS-05
section 4.3 calls "useful for detecting renames". The price is latency: RS-05 documents
"typically up to ~2 hours for many views", and some views are worse -- this module assumes
:data:`ACCOUNT_USAGE_LAG` (3 hours) rather than the typical figure.

**The lag is the correctness problem, and it is handled explicitly, not implicitly.** A
watermark advanced to "now" against a lagging view silently drops every change that had
not yet propagated into the view when the poll ran -- those rows were simply not there to
be seen, and a later poll starting at "now" would never look back for them. Two mechanisms
together close that hole:

1. **The proposed watermark is held back.** The high-water mark this module returns is
   ``snowflake_now - safety_margin`` (:data:`DEFAULT_WATERMARK_SAFETY_MARGIN`, equal to
   :data:`ACCOUNT_USAGE_LAG`), never "now". The watermark therefore only ever claims
   coverage of a region that has certainly finished propagating.
2. **The next poll re-scans an overlap on top of that.** The query's lower bound is
   ``high_water - rescan_overlap`` (:data:`DEFAULT_RESCAN_OVERLAP`), which absorbs
   boundary rounding and any residual skew.

Formally: a change with event time ``T`` becomes visible at ``T + L`` for some real lag
``L``. Let ``P`` be the first poll at or after ``T + L`` and ``P_prev`` its predecessor
(so ``P_prev < T + L``). ``P``'s lower bound is
``P_prev - margin - overlap < T + (L - margin) - overlap <= T`` whenever ``L <= margin``.
The change is inside ``P``'s window and is reported. The guarantee is exactly as good as
the margin: if a tenant's real lag ever exceeded :data:`ACCOUNT_USAGE_LAG`, changes could
still be lost, which is why the margin is a named, overridable constant rather than a
number buried in a query.

**Re-delivery is harmless by construction.** Everything the overlap re-scans is compared
against a per-object checksum carried in the watermark, so an object whose row has not
actually moved is not re-reported -- the overlap costs a slightly wider query, not a
storm of duplicate candidates. (The engine tolerates a candidate reported twice; it must
never miss one. This design pays the cheap side of that trade deliberately.)

**"Now" comes from Snowflake, not from this process.** One extra statement per poll,
``SELECT CURRENT_TIMESTAMP()``, anchors the margin arithmetic in the same clock that
stamped ``LAST_ALTERED``/``DELETED``, so host clock skew cannot eat into the margin. If
that value cannot be parsed the module falls back to :attr:`StatementClient.clock` with a
warning rather than failing the poll.

**The watermark is a ``CURSOR``, not a ``TIMESTAMP``.** ``Watermark`` refuses to carry a
timestamp and a cursor at once, and this connector needs both: the high-water instant
*and* a per-object checksum census (which is also the only way listings, whose ``SHOW``
surface exposes no modified-time and no delete record, can be diffed at all). The cursor
payload is opaque JSON meaningful only to this module --
``{"v", "high_water", "objects", "listings"}`` -- exactly what
:attr:`~qlabs_catalog_sync_sdk.contract.WatermarkKind.CURSOR` is documented for, and the
same choice ``qlabs_connector_databricks.changes`` makes. The consequence is that
``Watermark.is_after`` cannot order two of these (it returns ``False`` for cursor pairs by
design); "never moves backwards" is therefore an invariant this module enforces on the
encoded ``high_water`` itself, and proves as a test.

**What each change kind means here, per entity type:**

* ``DATASET`` -- scanned from ``SNOWFLAKE.ACCOUNT_USAGE.TABLES``. Creates/updates come
  from ``CREATED``/``LAST_ALTERED``; **deletes come from the ``DELETED`` column**, so they
  are genuinely covered. Views are expected to appear in ``TABLES`` with
  ``TABLE_TYPE = 'VIEW'``; ``ACCOUNT_USAGE.VIEWS`` is deliberately *not* also scanned (it
  would double the statement count to re-report the same objects, and its ``TABLE_ID`` /
  ``DELETED`` columns are undocumented in RS-05). If a tenant turns out to report some
  view kind only in ``VIEWS``, that kind is invisible to this change feed -- stated
  plainly rather than assumed away.
* ``DATA_PRODUCT``, schema shape -- scanned from ``SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA``,
  with the same ``DELETED``-column delete coverage.
* ``DATA_PRODUCT``, listing shape -- scanned with ``SHOW LISTINGS``, which has **no**
  modified-time column and **no** delete record. Both are therefore derived from the
  checksum census instead: a listing whose row changed is an update, and a listing that
  has disappeared from a complete ``SHOW LISTINGS`` is a delete. That is a genuine
  difference in provenance from the two structural shapes, not an equivalent guarantee.
  ``SHOW LISTINGS`` needs listing privileges a read-only sync role may not hold, so an
  ``AuthError``/``CapabilityError`` there degrades to "listings not scanned this cycle"
  (previous listing census preserved, no phantom deletes) rather than taking the schema
  half of the same stream down with it.

Decision D4 / the v1 guardrail says the engine never deletes in Qlik and reports orphans
instead; that does not license the connector to under-report, so a delete it can see is
always reported as :attr:`~qlabs_catalog_sync_sdk.contract.ChangeKind.DELETED`.

**Renames.** Identity here is the FQN (see "Identity" above), so a rename moves the
native key. ``ACCOUNT_USAGE`` object ids are what make it detectable anyway: the census
remembers ``object_id -> native_key``, so when a familiar id turns up under a new FQN this
module knows it is one object that moved rather than two unrelated events. How that is
then *reported* differs by entity type, and only because of what ``manifest.py`` declares:

* ``DATASET`` declares ``object_id`` as an identity key, so the id can travel in the
  ``ChangeRef``'s ``secondary_keys`` and the engine can follow the identity itself. The
  rename is reported as **one** ``UPDATED`` on the new FQN, and the phantom ``DELETED``
  for the old name is suppressed -- the object is not deleted, and saying so would be the
  false-orphan bug ``qlabs_connector_databricks.changes`` keys on stable ids to avoid.
* ``DATA_PRODUCT`` declares only ``fully_qualified_name`` / ``listing_global_name``, with
  no id key, so ``SCHEMA_ID`` cannot legitimately travel in the ref. Reporting only the
  new FQN would leave the old one to rot silently in the engine's IdentityMap, so a schema
  rename is reported honestly as ``CREATED`` of the new FQN **plus** ``DELETED`` of the
  old one -- which is what T6.4 already documented as the cost of FQN identity, now at
  least detected and correlated (the pairing is logged) instead of half-missed.
* A **listing** rename is the easy case: the local name may change but ``global_name`` --
  the native key -- does not, so it is one plain ``UPDATED`` with no delete at all.

Where a tenant returns no id for an object kind, rename detection for that kind degrades
to "new FQN reported, old FQN left in the census"; nothing silently claims otherwise.

**Pagination and exhaustion.** One call is one complete traversal, so ``has_more`` is
always ``False`` and :attr:`~qlabs_catalog_sync_sdk.contract.ListChangedResult.is_exhausted`
is truthfully ``True``: :class:`StatementClient` already drains every
``resultSetMetaData.partitionInfo`` partition before returning, and its
:data:`DEFAULT_MAX_PARTITIONS` backstop raises
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` rather than silently truncating
-- a partial traversal must never be mistaken for a complete one, because the census and
its delete detection depend on having actually seen everything the query matched.

**The gap this scan does not close, stated rather than papered over:** a tag assignment
does not move an object's ``LAST_ALTERED`` (RS-05 section 3.4 -- tag assignments live in
``TAG_REFERENCES``, a separate surface), so a tag-only edit is not caught by this poll. It
is caught whenever ``read()`` hydrates the object for another reason. This is the same
documented gap ``qlabs_connector_databricks.changes`` carries, for the same reason.

## TENANT-UNVERIFIED assumptions

There is no live Snowflake tenant for this build (agent guide, "Escalation"). Everything
below is built against ``respx`` mocks and RS-05's documentation; these specific points
need a real tenant to confirm and are listed in this task's report:

1. The ``/api/v2/statements`` response envelope: ``resultSetMetaData.rowType[].name``,
   ``resultSetMetaData.partitionInfo``, ``data`` as an array of arrays of strings,
   ``statementHandle``, and 202 as the "still running" status code.
2. The ``bindings`` request shape ``{"1": {"type": "TEXT", "value": ...}}`` with ``?``
   placeholders, and that bind values are accepted in the ``WHERE`` clauses used here.
3. ``SHOW LISTINGS`` / ``DESCRIBE LISTING`` column names, their lower-cased spelling, and
   whether ``DESCRIBE LISTING`` flattens the listing manifest into columns
   (``title``/``subtitle``/``description``/``categories``/...) or returns only the raw
   YAML. This module reads the flattened columns when present and round-trips whatever
   else comes back; it deliberately does **not** parse YAML (that would need a dependency
   this connector is not permitted to add).
4. ``DESCRIBE SHARE`` row shape (``kind``/``name``).
5. ``INFORMATION_SCHEMA.TAG_REFERENCES`` / ``ACCOUNT_USAGE.TAG_REFERENCES`` column names.
   RS-05 section 3.4 names ``TAG_NAME``, ``TAG_VALUE``, ``OBJECT_DATABASE``,
   ``OBJECT_SCHEMA``, ``OBJECT_NAME``, ``DOMAIN`` and ``COLUMN_NAME`` but not
   ``TAG_DATABASE``/``TAG_SCHEMA``, which this module also selects because the tag's own
   FQN is its identity (RS-05 section 4.3) and system-classification detection needs the
   ``SNOWFLAKE.CORE`` namespace.
6. Which ``ACCOUNT_USAGE``/``INFORMATION_SCHEMA`` view exposes an object id per object
   kind. ``ACCOUNT_USAGE.TABLES.TABLE_ID`` is the documented one; no
   ``INFORMATION_SCHEMA`` view this module reads carries any id, which is why identity is
   FQN-keyed (see "Identity" above).
7. The exact ``INFORMATION_SCHEMA.TABLES.TABLE_TYPE`` string vocabulary, and whether
   materialized/dynamic/Iceberg tables appear in ``TABLES``, in ``VIEWS``, or in both.
8. That a *failed* statement surfaces as a non-2xx HTTP status (so
   ``translate_snowflake_error`` sees it) rather than as a 200 carrying an error body.
9. Everything the change feed reads from ``SNOWFLAKE.ACCOUNT_USAGE`` (T6.3): that
   ``TABLES`` carries ``TABLE_ID``/``DELETED`` and that ``SCHEMATA`` carries
   ``SCHEMA_ID``/``DELETED``/``IS_MANAGED_ACCESS`` under those exact names; that views
   appear in ``TABLES`` with ``TABLE_TYPE = 'VIEW'``; and that ``DELETED`` is ``NULL``
   for a live object. RS-05 names ``TABLES.TABLE_ID`` and says ``ACCOUNT_USAGE`` covers
   "dropped objects" but does not enumerate these columns per view.
10. The **actual** ``ACCOUNT_USAGE`` latency for this account. RS-05 says "typically up
    to ~2 hours for many views"; :data:`ACCOUNT_USAGE_LAG` assumes 3 hours. The safety
    margin is only as good as this number, so it is the single most important thing to
    confirm against a real tenant.
11. That ``TO_TIMESTAMP_TZ(?)`` accepts an ISO-8601 string with an explicit offset as a
    ``TEXT`` bind and compares correctly against ``LAST_ALTERED``/``DELETED``, and that
    ``SELECT CURRENT_TIMESTAMP()`` renders in one of the two encodings
    :func:`_parse_snowflake_timestamp` accepts.
12. ``SHOW LISTINGS`` change semantics: whether the row a listing edit produces actually
    differs (so the checksum moves), whether an unpublished/dropped listing disappears
    from the output or merely changes ``state``, and whether any ``updated_on``-style
    column is present. This module treats disappearance as a delete and a changed row as
    an update; neither is confirmed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import httpx
import structlog

from qlabs_catalog_sync_sdk.config import Clock, SystemClock
from qlabs_catalog_sync_sdk.contract import (
    ChangeKind,
    ChangeRef,
    ListChangedResult,
    Watermark,
    WatermarkKind,
)
from qlabs_catalog_sync_sdk.envelope import build_field_envelopes, compute_checksum
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConnectorError,
    NotFound,
    TransientError,
)
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
    NeutralEntity,
)

from .auth import translate_snowflake_error
from .mapping import (
    MAPPED_LISTING_FIELDS,
    MAPPED_SOURCE_FIELDS,
    TagReference,
    asset_type_for_object,
    lookup,
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_listing_fields,
    tag_reference_from_row,
)

__all__ = [
    "ACCOUNT_USAGE_LAG",
    "COLUMNS_KEY",
    "CURRENT_TIMESTAMP_STATEMENT",
    "DEFAULT_MAX_PARTITIONS",
    "DEFAULT_MAX_POLL_ATTEMPTS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_RESCAN_OVERLAP",
    "DEFAULT_STATEMENT_TIMEOUT_SECONDS",
    "DEFAULT_WATERMARK_SAFETY_MARGIN",
    "MAX_IDENTIFIER_LENGTH",
    "SHARE_COMPOSITION_KEY",
    "SNAPSHOT_FORMAT_VERSION",
    "STATEMENTS_PATH",
    "DataProductRead",
    "DataProductShape",
    "IdentifierError",
    "StatementClient",
    "StatementResult",
    "build_dataset",
    "build_listing_data_product",
    "build_schema_data_product",
    "data_product_shape",
    "list_changed_candidates",
    "quote_identifier",
    "quote_literal",
    "quote_qualified_name",
    "read_dataset",
    "read_entity",
    "read_listing",
    "read_object_tag_references",
    "read_schema",
    "read_share_composition",
    "split_object_name",
    "split_schema_name",
]

_logger = structlog.get_logger("qlabs_connector_snowflake.read")

STATEMENTS_PATH: Final[str] = "/api/v2/statements"
"""RS-05 section 3.8's SQL REST API. The only Snowflake endpoint this module calls."""

DEFAULT_STATEMENT_TIMEOUT_SECONDS: Final[int] = 60
"""Server-side statement timeout sent in every request body (RS-05 section 3.8's example
uses the same value). Bounds how long Snowflake itself will run one metadata query."""

DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 1.0
"""First wait before re-checking an asynchronous (202) statement. Subsequent waits back
off exponentially, capped at :data:`MAX_POLL_INTERVAL_SECONDS`."""

MAX_POLL_INTERVAL_SECONDS: Final[float] = 8.0
"""Ceiling on the exponential poll backoff, so a slow statement is still checked
regularly instead of the wait growing without bound."""

DEFAULT_MAX_POLL_ATTEMPTS: Final[int] = 30
"""Bound on asynchronous polling, by attempt count rather than wall clock: an attempt
count is exact and trivially assertable under a ``ManualClock``, where a deadline would
make a test reason about elapsed time. Mirrors ``qlabs_connector_databricks.sql_tags``."""

DEFAULT_MAX_PARTITIONS: Final[int] = 100
"""Safety backstop on result-set partitions, not a business limit -- a metadata query
returning more than this many partitions means something is wrong, and failing loudly
beats paging forever."""

MAX_IDENTIFIER_LENGTH: Final[int] = 255
"""Snowflake's identifier length limit. Enforced by :func:`quote_identifier` as a
defense-in-depth bound, before any HTTP call."""

COLUMNS_KEY: Final[str] = "COLUMNS"
"""Key under which :func:`read_dataset` folds the object's column list into the raw row
before mapping. Snowflake returns columns from a *separate* ``INFORMATION_SCHEMA.COLUMNS``
query, not inside the ``TABLES`` row, so this one assembly step is explicit here rather
than looking like something Snowflake itself returned. Everything after the assembly is
pure passthrough (``mapping.map_custom_attributes`` only ever removes keys)."""

SHARE_COMPOSITION_KEY: Final[str] = "share_composition"
"""Key under which :func:`read_listing` folds a listing's share composition (RS-05
section 3.5) into the raw listing row. Same explicit-assembly note as
:data:`COLUMNS_KEY`; lower-cased to match the ``SHOW``/``DESCRIBE`` surface's own column
spelling."""


# ----------------------------------------------------------------------------------
# Identifier, literal and bind-value safety
# ----------------------------------------------------------------------------------


class IdentifierError(ValueError):
    """A name failed validation before any HTTP call was made.

    Deliberately **not** part of the SDK's
    :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` hierarchy, matching
    ``qlabs_connector_databricks.sql_tags.IdentifierError``: it guards an internal
    invariant (a name this module is about to put into SQL text must be provably safe)
    rather than reporting an operational failure against the endpoint. There is no
    request to retry and nothing was sent, so there is no ``retryable`` verdict to give.
    """


def quote_identifier(name: str) -> str:
    """One SQL identifier, double-quoted with any embedded ``"`` doubled.

    Raises :class:`IdentifierError` for an empty name, a name containing a NUL byte, or
    one past :data:`MAX_IDENTIFIER_LENGTH` -- all checked before the value is used, so an
    unsafe name is refused rather than concatenated (see the module docstring's
    "Identifier and value safety").

    Quoting makes the identifier case-**sensitive**, which is deliberate: Snowflake stores
    unquoted identifiers upper-cased, and RS-05 section 3.8 warns that identifiers must
    match the stored case. This module never guesses at case on the caller's behalf.
    """
    if not isinstance(name, str) or not name:
        raise IdentifierError("a Snowflake identifier must be a non-empty string")
    if "\x00" in name:
        raise IdentifierError("a Snowflake identifier must not contain a NUL byte")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise IdentifierError(
            f"identifier is {len(name)} characters, past Snowflake's "
            f"{MAX_IDENTIFIER_LENGTH}-character limit; refusing to interpolate it"
        )
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def quote_qualified_name(*parts: str) -> str:
    """A dotted, fully-quoted qualified name, e.g. ``"SALES_DB"."PUBLIC"."ORDERS"``.

    Every part is quoted independently by :func:`quote_identifier`, so a dot inside one
    part can never split it into two namespace levels.
    """
    if not parts:
        raise IdentifierError("a qualified name needs at least one part")
    return ".".join(quote_identifier(part) for part in parts)


def quote_literal(value: str) -> str:
    """One SQL string literal, single-quoted with ``'`` and ``\\`` escaped.

    Used only where a bind parameter is not available -- the ``TAG_REFERENCES`` table
    function's arguments and ``SHOW ... LIKE``'s pattern. Backslash is escaped as well as
    the quote because Snowflake honors backslash escapes inside string constants, so
    escaping only ``'`` would leave ``\\'`` as an injection path.
    """
    if not isinstance(value, str):
        raise IdentifierError("a SQL literal must be a string")
    if "\x00" in value:
        raise IdentifierError("a SQL literal must not contain a NUL byte")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def _bindings(values: Sequence[str]) -> dict[str, dict[str, str]]:
    """The SQL REST API's ``bindings`` object for positional ``?`` placeholders.

    RS-05 section 3.8 documents bind variables as the supported way to pass *values*;
    they are 1-indexed and keyed by their position as a string.
    TENANT-UNVERIFIED (module docstring assumption 2): the exact object shape.
    """
    return {str(index): {"type": "TEXT", "value": value} for index, value in enumerate(values, 1)}


# ----------------------------------------------------------------------------------
# Statement execution
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementResult:
    """One statement's materialized result set.

    ``columns`` comes from ``resultSetMetaData.rowType[].name`` (the column descriptors),
    so :meth:`mappings` can key each row by the name Snowflake itself reported -- upper
    case for ``INFORMATION_SCHEMA``, lower case for ``SHOW``/``DESCRIBE``. ``mapping.py``
    matches keys case-insensitively precisely so both spellings work unchanged.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    statement_handle: str | None = None

    def mappings(self) -> list[dict[str, Any]]:
        """Every row as a ``{column_name: value}`` dict, in result order.

        Raises :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` when a row is
        narrower than the column descriptors -- that means the response shape is not what
        this module assumes (module docstring assumption 1), and saying so plainly beats
        an ``IndexError`` the engine has no handling for or a silently truncated row.
        """
        width = len(self.columns)
        result: list[dict[str, Any]] = []
        for row in self.rows:
            if len(row) < width:
                raise ConnectorError(
                    f"a Snowflake result row has {len(row)} value(s) but the result set "
                    f"declares {width} column(s); the assumed /api/v2/statements response "
                    f"shape does not match this account"
                )
            result.append(dict(zip(self.columns, row, strict=False)))
        return result

    def first_mapping(self) -> dict[str, Any] | None:
        """The first row as a dict, or ``None`` when the statement returned no rows."""
        mappings = self.mappings()
        return mappings[0] if mappings else None


@dataclass(slots=True)
class StatementClient:
    """Executes one SQL statement at a time against ``POST /api/v2/statements``.

    Holds no connection of its own -- it borrows the connector's already-authenticated
    :class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint`, which is what makes every path
    here ``respx``-mockable (the ``snowflake-connector-python`` driver's own transport is
    not, which is why ``auth.py`` uses that package only for the key fingerprint).

    ``role`` and ``warehouse`` come from
    :class:`~qlabs_connector_snowflake.auth.SnowflakeConfig` and are sent on every
    statement when configured (RS-05 section 3.8's request body). Both are optional: the
    metadata queries this module issues are served by the cloud-services layer, so a
    deployment without a warehouse still reads successfully.
    """

    http: HttpEndpoint
    endpoint: str
    role: str | None = None
    warehouse: str | None = None
    clock: Clock = field(default_factory=SystemClock)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_poll_interval_seconds: float = MAX_POLL_INTERVAL_SECONDS
    max_poll_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS
    max_partitions: int = DEFAULT_MAX_PARTITIONS
    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS
    request_id_factory: Callable[[], str] = field(default=lambda: str(uuid.uuid4()))

    async def execute(
        self,
        statement: str,
        *,
        database: str | None = None,
        schema: str | None = None,
        bind_values: Sequence[str] | None = None,
        entity_type: EntityType | None = None,
    ) -> StatementResult:
        """Run ``statement`` and return its full, materialized result set.

        Handles the synchronous 200, the asynchronous 202-plus-polling and the paged
        (``partitionInfo``) shapes uniformly, so callers only ever see rows. See the
        module docstring's "Statement execution" section for the whole contract.

        ``bind_values`` are positional ``?`` placeholders (see :func:`_bindings`);
        identifiers must already be quoted into ``statement`` by :func:`quote_identifier`.
        """
        body: dict[str, Any] = {
            "statement": statement,
            "timeout": self.statement_timeout_seconds,
        }
        if database is not None:
            body["database"] = database
        if schema is not None:
            body["schema"] = schema
        if self.warehouse is not None:
            body["warehouse"] = self.warehouse
        if self.role is not None:
            body["role"] = self.role
        if bind_values:
            body["bindings"] = _bindings(bind_values)

        payload, status = await self._request(
            "POST",
            STATEMENTS_PATH,
            entity_type=entity_type,
            json=body,
            params={"requestId": self.request_id_factory()},
        )
        handle = _statement_handle(payload)
        payload = await self._await_completion(
            payload, status, handle=handle, entity_type=entity_type
        )
        return await self._collect(payload, handle=handle, entity_type=entity_type)

    async def _await_completion(
        self,
        payload: dict[str, Any],
        status: int,
        *,
        handle: str | None,
        entity_type: EntityType | None,
    ) -> dict[str, Any]:
        """Poll a 202-accepted statement until it answers 200, or give up loudly."""
        attempts = 0
        while status == httpx.codes.ACCEPTED:
            if handle is None:
                raise ConnectorError(
                    "Snowflake accepted the statement asynchronously (HTTP 202) but the "
                    "response carried no statementHandle to poll",
                    endpoint=self.endpoint,
                    entity_type=entity_type,
                )
            attempts += 1
            if attempts > self.max_poll_attempts:
                raise TransientError(
                    f"statement {handle} did not complete within {self.max_poll_attempts} "
                    f"poll attempts",
                    endpoint=self.endpoint,
                    entity_type=entity_type,
                )
            await self.clock.sleep(self._poll_delay(attempts))
            payload, status = await self._request(
                "GET", f"{STATEMENTS_PATH}/{handle}", entity_type=entity_type
            )
        return payload

    def _poll_delay(self, attempt: int) -> float:
        """Exponential backoff for poll ``attempt`` (1-based), capped."""
        delay = self.poll_interval_seconds * (2.0 ** (attempt - 1))
        return min(delay, self.max_poll_interval_seconds)

    async def _collect(
        self, payload: dict[str, Any], *, handle: str | None, entity_type: EntityType | None
    ) -> StatementResult:
        """Materialize every partition of a completed statement's result set."""
        columns = _column_names(payload)
        rows: list[tuple[Any, ...]] = [tuple(row) for row in _data_rows(payload)]
        partitions = _partition_count(payload)
        if partitions > self.max_partitions:
            raise TransientError(
                f"statement result declares {partitions} partitions, past the defensive "
                f"bound of {self.max_partitions}; refusing to page further",
                endpoint=self.endpoint,
                entity_type=entity_type,
            )
        for index in range(1, partitions):
            if handle is None:
                raise ConnectorError(
                    "the result set is partitioned but the response carried no "
                    "statementHandle to fetch the remaining partitions with",
                    endpoint=self.endpoint,
                    entity_type=entity_type,
                )
            chunk, _status = await self._request(
                "GET",
                f"{STATEMENTS_PATH}/{handle}",
                entity_type=entity_type,
                params={"partition": index},
            )
            rows.extend(tuple(row) for row in _data_rows(chunk))
        return StatementResult(columns=columns, rows=tuple(rows), statement_handle=handle)

    async def _request(
        self, method: str, url: str, *, entity_type: EntityType | None, **kwargs: Any
    ) -> tuple[dict[str, Any], int]:
        try:
            response = await self.http.request(method, url, **kwargs)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            raise translate_snowflake_error(
                exc, endpoint=self.endpoint, entity_type=entity_type
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorError(
                f"Snowflake returned a non-JSON body for {method} {url}",
                endpoint=self.endpoint,
                entity_type=entity_type,
                cause=exc,
            ) from exc
        if not isinstance(payload, dict):
            raise ConnectorError(
                f"Snowflake returned a {type(payload).__name__} body for {method} {url}; "
                "the assumed /api/v2/statements response shape is an object",
                endpoint=self.endpoint,
                entity_type=entity_type,
            )
        return payload, response.status_code


def _statement_handle(payload: Mapping[str, Any]) -> str | None:
    handle = payload.get("statementHandle")
    return handle if isinstance(handle, str) and handle else None


def _result_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = payload.get("resultSetMetaData")
    return metadata if isinstance(metadata, Mapping) else {}


def _column_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Column descriptors from ``resultSetMetaData.rowType`` (module docstring, 1)."""
    row_type = _result_metadata(payload).get("rowType")
    if not isinstance(row_type, Sequence) or isinstance(row_type, str | bytes):
        return ()
    names: list[str] = []
    for descriptor in row_type:
        if isinstance(descriptor, Mapping):
            name = descriptor.get("name")
            names.append(name if isinstance(name, str) else "")
        else:
            names.append("")
    return tuple(names)


def _data_rows(payload: Mapping[str, Any]) -> list[list[Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [list(row) if isinstance(row, list) else [row] for row in data]


def _partition_count(payload: Mapping[str, Any]) -> int:
    """How many partitions the result set has; at least one whenever it has any rows."""
    partition_info = _result_metadata(payload).get("partitionInfo")
    if isinstance(partition_info, list) and partition_info:
        return len(partition_info)
    return 1


# ----------------------------------------------------------------------------------
# Column projections (TENANT-UNVERIFIED: module docstring assumptions 5 and 7)
# ----------------------------------------------------------------------------------

_TABLES_COLUMNS: Final[tuple[str, ...]] = (
    "TABLE_CATALOG",
    "TABLE_SCHEMA",
    "TABLE_NAME",
    "TABLE_OWNER",
    "TABLE_TYPE",
    "IS_TRANSIENT",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

_VIEWS_COLUMNS: Final[tuple[str, ...]] = (
    "TABLE_CATALOG",
    "TABLE_SCHEMA",
    "TABLE_NAME",
    "TABLE_OWNER",
    "IS_SECURE",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

_COLUMNS_COLUMNS: Final[tuple[str, ...]] = (
    "COLUMN_NAME",
    "ORDINAL_POSITION",
    "DATA_TYPE",
    "IS_NULLABLE",
    "COMMENT",
)

_SCHEMATA_COLUMNS: Final[tuple[str, ...]] = (
    "CATALOG_NAME",
    "SCHEMA_NAME",
    "SCHEMA_OWNER",
    "IS_TRANSIENT",
    "IS_MANAGED_ACCESS",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

_TAG_REFERENCE_COLUMNS: Final[tuple[str, ...]] = (
    "TAG_DATABASE",
    "TAG_SCHEMA",
    "TAG_NAME",
    "TAG_VALUE",
    "DOMAIN",
    "OBJECT_DATABASE",
    "OBJECT_SCHEMA",
    "OBJECT_NAME",
    "COLUMN_NAME",
)

#: Identity columns consumed into ``IdentityRef``/``name``/``physical_ref``, plus the
#: content columns ``mapping.py`` promotes -- excluded from the ``custom_attributes``
#: passthrough so the same fact never appears in two places with no authority between
#: them. ``TABLE_TYPE``/``IS_TRANSIENT``/``IS_SECURE`` deliberately stay in the
#: passthrough: the neutral ``asset_type`` is a lossy collapse of them (RS-05 section
#: 1.2's six table kinds and four view kinds map onto ``TABLE``/``VIEW``), so the native
#: kind must survive somewhere.
_OBJECT_STRUCTURAL_FIELDS: Final[frozenset[str]] = (
    frozenset({"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"}) | MAPPED_SOURCE_FIELDS
)

_SCHEMA_STRUCTURAL_FIELDS: Final[frozenset[str]] = (
    frozenset({"CATALOG_NAME", "SCHEMA_NAME"}) | MAPPED_SOURCE_FIELDS
)

#: The listing's own identity plus the columns promoted to ``name``/``description``/
#: ``documentation``. ``comment`` is **not** excluded here (unlike the two sets above,
#: which fold in :data:`~qlabs_connector_snowflake.mapping.MAPPED_SOURCE_FIELDS`): a
#: listing's ``comment`` column is a separate field from its ``subtitle``/``description``
#: and nothing maps it, so dropping it would lose it outright.
_LISTING_STRUCTURAL_FIELDS: Final[frozenset[str]] = (
    frozenset({"global_name", "name"}) | MAPPED_LISTING_FIELDS
)


# ----------------------------------------------------------------------------------
# Name parsing
# ----------------------------------------------------------------------------------


def _split_name(name: str, *, expected: int, shape: str) -> tuple[str, ...]:
    parts = tuple(name.split("."))
    if len(parts) != expected or not all(parts):
        raise ValueError(f"expected a {shape!r} Snowflake name, got {name!r}")
    return parts


def split_schema_name(name: str) -> tuple[str, str]:
    """Split ``DATABASE.SCHEMA`` into its two parts.

    Raises :class:`ValueError` for anything else, including a three-part object name --
    which must never be read as a schema's. A quoted identifier that itself contains a
    dot is not supported here; RS-05 gives no way to tell one apart from a separator in a
    flat string, and this module refuses rather than guessing (TENANT-UNVERIFIED).
    """
    database, schema = _split_name(name, expected=2, shape="DATABASE.SCHEMA")
    return database, schema


def split_object_name(name: str) -> tuple[str, str, str]:
    """Split ``DATABASE.SCHEMA.OBJECT`` into its three parts (RS-05 section 1.2)."""
    database, schema, obj = _split_name(name, expected=3, shape="DATABASE.SCHEMA.OBJECT")
    return database, schema, obj


class DataProductShape(StrEnum):
    """Which of the two native shapes a ``DATA_PRODUCT`` ref names (``manifest.py``)."""

    SCHEMA = "schema"
    LISTING = "listing"


def data_product_shape(ref: IdentityRef) -> DataProductShape:
    """Decide whether a ``DATA_PRODUCT`` ref names a schema or a listing.

    ``manifest.py`` declares ``identity_keys = ["fully_qualified_name",
    "listing_global_name"]`` and notes the two are mutually exclusive per instance, so the
    ref itself is what says which shape it is:

    * an explicit ``listing_global_name``/``listing_name`` secondary key means a listing;
    * otherwise a ``native_key`` of exactly two dotted parts is a schema's
      ``DATABASE.SCHEMA``;
    * anything else is a listing global name, which RS-05 section 2.4 describes as a
      single opaque, globally-unique token with no internal structure to test for.

    A three-part name raises :class:`ValueError`: that is a table/view FQN, and reading it
    as a data product would silently produce the wrong entity type.
    """
    if "listing_global_name" in ref.secondary_keys or "listing_name" in ref.secondary_keys:
        return DataProductShape.LISTING
    parts = ref.native_key.split(".")
    if len(parts) == 2 and all(parts):
        return DataProductShape.SCHEMA
    if len(parts) == 3:
        raise ValueError(
            f"{ref.native_key!r} is a three-part object name, not a data product; "
            "read it with an EntityType.DATASET ref instead"
        )
    return DataProductShape.LISTING


def _require_entity_type(ref: IdentityRef, expected: EntityType) -> None:
    if ref.entity_type is not expected:
        raise ValueError(f"expected a {expected!r} ref, got {ref.entity_type!r}")


# ----------------------------------------------------------------------------------
# Timestamps
# ----------------------------------------------------------------------------------


def _parse_snowflake_timestamp(value: object) -> datetime | None:
    """A Snowflake timestamp -> aware UTC ``datetime``, or ``None`` if unparseable.

    Best-effort and forgiving on purpose, mirroring
    ``qlabs_connector_databricks.read._parse_databricks_timestamp``: an unparseable
    ``LAST_ALTERED`` costs only the envelope's ``last_modified_at`` provenance hint, never
    the read itself -- change detection here is snapshot + checksum regardless
    (``manifest.py``: ``ConcurrencyMode.NONE``).

    Both encodings RS-05 leaves open are accepted (TENANT-UNVERIFIED, module docstring
    assumption 1): an epoch-seconds number or numeric string, which is what the SQL REST
    API's JSON result format uses for timestamp columns, and an ISO-8601 string with an
    explicit offset.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _from_epoch_seconds(float(value))
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _from_epoch_seconds(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _from_epoch_seconds(seconds: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# ----------------------------------------------------------------------------------
# Raw row -> neutral entity
# ----------------------------------------------------------------------------------


def _require_text(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        _, value = lookup(raw, key)
        if isinstance(value, str) and value:
            return value
    raise ConnectorError(
        f"Snowflake row is missing required column(s) {keys!r}: {sorted(raw)!r}",
    )


def _optional_text(raw: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        _, value = lookup(raw, key)
        if isinstance(value, str) and value:
            return value
    return None


def build_dataset(
    raw_object: Mapping[str, Any],
    *,
    endpoint: str,
    tenant_id: str,
    tag_references: Sequence[TagReference] | None = None,
    object_id: str | None = None,
) -> Dataset:
    """Build the neutral :class:`Dataset` for one table or view.

    Structural here: ``name``, ``identities`` and ``asset_type`` (from
    :func:`~qlabs_connector_snowflake.mapping.asset_type_for_object`), plus a lossless
    ``custom_attributes`` passthrough of everything else -- the native table/view kind and
    the assembled column list included. ``description``, ``owners``, ``physical_ref``,
    ``tags`` and ``classifications`` all come from ``mapping.py``.

    ``tag_references`` of ``None`` means the tag read never ran, so ``tags`` and
    ``classifications`` stay absent (no envelope, engine leaves the target alone) rather
    than reporting an empty list; see the module docstring's "Enrichment degrades" section.
    """
    database = _require_text(raw_object, "TABLE_CATALOG")
    schema = _require_text(raw_object, "TABLE_SCHEMA")
    name = _require_text(raw_object, "TABLE_NAME")
    ref = IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATASET,
        native_key=f"{database}.{schema}.{name}",
        tenant_id=tenant_id,
        secondary_keys={"object_id": object_id} if object_id else {},
    )
    asset_type = asset_type_for_object(raw_object)
    custom_attributes = map_custom_attributes(raw_object, exclude=_OBJECT_STRUCTURAL_FIELDS)
    content = map_dataset_fields(raw_object, tag_references=tag_references)
    values: dict[str, Any] = {
        "name": name,
        "asset_type": asset_type,
        "custom_attributes": custom_attributes,
        **content,
    }
    _, last_altered = lookup(raw_object, "LAST_ALTERED")
    return Dataset(
        identities=[ref],
        name=name,
        asset_type=asset_type,
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(
            values,
            source_endpoint=endpoint,
            last_modified_at=_parse_snowflake_timestamp(last_altered),
        ),
        **content,
    )


def build_schema_data_product(
    raw_schema: Mapping[str, Any],
    *,
    endpoint: str,
    tenant_id: str,
    tag_references: Sequence[TagReference] | None = None,
) -> DataProduct:
    """Build the neutral :class:`DataProduct` for one Snowflake schema.

    ``dataset_refs`` is deliberately left empty, for exactly the reason
    ``qlabs_connector_databricks.read.SchemaRead`` documents:
    :class:`~qlabs_catalog_sync_sdk.models.NeutralEntity` mints a fresh random
    ``neutral_id`` on every read, so writing this read's member ``Dataset`` ids into
    ``dataset_refs`` would change that field's checksum on every cycle even when nothing
    changed -- breaking the exact idempotency property
    :func:`~qlabs_catalog_sync_sdk.envelope.compute_checksum` exists to guarantee.
    Membership travels instead as :attr:`DataProductRead.datasets` /
    :attr:`DataProductRead.member_object_names`, for the sync loop to resolve through the
    IdentityMap.

    ``documentation``, ``status`` and ``placement`` are never set: a Snowflake schema has
    no long-form doc surface distinct from its ``COMMENT``, no lifecycle, and no
    space/domain placement (``manifest.py`` declares ``placement`` ``na`` for that reason;
    ``documentation``/``status`` exist for the listing shape only).
    """
    database = _require_text(raw_schema, "CATALOG_NAME")
    name = _require_text(raw_schema, "SCHEMA_NAME")
    ref = IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=f"{database}.{name}",
        tenant_id=tenant_id,
    )
    custom_attributes = map_custom_attributes(raw_schema, exclude=_SCHEMA_STRUCTURAL_FIELDS)
    content = map_data_product_fields(raw_schema, tag_references=tag_references)
    values: dict[str, Any] = {"name": name, "custom_attributes": custom_attributes, **content}
    _, last_altered = lookup(raw_schema, "LAST_ALTERED")
    return DataProduct(
        identities=[ref],
        name=name,
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(
            values,
            source_endpoint=endpoint,
            last_modified_at=_parse_snowflake_timestamp(last_altered),
        ),
        **content,
    )


def build_listing_data_product(
    raw_listing: Mapping[str, Any],
    *,
    endpoint: str,
    tenant_id: str,
    tag_references: Sequence[TagReference] | None = None,
) -> DataProduct:
    """Build the neutral :class:`DataProduct` for one Snowflake listing.

    Identity is the listing's ``global_name`` -- "a stable, globally-unique identifier
    that is the best matching key for a listing across accounts/regions" (RS-05 section
    2.4) -- with the local listing ``name`` alongside in ``secondary_keys`` because
    ``DESCRIBE LISTING`` addresses a listing by that local name, not by the global one.

    ``name`` comes from the listing ``title`` (falling back to the local listing name),
    ``description`` from ``subtitle`` and ``documentation`` from the long-form Markdown
    ``description`` -- see ``mapping.py``'s "subtitle vs. description" section for why the
    spelling collision must not be followed literally. ``status`` comes from the publish
    state (RS-05 sections 3.6/4.4). ``categories``, ``business_needs``,
    ``data_attributes``, ``compliance_badges``, ``data_dictionary``, V1 ``targets`` /
    V2 ``external_targets``+``locations``+``pricing_plans`` (RS-05 section 2.3) and the
    folded-in share composition all round-trip verbatim through ``custom_attributes``:
    RS-03 has no neutral field for any of them, and losing them would be worse than
    carrying them opaquely.
    """
    global_name = _require_text(raw_listing, "global_name")
    listing_name = _optional_text(raw_listing, "name")
    secondary_keys = {"listing_name": listing_name} if listing_name else {}
    ref = IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=global_name,
        tenant_id=tenant_id,
        secondary_keys=secondary_keys,
    )
    name = _optional_text(raw_listing, "title") or listing_name
    if not name:
        raise ConnectorError(
            f"listing {global_name!r} carries neither a title nor a name to use as the "
            "neutral data product name",
            endpoint=endpoint,
            entity_type=EntityType.DATA_PRODUCT,
        )
    custom_attributes = map_custom_attributes(raw_listing, exclude=_LISTING_STRUCTURAL_FIELDS)
    content = map_listing_fields(raw_listing, tag_references=tag_references)
    values: dict[str, Any] = {"name": name, "custom_attributes": custom_attributes, **content}
    _, updated_on = lookup(raw_listing, "updated_on")
    return DataProduct(
        identities=[ref],
        name=name,
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(
            values,
            source_endpoint=endpoint,
            last_modified_at=_parse_snowflake_timestamp(updated_on),
        ),
        **content,
    )


# ----------------------------------------------------------------------------------
# Statement builders
# ----------------------------------------------------------------------------------


def _information_schema_query(
    *, database: str, view: str, columns: Sequence[str], where: str, order_by: str = ""
) -> str:
    """``SELECT <columns> FROM "<database>".INFORMATION_SCHEMA.<view> WHERE ...``.

    ``database`` is quoted by :func:`quote_identifier`; ``view``, ``columns``, ``where``
    and ``order_by`` are module-private constants, never caller-supplied text.
    """
    projection = ", ".join(columns)
    sql = (
        f"SELECT {projection} FROM {quote_identifier(database)}.INFORMATION_SCHEMA.{view} "
        f"WHERE {where}"
    )
    if order_by:
        sql += f" ORDER BY {order_by}"
    return sql


def _tag_references_query(*, database: str, object_fqn: str, domain: str) -> str:
    """``SELECT ... FROM TABLE("<db>".INFORMATION_SCHEMA.TAG_REFERENCES(<obj>, <domain>))``.

    RS-05 section 3.4's per-object read path. Both arguments are string *literals* rather
    than bind values -- see the module docstring's "Identifier and value safety" for why a
    table function's arguments go through :func:`quote_literal` instead.
    """
    projection = ", ".join(_TAG_REFERENCE_COLUMNS)
    function = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TAG_REFERENCES"
    args = f"{quote_literal(object_fqn)}, {quote_literal(domain)}"
    return f"SELECT {projection} FROM TABLE({function}({args}))"


def _account_usage_tag_references_query() -> str:
    """Account-wide tag assignments for one ``database.schema``, in one statement.

    RS-05 section 1.4 names ``SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES`` as "the key one for
    tags" at account scope and warns it carries latency (typically up to ~2 hours). Used
    only to enrich the *member datasets* of a schema-shaped data product, where the
    alternative is one ``INFORMATION_SCHEMA.TAG_REFERENCES`` call per table -- a statement
    count that grows with the schema. The primary object of any read gets the fresh,
    per-object ``INFORMATION_SCHEMA`` path instead.
    """
    projection = ", ".join(_TAG_REFERENCE_COLUMNS)
    return (
        f"SELECT {projection} FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES "
        "WHERE OBJECT_DATABASE = ? AND OBJECT_SCHEMA = ?"
    )


def _tag_domain_for(asset_type: AssetType) -> str:
    """The ``TAG_REFERENCES`` object-domain argument for a neutral asset type.

    TENANT-UNVERIFIED (module docstring assumption 5): RS-05 shows ``'TABLE'`` in its
    example and does not enumerate the domain vocabulary.
    """
    return "VIEW" if asset_type is AssetType.VIEW else "TABLE"


# ----------------------------------------------------------------------------------
# Tag reads
# ----------------------------------------------------------------------------------


async def read_object_tag_references(
    client: StatementClient,
    *,
    database: str,
    object_fqn: str,
    domain: str,
    entity_type: EntityType | None = None,
) -> list[TagReference]:
    """Every tag assigned to one object (and to its columns), freshest surface first.

    Uses the per-database ``INFORMATION_SCHEMA.TAG_REFERENCES`` table function (RS-05
    section 3.4), which is real-time, rather than the account-wide ``ACCOUNT_USAGE`` view
    with its ~2h latency (RS-05 section 1.4). Column-level rows are included: a table's
    neutral ``classifications`` aggregates the categories Snowflake assigned to its
    columns (``manifest.py``'s own words).

    Errors are **not** swallowed here: this is the primary object's own metadata, so a
    failure is a failure of the read.
    """
    result = await client.execute(
        _tag_references_query(database=database, object_fqn=object_fqn, domain=domain),
        entity_type=entity_type,
    )
    return _tag_references_from(result)


def _tag_references_from(result: StatementResult) -> list[TagReference]:
    references: list[TagReference] = []
    for row in result.mappings():
        reference = tag_reference_from_row(row)
        if reference is not None:
            references.append(reference)
    return references


async def _read_member_tag_index(
    client: StatementClient, *, database: str, schema: str
) -> dict[str, list[TagReference]] | None:
    """Tags for every object in ``database.schema``, keyed by object FQN, in one statement.

    Returns ``None`` -- never an empty index -- when the ``ACCOUNT_USAGE`` share is not
    granted to this connector's role, so member datasets come back with ``tags`` absent
    rather than falsely empty (module docstring, "Enrichment degrades"). A transient
    failure is *not* absorbed: retrying is meaningful there, and hiding it would turn a
    blip into silently missing metadata on every cycle.
    """
    try:
        result = await client.execute(
            _account_usage_tag_references_query(),
            bind_values=(database, schema),
            entity_type=EntityType.DATASET,
        )
    except (AuthError, CapabilityError) as exc:
        _logger.warning(
            "snowflake.read.member_tags_unavailable",
            endpoint=client.endpoint,
            database=database,
            schema=schema,
            reason=str(exc),
        )
        return None
    index: dict[str, list[TagReference]] = {}
    for reference in _tag_references_from(result):
        object_fqn = reference.object_fqn
        if object_fqn is not None:
            index.setdefault(object_fqn, []).append(reference)
    return index


# ----------------------------------------------------------------------------------
# Dataset read
# ----------------------------------------------------------------------------------


async def _read_object_row(
    client: StatementClient, *, database: str, schema: str, name: str
) -> dict[str, Any] | None:
    """One ``TABLES`` row, falling back to ``VIEWS``, or ``None`` when neither has it.

    ``TABLES`` is tried first because Snowflake lists views there too; ``VIEWS`` is the
    fallback for the view kinds a given account may report only there
    (TENANT-UNVERIFIED, module docstring assumption 7), and it additionally carries
    ``IS_SECURE``, which ``TABLES`` does not.
    """
    tables = await client.execute(
        _information_schema_query(
            database=database,
            view="TABLES",
            columns=_TABLES_COLUMNS,
            where="TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        ),
        bind_values=(schema, name),
        entity_type=EntityType.DATASET,
    )
    row = tables.first_mapping()
    if row is not None:
        return row
    views = await client.execute(
        _information_schema_query(
            database=database,
            view="VIEWS",
            columns=_VIEWS_COLUMNS,
            where="TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        ),
        bind_values=(schema, name),
        entity_type=EntityType.DATASET,
    )
    return views.first_mapping()


async def _read_columns(
    client: StatementClient, *, database: str, schema: str, name: str
) -> list[dict[str, Any]]:
    """The object's column list, ordered by ``ORDINAL_POSITION`` (RS-05 section 1.2)."""
    result = await client.execute(
        _information_schema_query(
            database=database,
            view="COLUMNS",
            columns=_COLUMNS_COLUMNS,
            where="TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            order_by="ORDINAL_POSITION",
        ),
        bind_values=(schema, name),
        entity_type=EntityType.DATASET,
    )
    return result.mappings()


async def read_dataset(client: StatementClient, ref: IdentityRef) -> Dataset:
    """Read one table or view (``ref.entity_type`` must be ``DATASET``) into a
    :class:`Dataset`.

    ``ref.native_key`` is the fully-qualified ``DATABASE.SCHEMA.OBJECT`` name (see the
    module docstring's "Identity" section). Three statements: the object row, its columns,
    and its tag references. Raises :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`
    when neither ``INFORMATION_SCHEMA.TABLES`` nor ``.VIEWS`` knows the name -- which is
    also the honest answer when the name was given in the wrong case, since Snowflake
    stores identifiers case-sensitively once quoted.
    """
    _require_entity_type(ref, EntityType.DATASET)
    database, schema, name = split_object_name(ref.native_key)
    raw_object = await _read_object_row(client, database=database, schema=schema, name=name)
    if raw_object is None:
        raise NotFound(
            f"no table or view named {ref.native_key!r} exists",
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=ref.native_key,
        )
    raw_object[COLUMNS_KEY] = await _read_columns(
        client, database=database, schema=schema, name=name
    )
    tag_references = await read_object_tag_references(
        client,
        database=database,
        object_fqn=ref.native_key,
        domain=_tag_domain_for(asset_type_for_object(raw_object)),
        entity_type=EntityType.DATASET,
    )
    _logger.info(
        "snowflake.read.dataset",
        endpoint=ref.endpoint,
        object=ref.native_key,
        column_count=len(raw_object[COLUMNS_KEY]),
        tag_reference_count=len(tag_references),
    )
    return build_dataset(
        raw_object,
        endpoint=ref.endpoint,
        tenant_id=ref.tenant_id,
        tag_references=tag_references,
        object_id=ref.secondary_keys.get("object_id"),
    )


# ----------------------------------------------------------------------------------
# Data product read: schema shape
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataProductRead:
    """One data product plus the members it contains, read together.

    Both native shapes produce this: a schema read fills ``datasets`` with the member
    tables/views (Snowflake has no "the objects of this schema" primitive other than
    filtering ``INFORMATION_SCHEMA.TABLES``, so producing the product without them would
    throw that work away), while a listing read leaves ``datasets`` empty -- a listing's
    data dictionary can name objects in a *different* account entirely (RS-05 section 2.2),
    which this connector cannot read -- and reports their names in
    ``member_object_names`` instead.

    ``data_product.dataset_refs`` is empty in both cases; see
    :func:`build_schema_data_product` for the checksum-stability reason, which is the same
    one ``qlabs_connector_databricks.read.SchemaRead`` documents.
    """

    data_product: DataProduct
    datasets: list[Dataset]
    member_object_names: list[str]


async def read_schema(client: StatementClient, ref: IdentityRef) -> DataProductRead:
    """Read one Snowflake schema (a ``DATA_PRODUCT`` ref of the schema shape).

    ``ref.native_key`` is ``DATABASE.SCHEMA``. Four statements regardless of how many
    tables the schema holds: the schema row, the schema's own tag references, the member
    object rows, and one account-wide tag index covering every member (see
    :func:`_read_member_tag_index`). Raises
    :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound` when the schema does not exist.
    """
    _require_entity_type(ref, EntityType.DATA_PRODUCT)
    database, schema = split_schema_name(ref.native_key)
    schemata = await client.execute(
        _information_schema_query(
            database=database,
            view="SCHEMATA",
            columns=_SCHEMATA_COLUMNS,
            where="SCHEMA_NAME = ?",
        ),
        bind_values=(schema,),
        entity_type=EntityType.DATA_PRODUCT,
    )
    raw_schema = schemata.first_mapping()
    if raw_schema is None:
        raise NotFound(
            f"no schema named {ref.native_key!r} exists",
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=ref.native_key,
        )
    schema_tags = await read_object_tag_references(
        client,
        database=database,
        object_fqn=ref.native_key,
        domain="SCHEMA",
        entity_type=EntityType.DATA_PRODUCT,
    )
    members = await client.execute(
        _information_schema_query(
            database=database,
            view="TABLES",
            columns=_TABLES_COLUMNS,
            where="TABLE_SCHEMA = ?",
            order_by="TABLE_NAME",
        ),
        bind_values=(schema,),
        entity_type=EntityType.DATASET,
    )
    member_rows = members.mappings()
    tag_index = (
        await _read_member_tag_index(client, database=database, schema=schema)
        if member_rows
        else None
    )
    datasets: list[Dataset] = []
    member_object_names: list[str] = []
    for row in member_rows:
        object_fqn = f"{database}.{schema}.{_require_text(row, 'TABLE_NAME')}"
        member_object_names.append(object_fqn)
        datasets.append(
            build_dataset(
                row,
                endpoint=ref.endpoint,
                tenant_id=ref.tenant_id,
                tag_references=None if tag_index is None else tag_index.get(object_fqn, []),
            )
        )
    _logger.info(
        "snowflake.read.schema",
        endpoint=ref.endpoint,
        schema=ref.native_key,
        member_count=len(datasets),
        member_tags_read=tag_index is not None,
    )
    return DataProductRead(
        data_product=build_schema_data_product(
            raw_schema,
            endpoint=ref.endpoint,
            tenant_id=ref.tenant_id,
            tag_references=schema_tags,
        ),
        datasets=datasets,
        member_object_names=member_object_names,
    )


# ----------------------------------------------------------------------------------
# Data product read: listing shape (and the share beneath it)
# ----------------------------------------------------------------------------------


async def read_share_composition(
    client: StatementClient, share_name: str
) -> list[dict[str, Any]] | None:
    """The objects granted into one share (RS-05 section 3.5), or ``None`` if unreadable.

    **A share is not an entity.** RS-05 section 2.1 places it beneath a listing as the
    access mechanism the listing productizes, and ``manifest.py`` deliberately declares no
    share entity type -- so this function exists purely to enrich a listing-shaped data
    product, and its result is folded into that product's ``custom_attributes`` under
    :data:`SHARE_COMPOSITION_KEY`. Nothing ever calls it to *produce* an entity.

    ``DESCRIBE SHARE`` needs privileges on the share (RS-05 section 3.5 notes share
    management is an ACCOUNTADMIN-shaped privilege by default), which a read-only sync
    role may well not hold. A permission or capability refusal therefore degrades to
    ``None`` with a warning rather than failing the listing read: enrichment absent is not
    the same as the listing being unreadable. A transient failure still propagates.
    """
    try:
        result = await client.execute(
            f"DESCRIBE SHARE {quote_identifier(share_name)}",
            entity_type=EntityType.DATA_PRODUCT,
        )
    except (AuthError, CapabilityError, NotFound) as exc:
        _logger.warning(
            "snowflake.read.share_composition_unavailable",
            endpoint=client.endpoint,
            share=share_name,
            reason=str(exc),
        )
        return None
    return sorted(result.mappings(), key=_share_composition_sort_key)


def _share_composition_sort_key(row: Mapping[str, Any]) -> str:
    """A total, content-derived order for one ``DESCRIBE SHARE`` row.

    ``DESCRIBE SHARE`` takes no ``ORDER BY`` and RS-05 section 3.5 documents no ordering
    guarantee, so two otherwise-identical reads may return the same objects in a different
    order. The composition is folded into the listing's ``custom_attributes``, which the
    SDK checksums with ``ArrayOrder.PRESERVE`` (unlike ``tags``/``classifications``, which
    the SDK sorts) -- so an unstable order would move the listing's checksum on every poll
    and have the engine rewrite an unchanged listing forever. Sorting here makes the order
    a property of the content rather than of the response.

    The key is the whole row rendered canonically rather than ``(kind, name)``: RS-05 does
    not pin the column set, so a row that carries columns this connector has not seen must
    still order deterministically instead of colliding with its neighbours.
    """
    return json.dumps(dict(row), sort_keys=True, default=str)


async def _find_listing_row(
    client: StatementClient, ref: IdentityRef, listing_name: str | None
) -> dict[str, Any] | None:
    """The ``SHOW LISTINGS`` row for ``ref``, matched on ``global_name`` (RS-05 2.4).

    ``SHOW LISTINGS LIKE`` narrows on the *local* listing name when the ref carries one --
    its pattern is a literal, so it goes through :func:`quote_literal`, and its ``_``/``%``
    wildcards mean it may match more than intended, which is exactly why the row is still
    selected client-side by identity rather than by "the only row returned".

    A ref whose ``native_key`` is the local name rather than the global name is matched
    too, so a caller that only has the local name still resolves.
    """
    if listing_name is not None:
        statement = f"SHOW LISTINGS LIKE {quote_literal(listing_name)}"
    else:
        statement = "SHOW LISTINGS"
    result = await client.execute(statement, entity_type=EntityType.DATA_PRODUCT)
    wanted = {ref.native_key}
    global_name = ref.secondary_keys.get("listing_global_name")
    if global_name:
        wanted.add(global_name)
    for row in result.mappings():
        candidates = {
            _optional_text(row, "global_name"),
            _optional_text(row, "name"),
        }
        if candidates & wanted:
            return row
    return None


async def read_listing(client: StatementClient, ref: IdentityRef) -> DataProductRead:
    """Read one Snowflake listing (a ``DATA_PRODUCT`` ref of the listing shape).

    ``SHOW LISTINGS`` locates the listing and supplies its operational state (``state``,
    ``global_name``, ``owner``, ``share``); ``DESCRIBE LISTING`` then supplies the
    descriptive manifest fields (RS-05 section 3.6), and the two rows are merged with
    ``DESCRIBE``'s values winning -- it is the more detailed of the two surfaces. When the
    listing names a share, its composition is folded in as enrichment (see
    :func:`read_share_composition`), which is the only sense in which this connector reads
    shares at all.

    ``datasets`` on the returned :class:`DataProductRead` is always empty and
    ``member_object_names`` carries whatever object names the listing's data dictionary
    and share composition name -- see :class:`DataProductRead` for why the objects
    themselves are not fetched.
    """
    _require_entity_type(ref, EntityType.DATA_PRODUCT)
    listing_name = ref.secondary_keys.get("listing_name")
    row = await _find_listing_row(client, ref, listing_name)
    if row is None:
        raise NotFound(
            f"no listing matching {ref.native_key!r} exists",
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=ref.native_key,
        )
    raw_listing: dict[str, Any] = dict(row)
    local_name = _optional_text(raw_listing, "name") or listing_name
    if local_name:
        described = await client.execute(
            f"DESCRIBE LISTING {quote_identifier(local_name)}",
            entity_type=EntityType.DATA_PRODUCT,
        )
        detail = described.first_mapping()
        if detail is not None:
            raw_listing.update(detail)

    share_name = _optional_text(raw_listing, "share")
    if share_name:
        composition = await read_share_composition(client, share_name)
        if composition is not None:
            raw_listing[SHARE_COMPOSITION_KEY] = composition

    member_object_names = _listing_member_object_names(raw_listing)
    _logger.info(
        "snowflake.read.listing",
        endpoint=ref.endpoint,
        listing=ref.native_key,
        share=share_name,
        member_count=len(member_object_names),
    )
    return DataProductRead(
        data_product=build_listing_data_product(
            raw_listing, endpoint=ref.endpoint, tenant_id=ref.tenant_id
        ),
        datasets=[],
        member_object_names=member_object_names,
    )


def _listing_member_object_names(raw_listing: Mapping[str, Any]) -> list[str]:
    """Object names a listing points at, from its data dictionary and its share.

    RS-05 section 2.2 describes ``data_dictionary`` as naming a ``database``, ``schema``,
    object ``name`` and ``domain`` per entry; this reads that structure defensively and
    ignores anything that does not match it, because the exact JSON encoding
    ``DESCRIBE LISTING`` uses is TENANT-UNVERIFIED (module docstring assumption 3). The
    share's granted objects (``DESCRIBE SHARE``'s ``name`` column) are added when the
    composition was readable. Order is first-seen and duplicates are collapsed.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            names.append(candidate)

    _, dictionary = lookup(raw_listing, "data_dictionary")
    for entry in _iter_data_dictionary_entries(dictionary):
        database = entry.get("database")
        schema = entry.get("schema")
        name = entry.get("name")
        if all(isinstance(part, str) and part for part in (database, schema, name)):
            _add(f"{database}.{schema}.{name}")

    _, composition = lookup(raw_listing, SHARE_COMPOSITION_KEY)
    if isinstance(composition, list):
        for entry in composition:
            if isinstance(entry, Mapping):
                _add(_optional_text(entry, "name"))
    return names


def _iter_data_dictionary_entries(dictionary: object) -> list[dict[str, Any]]:
    """Flatten a listing ``data_dictionary`` into ``{database, schema, name, domain}`` dicts.

    Handles both encodings RS-05 section 2.2's example allows: a flat list of entries, and
    the ``{"featured": {"database": ..., "objects": [{"name":..., "schema":...}]}}`` nesting
    the manifest reference shows. A JSON **string** is decoded first, because the SQL REST
    API renders a structured column as text (TENANT-UNVERIFIED, module docstring assumption
    3) -- the raw string still round-trips verbatim into ``custom_attributes``; only this
    membership hint parses it. Anything else yields nothing rather than raising: an
    unrecognized shape costs the hint, never the read.
    """
    if isinstance(dictionary, str):
        try:
            dictionary = json.loads(dictionary)
        except ValueError:
            return []
    if isinstance(dictionary, list):
        return [entry for entry in dictionary if isinstance(entry, dict)]
    if not isinstance(dictionary, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for group in dictionary.values():
        if not isinstance(group, Mapping):
            continue
        database = group.get("database")
        objects = group.get("objects")
        if not isinstance(objects, list):
            continue
        for entry in objects:
            if not isinstance(entry, Mapping):
                continue
            merged: dict[str, Any] = dict(entry)
            merged.setdefault("database", database)
            entries.append(merged)
    return entries


# ----------------------------------------------------------------------------------
# Connector.read() entry point
# ----------------------------------------------------------------------------------


async def read_entity(client: StatementClient, ref: IdentityRef) -> NeutralEntity:
    """Matches ``Connector.read(ref)``'s signature exactly -- what ``__init__.py``
    delegates straight to.

    Dispatches on ``ref.entity_type`` and, for a ``DATA_PRODUCT``, on which of the two
    native shapes the ref names (:func:`data_product_shape`). Only the data product itself
    is returned; :func:`read_schema` and :func:`read_listing` are exported directly for a
    caller that wants the member half of :class:`DataProductRead` too (an initial
    full-scan discovery path, say) without re-issuing the same statements.

    Anything the manifest declares unsupported -- ``GLOSSARY_TERM``, ``CATEGORY`` -- raises
    :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`, never a silent empty
    entity.
    """
    if ref.entity_type is EntityType.DATASET:
        return await read_dataset(client, ref)
    if ref.entity_type is EntityType.DATA_PRODUCT:
        shape = data_product_shape(ref)
        if shape is DataProductShape.SCHEMA:
            return (await read_schema(client, ref)).data_product
        return (await read_listing(client, ref)).data_product
    raise CapabilityError(
        f"the Snowflake connector does not support reading {ref.entity_type!r}",
        endpoint=ref.endpoint,
        entity_type=ref.entity_type,
        operation="read",
    )


# ----------------------------------------------------------------------------------
# Change detection (T6.3): ACCOUNT_USAGE scan + checksum census
# ----------------------------------------------------------------------------------

ACCOUNT_USAGE_LAG: Final[timedelta] = timedelta(hours=3)
"""How stale ``SNOWFLAKE.ACCOUNT_USAGE`` is assumed to be.

RS-05 section 1.4 says "typically up to ~2 hours for many views"; Snowflake's own
documentation puts some views at three. This module assumes the worse of the two, because
the failure mode of assuming too little is a *silently lost change* while the failure mode
of assuming too much is only a slightly wider re-scan. TENANT-UNVERIFIED (module docstring
assumption 10): the real figure for an account can only be measured on that account.
"""

DEFAULT_WATERMARK_SAFETY_MARGIN: Final[timedelta] = ACCOUNT_USAGE_LAG
"""How far behind Snowflake's own "now" the proposed watermark is held.

Equal to :data:`ACCOUNT_USAGE_LAG` by default: a watermark is only allowed to claim
coverage of a region that has certainly finished propagating into ``ACCOUNT_USAGE``. See
the module docstring's "Change detection" section for the proof that no change can fall
between two polls while the real lag stays inside this margin.

It is a **module constant plus a keyword argument**, not a config field: adding one would
mean editing ``SnowflakeConfig`` in ``auth.py``, which T6.3 is not permitted to touch. A
deployment that measures a different real lag overrides it per call today, and the field
belongs in ``SnowflakeConfig`` the next time ``auth.py`` is legitimately in scope.
"""

DEFAULT_RESCAN_OVERLAP: Final[timedelta] = timedelta(minutes=15)
"""Extra window re-scanned below the stored high-water mark on every incremental poll.

Belt to the safety margin's braces: it absorbs boundary rounding, the sub-second
difference between the instant ``CURRENT_TIMESTAMP()`` was evaluated and the instant the
scan query ran, and any residual clock disagreement. Re-scanning is cheap and safe here
precisely because a re-observed row whose content has not moved is suppressed by its
checksum rather than re-reported.
"""

SNAPSHOT_FORMAT_VERSION: Final[int] = 1
"""Schema version stamped into every cursor payload this module writes.

A cursor whose version is not this one is treated as "no prior knowledge" (a full scan)
rather than mis-parsed -- bump this and update :func:`_decode_change_snapshot` if the
payload shape ever changes incompatibly.
"""

CURRENT_TIMESTAMP_STATEMENT: Final[str] = "SELECT CURRENT_TIMESTAMP() AS SNOWFLAKE_NOW"
"""The one extra statement each poll issues, so the safety margin is measured against the
same clock that stamps ``LAST_ALTERED``/``DELETED`` rather than against this host's."""

#: ``ACCOUNT_USAGE.TABLES`` projection: exactly the columns :data:`_TABLES_COLUMNS` already
#: bets on for ``INFORMATION_SCHEMA``, plus the two this surface adds and the change feed
#: needs -- the stable object id (RS-05 section 4.3) and the drop timestamp (RS-05 section
#: 1.4: ``ACCOUNT_USAGE`` covers "dropped objects"). TENANT-UNVERIFIED, assumption 9.
_ACCOUNT_USAGE_TABLES_COLUMNS: Final[tuple[str, ...]] = ("TABLE_ID", *_TABLES_COLUMNS, "DELETED")

#: ``ACCOUNT_USAGE.SCHEMATA`` projection -- same construction as
#: :data:`_ACCOUNT_USAGE_TABLES_COLUMNS`, over :data:`_SCHEMATA_COLUMNS`.
_ACCOUNT_USAGE_SCHEMATA_COLUMNS: Final[tuple[str, ...]] = (
    "SCHEMA_ID",
    *_SCHEMATA_COLUMNS,
    "DELETED",
)

#: The initial (full) scan: every object that still exists. No lower bound, because there
#: is no prior watermark to bound against, and no dropped rows, because an object that was
#: already gone before this connector ever looked was never reported as present.
_FULL_SCAN_WHERE: Final[str] = "DELETED IS NULL"

#: The incremental scan. Three disjuncts, each deliberate:
#:
#: * ``DELETED >= ?`` -- drops that happened inside the window, the only delete signal
#:   ``ACCOUNT_USAGE`` gives;
#: * ``LAST_ALTERED >= ?`` -- the ordinary create/update signal;
#: * ``LAST_ALTERED IS NULL`` -- the checksum fallback bucket. An object kind whose
#:   ``LAST_ALTERED`` this account never populates would be filtered out entirely by a
#:   timestamp predicate and could then never be seen to change; those rows bypass the
#:   time filter altogether and are decided purely by comparing their checksum against the
#:   census.
_INCREMENTAL_SCAN_WHERE: Final[str] = (
    "DELETED >= TO_TIMESTAMP_TZ(?) OR LAST_ALTERED >= TO_TIMESTAMP_TZ(?) OR LAST_ALTERED IS NULL"
)

_SHOW_LISTINGS_STATEMENT: Final[str] = "SHOW LISTINGS"


@dataclass(frozen=True, slots=True)
class _StructuralStream:
    """Which ``ACCOUNT_USAGE`` view backs one entity type's structural scan, and how a row
    of it becomes an :class:`~qlabs_catalog_sync_sdk.models.IdentityRef`.

    ``id_is_declared_identity_key`` is the whole reason a schema rename and a table rename
    are reported differently: ``manifest.py`` declares ``object_id`` as a ``DATASET``
    identity key and declares no id key at all for ``DATA_PRODUCT``, so the id may travel
    in a dataset's ref and may not travel in a schema's. See the module docstring's
    "Renames".
    """

    entity_type: EntityType
    view: str
    columns: tuple[str, ...]
    id_column: str
    name_columns: tuple[str, ...]
    id_is_declared_identity_key: bool


_STRUCTURAL_STREAMS: Final[dict[EntityType, _StructuralStream]] = {
    EntityType.DATASET: _StructuralStream(
        entity_type=EntityType.DATASET,
        view="TABLES",
        columns=_ACCOUNT_USAGE_TABLES_COLUMNS,
        id_column="TABLE_ID",
        name_columns=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"),
        id_is_declared_identity_key=True,
    ),
    EntityType.DATA_PRODUCT: _StructuralStream(
        entity_type=EntityType.DATA_PRODUCT,
        view="SCHEMATA",
        columns=_ACCOUNT_USAGE_SCHEMATA_COLUMNS,
        id_column="SCHEMA_ID",
        name_columns=("CATALOG_NAME", "SCHEMA_NAME"),
        id_is_declared_identity_key=False,
    ),
}


@dataclass(frozen=True, slots=True)
class _Observed:
    """One object as this poll saw it."""

    native_key: str
    ref: IdentityRef
    checksum: str
    object_id: str | None
    display_name: str
    last_modified_at: datetime | None
    is_deleted: bool = False


@dataclass(frozen=True, slots=True)
class _Remembered:
    """One object as a *previous* poll recorded it in the census.

    ``checksum`` is what makes a re-scanned but unmoved row silent; ``object_id`` is what
    makes a rename detectable; ``secondary_keys`` is what lets a vanished object still get
    a complete, valid ``IdentityRef`` for its ``DELETED`` report, when there is no fresh
    row left to build one from.
    """

    checksum: str
    object_id: str | None
    secondary_keys: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ChangeSnapshot:
    """The decoded cursor payload: where the last poll got to, and what it had seen."""

    high_water: datetime | None
    objects: dict[str, _Remembered]
    listings: dict[str, _Remembered]


# ----------------------------------------------------------------------------------
# Cursor codec
# ----------------------------------------------------------------------------------


def _decode_census(raw: object) -> dict[str, _Remembered]:
    """One ``{native_key: entry}`` map from a cursor payload, skipping malformed entries."""
    if not isinstance(raw, Mapping):
        return {}
    census: dict[str, _Remembered] = {}
    for native_key, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        checksum = entry.get("checksum")
        if not isinstance(checksum, str) or not checksum:
            continue
        object_id = entry.get("object_id")
        keys = entry.get("keys")
        census[str(native_key)] = _Remembered(
            checksum=checksum,
            object_id=object_id if isinstance(object_id, str) and object_id else None,
            secondary_keys={
                str(key): str(value)
                for key, value in (keys.items() if isinstance(keys, Mapping) else ())
                if isinstance(value, str)
            },
        )
    return census


def _decode_change_snapshot(since: Watermark) -> _ChangeSnapshot:
    """The census and high-water mark a previous ``next_watermark`` carried.

    Empty (which means "full scan, no prior knowledge") for an ``initial`` watermark and
    for anything that is not a well-formed cursor of this module's own current version.
    Failing *open* like this is deliberate: an unreadable state-store artifact costs one
    wider scan and a burst of ``UPSERT`` candidates the engine will find unchanged, where
    raising would cost the whole cycle.
    """
    if since.kind is not WatermarkKind.CURSOR or not since.cursor:
        return _ChangeSnapshot(high_water=None, objects={}, listings={})
    try:
        payload: Any = json.loads(since.cursor)
        if not isinstance(payload, dict) or payload.get("v") != SNAPSHOT_FORMAT_VERSION:
            raise ValueError("cursor payload is not a version this module wrote")
        raw_high_water = payload.get("high_water")
        high_water = (
            _parse_snowflake_timestamp(raw_high_water) if isinstance(raw_high_water, str) else None
        )
        return _ChangeSnapshot(
            high_water=high_water,
            objects=_decode_census(payload.get("objects")),
            listings=_decode_census(payload.get("listings")),
        )
    except Exception:
        _logger.warning(
            "snowflake.list_changed.snapshot_unparseable",
            stream=since.stream_key,
        )
        return _ChangeSnapshot(high_water=None, objects={}, listings={})


def _encode_census(census: Mapping[str, _Remembered]) -> dict[str, dict[str, Any]]:
    encoded: dict[str, dict[str, Any]] = {}
    for native_key, entry in census.items():
        item: dict[str, Any] = {"checksum": entry.checksum}
        if entry.object_id:
            item["object_id"] = entry.object_id
        if entry.secondary_keys:
            item["keys"] = dict(entry.secondary_keys)
        encoded[native_key] = item
    return encoded


def _encode_change_snapshot(
    *,
    high_water: datetime,
    objects: Mapping[str, _Remembered],
    listings: Mapping[str, _Remembered],
) -> str:
    """The opaque cursor string. Sorted and separator-tight so an unchanged census encodes
    byte-identically twice running -- which is what makes "re-running found nothing" a
    checkable property of the watermark itself, not just of the candidate list."""
    return json.dumps(
        {
            "v": SNAPSHOT_FORMAT_VERSION,
            "high_water": high_water.astimezone(UTC).isoformat(),
            "objects": _encode_census(objects),
            "listings": _encode_census(listings),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# ----------------------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------------------


def _account_usage_query(*, view: str, columns: Sequence[str], where: str) -> str:
    """``SELECT <columns> FROM SNOWFLAKE.ACCOUNT_USAGE.<view> WHERE ...``.

    ``view``, ``columns`` and ``where`` are all module-private constants -- never
    caller-supplied text -- so there is nothing here to quote or bind. The only
    caller-influenced values in the whole change feed are the two timestamps, and those go
    through the SQL REST API's ``bindings`` object.
    """
    projection = ", ".join(columns)
    return f"SELECT {projection} FROM SNOWFLAKE.ACCOUNT_USAGE.{view} WHERE {where}"


async def _read_snowflake_now(client: StatementClient, *, entity_type: EntityType) -> datetime:
    """Snowflake's own current instant (see :data:`CURRENT_TIMESTAMP_STATEMENT`).

    Falls back to :attr:`StatementClient.clock` with a warning when the value cannot be
    parsed: the timestamp encoding is TENANT-UNVERIFIED (module docstring assumption 11)
    and failing a whole poll over a rendering detail would be a worse answer than
    proceeding on this host's clock, which is at most seconds out.
    """
    result = await client.execute(CURRENT_TIMESTAMP_STATEMENT, entity_type=entity_type)
    row = result.first_mapping()
    if row is not None:
        for value in row.values():
            parsed = _parse_snowflake_timestamp(value)
            if parsed is not None:
                return parsed
    fallback = client.clock.now()
    _logger.warning(
        "snowflake.list_changed.clock_unreadable",
        endpoint=client.endpoint,
        entity_type=entity_type.value,
        fallback=fallback.isoformat(),
    )
    return fallback


def _coerce_object_id(value: object) -> str | None:
    """An ``ACCOUNT_USAGE`` numeric object id as a string, or ``None`` when absent.

    The SQL REST API renders numbers as strings, but an account (or a future response
    format) returning a JSON number must not silently produce no id at all, so both are
    accepted. ``bool`` is excluded before ``int`` because ``bool`` subclasses it.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _row_is_deleted(row: Mapping[str, Any]) -> bool:
    """Whether an ``ACCOUNT_USAGE`` row describes a dropped object.

    Presence of *any* non-blank ``DELETED`` value counts, not "does it parse as a
    timestamp": a drop timestamp this module cannot parse still means the object is gone,
    and treating an unparseable value as "still alive" would silently drop a real delete.
    """
    present, value = lookup(row, "DELETED")
    if not present or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _observe_structural_row(
    row: Mapping[str, Any],
    stream: _StructuralStream,
    *,
    endpoint: str,
    tenant_id: str,
) -> _Observed | None:
    """One ``ACCOUNT_USAGE`` row -> :class:`_Observed`, or ``None`` if it has no identity.

    The identity built here is *the same* identity ``read()`` builds for the same object
    (:func:`build_dataset`, :func:`build_schema_data_product`) -- the FQN as the native
    key, plus ``object_id`` only where ``manifest.py`` declares it. That agreement is what
    lets the engine take a ``ChangeRef`` straight back into :func:`read_entity`, and
    ``tests/read/changes/test_identity_agreement.py`` pins it.
    """
    parts: list[str] = []
    for column in stream.name_columns:
        _, value = lookup(row, column)
        if not isinstance(value, str) or not value:
            _logger.warning(
                "snowflake.list_changed.unusable_row",
                endpoint=endpoint,
                view=stream.view,
                missing_column=column,
            )
            return None
        parts.append(value)
    native_key = ".".join(parts)

    _, raw_id = lookup(row, stream.id_column)
    object_id = _coerce_object_id(raw_id)
    secondary_keys = (
        {"object_id": object_id} if object_id and stream.id_is_declared_identity_key else {}
    )
    _, last_altered = lookup(row, "LAST_ALTERED")
    _, deleted = lookup(row, "DELETED")
    is_deleted = _row_is_deleted(row)
    return _Observed(
        native_key=native_key,
        ref=IdentityRef(
            endpoint=endpoint,
            entity_type=stream.entity_type,
            native_key=native_key,
            tenant_id=tenant_id,
            secondary_keys=secondary_keys,
        ),
        checksum=compute_checksum(dict(row)),
        object_id=object_id,
        display_name=native_key,
        last_modified_at=_parse_snowflake_timestamp(deleted if is_deleted else last_altered),
        is_deleted=is_deleted,
    )


async def _scan_structural(
    client: StatementClient,
    stream: _StructuralStream,
    *,
    endpoint: str,
    tenant_id: str,
    lower_bound: datetime | None,
) -> list[_Observed]:
    """Every row the account-wide view matches for this poll's window.

    ``lower_bound`` of ``None`` means the full initial scan; otherwise the same instant is
    bound twice, once for each timestamp column in :data:`_INCREMENTAL_SCAN_WHERE`.
    """
    if lower_bound is None:
        statement = _account_usage_query(
            view=stream.view, columns=stream.columns, where=_FULL_SCAN_WHERE
        )
        bind_values: tuple[str, ...] = ()
    else:
        statement = _account_usage_query(
            view=stream.view, columns=stream.columns, where=_INCREMENTAL_SCAN_WHERE
        )
        boundary = lower_bound.astimezone(UTC).isoformat()
        bind_values = (boundary, boundary)

    result = await client.execute(
        statement, bind_values=bind_values, entity_type=stream.entity_type
    )
    observed: list[_Observed] = []
    for row in result.mappings():
        candidate = _observe_structural_row(row, stream, endpoint=endpoint, tenant_id=tenant_id)
        if candidate is not None:
            observed.append(candidate)
    return observed


async def _scan_listings(
    client: StatementClient, *, endpoint: str, tenant_id: str
) -> list[_Observed] | None:
    """Every listing ``SHOW LISTINGS`` reports, or ``None`` when it may not be run.

    ``None`` is not "no listings" -- it is "this role cannot see listings", which is a
    normal state for a read-only sync role (RS-05 section 3.6: ``CREATE LISTING`` is an
    ACCOUNTADMIN-shaped privilege by default, and visibility follows). Distinguishing the
    two matters because the schema shape shares this stream: a role without listing
    privileges must still get its schema changes, and a listing it simply cannot see must
    never be reported as deleted. A transient failure still propagates -- retrying that is
    meaningful, and swallowing it would turn a blip into silently missing candidates.

    The secondary keys built here match :func:`build_listing_data_product` exactly, so a
    ``ChangeRef`` for a listing feeds straight back into :func:`read_entity`.
    """
    try:
        result = await client.execute(_SHOW_LISTINGS_STATEMENT, entity_type=EntityType.DATA_PRODUCT)
    except (AuthError, CapabilityError) as exc:
        _logger.warning(
            "snowflake.list_changed.listings_unavailable",
            endpoint=endpoint,
            reason=str(exc),
        )
        return None

    observed: list[_Observed] = []
    for row in result.mappings():
        global_name = _optional_text(row, "global_name")
        if not global_name:
            _logger.warning("snowflake.list_changed.listing_without_global_name", endpoint=endpoint)
            continue
        listing_name = _optional_text(row, "name")
        secondary_keys = (
            {"listing_name": listing_name} if listing_name else {"listing_global_name": global_name}
        )
        _, updated_on = lookup(row, "updated_on")
        observed.append(
            _Observed(
                native_key=global_name,
                ref=IdentityRef(
                    endpoint=endpoint,
                    entity_type=EntityType.DATA_PRODUCT,
                    native_key=global_name,
                    tenant_id=tenant_id,
                    secondary_keys=secondary_keys,
                ),
                checksum=compute_checksum(dict(row)),
                object_id=None,
                display_name=listing_name or global_name,
                last_modified_at=_parse_snowflake_timestamp(updated_on),
            )
        )
    return observed


# ----------------------------------------------------------------------------------
# Diffing
# ----------------------------------------------------------------------------------


def _remember(observed: _Observed) -> _Remembered:
    return _Remembered(
        checksum=observed.checksum,
        object_id=observed.object_id,
        secondary_keys=dict(observed.ref.secondary_keys),
    )


def _upsert_change(observed: _Observed, kind: ChangeKind) -> ChangeRef:
    return ChangeRef(
        ref=observed.ref,
        kind=kind,
        last_modified_at=observed.last_modified_at,
        display_name=observed.display_name,
    )


def _vanished_change(
    native_key: str,
    remembered: _Remembered,
    *,
    entity_type: EntityType,
    endpoint: str,
    tenant_id: str,
) -> ChangeRef:
    """A ``DELETED`` report for an object there is no fresh row to describe.

    The census carried its ``secondary_keys`` forward for exactly this moment, so the ref
    is as complete as the one the object had while it existed.
    """
    return ChangeRef(
        ref=IdentityRef(
            endpoint=endpoint,
            entity_type=entity_type,
            native_key=native_key,
            tenant_id=tenant_id,
            secondary_keys=dict(remembered.secondary_keys),
        ),
        kind=ChangeKind.DELETED,
        display_name=remembered.secondary_keys.get("listing_name") or native_key,
    )


def _diff_structural(
    observed: Sequence[_Observed],
    previous: Mapping[str, _Remembered],
    stream: _StructuralStream,
    *,
    full_scan: bool,
    unknown_baseline: bool,
    endpoint: str,
    tenant_id: str,
) -> tuple[list[ChangeRef], dict[str, _Remembered]]:
    """Turn one scan plus the previous census into candidates and the next census.

    The census is *merged* on an incremental scan and *rebuilt* on a full one, which is
    the only honest reading of each: an incremental scan says nothing about the objects it
    did not match, while a full scan has seen everything that exists, so anything it did
    not see is genuinely gone.
    """
    live = {item.native_key: item for item in observed if not item.is_deleted}
    dropped = [item for item in observed if item.is_deleted]
    previous_by_id = {
        entry.object_id: native_key
        for native_key, entry in previous.items()
        if entry.object_id is not None
    }
    census: dict[str, _Remembered] = {} if full_scan else dict(previous)
    changes: list[ChangeRef] = []
    renamed_away: set[str] = set()

    for native_key in sorted(live):
        item = live[native_key]
        census[native_key] = _remember(item)
        prior = previous.get(native_key)
        if prior is not None and prior.checksum == item.checksum:
            continue
        if prior is not None:
            changes.append(_upsert_change(item, ChangeKind.UPDATED))
            continue

        previous_key = previous_by_id.get(item.object_id) if item.object_id else None
        if previous_key is not None and previous_key != native_key and previous_key not in live:
            renamed_away.add(previous_key)
            remembered = previous[previous_key]
            census.pop(previous_key, None)
            _logger.info(
                "snowflake.list_changed.rename_detected",
                endpoint=endpoint,
                entity_type=stream.entity_type.value,
                object_id=item.object_id,
                previous_native_key=previous_key,
                native_key=native_key,
            )
            if stream.id_is_declared_identity_key:
                # The engine can follow this identity itself through `object_id`, so the
                # object moved -- it was not deleted, and saying otherwise would orphan a
                # live object at the target.
                changes.append(_upsert_change(item, ChangeKind.UPDATED))
            else:
                # No id key is declared for this entity type, so the old FQN would rot in
                # the IdentityMap unreported. Telling the truth about both halves is the
                # best available answer -- see the module docstring's "Renames".
                changes.append(_upsert_change(item, ChangeKind.CREATED))
                changes.append(
                    _vanished_change(
                        previous_key,
                        remembered,
                        entity_type=stream.entity_type,
                        endpoint=endpoint,
                        tenant_id=tenant_id,
                    )
                )
            continue

        changes.append(
            _upsert_change(item, ChangeKind.UPSERT if unknown_baseline else ChangeKind.CREATED)
        )

    for item in sorted(dropped, key=lambda observed_item: observed_item.native_key):
        if item.native_key in renamed_away or item.native_key not in previous:
            # Either the row is the far side of a rename this poll already reported, or
            # this connector never told the engine the object existed. Reporting a delete
            # for something nobody was ever told about is noise, not honesty.
            continue
        census.pop(item.native_key, None)
        changes.append(
            ChangeRef(
                ref=item.ref,
                kind=ChangeKind.DELETED,
                last_modified_at=item.last_modified_at,
                display_name=item.display_name,
            )
        )

    if full_scan:
        for native_key in sorted(set(previous) - set(live) - renamed_away):
            changes.append(
                _vanished_change(
                    native_key,
                    previous[native_key],
                    entity_type=stream.entity_type,
                    endpoint=endpoint,
                    tenant_id=tenant_id,
                )
            )
    return changes, census


def _diff_listings(
    observed: Sequence[_Observed] | None,
    previous: Mapping[str, _Remembered],
    *,
    unknown_baseline: bool,
    endpoint: str,
    tenant_id: str,
) -> tuple[list[ChangeRef], dict[str, _Remembered]]:
    """``SHOW LISTINGS`` is always a complete listing, so this diff is a plain census diff.

    ``observed is None`` means the scan could not run at all: the previous census is
    carried forward untouched and nothing is reported, so a role that loses listing
    visibility neither loses its remembered state nor emits a wave of phantom deletes.
    """
    if observed is None:
        return [], dict(previous)

    fresh = {item.native_key: item for item in observed}
    census: dict[str, _Remembered] = {}
    changes: list[ChangeRef] = []
    for native_key in sorted(fresh):
        item = fresh[native_key]
        census[native_key] = _remember(item)
        prior = previous.get(native_key)
        if prior is not None and prior.checksum == item.checksum:
            continue
        if prior is not None:
            changes.append(_upsert_change(item, ChangeKind.UPDATED))
            continue
        changes.append(
            _upsert_change(item, ChangeKind.UPSERT if unknown_baseline else ChangeKind.CREATED)
        )

    for native_key in sorted(set(previous) - set(fresh)):
        changes.append(
            _vanished_change(
                native_key,
                previous[native_key],
                entity_type=EntityType.DATA_PRODUCT,
                endpoint=endpoint,
                tenant_id=tenant_id,
            )
        )
    return changes, census


# ----------------------------------------------------------------------------------
# Connector.list_changed() entry point
# ----------------------------------------------------------------------------------


def _require_matching_stream(since: Watermark, *, endpoint: str, entity_type: EntityType) -> None:
    """The engine must never hand this connector another stream's resume state."""
    if since.endpoint != endpoint or since.entity_type is not entity_type:
        raise ValueError(
            f"since watermark is for stream {since.stream_key!r}, not "
            f"{endpoint}:{entity_type.value!r}"
        )


async def list_changed_candidates(
    client: StatementClient,
    entity_type: EntityType,
    since: Watermark,
    *,
    endpoint: str,
    tenant_id: str,
    safety_margin: timedelta = DEFAULT_WATERMARK_SAFETY_MARGIN,
    rescan_overlap: timedelta = DEFAULT_RESCAN_OVERLAP,
) -> ListChangedResult:
    """Candidates changed since ``since``, plus the watermark this connector proposes.

    Named ``list_changed_candidates`` rather than ``list_changed`` for the same reason
    :func:`read_entity` is not called ``read``: ``__init__.py``'s ``Connector`` method has
    that name, and a module-level function sharing it would shadow confusingly at the one
    place both are in scope. ``__init__.py`` delegates straight to this.

    ``tenant_id`` is the account identifier
    (:attr:`~qlabs_connector_snowflake.auth.SnowflakeConfig.account_identifier`), passed in
    rather than derived here because ``ACCOUNT_USAGE`` rows carry no account column of
    their own -- unlike Unity Catalog's ``metastore_id``, which the Databricks connector
    reads off each row. It must be the same value the refs handed to :meth:`Connector.read`
    carry, or the engine's IdentityMap would split one object across two identities.

    ``safety_margin`` and ``rescan_overlap`` are the two knobs the ``ACCOUNT_USAGE`` lag
    forces; see the module docstring's "Change detection" section and
    :data:`DEFAULT_WATERMARK_SAFETY_MARGIN` for why they are arguments with module-constant
    defaults rather than config fields.

    Raises :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` for an entity type
    ``manifest.py`` declares unsupported (``GLOSSARY_TERM``, ``CATEGORY``), and
    :class:`ValueError` for a watermark belonging to a different stream. Every HTTP failure
    arrives already translated by
    :func:`~qlabs_connector_snowflake.auth.translate_snowflake_error`, via
    :class:`StatementClient`.
    """
    _require_matching_stream(since, endpoint=endpoint, entity_type=entity_type)
    stream = _STRUCTURAL_STREAMS.get(entity_type)
    if stream is None:
        raise CapabilityError(
            f"the Snowflake connector does not support list_changed for {entity_type!r}; "
            "manifest.py declares GLOSSARY_TERM and CATEGORY unsupported (no native "
            "glossary -- semantic views are a different structure, RS-05 section 5)",
            endpoint=endpoint,
            entity_type=entity_type,
            operation="list_changed",
        )

    snapshot = _decode_change_snapshot(since)
    full_scan = snapshot.high_water is None
    unknown_baseline = since.is_initial or full_scan
    lower_bound = None if snapshot.high_water is None else snapshot.high_water - rescan_overlap

    snowflake_now = await _read_snowflake_now(client, entity_type=entity_type)
    observed = await _scan_structural(
        client, stream, endpoint=endpoint, tenant_id=tenant_id, lower_bound=lower_bound
    )
    changes, objects = _diff_structural(
        observed,
        snapshot.objects,
        stream,
        full_scan=full_scan,
        unknown_baseline=unknown_baseline,
        endpoint=endpoint,
        tenant_id=tenant_id,
    )

    listings = snapshot.listings
    if entity_type is EntityType.DATA_PRODUCT:
        listing_rows = await _scan_listings(client, endpoint=endpoint, tenant_id=tenant_id)
        listing_changes, listings = _diff_listings(
            listing_rows,
            snapshot.listings,
            unknown_baseline=unknown_baseline,
            endpoint=endpoint,
            tenant_id=tenant_id,
        )
        changes.extend(listing_changes)

    # Held back by the margin, and never allowed to regress -- a watermark that moved
    # backwards would silently widen every later scan, and one that moved forward past the
    # propagation frontier would silently lose the changes still in flight.
    high_water = snowflake_now - safety_margin
    if snapshot.high_water is not None and snapshot.high_water > high_water:
        high_water = snapshot.high_water

    _logger.info(
        "snowflake.list_changed.complete",
        endpoint=endpoint,
        entity_type=entity_type.value,
        full_scan=full_scan,
        objects_observed=len(objects),
        listings_observed=len(listings),
        candidates=len(changes),
        high_water=high_water.isoformat(),
    )
    return ListChangedResult(
        changes=changes,
        next_watermark=Watermark.from_cursor(
            endpoint,
            entity_type,
            _encode_change_snapshot(high_water=high_water, objects=objects, listings=listings),
            observed_at=snowflake_now,
        ),
        # One call is one complete traversal: StatementClient has already drained every
        # partition of every statement above, so there is genuinely no next page.
        has_more=False,
    )
