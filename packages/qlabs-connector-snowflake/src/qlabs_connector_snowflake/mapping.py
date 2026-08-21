"""Snowflake source-to-neutral mapping.

WP6 / T6.5. Turns the raw rows ``read.py`` pulls off the SQL REST API -- one
``INFORMATION_SCHEMA.TABLES``/``.VIEWS``/``.SCHEMATA`` row, one ``SHOW LISTINGS`` /
``DESCRIBE LISTING`` row, a list of ``TAG_REFERENCES`` rows -- into their proper neutral
fields: ``COMMENT`` -> ``description`` (RS-05 sections 3.3/4.1), tag assignments ->
``tags`` and system classification tags -> ``classifications`` (RS-05 sections 3.4/4.2),
the owning **role** -> ``owners``, the table/view kind -> ``asset_type``, and the
fully-qualified name -> ``physical_ref``. Everything else round-trips verbatim through
``custom_attributes``.

Every function here is a **pure, synchronous** transform on a raw row
(``Mapping[str, Any]``): the only I/O in this connector's read path already happened by
the time these run, exactly as in ``qlabs_connector_databricks.mapping``. Each returns a
*fragment* dict -- only the neutral field key(s) it actually has something to say about
-- so ``read.py`` merges fragments with a plain ``dict.update()`` and a field this module
has nothing to report for is simply absent, never forced to a guessed value.

## Absent vs. explicit-empty

``envelope.py`` rule 10 is load-bearing: ``null`` and *absent* are different, and the
engine depends on the difference -- ``null`` clears the target field on the next sync,
absent leaves it alone. A Snowflake row genuinely can be either: a projection that never
selected ``COMMENT`` at all (this connector has nothing to say -- leave the target alone)
or a row carrying ``COMMENT`` as SQL ``NULL``/``''`` (a human cleared it -- the source is
asserting emptiness). :func:`map_description` and :func:`map_owners` both honor this,
matching the Databricks connector's identical split.

## Case-insensitive key matching (a Snowflake-specific wrinkle)

Snowflake returns ``INFORMATION_SCHEMA`` column names **upper-cased**
(``TABLE_NAME``, ``COMMENT``) and ``SHOW``/``DESCRIBE`` column names **lower-cased**
(``global_name``, ``subtitle``) -- the same logical field arrives under two spellings
depending on which surface produced it. Rather than normalizing the raw rows (which would
make ``custom_attributes`` a lie about what Snowflake actually returned), every lookup
here goes through :func:`lookup`, which matches case-insensitively, and
:func:`map_custom_attributes` excludes case-insensitively too. Raw keys are otherwise
preserved byte-for-byte.

## ``COMMENT`` -> ``description``: plain text, never markdown

A Snowflake ``COMMENT`` (RS-05 sections 1.3/3.3) is free text with no format contract and
no renderer named anywhere in the SQL or REST surface that produces it. Labelling it
:attr:`~qlabs_catalog_sync_sdk.models.TextFormat.MARKDOWN` would be a claim this module
has no basis for, and a wrong one corrupts content downstream -- a comment that happens to
start with ``#`` would render as a heading it was never meant to be. The **one** genuinely
markdown-shaped Snowflake field is a listing's long-form ``description``, which RS-05
section 3.6 documents explicitly as "Markdown, up to 7,500 chars"; that one, and only
that one, is mapped as markdown -- see the next section.

## A listing's ``subtitle`` vs. its ``description``: two different neutral fields

RS-05 sections 2.2/3.6 give a listing three descriptive text fields, not one: ``title``
(the name), ``subtitle`` (a one-line strapline) and ``description`` (long-form Markdown,
up to 7,500 chars). The neutral model has exactly the matching pair (RS-03 section 3.1):
``description`` is documented as "Short description" and ``documentation`` as "Long-form
readme/docs (markdown)". So:

* listing ``subtitle`` -> neutral ``description`` (plain text -- a strapline is not
  markdown),
* listing ``description`` -> neutral ``documentation`` (markdown),
* listing ``title`` -> neutral ``name``.

Mapping the listing's long ``description`` onto the neutral ``description`` because the
two share a spelling would put 7,500 characters of Markdown into a field every target
renders as a one-line summary. The spelling collision is a trap, and this is the
deliberate way through it.

## Tags: a Snowflake tag assignment is a ``(tag FQN, value)`` pair

RS-05 sections 1.3/3.4: a Snowflake tag is itself a **schema-level object** with its own
fully-qualified name (``db.schema.tag``), and assigning it to another object records a
value (an arbitrary string up to 256 chars, or SQL ``NULL`` for a bare assignment). That
is already the neutral :class:`~qlabs_catalog_sync_sdk.models.Tag` shape -- ``key`` plus
optional ``value`` -- so the mapping is direct, with two decisions worth stating:

* **``Tag.key`` is the tag's fully-qualified name, not its bare name.** RS-05 section 4.3
  names "the tag object's own FQN (``db.schema.tag``)" as the tag's identity, and two tags
  in different schemas may legitimately share a bare name (``governance.tags.owner`` and
  ``finance.tags.owner`` are different tags). Collapsing both to ``"OWNER"`` would merge
  two unrelated classifications into one neutral tag. The Databricks precedent
  (``sql_tags.py``: ``Tag(key=str(tag_name), ...)``) uses the bare name because Unity
  Catalog tags *have* no namespace -- the tag key is the whole identity there. Same
  principle, different native model.
* **``NULL`` and ``''`` never collapse into each other.** A tag assigned with no value
  comes back as SQL ``NULL`` -> :attr:`Tag.value` ``None``; a tag explicitly set to the
  empty string comes back as ``''`` -> :attr:`Tag.value` ``""``. Mirrors
  ``qlabs_connector_databricks.sql_tags._tag_from_row`` exactly.

Tag names are carried through verbatim -- never case-folded.

## System classification tags -> ``classifications``, kept out of ``tags``

RS-05 section 4.2 is explicit that ``SNOWFLAKE.CORE.SEMANTIC_CATEGORY`` and
``SNOWFLAKE.CORE.PRIVACY_CATEGORY`` are "produced by Snowflake's classification engine...
treat the system values as machine-generated rather than something to overwrite". They
are therefore routed to the neutral ``classifications`` list and **excluded** from
``tags``: mixing a machine-generated category in among the tags a human authored would
make an upstream-only sync present generated metadata as curated metadata.

The rendering is ``"<BARE_TAG_NAME>=<VALUE>"`` -- ``"PRIVACY_CATEGORY=IDENTIFIER"``,
``"SEMANTIC_CATEGORY=NAME"``. Both halves are needed (the category *kind* and the
category *value* are different facts and neither implies the other), while the constant
``SNOWFLAKE.CORE.`` prefix carries no information once
:func:`is_system_classification_tag` has already established these are system tags.
Classification is applied per **column** (RS-05 section 1.3), so a table's list aggregates
its columns' categories; duplicates are collapsed, first-seen order kept, since the same
category applying to four columns is one fact about the table, not four.

## Owner: a Snowflake owner is a **role**, not a person -- and never an email

Every Snowflake object has an owning **role** (the role holding OWNERSHIP; RS-05 sections
1.3/4.2), not a user and not an address. This is a real divergence from the Databricks
connector, where ``owner`` genuinely is a user email / group name / service-principal id
and ``qlabs_connector_databricks.mapping.owner_party`` classifies it into
:attr:`Party.email` or :attr:`Party.party_id` accordingly.

Here there is nothing to classify: ``ACCOUNTADMIN``, ``SALES_ENGINEER`` and
``SYSADMIN`` are role names. They go to :attr:`Party.display_name`, and
:attr:`Party.email` is **never** populated -- not even by deriving one. The project
guardrail is "owners are best-effort metadata, correlated on email; never an identity
system", and the engine's own owner correlation keys on
:attr:`Party.email`; writing a fabricated address there would make a role silently
correlate against a real person's Qlik account. An empty ``email`` means "this connector
has no email for this owner", which is the truth. The role name additionally round-trips
verbatim in ``custom_attributes`` (it is never excluded from the passthrough by
:data:`MAPPED_SOURCE_FIELDS` alone -- see :func:`map_custom_attributes`), so a downstream
consumer that *does* have a role-to-person directory has the raw role to work from.

A listing carries an owner role the same way (``SHOW LISTINGS``' ``owner`` column) and is
mapped identically.

## Table/view kind -> ``asset_type``

RS-05 section 1.2 lists the kinds: tables (standard, transient, external, dynamic, event,
Apache Iceberg) and views (standard, materialized, secure, semantic). The neutral
:class:`~qlabs_catalog_sync_sdk.models.AssetType` vocabulary is coarser -- ``TABLE`` /
``VIEW`` / ``VOLUME`` / ``DATASET`` / ``OTHER`` -- so every table kind collapses to
``TABLE`` and every view kind to ``VIEW``, with the *native* kind preserved verbatim in
``custom_attributes`` (``TABLE_TYPE``, ``IS_TRANSIENT``, ``IS_ICEBERG``, ``IS_DYNAMIC``,
``IS_SECURE``, ``IS_MATERIALIZED``, ...) so nothing is lost. An unrecognized or missing
kind becomes ``OTHER`` rather than a guess. ``VOLUME`` never appears: Snowflake stages are
a different object type this connector does not read.

## Listing categories, business needs, data attributes, compliance badges

RS-05 section 2.2 lists them as first-class listing-manifest metadata; RS-03 has no
neutral field for any of them. They round-trip through ``custom_attributes`` untouched
(:func:`map_custom_attributes` only ever *removes* keys, never renames or re-nests one),
so a target that understands them can read them and a sync round trip cannot lose them.

## Read-only, structurally

Every field this module produces comes from a **read-only source connector** (v1 scope
guardrail). Nothing here is ever a write target by construction: the connector's inherited
``create``/``update``/``delete`` refuse with ``CapabilityError``, so no mapped or
passed-through value can reach a write path at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import JsonValue

from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProductStatus,
    Party,
    PartyRole,
    Tag,
    TextField,
)

__all__ = [
    "MAPPED_LISTING_FIELDS",
    "MAPPED_SOURCE_FIELDS",
    "SYSTEM_TAG_DATABASE",
    "SYSTEM_TAG_SCHEMA",
    "TagReference",
    "asset_type_for_object",
    "is_system_classification_tag",
    "lookup",
    "map_classifications",
    "map_custom_attributes",
    "map_data_product_fields",
    "map_dataset_fields",
    "map_description",
    "map_documentation",
    "map_listing_fields",
    "map_owners",
    "map_physical_ref",
    "map_status",
    "map_tags",
    "owner_party",
    "tag_reference_from_row",
]

SYSTEM_TAG_DATABASE: Final[str] = "SNOWFLAKE"
"""Database half of the system classification tags' namespace (RS-05 sections 1.3/4.2)."""

SYSTEM_TAG_SCHEMA: Final[str] = "CORE"
"""Schema half of the same namespace: ``SNOWFLAKE.CORE.SEMANTIC_CATEGORY`` and
``SNOWFLAKE.CORE.PRIVACY_CATEGORY`` are the two tags Snowflake's classification engine
assigns."""

MAPPED_SOURCE_FIELDS: Final[frozenset[str]] = frozenset({"comment"})
"""Raw column names promoted to their own neutral field and therefore excluded from
:func:`map_custom_attributes`'s passthrough. Deliberately just ``COMMENT``: unlike the
Databricks connector, the owner column is *not* excluded -- the neutral ``owners`` entry
carries only a role display name, so keeping the raw ``TABLE_OWNER``/``SCHEMA_OWNER``
value in ``custom_attributes`` preserves which native column it came from for a consumer
that has a role directory. Matched case-insensitively (see the module docstring)."""

MAPPED_LISTING_FIELDS: Final[frozenset[str]] = frozenset({"title", "subtitle", "description"})
"""The listing columns :func:`map_listing_fields` promotes to ``name`` / ``description`` /
``documentation``. ``state``/``review_state`` are **not** listed: :func:`map_status` maps
them to a *lossy* neutral enum (three Snowflake states collapse into four neutral ones and
``review_state`` has no neutral home at all), so both stay in the passthrough as well.
``global_name``/``name`` likewise stay -- they are identity, and ``read.py`` excludes them
structurally where appropriate."""

# RS-05 section 1.2's table kinds. Snowflake reports the kind in TABLE_TYPE, whose exact
# vocabulary is TENANT-UNVERIFIED (RS-05 names the kinds, not the column's string values);
# both the spaced ("BASE TABLE") and underscored ("BASE_TABLE") spellings are accepted so a
# tenant that returns either is handled.
_TABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "BASE TABLE",
        "BASE_TABLE",
        "TABLE",
        "LOCAL TEMPORARY",
        "TEMPORARY TABLE",
        "TRANSIENT TABLE",
        "EXTERNAL TABLE",
        "EVENT TABLE",
        "DYNAMIC TABLE",
        "ICEBERG TABLE",
        "HYBRID TABLE",
    }
)

_VIEW_TYPES: Final[frozenset[str]] = frozenset(
    {
        "VIEW",
        "MATERIALIZED VIEW",
        "SECURE VIEW",
        "SEMANTIC VIEW",
    }
)

# RS-05 sections 3.6/4.4: a listing moves through a review/publish lifecycle, and "a sync
# that manages listings must model draft vs published state, not just field values".
# TENANT-UNVERIFIED: RS-05 names the PUBLISH/REVIEW matrix but not the exact strings
# SHOW/DESCRIBE LISTING report in the `state` column.
_LISTING_STATE_TO_STATUS: Final[dict[str, DataProductStatus]] = {
    "DRAFT": DataProductStatus.DRAFT,
    "PUBLISHED": DataProductStatus.ACTIVE,
    "LIVE": DataProductStatus.ACTIVE,
    "UNPUBLISHED": DataProductStatus.ARCHIVED,
}


# ----------------------------------------------------------------------------------
# Case-insensitive raw-row access
# ----------------------------------------------------------------------------------


def lookup(raw: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    """``(present, value)`` for ``key`` in ``raw``, matched case-insensitively.

    Returns the presence flag separately from the value because absent and ``None`` are
    different answers everywhere in this module (see the module docstring's "Absent vs.
    explicit-empty"): ``(False, None)`` means the row never carried the column,
    ``(True, None)`` means it carried it as SQL ``NULL``.

    An exact match wins; otherwise the first case-insensitive match in iteration order is
    used. Snowflake never returns two columns differing only in case from one projection,
    so "first match" is unambiguous in practice.
    """
    if key in raw:
        return True, raw[key]
    folded = key.casefold()
    for candidate, value in raw.items():
        if candidate.casefold() == folded:
            return True, value
    return False, None


def _first_present(raw: Mapping[str, Any], keys: Sequence[str]) -> tuple[bool, Any]:
    """The first of ``keys`` present in ``raw`` (case-insensitively), with its value."""
    for key in keys:
        present, value = lookup(raw, key)
        if present:
            return True, value
    return False, None


def _text_of(value: Any) -> str:
    """``value`` as text, treating SQL ``NULL`` and any non-string as empty.

    Never raises and never coerces a number into a description: a column that should be
    text but is not is treated as "the source has nothing here", the same conservative
    reading ``qlabs_connector_databricks.mapping.map_description`` takes.
    """
    return value if isinstance(value, str) else ""


# ----------------------------------------------------------------------------------
# Tag references
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TagReference:
    """One row of ``TAG_REFERENCES``: a tag assignment on one object or column.

    Declared here rather than in ``read.py`` because everything interesting about a tag
    reference is a *mapping* decision (which neutral field it lands in, how its key is
    spelled, whether it is a system classification), and ``read.py`` imports this module
    rather than the other way round.

    ``column_name`` is set for a column-level assignment and ``None`` for an
    object-level one; both aggregate onto the owning entity's neutral fields, since the
    neutral model has no column entity (RS-03 section 3).
    """

    tag_database: str
    tag_schema: str
    tag_name: str
    tag_value: str | None
    object_database: str | None = None
    object_schema: str | None = None
    object_name: str | None = None
    column_name: str | None = None
    domain: str | None = None

    @property
    def tag_fqn(self) -> str:
        """``DATABASE.SCHEMA.TAG`` -- the tag object's own identity (RS-05 section 4.3)."""
        return f"{self.tag_database}.{self.tag_schema}.{self.tag_name}"

    @property
    def object_fqn(self) -> str | None:
        """``DATABASE.SCHEMA.OBJECT`` for the tagged object, when the row carries it."""
        if not (self.object_database and self.object_schema and self.object_name):
            return None
        return f"{self.object_database}.{self.object_schema}.{self.object_name}"


def tag_reference_from_row(row: Mapping[str, Any]) -> TagReference | None:
    """Build a :class:`TagReference` from one raw ``TAG_REFERENCES`` row.

    Returns ``None`` for a row with no usable tag name -- a row that cannot name the tag
    it is asserting carries no information, and dropping it is better than emitting a
    :class:`~qlabs_catalog_sync_sdk.models.Tag` with an empty key (which
    :class:`Tag`'s own ``min_length=1`` validator would reject anyway).

    Column names are matched case-insensitively, so the same function serves both the
    ``INFORMATION_SCHEMA.TAG_REFERENCES`` table function and
    ``SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES`` (RS-05 sections 1.4/3.4).
    """
    _, tag_name = lookup(row, "TAG_NAME")
    if not isinstance(tag_name, str) or not tag_name:
        return None
    _, tag_value = lookup(row, "TAG_VALUE")
    _, tag_database = lookup(row, "TAG_DATABASE")
    _, tag_schema = lookup(row, "TAG_SCHEMA")
    _, object_database = lookup(row, "OBJECT_DATABASE")
    _, object_schema = lookup(row, "OBJECT_SCHEMA")
    _, object_name = lookup(row, "OBJECT_NAME")
    _, column_name = lookup(row, "COLUMN_NAME")
    _, domain = lookup(row, "DOMAIN")
    return TagReference(
        tag_database=_text_of(tag_database),
        tag_schema=_text_of(tag_schema),
        tag_name=tag_name,
        tag_value=None if tag_value is None else str(tag_value),
        object_database=object_database if isinstance(object_database, str) else None,
        object_schema=object_schema if isinstance(object_schema, str) else None,
        object_name=object_name if isinstance(object_name, str) else None,
        column_name=column_name if isinstance(column_name, str) else None,
        domain=domain if isinstance(domain, str) else None,
    )


def is_system_classification_tag(reference: TagReference) -> bool:
    """True for a tag Snowflake's classification engine produced (RS-05 section 4.2).

    Matched on the full ``SNOWFLAKE.CORE`` namespace rather than the bare tag name: a
    user-defined tag legitimately named ``PRIVACY_CATEGORY`` in their own schema is
    authored metadata and belongs in ``tags``, not in ``classifications``.
    """
    return (
        reference.tag_database.upper() == SYSTEM_TAG_DATABASE
        and reference.tag_schema.upper() == SYSTEM_TAG_SCHEMA
    )


def map_tags(references: Sequence[TagReference] | None) -> dict[str, list[Tag]]:
    """Tag assignments -> ``{"tags": [...]}``, or ``{}`` when tags were not read at all.

    ``None`` means the tag read never ran, so the fragment is empty and the engine leaves
    the target's tags alone. An empty sequence means the read ran and the object genuinely
    has no user-defined tags -- a real answer worth syncing, so it produces an explicit
    ``[]``. This is exactly the "structural unavailable vs. no tags" distinction
    ``qlabs_connector_databricks.sql_tags`` draws with ``CatalogTagIndex | None``.

    System classification tags are excluded here and reported by :func:`map_classifications`
    instead (module docstring). Duplicate ``(key, value)`` pairs -- the same tag assigned to
    several columns of one table -- are collapsed, first-seen order kept.
    """
    if references is None:
        return {}
    tags: list[Tag] = []
    seen: set[tuple[str, str | None]] = set()
    for reference in references:
        if is_system_classification_tag(reference):
            continue
        identity = (reference.tag_fqn, reference.tag_value)
        if identity in seen:
            continue
        seen.add(identity)
        tags.append(Tag(key=reference.tag_fqn, value=reference.tag_value))
    return {"tags": tags}


def map_classifications(references: Sequence[TagReference] | None) -> dict[str, list[str]]:
    """System classification tags -> ``{"classifications": [...]}``, or ``{}`` if unread.

    Same ``None``-vs-empty contract as :func:`map_tags`. Each entry renders as
    ``"<BARE_TAG_NAME>=<VALUE>"`` -- see the module docstring for why both halves are kept
    and the constant ``SNOWFLAKE.CORE.`` prefix is not. A system tag with a ``NULL`` value
    contributes its bare name alone; there is no ``"NAME=None"`` in the output.
    """
    if references is None:
        return {}
    classifications: list[str] = []
    seen: set[str] = set()
    for reference in references:
        if not is_system_classification_tag(reference):
            continue
        rendered = (
            reference.tag_name
            if reference.tag_value is None
            else f"{reference.tag_name}={reference.tag_value}"
        )
        if rendered in seen:
            continue
        seen.add(rendered)
        classifications.append(rendered)
    return {"classifications": classifications}


# ----------------------------------------------------------------------------------
# COMMENT -> description / documentation
# ----------------------------------------------------------------------------------


def map_description(
    raw: Mapping[str, Any], *, source_key: str = "COMMENT"
) -> dict[str, TextField | None]:
    """``COMMENT`` -> ``{"description": ...}``, or ``{}`` if the row never carried it.

    Plain text, never markdown -- see the module docstring. Absent vs. explicit-empty is
    deliberate: a row without the column produces no fragment at all (leave the target
    alone), while ``NULL``/``''``/a non-string produces an explicit ``None`` (the source
    asserts there is no description).
    """
    present, comment = lookup(raw, source_key)
    if not present:
        return {}
    text = _text_of(comment)
    if not text:
        return {"description": None}
    return {"description": TextField.plain(text)}


def map_documentation(
    raw: Mapping[str, Any], *, source_key: str = "description"
) -> dict[str, TextField | None]:
    """A listing's long-form ``description`` -> ``{"documentation": ...}``, as **markdown**.

    The one Snowflake text field with a documented format: RS-05 section 3.6 specifies the
    listing manifest's ``description`` as "Markdown, up to 7,500 chars". Same absent-vs-
    explicit-empty contract as :func:`map_description`. Only ever called for the
    listing-shaped data product -- a schema has no long-form doc surface distinct from its
    ``COMMENT``.
    """
    present, value = lookup(raw, source_key)
    if not present:
        return {}
    text = _text_of(value)
    if not text:
        return {"documentation": None}
    return {"documentation": TextField.markdown(text)}


# ----------------------------------------------------------------------------------
# Owning role -> owners
# ----------------------------------------------------------------------------------


def owner_party(owner: object) -> Party | None:
    """One raw owner **role** name -> a :class:`Party`, or ``None`` if it carries nothing.

    Always :attr:`Party.display_name` with :attr:`PartyRole.OWNER`, never
    :attr:`Party.email` and never :attr:`Party.party_id` -- a Snowflake owner is a role,
    not a person and not an address (module docstring). ``None`` for anything that is not
    a non-blank string, matching :class:`Party`'s own "at least one identifying attribute"
    validator.
    """
    if not isinstance(owner, str):
        return None
    value = owner.strip()
    if not value:
        return None
    return Party(display_name=value, role=PartyRole.OWNER)


def map_owners(
    raw: Mapping[str, Any],
    *,
    source_keys: Sequence[str] = ("TABLE_OWNER", "SCHEMA_OWNER", "OWNER"),
) -> dict[str, list[Party]]:
    """The owning role -> ``{"owners": [...]}``, or ``{}`` if no owner column was selected.

    ``source_keys`` is a fallback chain rather than a single name because the column
    differs per surface: ``INFORMATION_SCHEMA.TABLES``/``.VIEWS`` call it ``TABLE_OWNER``,
    ``.SCHEMATA`` calls it ``SCHEMA_OWNER``, and ``SHOW LISTINGS``/``SHOW SHARES`` call it
    ``owner``. The first key actually present wins, so one function serves every entity
    shape without the caller having to know which surface produced the row.

    Absent vs. explicit-empty as everywhere else: no owner column at all produces no
    fragment; a present-but-blank/``NULL``/non-string owner produces an explicit ``[]``.
    Snowflake reports exactly one owning role per object (RS-05 section 1.3), so the list
    never holds more than one element.
    """
    present, owner = _first_present(raw, source_keys)
    if not present:
        return {}
    party = owner_party(owner)
    return {"owners": [party] if party is not None else []}


# ----------------------------------------------------------------------------------
# Kind -> asset_type
# ----------------------------------------------------------------------------------


def asset_type_for_object(raw: Mapping[str, Any]) -> AssetType:
    """The neutral :class:`AssetType` for one ``TABLES``/``VIEWS`` row.

    Reads ``TABLE_TYPE`` first (RS-05 section 1.2's kinds), then falls back to the boolean
    flag columns a row can carry instead: an ``INFORMATION_SCHEMA.VIEWS`` row has no
    ``TABLE_TYPE`` at all but is unambiguously a view, and ``IS_ICEBERG``/``IS_DYNAMIC``/
    ``IS_TRANSIENT`` mark table sub-kinds that all collapse to ``TABLE``.

    Anything unrecognized -- a future Snowflake object kind, a missing column -- becomes
    :attr:`AssetType.OTHER` rather than a guess, mirroring
    ``qlabs_connector_databricks.read.asset_type_for_table``.
    """
    _, table_type = lookup(raw, "TABLE_TYPE")
    if isinstance(table_type, str):
        normalized = table_type.strip().upper()
        if normalized in _VIEW_TYPES:
            return AssetType.VIEW
        if normalized in _TABLE_TYPES:
            return AssetType.TABLE
        if normalized:
            return AssetType.OTHER
    if _is_yes(raw, "IS_MATERIALIZED") or _is_yes(raw, "IS_SECURE"):
        # Only INFORMATION_SCHEMA.VIEWS carries these two columns.
        return AssetType.VIEW
    if _is_yes(raw, "IS_ICEBERG") or _is_yes(raw, "IS_DYNAMIC") or _is_yes(raw, "IS_TRANSIENT"):
        return AssetType.TABLE
    return AssetType.OTHER


def _is_yes(raw: Mapping[str, Any], key: str) -> bool:
    """Snowflake's ``YES``/``NO`` flag columns, tolerating a real boolean too.

    ``INFORMATION_SCHEMA`` reports these as the strings ``'YES'``/``'NO'``, but the SQL
    REST API's JSON encoding of a boolean column is TENANT-UNVERIFIED, so a genuine
    ``true`` is accepted as well rather than silently read as "no".
    """
    _, value = lookup(raw, key)
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().upper() in {"YES", "TRUE", "Y"}


# ----------------------------------------------------------------------------------
# FQN -> physical_ref
# ----------------------------------------------------------------------------------


def map_physical_ref(raw: Mapping[str, Any]) -> dict[str, str]:
    """``DATABASE.SCHEMA.OBJECT`` -> ``{"physical_ref": ...}``, or ``{}`` if unbuildable.

    Dataset-only: :class:`~qlabs_catalog_sync_sdk.models.DataProduct` has no
    ``physical_ref`` field (RS-03 section 3.1), so a caller must never call this for a
    schema or a listing. Prefers an already-assembled ``FULLY_QUALIFIED_NAME`` column when
    the row carries one, otherwise assembles it from the three catalog/schema/name columns
    -- unquoted and dot-separated, exactly the form RS-05 sections 1.2/4.3 name as the
    primary matching key.
    """
    present, assembled = lookup(raw, "FULLY_QUALIFIED_NAME")
    if present and isinstance(assembled, str) and assembled:
        return {"physical_ref": assembled}
    _, database = _first_present(raw, ("TABLE_CATALOG", "CATALOG_NAME"))
    _, schema = _first_present(raw, ("TABLE_SCHEMA", "SCHEMA_NAME"))
    _, name = _first_present(raw, ("TABLE_NAME", "NAME"))
    if not all(isinstance(part, str) and part for part in (database, schema, name)):
        return {}
    return {"physical_ref": f"{database}.{schema}.{name}"}


# ----------------------------------------------------------------------------------
# Listing publish state -> status
# ----------------------------------------------------------------------------------


def map_status(raw: Mapping[str, Any], *, source_key: str = "state") -> dict[str, Any]:
    """A listing's publish state -> ``{"status": DataProductStatus | None}``, or ``{}``.

    RS-05 section 4.4: "a sync that manages listings must model draft vs published state,
    not just field values", and section 4.2 lists publish status among the read-only
    outputs. ``DRAFT`` -> ``DRAFT``, ``PUBLISHED``/``LIVE`` -> ``ACTIVE``, ``UNPUBLISHED``
    -> ``ARCHIVED`` (the listing was published and has been withdrawn -- ``DEPRECATED``
    would imply a successor, which Snowflake does not assert).

    An unrecognized state produces an explicit ``None`` rather than a guess: the source
    said something, this connector could not read it, and inventing ``ACTIVE`` would
    publish a draft downstream. A row with no state column at all produces no fragment.
    Only ever meaningful for the listing shape -- a Snowflake schema has no lifecycle.
    """
    present, state = lookup(raw, source_key)
    if not present:
        return {}
    if not isinstance(state, str):
        return {"status": None}
    return {"status": _LISTING_STATE_TO_STATUS.get(state.strip().upper())}


# ----------------------------------------------------------------------------------
# Passthrough -> custom_attributes
# ----------------------------------------------------------------------------------


def map_custom_attributes(
    raw: Mapping[str, Any], *, exclude: frozenset[str] = MAPPED_SOURCE_FIELDS
) -> dict[str, JsonValue]:
    """Every column of ``raw`` not in ``exclude``, verbatim.

    A pure exclusion -- never a rename, never a re-nesting -- so a value Snowflake returns
    as nested JSON (a listing's ``data_dictionary``, ``categories``, ``business_needs``,
    ``data_attributes``, ``compliance_badges``) comes out byte-identical to what the source
    reported. Matching is case-insensitive on both sides, so a caller can pass the
    exclusion set in either spelling regardless of which surface produced the row.

    ``exclude`` **replaces** the default rather than adding to it, unlike the Databricks
    connector's equivalent, which always folds its ``MAPPED_SOURCE_FIELDS`` in on top of
    the caller's set. The difference is forced by Snowflake having two surfaces with
    different promotion rules: ``COMMENT`` is promoted to ``description`` on an
    ``INFORMATION_SCHEMA`` row, but a listing's ``comment`` column is a *different* field
    that nothing promotes (its ``subtitle`` becomes the neutral ``description`` instead),
    so an unconditional exclusion would drop the listing's comment on the floor. Callers
    that want both union the sets explicitly at the call site -- see ``read.py``'s
    ``_OBJECT_STRUCTURAL_FIELDS`` / ``_LISTING_STRUCTURAL_FIELDS``.
    """
    skip = {key.casefold() for key in exclude}
    return {key: value for key, value in raw.items() if key.casefold() not in skip}


# ----------------------------------------------------------------------------------
# Per-entity convenience: everything this module has to say about one raw row
# ----------------------------------------------------------------------------------


def map_data_product_fields(
    raw_schema: Mapping[str, Any], *, tag_references: Sequence[TagReference] | None = None
) -> dict[str, Any]:
    """Every content mapping for one ``INFORMATION_SCHEMA.SCHEMATA`` row, merged.

    Ready to ``dict.update()`` into ``read.py``'s ``values`` mapping (before
    ``build_field_envelopes``) and to unpack as keyword arguments into
    ``DataProduct(...)`` -- the fragment's keys are exactly that model's field names.
    No ``physical_ref`` (a data product has none) and no ``documentation``/``status``
    (a Snowflake schema has neither -- both belong to the listing shape only).
    """
    fields: dict[str, Any] = {}
    fields.update(map_description(raw_schema))
    fields.update(map_owners(raw_schema))
    fields.update(map_tags(tag_references))
    return fields


def map_listing_fields(
    raw_listing: Mapping[str, Any], *, tag_references: Sequence[TagReference] | None = None
) -> dict[str, Any]:
    """Every content mapping for one merged ``SHOW LISTINGS`` + ``DESCRIBE LISTING`` row.

    Same contract as :func:`map_data_product_fields`, but for the listing shape: the
    ``subtitle``/``description`` split into neutral ``description``/``documentation``
    (module docstring) plus the publish-state ``status``. ``name`` is set by ``read.py``
    from the listing ``title`` -- it is required by the neutral model and so cannot be an
    optional fragment.
    """
    fields: dict[str, Any] = {}
    fields.update(map_description(raw_listing, source_key="subtitle"))
    fields.update(map_documentation(raw_listing))
    fields.update(map_status(raw_listing))
    fields.update(map_owners(raw_listing, source_keys=("owner",)))
    fields.update(map_tags(tag_references))
    return fields


def map_dataset_fields(
    raw_object: Mapping[str, Any], *, tag_references: Sequence[TagReference] | None = None
) -> dict[str, Any]:
    """Every content mapping for one ``INFORMATION_SCHEMA.TABLES``/``.VIEWS`` row, merged.

    Same contract as :func:`map_data_product_fields`, plus ``physical_ref`` and
    ``classifications``. ``asset_type`` is deliberately *not* here: ``read.py`` sets it
    structurally from :func:`asset_type_for_object` while building the entity, mirroring
    how the Databricks connector keeps ``table_type`` classification in its read path.
    """
    fields: dict[str, Any] = {}
    fields.update(map_description(raw_object))
    fields.update(map_owners(raw_object))
    fields.update(map_physical_ref(raw_object))
    fields.update(map_tags(tag_references))
    fields.update(map_classifications(tag_references))
    return fields
