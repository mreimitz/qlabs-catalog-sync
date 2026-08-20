"""T3.8 / T1.8: the SDK's conformance kit, run against the real Qlik connector in write
mode — the DoD this task certifies against.

``ConnectorConformanceSuite`` (``qlabs_catalog_sync_sdk.conformance.suite``) is
subclassed once here, with one ``connector`` fixture yielding an already-``setup()``
:class:`~qlabs_catalog_sync_sdk.contract.Connector` built by ``conftest.py``'s
``setup_connector`` against a fresh, empty :class:`~.conftest.FakeQlikTenant`. Every test
method the base class defines then runs against it — see that module's own docstring for
what each one checks and why.

**Unlike Databricks (T4.6), this connector has genuinely writable fields** — ``DATA_
PRODUCT`` declares ``name``/``description``/``documentation``/``status``/``owners``/
``tags``/``dataset_refs`` all ``rw`` (``manifest.py``). That means
``_writable_entity_types`` is non-empty here, so the round-trip and idempotency checks
that skip for Databricks *run for real* against this connector:

* ``test_connector_declares_a_name_and_config_model``,
  ``test_capabilities_returns_a_manifest``, ``test_healthcheck_returns_a_status``,
  ``test_every_supported_entity_declares_identity_keys`` — run for real, no different
  from Databricks.
* ``test_unsupported_entities_refuse_writes_with_capability_error`` — runs for real,
  against ``GLOSSARY_TERM``/``CATEGORY`` (decision D5, declared ``supported=False``).
* ``test_writing_a_ro_or_na_field_raises_capability_error`` — runs for real, against
  ``DATA_PRODUCT.glossary_term_refs``/``.placement`` (``na``/``ro``) and every one of
  ``DATASET``'s eight fields (all ``ro``/``na`` — decision D2: a Qlik dataset is resolved,
  never created or updated by this connector).
* ``test_create_then_read_round_trips_writable_fields``,
  ``test_update_of_a_writable_field_is_reflected_on_read``,
  ``test_ro_and_na_fields_are_never_mutated_by_an_update``,
  ``test_reapplying_an_unchanged_diff_is_a_no_op`` — **do not skip**: ``DATA_PRODUCT`` has
  writable fields, so the base suite exercises ``create()``/``read()``/``update()`` for
  real against the fake tenant. Two of the three mismatches this originally exposed were
  genuine connector defects and are now fixed (``write.py``'s idempotency pre-read, point
  12a; ``read.py``'s ``DatasetRefLookup``, point 4) — so
  ``test_update_of_a_writable_field_is_reflected_on_read`` and
  ``test_reapplying_an_unchanged_diff_is_a_no_op`` now pass for real, against
  ``dataset_refs``, the alphabetically-first writable field they both exercise.

  ``test_create_then_read_round_trips_writable_fields`` still fails, and deliberately so:
  it walks **every** writable field, and four of them cannot round-trip through Qlik for
  reasons that are decisions or wire facts rather than bugs. Each is pinned individually
  in ``test_round_trip_findings.py`` rather than left as an opaque failure inside the base
  suite's loop. Nothing here is softened to make the loop green — a conformance suite that
  passes because the expectation was lowered certifies nothing:

  - ``name``, ``description``, ``dataset_refs`` round-trip cleanly. ``dataset_refs`` does
    so **only** when the reverse identity seam is wired (see :class:`_FakeIdentityMap`
    below); with no seam, ``read()`` does not report the field at all rather than
    reporting it wrong, which is the honest default and is itself pinned as a test.
  - ``tags`` does not: the neutral :class:`~qlabs_catalog_sync_sdk.models.Tag` is
    key/value and Qlik's ``tags`` is a bare ``string[]``, so ``write.py`` flattens a
    valued tag to ``"key=value"`` (its module docstring, point 4) and ``read.py`` maps
    each string back to a key-only ``Tag``. The sample is ``Tag(key="conformance",
    value="v0")``, so it comes back as ``Tag(key="conformance=v0")``. Splitting on ``=``
    on the way back would round-trip the sample at the cost of inventing structure in any
    tag a human typed with an ``=`` in it — the same class of invention decision D2
    forbids for references, so it is not done here on a connector's own initiative.
  - ``documentation`` does not: ``write.py`` sends a ``TextField``'s ``.text`` body to
    ``readMe`` regardless of the field's ``.format`` (``_text_value`` never reads
    ``.format`` at all), and ``read.py`` unconditionally reads ``readMe`` back as
    :attr:`~qlabs_catalog_sync_sdk.models.TextFormat.MARKDOWN` (module docstring point 5
    documents the *symmetric* case for ``description``/plain, but the same asymmetry for
    ``readMe``/markdown is not stated). The conformance kit's own synthetic sample
    (``qlabs_catalog_sync_sdk.conformance.samples.sample_value``) builds ``documentation``
    as ``TextField.plain(...)``, so its ``.format`` never survives the round trip even
    though its ``.text`` does.
  - ``owners`` does not, for a reason outside this connector's own code: the conformance
    kit's synthetic ``Party`` for ``owners`` carries only ``display_name`` (``samples.py``
    has no ``email`` generator), and D3's owner resolution (``resolve.py``) requires an
    email to look a Qlik user up at all — a display-name-only party is reported
    ``NO_EMAIL`` and dropped, honestly, before any HTTP call. The round-trip check's
    generic sample can never exercise a real ``keyContacts`` write for *any* connector
    whose owner matching is email-based.
  - ``status`` does not: decision D7 makes activation opt-in and off by default, so
    ``create()`` unconditionally skips a non-``draft`` status (``_apply_status``) — the
    product is created deactivated regardless of what the sample asked for, exactly as
    documented, and reported as skipped rather than silently dropped.

Zero-HTTP-calls verifiability: every capability-honesty assertion below goes through
``qlabs_catalog_sync_sdk.conformance.harness.assert_no_http_calls``, which patches httpx
via respx — and this connector's entire wire surface (data-products, Items, Users, the
OAuth2 token endpoint) goes through the SDK's httpx-based ``HttpEndpoint``
(``auth.py``'s ``build_http_endpoint``), so respx sees everything this connector could
possibly send. Unlike Databricks, there is no second, respx-blind transport in the loop
(no vendor SDK) — "0 calls captured" is therefore always a sound proof of "0 calls sent
on any transport" for this connector, not merely "respx saw nothing."
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
from qlabs_catalog_sync_sdk.contract import Connector

from .conftest import FakeQlikTenant, setup_connector


class _FakeIdentityMap:
    """The engine's IdentityMap, reduced to the three seams this connector actually uses.

    The base suite's synthetic sample (``qlabs_catalog_sync_sdk.conformance.samples``)
    hands ``create()``/``update()`` a bare, freshly-random ``uuid4()`` for
    ``dataset_refs`` with no name and no pre-existing binding — exactly what a first sync
    cycle looks like. This stands in for what the engine does around that: it knows a
    display name for each member (tier-2 resolution, decision D2), and it records the
    ``neutral id <-> Qlik dataset`` binding that resolution establishes, so a later read
    can map ``datasetIds`` back (``read.py``'s ``DatasetRefLookup``).

    **The binding is one-to-one, and that matters.** An earlier version of this fixture
    answered the *same* dataset name for every neutral id, which collapsed every member
    onto one Qlik dataset. That made the fixture unable to express the very thing two of
    the base suite's checks are about: with every member resolving to the same
    ``datasetIds`` value, "change the members" and "replay the members" produce a
    byte-identical wire value, so a connector with working idempotency would report the
    *first* update as a no-op and the suite's own sanity assertion would fail — for a
    fixture reason, not a connector one. A real IdentityMap is a bijection; modelling it
    as one is what lets the checks mean what they say. Each member therefore gets its own
    name and its own seeded dataset in the target space.
    """

    def __init__(self, tenant: FakeQlikTenant) -> None:
        self._tenant = tenant
        self._neutral_by_native: dict[str, uuid.UUID] = {}

    async def dataset_name(self, neutral_id: uuid.UUID) -> str | None:
        """``write.DatasetNameLookup``: the display name to match within the space.

        Seeds the matching dataset as it answers — the D2 precondition is that the Qlik
        dataset already exists, and this fixture has no other moment to establish it —
        and records the binding that resolution is about to make, which is what the
        engine itself does once the write lands.
        """
        name = f"Conformance Dataset {neutral_id}"
        resource_id = f"ds-{neutral_id}"
        self._tenant.seed_dataset(name=name, resource_id=resource_id)
        self._neutral_by_native[resource_id] = neutral_id
        return name

    async def dataset_ref(self, native_id: str) -> uuid.UUID | None:
        """``read.DatasetRefLookup``: the neutral id bound to this Qlik dataset id.

        ``None`` for anything never bound — the honest answer, and the one that makes
        ``read()`` decline to report ``dataset_refs`` at all rather than report it wrong.
        """
        return self._neutral_by_native.get(native_id)


class TestQlikConformance(ConnectorConformanceSuite):
    """The full base suite, against a real ``Connector`` over a fake tenant whose member
    datasets resolve (decision D2) and map back (``read.py`` point 4) through one
    bijective :class:`_FakeIdentityMap` — see that class for why one-to-one is the part
    that matters."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        tenant = FakeQlikTenant()
        identity_map = _FakeIdentityMap(tenant)
        async with setup_connector(
            tenant=tenant,
            dataset_name_lookup=identity_map.dataset_name,
            dataset_ref_lookup=identity_map.dataset_ref,
        ) as connector:
            yield connector
