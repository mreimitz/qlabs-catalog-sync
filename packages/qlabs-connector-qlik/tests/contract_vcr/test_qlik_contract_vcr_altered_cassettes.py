"""The other half of T8.5's DoD: a deliberately altered cassette fails the suite.

Each test here takes a **copy** of one of this directory's golden cassettes (never the
committed file itself), mutates one field's JSON the way a realistic upstream change
would, and replays the connector's real mapping code against the mutated copy. The
mutation runs fresh on every test invocation via ``conftest.py``'s
``write_mutated_cassette`` -- so this is a standing, permanent guarantee (it re-derives
the "broken" cassette from whatever the golden one currently says), not a one-off,
hand-edited demonstration cassette that could quietly go stale.

Two mutations, two of the three shapes of change the task brief names:

* **A renamed field** -- ``resourceAttributes.secureQri`` becomes ``secureQRI`` on the
  dataset item cassette. The connector does **not** raise: ``read.py``'s identity rule
  falls through to the legacy ``resourceAttributes.qri`` (module docstring, point 1),
  which is still present. That is exactly the "silent data loss" this task exists to
  catch -- a maintainer would see every dataset re-identified under a different key with
  no error at all, unless a test pins the *exact* identity value and fails when it
  changes. That is what this test does.
* **A type change** -- the data product's ``tags`` field goes from Qlik's documented
  ``string[]`` to an array of ``{"name": ...}`` objects (a shape that would be
  indistinguishable from a reasonable-looking upstream "enrich the tag" change).
  ``read.py``'s ``_string_tags`` only accepts bare strings, so every tag is silently
  dropped -- ``data_product.tags`` goes from two tags to zero with no error raised at
  all. This is the purer "silent data loss" case: nothing crashes, data just vanishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_qlik import Connector, read

from .conftest import (
    ENDPOINT,
    TENANT_BASE_URL,
    build_ctx,
    dataset_ref,
    product_ref,
    vcr_for,
    write_mutated_cassette,
)


def _rename_secure_qri(body: dict[str, Any]) -> dict[str, Any]:
    attrs = body["resourceAttributes"]
    assert "secureQri" in attrs, "golden cassette no longer carries secureQri to rename"
    attrs["secureQRI"] = attrs.pop("secureQri")
    return body


async def test_renaming_secure_qri_silently_degrades_the_dataset_identity(
    tmp_path: Path,
) -> None:
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    write_mutated_cassette(
        source_name="qlik_read_dataset_item.yaml",
        dest_path=mutated_dir / "qlik_read_dataset_item.yaml",
        interaction_index=0,
        mutate=_rename_secure_qri,
    )

    async with HttpEndpoint(
        TENANT_BASE_URL, auth=("Bearer", "contract-vcr-static-token")
    ) as http:
        with vcr_for(mutated_dir).use_cassette("qlik_read_dataset_item.yaml"):
            dataset = await read.read_dataset(
                http, dataset_ref("item-contract-vcr-orders-1"), endpoint=ENDPOINT
            )

    # The golden-cassette assertion (test_qlik_contract_vcr_reads.py) is:
    #   identity.native_key == "qdf-secure:contract-vcr-tenant:...:ds-orders-contract-1"
    # That assertion now FAILS against the mutated cassette -- proving the point rather
    # than asserting it away, so a future refactor that "fixes" this by accident cannot
    # make the proof pass for the wrong reason.
    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    secure_qri = "qdf-secure:contract-vcr-tenant:710b3f4c5d6e7f8a9b0c1d2e:ds-orders-contract-1"
    assert identity.native_key != secure_qri, (
        "renaming secureQri should have broken the pinned identity value -- if this "
        "assertion fails, read.py started reading the identity from somewhere this "
        "mutation did not touch, and the proof needs updating"
    )
    # What it silently fell back to instead: the legacy qri, not an error.
    legacy_qri = "qdf:contract-vcr-tenant:710b3f4c5d6e7f8a9b0c1d2e:ds-orders-contract-1"
    assert identity.native_key == legacy_qri


def _tags_become_objects(body: dict[str, Any]) -> dict[str, Any]:
    assert body["tags"] == ["sales", "revenue"], "golden cassette tags shape changed"
    body["tags"] = [{"name": "sales"}, {"name": "revenue"}]
    return body


async def test_tags_becoming_objects_silently_drops_every_tag(tmp_path: Path) -> None:
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    write_mutated_cassette(
        source_name="qlik_setup_and_read_data_product.yaml",
        dest_path=mutated_dir / "qlik_setup_and_read_data_product.yaml",
        interaction_index=1,  # the data-product GET, not the OAuth token POST
        mutate=_tags_become_objects,
    )

    with vcr_for(mutated_dir).use_cassette("qlik_setup_and_read_data_product.yaml"):
        connector = Connector()
        await connector.setup(build_ctx())
        try:
            data_product = await connector.read(product_ref("9a1b2c3d4e5f60718293a4b6"))
        finally:
            await connector.close()

    # The golden-cassette assertion is `{tag.key for tag in data_product.tags} ==
    # {"sales", "revenue"}`. Against the mutated (object-shaped) cassette it silently
    # becomes an empty list -- no exception, no warning surfaced to the caller.
    assert data_product.tags == [], (
        "tags becoming object-shaped should have silently emptied data_product.tags -- "
        "if this assertion fails, read.py's tag mapping changed to accept a different "
        "shape and the proof needs updating"
    )
