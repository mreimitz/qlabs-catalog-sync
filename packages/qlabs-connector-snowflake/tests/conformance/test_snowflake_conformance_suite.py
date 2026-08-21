"""T6.6 / T1.8: the SDK's conformance kit, run against the real Snowflake connector.

``ConnectorConformanceSuite`` (``qlabs_catalog_sync_sdk.conformance.suite``) is the DoD
this task certifies against: subclass it, supply one ``connector`` fixture yielding an
already-``setup()`` :class:`~qlabs_catalog_sync_sdk.contract.Connector`, and every test
method on the base class runs against it.

**One subclass, not two.** The Databricks conformance suite needs two, because decision
D6 makes its manifest depend on whether a SQL warehouse is configured. Snowflake's
``build_manifest()`` takes no arguments and imports nothing from ``auth.py``
(``manifest.py``'s "Config-independence" section), so there is exactly one manifest to
certify no matter how the config is set.

What actually executes for a connector this shape (every field ``ro``/``na``, nothing
writable at all) versus what honestly skips:

* ``test_connector_declares_a_name_and_config_model``,
  ``test_capabilities_returns_a_manifest``,
  ``test_every_supported_entity_declares_identity_keys`` -- all run for real against the
  real ``CapabilityManifest``, so ``_require_concrete_manifest`` never skips.
* ``test_healthcheck_returns_a_status`` -- overridden below, and strengthened rather than
  weakened; see that method's own docstring for why.
* ``test_unsupported_entities_refuse_writes_with_capability_error`` -- runs for real,
  against ``GLOSSARY_TERM``/``CATEGORY`` (declared ``supported=False``: Snowflake's
  genuine business-semantics analog is Semantic Views, out of WP6's scope --
  ``manifest.py``'s "Glossary and semantic layer" section). Proves ``create``/``delete``
  refuse with ``CapabilityError`` and zero HTTP calls for those two.
* ``test_writing_a_ro_or_na_field_raises_capability_error`` -- runs for real, and for
  this connector it is the whole ballgame: every field of every supported entity type
  (``DATA_PRODUCT``, ``DATASET``) is ``ro`` or ``na``, so ``_non_writable_fields`` is
  never empty and this proves ``update()`` refuses honestly, without a request, for every
  single one of them.
* ``test_create_then_read_round_trips_writable_fields``,
  ``test_update_of_a_writable_field_is_reflected_on_read``,
  ``test_ro_and_na_fields_are_never_mutated_by_an_update``,
  ``test_reapplying_an_unchanged_diff_is_a_no_op`` -- all **skip**, with the stated reason
  "manifest declares no entity type with any writable field". That is the honest outcome
  for a read-only source connector, not a false green (``suite.py``'s own docstring on
  why a skip beats a silent pass). The consequence worth naming plainly: because these
  four skip, the base suite never calls ``Connector.read()`` at all.
  ``test_read_cassettes.py`` and ``test_manifest_read_honesty.py`` in this directory
  exercise the read path directly instead, and ``test_write_refusal.py`` closes the
  create/delete gap the base suite leaves for a connector shaped like this one.

Zero-HTTP-calls verifiability: every capability-honesty assertion goes through
``qlabs_catalog_sync_sdk.conformance.harness.assert_no_http_calls``, which patches httpx
via respx. Unlike the Databricks connector -- whose ``databricks-sdk`` client talks over
``requests`` and is therefore invisible to respx -- **this connector has no second
transport at all**: ``auth.py`` uses ``snowflake-connector-python`` only to compute the
public-key fingerprint (a pure local computation, no client, no socket), and every
request the connector ever makes goes through the SDK's httpx-based ``HttpEndpoint``. So
"0 calls captured by respx" here is a plain proof that nothing was sent, with no blind
spot to caveat -- and on top of that, ``__init__.py`` never overrides
``create``/``update``/``delete``, so each is the inherited ``Connector`` ABC default
(``contract.py``) that raises ``CapabilityError`` as its first and only statement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import respx

from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
from qlabs_catalog_sync_sdk.contract import Connector, HealthState, HealthStatus

from .conftest import mock_healthcheck, setup_connector


class TestSnowflakeConformance(ConnectorConformanceSuite):
    """The Snowflake connector, certified against the SDK's conformance kit."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        async with setup_connector() as connector:
            yield connector

    async def test_healthcheck_returns_a_status(self, connector: Connector) -> None:
        """Overridden to supply the probe's response, and asserting more than the base
        method does, not less.

        ``Connector.healthcheck()`` issues a real ``GET /api/v2/databases?showLimit=1``
        (``__init__.py``), so the base method -- which calls it with nothing intercepting
        httpx -- would attempt a DNS lookup for an account host that does not exist and
        then pass anyway, because ``healthcheck()`` maps a transport failure to
        ``HealthStatus.degraded`` rather than raising. That is a live network round trip
        in CI certifying only the failure path.

        The router cannot live on the ``connector`` fixture instead: respx routers do not
        nest, so an outer one would silently answer (and therefore hide) every request the
        base suite's ``assert_no_http_calls`` checks are trying to prove never happens --
        see ``conftest.py``'s module docstring. Confining it to this one method keeps
        every other check in the suite sound.

        So: mock the probe here, keep the base assertion (``isinstance(status,
        HealthStatus)`` is the contract the kit certifies), and additionally pin down that
        a healthy account actually reports healthy.
        """
        with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
            route = mock_healthcheck(router)
            status = await connector.healthcheck()

        assert isinstance(status, HealthStatus)
        assert status.state is HealthState.HEALTHY, (
            f"a 200 from the probe must report healthy, got {status!r}"
        )
        assert route.call_count == 1, "healthcheck must make exactly one probe call"
