"""Field envelope construction and the canonical checksum.

WP1 / T1.6. Deterministic normalization and stable checksums over field values, so the
engine can answer "did this actually change?" reproducibly. Everything idempotent about
the sync rests here: :func:`compute_checksum` is what makes a re-run over an unchanged
source perform zero API writes (RS-03 section 5; RS-07 section 2, steps 4 and 8).

Two projections of a value exist and the difference between them is load-bearing:

* :func:`to_json_value` is the **faithful** projection. It is what an envelope *stores*,
  and therefore what eventually gets written to the target: strings keep their
  whitespace and line endings, arrays keep their order.
* :func:`canonicalize` is the **canonical** projection. It is only ever hashed, never
  written. It deliberately discards representational differences that carry no meaning.

Normalization never rewrites the value that will be sent to an endpoint. Sorting Qlik's
``datasetIds`` before a full-replace write would be a gratuitous mutation; sorting them
before hashing is what stops a meaningless reorder from looking like a change.

Canonicalization rules — each is a decision the field diff engine (T7.2) and the sync
loop (T2.4) are built on:

1. **Object keys** are NFC-normalized, outer-whitespace-stripped, and sorted by Unicode
   code point, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` hash identically. Two
   keys that collide after normalization raise, rather than silently overwriting.
2. **Line endings** normalize: ``\\r\\n`` and a lone ``\\r`` both become ``\\n``. A line
   ending is transport, not content.
3. **Outer whitespace is stripped** from every string (ASCII space, tab, newline, form
   feed, vertical tab). A whitespace-only string therefore hashes as the empty string.
   Non-ASCII spaces such as U+00A0 are content and are left alone.
4. **Internal whitespace is never collapsed, and end-of-line whitespace is never
   trimmed.** ``documentation`` and ``description`` are synced markdown: indentation
   opens code blocks and nests lists, blank-line runs separate paragraphs, and two
   trailing spaces are a hard line break. Collapsing any of it would make a genuine
   edit invisible to the sync — a much worse failure than an occasional cosmetic diff.
5. **Strings are NFC-normalized**, not NFKC. Endpoints disagree about precomposed versus
   decomposed accents for the same typed character, so NFC is required; NFKC would fold
   ``ﬁ`` to ``fi`` and full-width forms to ASCII, which are different characters a human
   deliberately entered.
6. **Timestamps** normalize to UTC at **millisecond precision, truncated**, rendered
   ``YYYY-MM-DDTHH:MM:SS.sssZ``. The same instant written ``+02:00`` and written ``Z``
   therefore hashes identically. Milliseconds is the precision the endpoints actually
   agree on (Qlik emits RFC 3339, Databricks epoch milliseconds), so sub-millisecond
   noise is deliberately invisible; truncation rather than rounding keeps the mapping
   monotone and never carries into the next second. Naive datetimes raise, matching
   ``FieldEnvelope``'s ``AwareDatetime`` fields. A **string** that is a complete RFC 3339
   date-time with an explicit offset is recognized and normalized the same way — most
   timestamps reach the engine as JSON strings, and the ``+02:00`` vs ``Z`` rule would be
   useless if it only applied to typed values. A bare date and an offset-less date-time
   are left verbatim: there is nothing to normalize and guessing a zone would be wrong.
7. **Array order is decided per field, never globally** (:class:`ArrayOrder`). The
   default is :attr:`ArrayOrder.PRESERVE`: order is part of the value.
   :attr:`ArrayOrder.SORTED` marks a field order-insensitive; its arrays are sorted by
   the canonical text of their elements, at every depth within that field's value. The
   order-insensitive neutral fields are listed in :data:`ORDER_INSENSITIVE_FIELDS` —
   ``tags``, ``owners``, ``stewards``, ``classifications``, ``dataset_refs``,
   ``glossary_term_refs``, ``term_relations`` and ``asset_links`` — every one of them a
   set-like collection that Qlik takes as a full replace and that sources return in
   arbitrary order. ``custom_attributes`` is never sorted: its meaning is
   endpoint-defined, so its arrays are assumed order-bearing. Sorting is multiset, not
   set: duplicates are kept, because dropping one would hide a real change in a
   full-replace payload.
8. **Numbers.** ``1`` and ``1.0`` hash identically — a float with no fractional part
   canonicalizes to its integer form, because JSON encoders on either side of the sync
   disagree about which they emit for the same value and a phantom diff every cycle is
   unacceptable. ``-0.0`` is ``0``. Other floats use Python's shortest round-trip
   repr. ``NaN`` and infinities raise: they cannot cross a JSON boundary. ``Decimal``
   goes through the same rule (integral to ``int``, otherwise via ``float``), so values
   beyond double precision are not preserved — metadata carries no such numbers.
9. **Types are never coerced across the JSON type boundary.** ``1``, ``1.0`` and ``"1"``
   are not all equal — the string is a different value. ``True`` is not ``1`` and
   ``False`` is not ``0``; booleans are checked before integers.
10. **``null`` and absent are different.** ``{"a": null}`` and ``{}`` hash differently,
    and so do ``""``, ``null`` and a missing key. The engine depends on this: ``null``
    is the source asserting the field is empty (clear the target), while absent is the
    source not reporting the field at all (leave the target alone). At the sidecar
    level :func:`changed_fields` applies the same rule and never reports a field the
    fresh read did not carry.
11. **Hashing** is SHA-256 over the UTF-8 bytes of the canonical JSON text (compact
    separators, ``ensure_ascii=False``, keys sorted), returned as
    ``sha256:<lowercase hex>``. The algorithm prefix is stored in state so a future
    change of algorithm is detectable instead of silently mis-comparing. The checksum
    identifies the canonical *value*; it does not encode which :class:`ArrayOrder` was
    used, since the policy is a fixed property of the field being compared.

Nothing here is nondeterministic across processes: no ``hash()``, no ``id()``, no clock,
no set iteration order (a ``set`` is canonicalized to a sorted list), and no
platform-dependent ``strftime``. The same logical value hashes to the same string on
every run, on every machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, JsonValue

from .models import FieldEnvelope

__all__ = [
    "ArrayOrder",
    "CHECKSUM_ALGORITHM",
    "CHECKSUM_PREFIX",
    "CanonicalizationError",
    "ORDER_INSENSITIVE_FIELDS",
    "TIMESTAMP_PRECISION_DIGITS",
    "build_envelope",
    "build_field_envelopes",
    "canonical_json",
    "canonicalize",
    "changed_fields",
    "compute_checksum",
    "has_changed",
    "order_for",
    "refresh_checksum",
    "to_json_value",
]


CHECKSUM_ALGORITHM: Final = "sha256"
"""The digest the checksum uses. Recorded in every checksum string."""

CHECKSUM_PREFIX: Final = f"{CHECKSUM_ALGORITHM}:"
"""Prefix every checksum carries, so a stored value names the algorithm that made it."""

TIMESTAMP_PRECISION_DIGITS: Final = 3
"""Sub-second digits kept when canonicalizing an instant: milliseconds, truncated."""

_MAX_DEPTH: Final = 64
"""Nesting limit. Turns a cyclic or pathological structure into a clear error."""

# ASCII whitespace only. U+00A0 and friends are content, not formatting.
_OUTER_WHITESPACE: Final = " \t\n\r\f\v"

# A *complete* RFC 3339 date-time with an explicit offset, and nothing else. Anchored so
# prose that merely contains a timestamp is left alone.
_RFC3339_INSTANT: Final = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})\Z"
)

_CAMEL_BOUNDARY: Final = re.compile(r"(?<!^)(?=[A-Z])")


class CanonicalizationError(ValueError):
    """A value cannot be canonicalized deterministically.

    Subclasses :class:`ValueError` so callers that only care that the input was bad do
    not need to know about this module.
    """


class ArrayOrder(StrEnum):
    """Whether the order of a field's arrays is part of its value.

    Chosen per field rather than globally: Qlik's ``datasetIds`` and ``tags`` are
    full-replace arrays whose source order is arbitrary, so a reorder must not look like
    a change; an array whose order encodes a sequence must never be silently sorted.
    """

    PRESERVE = "preserve"
    """Order is meaningful. ``["a", "b"]`` and ``["b", "a"]`` are different values."""

    SORTED = "sorted"
    """Order is not meaningful. Arrays are sorted before hashing, at every depth."""


ORDER_INSENSITIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "asset_links",
        "classifications",
        "dataset_refs",
        "glossary_term_refs",
        "owners",
        "stewards",
        "tags",
        "term_relations",
    }
)
"""Neutral field names whose arrays are set-like, and whose order therefore never counts.

Every entry is a full-replace collection that sources return in arbitrary order.
Anything not listed here — including ``custom_attributes``, whose meaning is
endpoint-defined — keeps :attr:`ArrayOrder.PRESERVE`.
"""


def order_for(field: str) -> ArrayOrder:
    """The array-ordering policy for a neutral field name.

    Accepts either spelling of the name (``dataset_refs`` or ``datasetRefs``), matching
    the neutral models, which validate and emit both.
    """
    return (
        ArrayOrder.SORTED
        if _CAMEL_BOUNDARY.sub("_", field).lower() in ORDER_INSENSITIVE_FIELDS
        else ArrayOrder.PRESERVE
    )


# --------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------


def to_json_value(value: Any) -> JsonValue:
    """Project ``value`` onto plain JSON **faithfully** — this is what gets stored.

    Models are dumped, instants become canonical UTC strings, ``UUID``/``Enum``/
    ``Decimal`` become their JSON forms and tuples become lists. Strings keep their
    whitespace and line endings and arrays keep their order, because this value is what
    a writer will send to the target endpoint.
    """
    return _project(value, order=ArrayOrder.PRESERVE, canonical=False, depth=0)


def canonicalize(value: Any, *, order: ArrayOrder = ArrayOrder.PRESERVE) -> JsonValue:
    """Project ``value`` onto its **canonical** JSON form — this is what gets hashed.

    Applies every rule in this module's docstring. The result is only ever hashed or
    compared; never write it to an endpoint.
    """
    return _project(value, order=order, canonical=True, depth=0)


def canonical_json(value: Any, *, order: ArrayOrder = ArrayOrder.PRESERVE) -> str:
    """The exact text whose UTF-8 bytes :func:`compute_checksum` hashes.

    Useful for debugging a checksum mismatch: two values with different checksums differ
    somewhere in this string.
    """
    return _dumps(canonicalize(value, order=order))


def compute_checksum(value: Any, *, order: ArrayOrder = ArrayOrder.PRESERVE) -> str:
    """The canonical checksum of ``value`` as ``sha256:<hex>``.

    Stable across processes, machines and Python runs: identical logical values always
    produce identical checksums, and any real difference changes the digest.
    """
    digest = hashlib.sha256(canonical_json(value, order=order).encode("utf-8")).hexdigest()
    return f"{CHECKSUM_PREFIX}{digest}"


def _project(value: Any, *, order: ArrayOrder, canonical: bool, depth: int) -> JsonValue:
    """Shared recursion behind :func:`to_json_value` and :func:`canonicalize`.

    ``canonical`` toggles exactly the lossy rules: string normalization, integral-float
    collapsing, key sorting and array sorting. Keeping one recursion means the two
    projections can never drift apart on which types they accept.
    """
    if depth > _MAX_DEPTH:
        raise CanonicalizationError(
            f"value nests deeper than {_MAX_DEPTH} levels (or contains a cycle)"
        )

    if value is None:
        return None
    if isinstance(value, bool):
        # Before the int check: bool subclasses int, and True must never become 1.
        return value
    if isinstance(value, BaseModel):
        return _project(
            value.model_dump(mode="json"), order=order, canonical=canonical, depth=depth
        )
    if isinstance(value, Enum):
        # Before the str check: StrEnum is also a str, and we want its plain value.
        return _project(value.value, order=order, canonical=canonical, depth=depth)
    if isinstance(value, str):
        return _canonical_string(value) if canonical else value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _canonical_float(value) if canonical else _checked_float(value)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        # Before the date check: datetime subclasses date.
        return _canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        raise CanonicalizationError(
            "binary values have no canonical JSON form; decode or encode them first"
        )
    if isinstance(value, Mapping):
        return _project_mapping(value, order=order, canonical=canonical, depth=depth)
    if isinstance(value, set | frozenset):
        # A set has no order to preserve, so it is always sorted — set iteration order is
        # not stable across processes and must never reach a checksum.
        return _sort_canonical(
            [_project(item, order=order, canonical=canonical, depth=depth + 1) for item in value]
        )
    if isinstance(value, Sequence):
        items = [
            _project(item, order=order, canonical=canonical, depth=depth + 1) for item in value
        ]
        return _sort_canonical(items) if canonical and order is ArrayOrder.SORTED else items

    raise CanonicalizationError(
        f"cannot canonicalize a value of type {type(value).__name__!r}; "
        "map it to a JSON-compatible neutral value first"
    )


def _project_mapping(
    value: Mapping[Any, Any], *, order: ArrayOrder, canonical: bool, depth: int
) -> dict[str, JsonValue]:
    projected: dict[str, JsonValue] = {}
    for raw_key, raw_value in value.items():
        key = _project_key(raw_key, canonical=canonical)
        if key in projected:
            raise CanonicalizationError(f"duplicate object key after normalization: {key!r}")
        projected[key] = _project(raw_value, order=order, canonical=canonical, depth=depth + 1)
    if not canonical:
        return projected
    # Sorted so the returned canonical form is canonical when inspected, not only when
    # serialized. `_dumps` sorts again; the two agree because keys are already NFC.
    return dict(sorted(projected.items()))


def _project_key(raw_key: Any, *, canonical: bool) -> str:
    key = raw_key.value if isinstance(raw_key, Enum) else raw_key
    if not isinstance(key, str):
        raise CanonicalizationError(f"object keys must be strings, got {type(raw_key).__name__!r}")
    text = str(key)
    if not canonical:
        return text
    # Keys are identifiers: normalize and trim them, but never treat one as a timestamp.
    return unicodedata.normalize("NFC", text).strip(_OUTER_WHITESPACE)


# --------------------------------------------------------------------------------------
# Scalar rules
# --------------------------------------------------------------------------------------


def _canonical_string(text: str) -> str:
    normalized = unicodedata.normalize("NFC", str(text))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip(_OUTER_WHITESPACE)
    instant = _as_canonical_instant(normalized)
    return normalized if instant is None else instant


def _as_canonical_instant(text: str) -> str | None:
    """The canonical UTC rendering of ``text``, or ``None`` if it is not an instant."""
    if not _RFC3339_INSTANT.match(text):
        return None
    # RFC 3339 allows a lowercase zone designator; `fromisoformat` only takes "Z".
    candidate = f"{text[:-1]}Z" if text.endswith("z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return _canonical_datetime(parsed)


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise CanonicalizationError(
            "naive datetimes have no canonical instant; attach a timezone "
            f"(got {value.isoformat()})"
        )
    utc = value.astimezone(UTC)
    subsecond = utc.microsecond // 10 ** (6 - TIMESTAMP_PRECISION_DIGITS)
    # Built by hand rather than with strftime: %Y is not reliably zero-padded across
    # platforms, and this must not vary by libc or locale.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{subsecond:0{TIMESTAMP_PRECISION_DIGITS}d}Z"
    )


def _checked_float(value: float) -> float:
    if not math.isfinite(value):
        raise CanonicalizationError(f"{value!r} has no JSON representation")
    return value


def _canonical_float(value: float) -> int | float:
    checked = _checked_float(value)
    return int(checked) if checked.is_integer() else checked


def _canonical_decimal(value: Decimal) -> int | float:
    if not value.is_finite():
        raise CanonicalizationError(f"{value!r} has no JSON representation")
    try:
        integral = value == value.to_integral_value()
    except InvalidOperation as exc:  # pragma: no cover - only for absurd exponents
        raise CanonicalizationError(f"{value!r} cannot be canonicalized") from exc
    return int(value) if integral else _canonical_float(float(value))


# --------------------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------------------


def _dumps(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sort_canonical(items: list[JsonValue]) -> list[JsonValue]:
    """Sort by canonical text, keeping duplicates and never comparing values directly."""
    keys = sorted((_dumps(item), index) for index, item in enumerate(items))
    return [items[index] for _text, index in keys]


# --------------------------------------------------------------------------------------
# Envelope helpers
# --------------------------------------------------------------------------------------


def build_envelope(
    value: Any,
    *,
    source_endpoint: str,
    order: ArrayOrder = ArrayOrder.PRESERVE,
    source_revision: str | None = None,
    last_modified_at: datetime | None = None,
    last_synced_at: datetime | None = None,
) -> FieldEnvelope[JsonValue]:
    """A :class:`~.models.FieldEnvelope` around ``value`` with its checksum filled in.

    The stored ``value`` is the faithful projection (:func:`to_json_value`); the checksum
    is over the canonical one. Rebuilding an envelope from its own stored value yields
    the same checksum, so state that has been through a round of persistence still
    compares equal.
    """
    return FieldEnvelope[JsonValue](
        value=to_json_value(value),
        source_endpoint=source_endpoint,
        source_revision=source_revision,
        last_modified_at=last_modified_at,
        last_synced_at=last_synced_at,
        checksum=compute_checksum(value, order=order),
    )


def build_field_envelopes(
    values: Mapping[str, Any],
    *,
    source_endpoint: str,
    source_revision: str | None = None,
    last_modified_at: datetime | None = None,
    last_synced_at: datetime | None = None,
) -> dict[str, FieldEnvelope[JsonValue]]:
    """Build a whole ``field_envelopes`` sidecar, one envelope per neutral field.

    Each field's array-ordering policy comes from :func:`order_for`, so a connector gets
    the documented per-field behavior without restating it.
    """
    return {
        field: build_envelope(
            value,
            source_endpoint=source_endpoint,
            order=order_for(field),
            source_revision=source_revision,
            last_modified_at=last_modified_at,
            last_synced_at=last_synced_at,
        )
        for field, value in values.items()
    }


def refresh_checksum(
    envelope: FieldEnvelope[JsonValue],
    *,
    order: ArrayOrder = ArrayOrder.PRESERVE,
    last_synced_at: datetime | None = None,
) -> FieldEnvelope[JsonValue]:
    """A copy of ``envelope`` with its checksum recomputed from its stored value.

    Use it to fill in or re-verify a checksum on an envelope that came from somewhere
    other than :func:`build_envelope` — a state-store row, or one whose policy changed.
    ``last_synced_at``, when given, is stamped at the same time.
    """
    updates: dict[str, Any] = {"checksum": compute_checksum(envelope.value, order=order)}
    if last_synced_at is not None:
        updates["last_synced_at"] = last_synced_at
    return envelope.model_copy(update=updates)


def has_changed(fresh: FieldEnvelope[JsonValue], stored: FieldEnvelope[JsonValue] | None) -> bool:
    """Whether a freshly read field differs from the last-known one. The engine's gate.

    A checksum comparison and nothing else — the whole point is that this is cheap enough
    to run for every field of every entity on every cycle. Provenance is deliberately
    ignored: a value read from a different endpoint or at a different time has not
    changed unless the value itself has.

    No stored envelope means changed. A stored envelope with no checksum also means
    changed: equality cannot be proven, and writing is the safe outcome. A ``fresh``
    envelope with no checksum is a bug — build it with :func:`build_envelope`.
    """
    if fresh.checksum is None:
        raise CanonicalizationError(
            "the fresh envelope carries no checksum; build it with build_envelope() "
            "or refresh_checksum()"
        )
    if stored is None or stored.checksum is None:
        return True
    return fresh.checksum != stored.checksum


def changed_fields(
    fresh: Mapping[str, FieldEnvelope[JsonValue]],
    stored: Mapping[str, FieldEnvelope[JsonValue]],
) -> list[str]:
    """The neutral field names that differ between two envelope sidecars, sorted.

    Only fields present in ``fresh`` can be reported. A field the source did not report
    this cycle is absent, not ``null``, and upstream-only sync leaves the target's copy
    of it alone (rule 10).
    """
    return sorted(
        name for name, envelope in fresh.items() if has_changed(envelope, stored.get(name))
    )
