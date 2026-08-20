"""The other half of T8.5's DoD: a deliberately altered cassette fails the suite.

Each test here takes a **copy** of one of this directory's golden cassettes (never the
committed file itself), mutates one field's JSON the way a realistic upstream change
would, and replays the connector's real code against the mutated copy. The mutation
runs fresh on every test invocation via ``conftest.py``'s ``write_mutated_cassette`` --
so this is a standing, permanent guarantee (it re-derives the "broken" cassette from
whatever the golden one currently says), not a one-off, hand-edited demonstration
cassette that could quietly go stale.

Two mutations, the two of the task brief's three named shapes of change that were not
already used in the sibling Qlik suite (which covers "a renamed field" and "a type
change" -- see ``test_qlik_contract_vcr_altered_cassettes.py`` in
``packages/qlabs-connector-qlik``):

* **A renamed field** -- ``next_page_token`` becomes ``nextPageToken`` on page 1 of the
  ``/schemas`` listing. ``HttpEndpoint.paginate_offset`` looks for the literal key
  ``next_page_token`` (``http.py``'s own default ``next_token_key``); when it is not
  found, pagination simply stops -- no error, no warning, just fewer results. This is
  the purest "silent data loss": a listing that should have returned three schemas
  silently returns two.
* **A moved nesting level** -- the terminal ``SUCCEEDED`` response's ``status.state``
  is promoted out of its ``status`` wrapper to a top-level ``state`` key.
  ``sql_tags._state_of`` only ever looks inside ``status``, so it reads "UNKNOWN" --
  which is not ``SUCCEEDED``, so the connector raises ``TransientError`` rather than
  silently treating the response as a success with no rows. Unlike the two "silent"
  proofs (this file's pagination case and both of the Qlik suite's), this one surfaces
  loudly -- which is also a legitimate way for "what the connector does with a shape it
  does not expect" to show up, and worth pinning precisely because a maintainer would
  otherwise see only an opaque "TransientError: state=UNKNOWN" with no cassette to
  compare it against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks import read, sql_tags

from .conftest import HOST, vcr_for, write_mutated_cassette


def _rename_next_page_token(body: dict[str, Any]) -> dict[str, Any]:
    assert "next_page_token" in body, "golden cassette no longer carries next_page_token"
    body["nextPageToken"] = body.pop("next_page_token")
    return body


async def test_renaming_next_page_token_silently_truncates_the_listing(
    tmp_path: Path,
) -> None:
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    write_mutated_cassette(
        source_name="databricks_schemas_pagination.yaml",
        dest_path=mutated_dir / "databricks_schemas_pagination.yaml",
        interaction_index=0,  # page 1's response
        mutate=_rename_next_page_token,
    )

    async with HttpEndpoint(HOST) as http:
        with vcr_for(mutated_dir).use_cassette("databricks_schemas_pagination.yaml"):
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

    # The golden-cassette assertion (test_databricks_contract_vcr_pagination.py) is:
    #   [s["name"] for s in schemas] == ["sales", "marketing", "finance"]
    # That assertion now FAILS against the mutated cassette -- page 2 ("finance") is
    # silently dropped because paginate_offset no longer recognizes the (renamed)
    # continuation token, not because anything raised.
    names = [schema["name"] for schema in schemas]
    assert names != ["sales", "marketing", "finance"], (
        "renaming next_page_token should have broken pagination -- if this assertion "
        "fails, iter_matching_schemas started reading the continuation token from "
        "somewhere this mutation did not touch, and the proof needs updating"
    )
    assert names == ["sales", "marketing"]  # page 1 only; page 2 silently lost


def _move_state_out_of_status(body: dict[str, Any]) -> dict[str, Any]:
    status = body.pop("status")
    assert status == {"state": "SUCCEEDED"}, "golden cassette's terminal status changed"
    body["state"] = status["state"]
    return body


async def test_moving_state_out_of_status_raises_instead_of_a_silent_success(
    tmp_path: Path,
) -> None:
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    write_mutated_cassette(
        source_name="databricks_sql_tags_statement_execution.yaml",
        dest_path=mutated_dir / "databricks_sql_tags_statement_execution.yaml",
        interaction_index=2,  # the SCHEMA_TAGS statement's terminal SUCCEEDED poll
        mutate=_move_state_out_of_status,
    )

    clock = ManualClock()
    async with HttpEndpoint(HOST) as http:
        with vcr_for(mutated_dir).use_cassette("databricks_sql_tags_statement_execution.yaml"):
            with pytest.raises(TransientError, match="UNKNOWN"):
                await sql_tags.read_catalog_tags(
                    http,
                    sql_warehouse_id="contract-vcr-wh-1",
                    catalog_name="prod",
                    endpoint="databricks",
                    schema_names=["sales"],
                    clock=clock,
                )
