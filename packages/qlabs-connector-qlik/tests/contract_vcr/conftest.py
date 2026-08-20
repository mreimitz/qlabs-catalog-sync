"""Shared fixtures for T8.5's Qlik VCR contract suite.

**Purpose, stated once so every test file in this directory can point back here.** The
conformance suites (``tests/conformance``, T3.8) and the pilot (T8.1) already prove the
connector *works* against friendly mocks. This directory exists for a narrower and
sharper job: freeze the exact request/response *shapes* this connector depends on as
``vcrpy`` cassettes, so that if Qlik ever renames a field, moves it to a different
nesting level, or changes its type, a test **fails with a clear message** instead of the
connector silently mapping less data than it used to. ``test_qlik_contract_vcr_altered_
cassettes.py`` is the proof that this actually happens: it takes a copy of a golden
cassette from this same directory, mutates one field the way a real upstream change
would, and shows a previously-passing assertion now fails.

**Every cassette under ``cassettes/`` is hand-authored, not captured from a live Qlik
Cloud tenant.** RM-01 is explicitly built without live tenants
(``planning/Roadmap/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md``, D8: "The
MVP is also built without live tenants"). Every field, value and URL below is typed by
hand from the shapes RS-02 documents — ``qlik-catalog-api-reference.md`` sections 1.1
(Items API envelope, ``resourceAttributes.secureQri``) and 3.1-3.5 (data-governance
paths, OAuth2, CRUD reference) plus ``qlik-two-way-sync-readiness.md`` section 2 (the
data-product create/PATCH body, ``keyContacts``, ETag concurrency) — never observed from
a real tenant. This mirrors exactly what T3.8's own
``tests/conformance/test_read_cassettes.py`` and T4.6's
``packages/qlabs-connector-databricks/tests/conformance/test_read_cassettes.py`` already
state and do; this directory is self-contained (owns its own ``cassettes/`` beside it,
imports nothing from another task's test directory) rather than reusing theirs, so this
task's guarantee does not depend on a directory a different task owns staying unchanged.

``vcr_config``'s ``match_on`` is ``(method, scheme, host, port, path, query)`` — it
deliberately excludes the request body (see
``qlabs_catalog_sync_sdk.conformance.harness.vcr_config``'s own docstring), so the
``body``/``headers`` values recorded in each cassette are realistic documentation of what
the connector actually sends, not a byte-exact replay requirement.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import vcr
import yaml

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.conformance.harness import vcr_config
from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_connector_qlik import Connector
from qlabs_connector_qlik.config import QlikConfig

ENDPOINT = "qlik"
TENANT_ID = "contract-vcr-tenant"
SPACE_ID = "710b3f4c5d6e7f8a9b0c1d2e"
TENANT_BASE_URL = "https://contract-vcr.eu.qlikcloud.example"

#: Where this task's hand-authored cassettes live — owned by T8.5, distinct from T3.8's
#: ``tests/cassettes/``.
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


def build_config(**overrides: Any) -> QlikConfig:
    """A minimally valid :class:`QlikConfig` for this suite, with any field overridden."""
    values: dict[str, Any] = {
        "base_url": TENANT_BASE_URL,
        "client_id": "contract-vcr-client",
        "client_secret": "contract-vcr-secret",
        "scope": "user_default",
        "space_id": SPACE_ID,
    }
    values.update(overrides)
    return QlikConfig(**values)


def build_ctx(config: QlikConfig | None = None) -> ConnectorContext[QlikConfig]:
    return ConnectorContext.build(
        config=config or build_config(), endpoint=ENDPOINT, tenant=TENANT_ID
    )


async def no_dataset_identity_binding(neutral_id: object) -> str | None:
    """A tier-1 IdentityMap lookup that always misses — the honest default for a
    first-sync cassette where no member dataset has been bound yet."""
    del neutral_id
    return None


def product_ref(native_id: str, **overrides: Any) -> IdentityRef:
    values: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "entity_type": EntityType.DATA_PRODUCT,
        "native_key": native_id,
        "tenant_id": TENANT_ID,
    }
    values.update(overrides)
    return IdentityRef(**values)


def dataset_ref(item_id: str, **overrides: Any) -> IdentityRef:
    values: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "entity_type": EntityType.DATASET,
        "native_key": item_id,
        "tenant_id": TENANT_ID,
        "secondary_keys": {"id": item_id},
    }
    values.update(overrides)
    return IdentityRef(**values)


@pytest.fixture
def qlik_contract_vcr() -> vcr.VCR:
    """The SDK's pre-configured ``vcr.VCR`` (``qlabs_catalog_sync_sdk.conformance
    .harness.vcr_config``), pointed at this task's own ``cassettes/`` directory.
    ``record_mode="once"`` means: play back what is already on disk, never silently hit
    a real network — there is nothing to hit anyway; every cassette here is hand-authored.
    """
    return vcr_config(CASSETTE_DIR)


@pytest.fixture
async def connector() -> AsyncIterator[Connector]:
    """A real, un-set-up :class:`Connector` — the caller runs ``setup()`` itself inside
    the ``use_cassette`` block, since that is what triggers the OAuth2 token POST this
    suite pins."""
    built = Connector()
    try:
        yield built
    finally:
        await built.close()


def write_mutated_cassette(
    *,
    source_name: str,
    dest_path: Path,
    interaction_index: int,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Copy a golden cassette from :data:`CASSETTE_DIR` to ``dest_path``, replacing one
    interaction's JSON response body with ``mutate(original_body)``.

    This is the mechanism every "altered cassette" proof in
    ``test_qlik_contract_vcr_altered_cassettes.py`` uses instead of a hand-edited,
    committed "broken" cassette file: the mutation runs fresh, from the *current* golden
    cassette, on every test invocation. That is what makes the DoD's second half ("a
    deliberately altered cassette fails the suite") a standing guarantee rather than a
    one-off demonstration — if a future change to the golden cassette shifts what field
    is being pinned, this mutation runs against the new content automatically, and the
    proof either still holds or fails loudly for someone to look at, rather than quietly
    testing a stale copy.
    """
    raw = yaml.safe_load((CASSETTE_DIR / source_name).read_text())
    interaction = raw["interactions"][interaction_index]
    body = json.loads(interaction["response"]["body"]["string"])
    interaction["response"]["body"]["string"] = json.dumps(mutate(body))
    dest_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def vcr_for(directory: Path) -> vcr.VCR:
    """A fresh :class:`vcr.VCR` pointed at an arbitrary directory — used to replay a
    mutated cassette written to ``tmp_path`` rather than :data:`CASSETTE_DIR`."""
    return vcr_config(directory)
