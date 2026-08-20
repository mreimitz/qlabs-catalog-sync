"""Databricks source-to-neutral mapping.

WP4 / T4.5 (Sonnet). Turns the raw Unity Catalog fields T4.4's read path already carries
verbatim in each entity's ``custom_attributes`` sidecar (see ``read.py``'s module docstring)
into their proper neutral fields: ``comment`` -> ``description``, ``owner`` -> :class:`Party`,
``full_name`` -> ``physical_ref`` (Dataset only), and ``properties`` staying a lossless,
verbatim ``custom_attributes`` entry once the fields promoted to their own neutral home are
pulled out of the passthrough bag. ``table_type`` -> :class:`AssetType` is already correctly
implemented in ``read.py`` (:func:`~qlabs_connector_databricks.read.asset_type_for_table`) --
it is a *structural* classification made while paging, not a content mapping, so it stays
there; nothing in this module touches it.

Every function here is a **pure, synchronous** transform on a raw UC payload
(``Mapping[str, Any]``) -- the only I/O in this connector's read path already happened by the
time these run (T4.4's HTTP paging), and nothing here does I/O of its own, matching the
engine's own ``qlabs_catalog_sync.party`` correlation module. Each function returns a
*fragment* dict -- only the neutral field key(s) it actually has something to say about -- so a
caller merges fragments into ``read.py``'s ``values`` mapping (and the entity constructor's
keyword arguments) with a plain ``dict.update()``: a field this module has nothing to report
for is simply absent from the fragment, never forced to a guessed value.

## Absent vs. explicit-empty (why a fragment can be ``{}`` or ``{"description": None}``)

``envelope.py`` rule 10 is load-bearing here: ``null`` and *absent* are different, and the
engine depends on the difference -- ``null`` clears the target field on the next sync, absent
leaves it alone. A UC field genuinely can be either: the payload can omit ``comment`` entirely
(this connector has nothing to say -- leave Qlik's copy alone) or carry ``"comment": ""`` /
explicit ``null`` (a human cleared it -- the source is asserting emptiness). :func:`map_description`
and :func:`map_owners` both honor this: a source key missing from the raw payload produces an
empty fragment (nothing merged into ``values``, so no envelope is built and the field is never
reported "changed"); a source key present but empty produces a fragment carrying an explicit
empty value (``None`` for ``description``, ``[]`` for ``owners``), which *does* get an envelope
and *does* count as a value -- a deliberate "the source says there is nothing here". Getting
this backwards is the difference between a no-op sync cycle and silently wiping a description
in a customer's Qlik tenant.

## ``comment`` -> ``description``: plain text, never markdown

Unity Catalog's ``comment`` (RS-01 section 1.2) is documented as "an open-ended free-text
field", with no format contract and no markdown renderer named anywhere in the Databricks
REST/UC surface that produces it. Labeling it
:attr:`~qlabs_catalog_sync_sdk.models.TextFormat.MARKDOWN` would be a claim this module has no
basis for, and a wrong one actively corrupts content downstream -- a comment that happens to
start with ``#`` or contain a stray ``_pair_`` would render as a heading or italics it was
never meant to be.  :attr:`~qlabs_catalog_sync_sdk.models.TextFormat.PLAIN` is therefore not a
hedge, it is the accurate reading of what UC actually promises.

## ``owner`` -> ``Party``: three shapes, told apart structurally, never guessed

RS-01 section 4.1 states plainly that "Principals (owner, grants) are identified by user email,
group name, or service principal application ID" -- three shapes sharing one free-text field,
and they are not the same kind of value:

* An **email** (``user@example.com``-shaped: an ``@`` splitting a local part from a domain part
  that itself contains a ``.``) lands in :attr:`Party.email`. This is the only shape D3 and the
  engine's own ``qlabs_catalog_sync.party`` owner correlation key on.
* A **service-principal application ID** is a UUID (Databricks/Azure AD's standard
  application-id format, ``8-4-4-4-12`` hex digits). It is not an email and must never be
  written to :attr:`Party.email` -- ``party.py``'s own docstring is explicit that doing so
  would let a bare identifier be silently treated as though it correlated. It lands in
  :attr:`Party.party_id` instead, recognizable by the same UUID shape on the way back out.
* **Everything else** -- a UC group name, or any owner string that is neither shape above --
  lands in :attr:`Party.display_name`. This module cannot tell a group name from some other
  free-form principal label without calling an identity API it does not have (SCIM is out of
  scope for a v1 read-only connector), so it does not try to guess further: "not an email, not
  a UUID" is itself the detectable, structural boundary, and the honest treatment for anything
  that falls in it is "a human-readable label" -- exactly what ``display_name`` means.

Every non-blank owner gets :attr:`PartyRole.OWNER` -- UC's field is *literally* named ``owner``,
so this is not a choice among the neutral role vocabulary, it is the one honest reading of it.

## ``properties`` -> ``custom_attributes``: verbatim, one level, never flattened

:func:`map_custom_attributes` returns every raw field not consumed elsewhere, unchanged --
``properties`` included, still nested exactly one level under its own ``"properties"`` key, the
same shape Databricks itself returns. Two mistakes this deliberately avoids: flattening
``properties``'s own keys into the same top-level namespace as sibling raw fields (a UC
property can be named anything, including something that collides with a raw field name like
``comment`` or ``owner`` -- flattening would let one silently clobber the other), and wrapping
it in an *extra* layer (``custom_attributes["properties"]["properties"]``, corrupting the one
level of nesting UC itself uses and breaking every consumer reading a specific property by
name). Neither transform happens here -- this function only ever *removes* keys (the ones
promoted to their own neutral field), never renames or re-nests one, so
``custom_attributes["properties"]`` continues to equal Databricks' ``properties`` value,
byte-identical, nested structures and mixed scalar types included.

## ``documentation``, ``status``, ``placement``, ``tags``, ``classifications`` -- not this module

The manifest (T4.2) declares ``documentation``, ``status`` and ``placement`` ``na`` for every
Databricks entity -- Unity Catalog has no long-form doc surface distinct from ``comment``, no
lifecycle enum, and no space/domain placement concept -- so nothing here ever sets them; they
stay at the neutral model's own default (``None``). ``tags`` and ``classifications`` are D6's
conditional fields, read over the Statement Execution API rather than the REST endpoints T4.4
pages over -- that is **T4.7**'s ``sql_tags.py``, in flight now. This module does not import it
and does not set either field; they stay at their neutral default (``[]``), exactly as T4.4's
``read.py`` already left them. That is the seam: whoever wires T4.7 in adds its own fragment
(``{"tags": [...]}`` and, for a table, ``{"classifications": [...]}``) beside the ones this
module returns, the same ``dict.update()`` way.

## Read-only, structurally -- not by convention

Every field this module (and T4.4's ``read.py``) reads comes from a **read-only source
connector** (v1 scope guardrail; ``manifest.py``'s own docstring: "every field declared below
is ``ro`` or ``na``, never ``rw``"). Lineage, audit stamps (``created_at``/``created_by``,
``updated_at``/``updated_by``, ``metastore_id``), generated identifiers, ``storage_location``,
``table_type`` and ``data_source_format`` are consequently never emitted as a write target by
*construction*, not by a check this module performs: the connector's inherited ``create``/
``update``/``delete`` methods are unimplemented and refuse with ``CapabilityError`` (v1 scope
guardrail; ``__init__.py``'s own comment), so nothing produced here -- mapped or passed through
-- ever reaches a write path in the first place. Audit stamps and generated identifiers are
simply never in UC's ``properties`` map to begin with (Databricks keeps them as separate
top-level fields), so this module never mentions them; ``storage_location``, ``table_type`` and
``data_source_format`` remain available as descriptive metadata through the passthrough bag
(``read.py``'s existing structural-field exclusion already keeps identifiers and audit stamps
out of it -- see ``_SCHEMA_STRUCTURAL_FIELDS``/``_TABLE_STRUCTURAL_FIELDS``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import JsonValue

from qlabs_catalog_sync_sdk.models import Party, PartyRole, TextField

__all__ = [
    "MAPPED_SOURCE_FIELDS",
    "map_custom_attributes",
    "map_data_product_fields",
    "map_dataset_fields",
    "map_description",
    "map_owners",
    "map_physical_ref",
    "owner_party",
]

# A user e-mail: a local part and a domain part (itself containing a ".") either side of one
# "@", with no whitespace. Not full RFC 5322 -- this only needs to say "not a UUID, not a bare
# name", which every real Databricks/Azure AD e-mail satisfies.
_EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# A service-principal application ID: Databricks/Azure AD's standard UUID shape
# (8-4-4-4-12 hex digits). Case-insensitive -- UUIDs are conventionally lowercase but that is
# not guaranteed on the wire.
_APPLICATION_ID_PATTERN: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_NO_EXCLUSIONS: Final[frozenset[str]] = frozenset()

MAPPED_SOURCE_FIELDS: Final[frozenset[str]] = frozenset({"comment", "owner"})
"""Raw fields this module promotes to their own neutral field, and therefore excludes from
:func:`map_custom_attributes`'s passthrough -- keeping ``comment``/``owner`` there too would
duplicate the same information in two places with no way to tell which one is authoritative."""


# ----------------------------------------------------------------------------------
# owner -> Party
# ----------------------------------------------------------------------------------


def owner_party(owner: object) -> Party | None:
    """Classify one raw ``owner`` value into a :class:`Party`, or ``None`` if it carries nothing.

    See the module docstring's "``owner`` -> ``Party``" section for the email /
    application-id / display-name boundary. ``None`` is returned for anything that is not a
    non-blank string -- a missing or blank owner is not a party with no information, per
    :class:`Party`'s own validator (it requires at least one identifying attribute).
    """
    if not isinstance(owner, str):
        return None
    value = owner.strip()
    if not value:
        return None
    if _EMAIL_PATTERN.match(value):
        return Party(email=value, role=PartyRole.OWNER)
    if _APPLICATION_ID_PATTERN.match(value):
        return Party(party_id=value, role=PartyRole.OWNER)
    return Party(display_name=value, role=PartyRole.OWNER)


def map_owners(raw: Mapping[str, Any], *, source_key: str = "owner") -> dict[str, list[Party]]:
    """``owner`` -> ``{"owners": [...]}``, or ``{}`` if ``owner`` was not reported at all.

    Same absent/explicit-empty split as :func:`map_description`: a missing ``owner`` key
    leaves the neutral ``owners`` list untouched (absent -- leave the target alone); a
    present-but-unrecognizable owner (blank, ``null``, or a non-string) reports an explicit
    empty list -- "the source says there is no owner", not "this connector has nothing to
    say". Databricks reports at most one owner per object (RS-01 section 1.2), so the list
    this builds never holds more than one element.
    """
    if source_key not in raw:
        return {}
    party = owner_party(raw.get(source_key))
    return {"owners": [party] if party is not None else []}


# ----------------------------------------------------------------------------------
# comment -> description
# ----------------------------------------------------------------------------------


def map_description(
    raw: Mapping[str, Any], *, source_key: str = "comment"
) -> dict[str, TextField | None]:
    """``comment`` -> ``{"description": ...}``, or ``{}`` if ``comment`` was not reported.

    Absent vs. empty is deliberate -- see the module docstring's "Absent vs. explicit-empty"
    section. ``source_key`` is overridable only so a caller building a non-UC-shaped test
    payload can reuse this function; real Databricks payloads only ever call the field
    ``comment``.
    """
    if source_key not in raw:
        return {}
    comment = raw.get(source_key)
    text = comment if isinstance(comment, str) else ""
    if not text:
        return {"description": None}
    return {"description": TextField.plain(text)}


# ----------------------------------------------------------------------------------
# full_name -> physical_ref (Dataset only)
# ----------------------------------------------------------------------------------


def map_physical_ref(raw: Mapping[str, Any], *, source_key: str = "full_name") -> dict[str, str]:
    """``full_name`` -> ``{"physical_ref": ...}`` for a Dataset, or ``{}`` if absent.

    Dataset-only: :class:`~qlabs_catalog_sync_sdk.models.DataProduct` has no ``physical_ref``
    field at all (RS-03 section 8.1 carries no ``physicalRef`` row for it -- see
    ``manifest.py``'s own docstring), so a caller must never call this for a schema. In
    practice ``full_name`` is always present by the time this runs -- T4.4's ``read.py``
    already requires it to build identity -- but this function stays as defensive as every
    other mapping here rather than assuming that validation ran first.
    """
    full_name = raw.get(source_key)
    if not isinstance(full_name, str) or not full_name:
        return {}
    return {"physical_ref": full_name}


# ----------------------------------------------------------------------------------
# properties -> custom_attributes
# ----------------------------------------------------------------------------------


def map_custom_attributes(
    raw: Mapping[str, Any], *, exclude: frozenset[str] = _NO_EXCLUSIONS
) -> dict[str, JsonValue]:
    """Every field of ``raw`` not in ``exclude`` or :data:`MAPPED_SOURCE_FIELDS`, verbatim.

    See the module docstring's "``properties`` -> ``custom_attributes``" section for why this
    is a pure exclusion (never a rename or a re-nesting): ``properties`` keeps exactly the
    nesting Databricks itself gives it. Pass the same structural-field exclusion set
    ``read.py`` already uses (``_SCHEMA_STRUCTURAL_FIELDS`` / ``_TABLE_STRUCTURAL_FIELDS``) as
    ``exclude`` so identity fields consumed elsewhere are not duplicated here either.
    """
    skip = exclude | MAPPED_SOURCE_FIELDS
    return {key: value for key, value in raw.items() if key not in skip}


# ----------------------------------------------------------------------------------
# Per-entity convenience: everything this module has to say about one raw payload
# ----------------------------------------------------------------------------------


def map_data_product_fields(raw_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Every T4.5 content mapping for one UC schema, merged into a single fragment.

    Ready to ``dict.update()`` into ``read.py``'s ``values`` mapping (before
    ``build_field_envelopes``) and to unpack as keyword arguments into ``DataProduct(...)`` --
    the fragment's keys are exactly that model's field names. No ``physical_ref`` -- a schema
    has none; see :func:`map_physical_ref`.
    """
    fields: dict[str, Any] = {}
    fields.update(map_description(raw_schema))
    fields.update(map_owners(raw_schema))
    return fields


def map_dataset_fields(raw_table: Mapping[str, Any]) -> dict[str, Any]:
    """Every T4.5 content mapping for one UC table/view, merged into a single fragment.

    Same contract as :func:`map_data_product_fields`, plus ``physical_ref``.
    """
    fields: dict[str, Any] = {}
    fields.update(map_description(raw_table))
    fields.update(map_owners(raw_table))
    fields.update(map_physical_ref(raw_table))
    return fields
