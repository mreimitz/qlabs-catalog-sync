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
  real against the fake tenant. What that reveals is this task's central finding, and it
  is a genuine one, not a fixture artifact — see the report and
  ``test_round_trip_findings.py`` in this same directory, which pins each specific
  mismatch down as its own documented, individually-diagnosed test rather than leaving it
  as an opaque failure inside the base suite's loop:

  - ``name``, ``description``, ``tags`` round-trip cleanly: plain string/array fields with
    no identity-resolution or lifecycle-action step between "the diff says X" and "the
    read says X".
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
  - ``dataset_refs`` does not, structurally, regardless of whether resolution succeeds:
    ``read.py``'s ``_map_data_product`` never sets ``dataset_refs`` at all (module
    docstring point 4 — resolving a native ``datasetIds`` entry back to the neutral UUID
    the engine assigned is the engine's IdentityMap's job, not a single connector read's),
    so ``DataProduct.dataset_refs`` is `[]` on every read of every Qlik-backed product,
    no matter what was written. See the report for the precise fix this implies.

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

from collections.abc import AsyncIterator
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
from qlabs_catalog_sync_sdk.contract import Connector

from .conftest import FakeQlikTenant, setup_connector

#: The name every member-dataset lookup below resolves to, and the one dataset seeded
#: in the fake tenant to match it — see ``connector()`` for why.
_SEEDED_DATASET_NAME = "Conformance Dataset"


async def _always_seeded_dataset(neutral_id: object) -> str | None:
    """A :data:`~qlabs_connector_qlik.write.DatasetNameLookup` that reports every member
    under the same display name, regardless of which neutral id it is asked about.

    The base suite's synthetic sample (``qlabs_catalog_sync_sdk.conformance.samples``)
    hands ``create()``/``update()`` a bare, freshly-random ``uuid4()`` for
    ``dataset_refs`` with no name attached (``DataProduct.dataset_refs`` carries no name
    pairing at all), so there is no way for this fixture to know in advance which name a
    given call will ask about. Answering the same fixed name for every id — paired with
    :meth:`~.conftest.FakeQlikTenant.seed_dataset` seeding exactly one dataset under
    that name in the target space — is what a real, properly-wired
    ``dataset_name_lookup`` (module docstring: "the orchestrator wires it") looks like
    for a space that already holds a dataset every member happens to share a display
    name with. Without this, dataset-member resolution fails for every sample entity
    (no pre-existing name to match, no IdentityMap binding), and the round-trip/
    idempotency checks below never get past "nothing resolved, so nothing was written"
    to the more informative question of whether a *successfully written* value actually
    round-trips — see the report for why that second question is where this task's
    central finding lives.
    """
    del neutral_id
    return _SEEDED_DATASET_NAME


class TestQlikConformance(ConnectorConformanceSuite):
    """The full base suite, against a real ``Connector`` over a fake tenant seeded with
    one pre-existing dataset so ``dataset_refs`` resolution (decision D2) succeeds —
    see :func:`_always_seeded_dataset`."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        tenant = FakeQlikTenant()
        tenant.seed_dataset(name=_SEEDED_DATASET_NAME, resource_id="ds-conformance-1")
        kwargs: dict[str, Any] = {
            "tenant": tenant,
            "dataset_name_lookup": _always_seeded_dataset,
        }
        async with setup_connector(**kwargs) as connector:
            yield connector
