"""The checksum corpus: equivalence classes of logical values.

The corpus states the whole contract of :mod:`qlabs_catalog_sync_sdk.envelope` as data:

* every member of a class is a *different representation of the same logical value*, so
  all members must produce the same checksum;
* every class is a *different logical value*, so no two classes may ever collide.

Adding a case here strengthens both tests at once. Keep classes genuinely distinct: two
classes whose canonical forms are equal (for example ``["a", "b"]`` preserved and
``["b", "a"]`` sorted) are the same logical value and belong in one class.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from qlabs_catalog_sync_sdk.envelope import ArrayOrder
from qlabs_catalog_sync_sdk.models import Party, PartyRole, Tag, TextField

UTC = UTC
CEST = timezone(timedelta(hours=2))
CHATHAM = timezone(timedelta(hours=12, minutes=45))

SAMPLE_UUID = UUID("11111111-1111-4111-8111-111111111111")

# A realistic markdown documentation body: indentation opens a code block, the blank-line
# run separates paragraphs, and the two trailing spaces on "Overview" are a hard break.
MARKDOWN_DOC = (
    "# Retail Sales\n\nOverview  \nof the product.\n\n    select 1\n\n- first\n  - nested\n"
)
MARKDOWN_DOC_COLLAPSED = "# Retail Sales Overview of the product. select 1 - first - nested"
MARKDOWN_DOC_NO_HARD_BREAK = MARKDOWN_DOC.replace("Overview  \n", "Overview\n")
MARKDOWN_DOC_FLAT_INDENT = MARKDOWN_DOC.replace("    select 1", "select 1")

# One instant, four spellings, plus sub-millisecond noise that must not register.
INSTANT_UTC = datetime(2026, 8, 19, 15, 5, 30, 123000, tzinfo=UTC)
INSTANT_CEST = datetime(2026, 8, 19, 17, 5, 30, 123000, tzinfo=CEST)
INSTANT_CHATHAM = datetime(2026, 8, 20, 3, 50, 30, 123000, tzinfo=CHATHAM)
INSTANT_SUBMILLI = datetime(2026, 8, 19, 15, 5, 30, 123999, tzinfo=UTC)
INSTANT_NEXT_MILLI = datetime(2026, 8, 19, 15, 5, 30, 124000, tzinfo=UTC)

NESTED_A: dict[str, Any] = {
    "qlik": {"spaceId": "s-1", "meta": {"counts": {"datasets": 3}, "tier": "gold"}},
    "databricks": {"properties": [{"key": "reader", "value": 2}], "ratio": 0.125},
}
NESTED_B: dict[str, Any] = {
    "databricks": {"ratio": 0.125, "properties": [{"value": 2, "key": "reader"}]},
    "qlik": {"meta": {"tier": "gold", "counts": {"datasets": 3}}, "spaceId": "s-1"},
}
NESTED_CHANGED: dict[str, Any] = {
    "qlik": {"spaceId": "s-1", "meta": {"counts": {"datasets": 4}, "tier": "gold"}},
    "databricks": {"properties": [{"key": "reader", "value": 2}], "ratio": 0.125},
}

OWNER = Party(party_id="u-1", display_name="Ada", email="ada@example.com", role=PartyRole.OWNER)
OWNER_AS_DICT: dict[str, Any] = {
    "party_id": "u-1",
    "display_name": "Ada",
    "email": "ada@example.com",
    "role": "owner",
}

# (label, array-ordering policy, representations that must all hash the same)
CLASSES: list[tuple[str, ArrayOrder, list[Any]]] = [
    # --- null, absent and empty are four different things -----------------------------
    ("null", ArrayOrder.PRESERVE, [None]),
    ("empty-string", ArrayOrder.PRESERVE, ["", "   ", "\n\t ", " \r\n\v\f "]),
    ("empty-object", ArrayOrder.PRESERVE, [{}]),
    ("empty-array", ArrayOrder.PRESERVE, [[], ()]),
    ("object-with-null", ArrayOrder.PRESERVE, [{"a": None}]),
    ("object-with-empty-string", ArrayOrder.PRESERVE, [{"a": ""}]),
    # --- whitespace --------------------------------------------------------------------
    (
        "text-outer-whitespace",
        ArrayOrder.PRESERVE,
        ["Retail Sales", "  Retail Sales\n", "\tRetail Sales \n"],
    ),
    ("text-line-endings", ArrayOrder.PRESERVE, ["one\ntwo", "one\r\ntwo", "one\rtwo"]),
    ("text-internal-double-space", ArrayOrder.PRESERVE, ["Retail  Sales"]),
    ("markdown-doc", ArrayOrder.PRESERVE, [MARKDOWN_DOC, MARKDOWN_DOC + "\n", "\n" + MARKDOWN_DOC]),
    ("markdown-doc-collapsed", ArrayOrder.PRESERVE, [MARKDOWN_DOC_COLLAPSED]),
    ("markdown-doc-no-hard-break", ArrayOrder.PRESERVE, [MARKDOWN_DOC_NO_HARD_BREAK]),
    ("markdown-doc-flat-indent", ArrayOrder.PRESERVE, [MARKDOWN_DOC_FLAT_INDENT]),
    ("text-nbsp-kept", ArrayOrder.PRESERVE, ["\u00a0Retail Sales\u00a0"]),
    # --- unicode -----------------------------------------------------------------------
    # "Cafe" + U+0301 combining acute vs the precomposed U+00E9: the same character.
    ("unicode-nfc", ArrayOrder.PRESERVE, ["Caf\u00e9", "Cafe\u0301", "  Cafe\u0301  "]),
    ("unicode-ligature-kept", ArrayOrder.PRESERVE, ["\ufb01le"]),  # NFKC would fold this to "file"
    ("unicode-ascii-fi", ArrayOrder.PRESERVE, ["file"]),
    ("unicode-key-nfc", ArrayOrder.PRESERVE, [{"Caf\u00e9": 1}, {"Cafe\u0301": 1}]),
    # --- numbers, booleans and strings never coerce across types ------------------------
    ("number-one", ArrayOrder.PRESERVE, [1, 1.0, Decimal("1.000")]),
    ("number-zero", ArrayOrder.PRESERVE, [0, 0.0, -0.0, Decimal("0.00")]),
    ("number-one-and-a-half", ArrayOrder.PRESERVE, [1.5, Decimal("1.50")]),
    ("string-one", ArrayOrder.PRESERVE, ["1"]),
    ("string-one-point-zero", ArrayOrder.PRESERVE, ["1.0"]),
    ("bool-true", ArrayOrder.PRESERVE, [True]),
    ("bool-false", ArrayOrder.PRESERVE, [False]),
    ("number-large", ArrayOrder.PRESERVE, [2**70, float(2**70)]),
    # --- instants ----------------------------------------------------------------------
    (
        "instant",
        ArrayOrder.PRESERVE,
        [
            INSTANT_UTC,
            INSTANT_CEST,
            INSTANT_CHATHAM,
            INSTANT_SUBMILLI,
            "2026-08-19T15:05:30.123Z",
            "2026-08-19T17:05:30.123+02:00",
            "2026-08-19t15:05:30.123456z",
            "2026-08-19 17:05:30.123+0200",
            "  2026-08-19T15:05:30.1234567Z  ",
        ],
    ),
    (
        "instant-next-millisecond",
        ArrayOrder.PRESERVE,
        [INSTANT_NEXT_MILLI, "2026-08-19T15:05:30.124Z"],
    ),
    ("instant-offset-less-string", ArrayOrder.PRESERVE, ["2026-08-19T15:05:30.123"]),
    ("plain-date", ArrayOrder.PRESERVE, [date(2026, 8, 19), "2026-08-19"]),
    # --- identifiers --------------------------------------------------------------------
    ("uuid", ArrayOrder.PRESERVE, [SAMPLE_UUID, "11111111-1111-4111-8111-111111111111"]),
    # --- object key order ---------------------------------------------------------------
    (
        "object-key-order",
        ArrayOrder.PRESERVE,
        [{"a": 1, "b": 2}, {"b": 2, "a": 1}, dict([("b", 2), ("a", 1)])],
    ),
    ("nested-structure", ArrayOrder.PRESERVE, [NESTED_A, NESTED_B]),
    ("nested-structure-changed", ArrayOrder.PRESERVE, [NESTED_CHANGED]),
    # --- neutral value types ------------------------------------------------------------
    (
        "textfield-plain",
        ArrayOrder.PRESERVE,
        [
            TextField.plain("Curated retail sales."),
            {"text": "Curated retail sales.", "format": "plain"},
        ],
    ),
    (
        "textfield-markdown",
        ArrayOrder.PRESERVE,
        [
            TextField.markdown("Curated retail sales."),
            {"text": "Curated retail sales.", "format": "markdown"},
        ],
    ),
    ("party", ArrayOrder.PRESERVE, [OWNER, OWNER_AS_DICT]),
    # --- arrays whose order is meaningful -------------------------------------------------
    ("ordered-array-ab", ArrayOrder.PRESERVE, [["a", "b"], ("a", "b")]),
    ("ordered-array-ba", ArrayOrder.PRESERVE, [["b", "a"]]),
    # --- arrays whose order is not ---------------------------------------------------------
    ("unordered-refs", ArrayOrder.SORTED, [["x", "y", "z"], ["z", "x", "y"], {"y", "z", "x"}]),
    ("unordered-refs-with-duplicate", ArrayOrder.SORTED, [["x", "y", "y"], ["y", "x", "y"]]),
    (
        "unordered-tags",
        ArrayOrder.SORTED,
        [
            [Tag(key="pii", value="false"), Tag(key="gold")],
            [Tag(key="gold"), Tag(key="pii", value="false")],
            [{"key": "gold", "value": None}, {"key": "pii", "value": "false"}],
        ],
    ),
    ("unordered-tags-one-member", ArrayOrder.SORTED, [[Tag(key="gold")]]),
    (
        "unordered-nested",
        ArrayOrder.SORTED,
        [
            {"outer": [{"inner": ["q", "p"]}, {"inner": ["s", "r"]}]},
            {"outer": [{"inner": ["r", "s"]}, {"inner": ["p", "q"]}]},
        ],
    ),
]
