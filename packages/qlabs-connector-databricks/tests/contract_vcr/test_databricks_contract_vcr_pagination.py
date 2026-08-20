"""VCR contract test -- Databricks ``/schemas`` offset/token pagination (T8.5).

See ``conftest.py`` for what this directory pins and why every cassette is
hand-authored. Pins ``HttpEndpoint.paginate_offset``'s stop condition as actually used
by this connector: a page carrying ``next_page_token`` means "there is more", and its
absence means "stop" -- the only signal in play. This cassette's mutation
(``test_databricks_contract_vcr_altered_cassettes.py``) is the proof that a renamed
pagination field truncates a listing silently rather than raising.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks import read

from .conftest import HOST


async def test_iter_matching_schemas_pins_next_page_token_pagination(
    databricks_contract_vcr: vcr.VCR,
) -> None:
    """``read.iter_matching_schemas()`` called directly against a plain
    ``HttpEndpoint`` -- pages until a response omits ``next_page_token``."""
    async with HttpEndpoint(HOST) as http:
        with databricks_contract_vcr.use_cassette("databricks_schemas_pagination.yaml"):
            schemas = [
                raw
                async for raw in read.iter_matching_schemas(
                    http,
                    catalog_names=["prod"],
                    catalog_schema_patterns=["prod.*"],
                    endpoint="databricks",
                    page_size=2,
                )
            ]

    assert [schema["name"] for schema in schemas] == ["sales", "marketing", "finance"]
