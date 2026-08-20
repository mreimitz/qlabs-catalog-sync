"""T4.6 / T1.8: the SDK's conformance kit, run against the real Databricks connector.

``ConnectorConformanceSuite`` (``qlabs_catalog_sync_sdk.conformance.suite``) is the DoD
this task certifies against: subclass it, supply one ``connector`` fixture yielding an
already-``setup()`` :class:`~qlabs_catalog_sync_sdk.contract.Connector`, and every test
method on the base class runs against it. Two subclasses below, not one -- decision D6
means ``manifest.py``'s ``build_manifest(has_sql_warehouse=...)`` genuinely differs
depending on whether a SQL warehouse is configured (``tags``/``classifications`` go
``ro`` vs. ``na``), and the suite reads the manifest to decide which checks apply, so
this is two distinct certifications of two distinct manifests, not one run duplicated.

What actually executes for a connector this shape (every field ``ro``/``na``, nothing
writable at all -- see ``manifest.py``'s own docstring) versus what honestly skips:

* ``test_connector_declares_a_name_and_config_model``,
  ``test_capabilities_returns_a_manifest``, ``test_healthcheck_returns_a_status``,
  ``test_every_supported_entity_declares_identity_keys`` -- all run for real. The
  manifest is real (``CapabilityManifest``, not a stand-in), so
  ``_require_concrete_manifest`` never skips here either.
* ``test_unsupported_entities_refuse_writes_with_capability_error`` -- runs for real,
  against ``GLOSSARY_TERM``/``CATEGORY`` (declared ``supported=False`` -- "Databricks has
  no native glossary at all", ``manifest.py``'s own docstring). Proves ``create``/
  ``delete`` refuse with ``CapabilityError`` and zero HTTP calls for those two.
* ``test_writing_a_ro_or_na_field_raises_capability_error`` -- runs for real, and for
  this connector it is the whole ballgame: every field of every supported entity type
  (``DATA_PRODUCT``, ``DATASET``) is ``ro`` or ``na``, so ``_non_writable_fields`` is
  never empty and this test proves ``update()`` refuses honestly, without a request, for
  every single one of them.
* ``test_create_then_read_round_trips_writable_fields``,
  ``test_update_of_a_writable_field_is_reflected_on_read``,
  ``test_ro_and_na_fields_are_never_mutated_by_an_update``,
  ``test_reapplying_an_unchanged_diff_is_a_no_op`` -- all **skip**, with the stated
  reason ``"manifest declares no entity type with any writable field"``
  (``_writable_entity_types(manifest)`` is empty for both D6 shapes: nothing is ever
  ``rw`` here). This is the honest outcome for a read-only source connector, not a false
  green -- see ``suite.py``'s own module docstring on why a skip beats a silent pass.
  A consequence worth naming plainly: because these four skip, the base suite never
  calls ``Connector.read()`` at all for this connector. ``test_read_cassettes.py`` in
  this same directory exercises the read path directly instead, and
  ``test_write_refusal.py`` closes a real coverage gap the base suite leaves for a
  connector shaped like this one (see that module's docstring).

Zero-HTTP-calls verifiability: every capability-honesty assertion above goes through
``qlabs_catalog_sync_sdk.conformance.harness.assert_no_http_calls``, which patches
httpx (via respx) and can see this connector's own read path (``read.py``/``changes.py``
go through the SDK's httpx-based ``HttpEndpoint``, per ``__init__.py``'s ``setup()``
comment) but is structurally blind to ``databricks-sdk``'s ``WorkspaceClient``, which
talks over ``requests`` (``auth.py``'s own docstring). That blind spot does not weaken
what these particular checks prove, though: Databricks never overrides ``create``/
``update``/``delete`` at all (``__init__.py``'s own comment -- "intentionally NOT
overridden"), so every one of them is the inherited ``Connector`` ABC default
(``contract.py``), which unconditionally raises ``CapabilityError`` as its very first and
only statement, before touching ``self._http`` *or* ``self._client``. There is no branch
in that code path that could reach ``WorkspaceClient`` (or anything else) to make a
call respx cannot see -- confirmed by reading ``contract.py``'s ``create``/``update``/
``delete``, not merely inferred from the mock's silence. So for this specific connector,
in this specific situation, "0 calls captured by respx" is not merely "respx saw
nothing" but a sound proof of "nothing was sent, on any transport" -- an exception this
task's own brief anticipates could exist, and it does here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
from qlabs_catalog_sync_sdk.contract import Connector

from .conftest import setup_connector


class TestDatabricksConformanceWithSqlWarehouse(ConnectorConformanceSuite):
    """D6 branch 1: ``sql_warehouse_id`` is configured, so ``manifest.py`` declares
    ``tags``/``classifications`` ``ro`` rather than ``na``."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        async with setup_connector(sql_warehouse_id="warehouse-conformance-1") as connector:
            yield connector


class TestDatabricksConformanceWithoutSqlWarehouse(ConnectorConformanceSuite):
    """D6 branch 2: no SQL warehouse configured -- ``tags``/``classifications`` are
    ``na``, the manifest's honest answer to "cannot even read this" (decision D6)."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        async with setup_connector(sql_warehouse_id=None) as connector:
            yield connector
