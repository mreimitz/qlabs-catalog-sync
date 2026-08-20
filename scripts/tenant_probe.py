#!/usr/bin/env python3
"""Tenant probe — exercises the ``TENANT_UNVERIFIED`` registry against a real tenant.

WP8 / T8.6. This build has no live Databricks workspace or Qlik tenant (see
``qlabs_connector_qlik.unverified``'s module docstring). This script is the other half
of that registry's promise: a runnable program a human points at a real tenant, before
production, to turn each registered assumption into an observed pass/fail/inconclusive
result instead of leaving it a guess forever.

**Credentials.** Read from the environment exactly the way the engine would build a
connector's config — ``ConnectorConfig.for_endpoint("qlik")`` reads
``QLIK__BASE_URL``/``QLIK__CLIENT_ID``/``QLIK__CLIENT_SECRET``/``QLIK__SCOPE``/
``QLIK__SPACE_ID``, and ``for_endpoint("databricks")`` reads ``DATABRICKS__HOST``/
``DATABRICKS__CLIENT_ID``/``DATABRICKS__CLIENT_SECRET``/``DATABRICKS__SQL_WAREHOUSE_ID``
(the endpoint key is ``--endpoint``/``--databricks-endpoint``, default ``qlik``/
``databricks``). Nothing is hard-coded and nothing is ever prompted for on a terminal —
export the variables, or source them from whatever secret manager populates the
process environment, before running this script. ``client_secret`` is a pydantic
``SecretStr``: this script never calls ``.get_secret_value()`` except where the SDK's
own OAuth2 provider needs it internally, and every log line goes through
``structlog``'s redaction processor (``qlabs_catalog_sync_sdk.logging``), so a token
never reaches stdout, a log file, or this script's own report — see
:func:`_summary_line`/:func:`_render_text_report`, which only ever print outcome
metadata (status codes, counts, ids), never a request/response body.

**Safety model — what this script touches.**

* **Default (read-only).** Every check is a ``GET`` against the configured tenant:
  list/read data products, list/read dataset items, a filtered users lookup, a filtered
  items lookup. Nothing is created, modified, or deleted. This is safe to run against a
  real customer tenant at any time.
* ``--include-destructive`` (opt-in, off by default). Creates **exactly one** throwaway
  Qlik data product, named ``qlabs-tenant-probe-<8 hex chars>`` and tagged
  ``qlabs-tenant-probe``, in the configured target space. It is used to observe the
  create/update/ETag/Content-Type/tags-charset behaviors that can only be seen by
  actually writing something, and is deleted again in a ``finally`` block regardless of
  how the checks that used it turned out — see :func:`_run_destructive_checks`.
  ``--sample-dataset-name``/``--sample-owner-email`` additionally attach a *reference*
  to one real dataset/user already in the tenant to the throwaway product (never
  creating or modifying that dataset/user itself) to test id/role-vocabulary
  assumptions; both are optional and skipped (reported inconclusive) when omitted.
  ``--keep-probe-product`` disables the automatic cleanup for debugging — it still
  requires ``--include-destructive`` and prints a loud warning naming the product left
  behind and how to delete it by hand.
* This script never calls Qlik's ``activate``/``deactivate``/``move`` actions, on the
  throwaway product or anything else — activation makes a product tenant-wide
  discoverable and move/deactivate can affect real consumers. Those four items
  (:data:`~qlabs_connector_qlik.unverified.REGISTRY`'s ``QLIK-ACTIVATE-*``,
  ``QLIK-DEACTIVATE-BODY``, ``QLIK-MOVE-IDENTITY-STABILITY``) are manual-only — see
  ``docs/tenant-verification.md``.
* The Databricks side (``--component databricks``/``both``) is **always** read-only:
  Databricks is a read-only connector in v1 by design (the root ``CLAUDE.md``'s v1
  scope guardrails), so there is no destructive path to opt into. It runs one
  ``INFORMATION_SCHEMA`` tag read against an operator-named catalog
  (``--databricks-catalog``), which needs a SQL warehouse configured
  (``DATABRICKS__SQL_WAREHOUSE_ID``) but reads no data outside ``SCHEMA_TAGS``/
  ``TABLE_TAGS`` for that one catalog.
* A deliberate rate-limit stress test is never run by this script (see
  ``DBX-RATE-LIMIT-CADENCE`` in the registry) — sustained load against a customer's
  tenant is not something a safety-first probe launches by default.

**Exit code.** ``0`` unless the script itself could not run to completion (bad config,
unreachable tenant, an unhandled error inside the probe harness itself). An individual
check reporting FAIL is not a script failure — it is exactly the information this script
exists to surface; read the printed report (or ``--json`` output) to see it. Pipe the
report into whatever gate a deployment pipeline wants; this script does not decide
"ready for production" on your behalf.

Run ``python scripts/tenant_probe.py --help`` for the full flag list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    Tag,
    TextField,
)
from qlabs_connector_databricks.auth import build_oauth_provider
from qlabs_connector_databricks.config import DatabricksConfig
from qlabs_connector_databricks.sql_tags import IdentifierError, read_catalog_tags
from qlabs_connector_qlik import Connector
from qlabs_connector_qlik.config import QlikConfig
from qlabs_connector_qlik.lifecycle import DestructiveAction
from qlabs_connector_qlik.read import DATA_PRODUCTS_PATH, ITEMS_PATH
from qlabs_connector_qlik.resolve import USERS_PATH
from qlabs_connector_qlik.unverified import REGISTRY, by_id

#: Marker used for every object this script itself creates, so a stray probe artifact is
#: always recognizable in the tenant's catalog by name or tag alone.
PROBE_TAG = "qlabs-tenant-probe"

Verdict = Literal["pass", "fail", "inconclusive", "skipped"]


@dataclass(slots=True)
class ProbeOutcome:
    """One check's result, keyed to a :mod:`qlabs_connector_qlik.unverified` entry id."""

    id: str
    verdict: Verdict
    detail: str


@dataclass(slots=True)
class ProbeReport:
    outcomes: list[ProbeOutcome] = field(default_factory=list)

    def add(self, outcome_id: str, verdict: Verdict, detail: str) -> None:
        self.outcomes.append(ProbeOutcome(id=outcome_id, verdict=verdict, detail=detail))


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Args:
    component: str
    qlik_endpoint: str
    databricks_endpoint: str
    databricks_catalog: str | None
    databricks_schema: tuple[str, ...]
    include_destructive: bool
    keep_probe_product: bool
    sample_dataset_name: str | None
    sample_owner_email: str | None
    json_output: bool


def _parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        prog="tenant_probe.py",
        description=(
            "Exercise the TENANT_UNVERIFIED registry (qlabs_connector_qlik.unverified) "
            "against a real Qlik tenant and/or Databricks workspace. Read-only by "
            "default; see the module docstring for exactly what --include-destructive "
            "creates and how it cleans up."
        ),
    )
    parser.add_argument(
        "--component",
        choices=("qlik", "databricks", "both"),
        default="qlik",
        help="Which connector's assumptions to probe (default: qlik).",
    )
    parser.add_argument(
        "--endpoint",
        dest="qlik_endpoint",
        default="qlik",
        help=(
            "The QlikConfig.for_endpoint() key: reads <KEY>__BASE_URL etc. from the "
            "environment (default: qlik, i.e. QLIK__...)."
        ),
    )
    parser.add_argument(
        "--databricks-endpoint",
        default="databricks",
        help=(
            "The DatabricksConfig.for_endpoint() key: reads <KEY>__HOST etc. (default: "
            "databricks, i.e. DATABRICKS__...)."
        ),
    )
    parser.add_argument(
        "--databricks-catalog",
        default=None,
        help="A real Unity Catalog catalog name to read SCHEMA_TAGS/TABLE_TAGS from.",
    )
    parser.add_argument(
        "--databricks-schema",
        action="append",
        default=[],
        help=(
            "Narrow the Databricks tag read to this schema (repeatable). Omit to read "
            "every schema in the named catalog."
        ),
    )
    parser.add_argument(
        "--include-destructive",
        action="store_true",
        help=(
            "Opt in to the Qlik write-path checks: creates one throwaway data product "
            "in the configured space, exercises it, and deletes it again. See the "
            "module docstring's safety model before passing this."
        ),
    )
    parser.add_argument(
        "--keep-probe-product",
        action="store_true",
        help=(
            "Skip automatic cleanup of the throwaway product (requires "
            "--include-destructive). For debugging only; prints how to delete it "
            "by hand."
        ),
    )
    parser.add_argument(
        "--sample-dataset-name",
        default=None,
        help=(
            "A real dataset name already present in the configured space, used only "
            "with --include-destructive to test the datasetIds id/name-filter "
            "assumptions. Never modified — only referenced from the throwaway product."
        ),
    )
    parser.add_argument(
        "--sample-owner-email",
        default=None,
        help=(
            "A real user's email in the tenant, used only with --include-destructive "
            "to test the keyContacts role vocabulary. Never modified."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit the report as JSON instead of a human-readable table.",
    )
    namespace = parser.parse_args(argv)
    return Args(
        component=namespace.component,
        qlik_endpoint=namespace.qlik_endpoint,
        databricks_endpoint=namespace.databricks_endpoint,
        databricks_catalog=namespace.databricks_catalog,
        databricks_schema=tuple(namespace.databricks_schema),
        include_destructive=namespace.include_destructive,
        keep_probe_product=namespace.keep_probe_product,
        sample_dataset_name=namespace.sample_dataset_name,
        sample_owner_email=namespace.sample_owner_email,
        json_output=namespace.json_output,
    )


# --------------------------------------------------------------------------------------
# Small HTTP helpers shared by the read-only checks
# --------------------------------------------------------------------------------------


async def _get_status(http: HttpEndpoint, url: str, *, params: dict[str, Any]) -> tuple[
    int, dict[str, Any] | None
]:
    """``GET url``, returning ``(status_code, json_body_or_None)``.

    Never raises for a non-2xx response that ``httpx`` would otherwise turn into
    ``HTTPStatusError`` on a retried call — this helper's whole purpose is to observe
    the raw status a probe check is testing for, not to classify it into a typed SDK
    exception the way the real connector code does.
    """
    try:
        response = await http.get(url, params=params)
    except httpx.HTTPStatusError as exc:
        return exc.response.status_code, _maybe_json(exc.response)
    return response.status_code, _maybe_json(response)


def _maybe_json(response: httpx.Response) -> dict[str, Any] | None:
    if not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _current_etag(http: HttpEndpoint, native_id: str) -> str | None:
    """A fresh ETag for the throwaway product, via a plain re-GET.

    Used between destructive-check steps so each one starts from a known-current
    revision instead of accumulating staleness from a previous step's write.
    """
    response = await http.get(f"{DATA_PRODUCTS_PATH}/{native_id}")
    etag = response.headers.get("etag")
    return etag if isinstance(etag, str) and etag else None


# --------------------------------------------------------------------------------------
# Qlik: read-only checks
# --------------------------------------------------------------------------------------


async def _run_qlik_readonly_checks(connector: Connector, report: ProbeReport) -> None:
    assert connector.http is not None  # narrowed: setup() already ran
    http = connector.http
    space_id = connector.ctx.config.space_id if connector.ctx is not None else ""

    # QLIK-DP-LIST-ENDPOINT — does a bare collection GET exist at all?
    status, body = await _get_status(
        http, DATA_PRODUCTS_PATH, params={"limit": 1, "spaceId": space_id}
    )
    products: list[dict[str, Any]] = []
    if status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
        products = [item for item in body["data"] if isinstance(item, dict)]
        report.add(
            "QLIK-DP-LIST-ENDPOINT",
            "pass",
            f"GET {DATA_PRODUCTS_PATH} returned 200 with a data[] array ({len(products)} item(s)).",
        )
    elif status == 200:
        report.add(
            "QLIK-DP-LIST-ENDPOINT",
            "inconclusive",
            f"GET {DATA_PRODUCTS_PATH} returned 200 but the body did not parse as "
            "{data: [...]}; the endpoint exists but its shape differs from the assumption.",
        )
    else:
        report.add(
            "QLIK-DP-LIST-ENDPOINT",
            "fail",
            f"GET {DATA_PRODUCTS_PATH} returned HTTP {status} — the bare collection "
            "endpoint may not exist as assumed.",
        )

    # QLIK-DP-STATUS-ACTIVATED-SIGNAL — informational only, from whatever we just listed.
    if products:
        activated_true = sum(1 for item in products if item.get("activated") is True)
        report.add(
            "QLIK-DP-STATUS-ACTIVATED-SIGNAL",
            "inconclusive",
            f"{activated_true}/{len(products)} sampled product(s) report activated=true. "
            "This is supporting evidence only — confirming whether a *deactivated* product "
            "is distinguishable from a never-activated one needs a live "
            "activate-then-deactivate-then-read cycle, which this script does not perform.",
        )
    else:
        report.add(
            "QLIK-DP-STATUS-ACTIVATED-SIGNAL",
            "skipped",
            "no products were listed to sample (see QLIK-DP-LIST-ENDPOINT).",
        )

    # QLIK-DATASET-RESOURCE-ATTRS — sample a few dataset items for resourceAttributes.
    status, body = await _get_status(
        http, ITEMS_PATH, params={"limit": 5, "resourceType": "dataset", "spaceId": space_id}
    )
    if status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
        items = [item for item in body["data"] if isinstance(item, dict)]
        if not items:
            report.add(
                "QLIK-DATASET-RESOURCE-ATTRS",
                "skipped",
                "no dataset items exist in the configured space to sample.",
            )
        else:
            with_secure_qri = 0
            for item in items[:5]:
                item_id = item.get("id")
                if not isinstance(item_id, str):
                    continue
                detail_status, detail_body = await _get_status(
                    http, f"{ITEMS_PATH}/{item_id}", params={}
                )
                if detail_status == 200 and isinstance(detail_body, dict):
                    attrs = detail_body.get("resourceAttributes")
                    if isinstance(attrs, dict) and attrs.get("secureQri"):
                        with_secure_qri += 1
            report.add(
                "QLIK-DATASET-RESOURCE-ATTRS",
                "pass" if with_secure_qri == len(items[:5]) else "inconclusive",
                f"{with_secure_qri}/{len(items[:5])} sampled dataset item(s) carry "
                "resourceAttributes.secureQri on the detail GET.",
            )
    else:
        report.add(
            "QLIK-DATASET-RESOURCE-ATTRS",
            "inconclusive",
            f"GET {ITEMS_PATH} (resourceType=dataset) returned HTTP {status}; could not "
            "sample dataset items.",
        )

    # QLIK-USERS-FILTER-SYNTAX — a filter that cannot match a real user; 200 vs 400.
    probe_email = f"{uuid.uuid4().hex}@qlabs-tenant-probe.invalid"
    status, _ = await _get_status(
        http, USERS_PATH, params={"filter": f"email eq '{probe_email}'", "limit": 1}
    )
    if status == 200:
        report.add(
            "QLIK-USERS-FILTER-SYNTAX",
            "pass",
            "GET /api/v1/users with filter=email eq '<value>' returned 200 (the "
            "parameter is at least recognized as well-formed).",
        )
    elif status == 400:
        report.add(
            "QLIK-USERS-FILTER-SYNTAX",
            "fail",
            "GET /api/v1/users with filter=email eq '<value>' returned 400 — the "
            "assumed filter syntax is rejected by this tenant.",
        )
    else:
        report.add(
            "QLIK-USERS-FILTER-SYNTAX",
            "inconclusive",
            f"GET /api/v1/users with the filter returned HTTP {status}.",
        )

    # QLIK-ITEMS-NAME-FILTER-SEMANTICS is only meaningful with a real dataset name, so it
    # is left unreported here rather than pre-filled: _run_qlik's fallback loop reports it
    # "skipped" when the destructive checks (the only path that can give it a real
    # verdict) do not run this pass, so it is never reported twice.


# --------------------------------------------------------------------------------------
# Qlik: destructive (opt-in) checks
# --------------------------------------------------------------------------------------


async def _run_qlik_destructive_checks(
    connector: Connector,
    report: ProbeReport,
    *,
    sample_dataset_name: str | None,
    sample_owner_email: str | None,
    keep_probe_product: bool,
) -> None:
    assert connector.http is not None and connector.writer is not None
    assert connector.lifecycle is not None
    http = connector.http
    writer = connector.writer

    probe_name = f"{PROBE_TAG}-{uuid.uuid4().hex[:8]}"
    print(
        f"[tenant_probe] creating throwaway Qlik data product {probe_name!r} "
        f"(tag={PROBE_TAG!r}) in the configured space for the destructive checks...",
        file=sys.stderr,
    )

    create_result = await connector.create(
        DataProduct(
            name=probe_name,
            description=TextField.plain(
                "Created by qlabs-catalog-sync's scripts/tenant_probe.py. Safe to delete "
                "if found outside a probe run."
            ),
            tags=[Tag(key=PROBE_TAG)],
        )
    )
    ref = create_result.ref
    native_id = ref.secondary_keys.get("id", ref.native_key)
    print(f"[tenant_probe] created {native_id!r}; will delete it when done.", file=sys.stderr)

    report.add(
        "QLIK-DP-CREATE-ETAG",
        "pass" if create_result.source_revision else "fail",
        (
            f"create() returned source_revision={create_result.source_revision!r}."
            if create_result.source_revision
            else "the create response carried no ETag header."
        ),
    )
    report.add("QLIK-ROLE-PERMISSION-STRINGS", "pass", "create() succeeded (no auth error).")

    try:
        await _probe_patch_etag_and_content_type(http, native_id, report)
        await _probe_tags_charset(writer, ref, http, native_id, report)
        await _probe_dataset_resolution(
            writer, ref, http, native_id, sample_dataset_name, report
        )
        await _probe_owner_role_vocab(writer, ref, http, native_id, sample_owner_email, report)
    except AuthError as exc:
        report.add(
            "QLIK-ROLE-PERMISSION-STRINGS",
            "fail",
            f"a write during the probe was rejected as unauthorized: {exc}",
        )
    finally:
        if keep_probe_product:
            print(
                f"[tenant_probe] --keep-probe-product set: leaving {native_id!r} "
                f"(name={probe_name!r}, tag={PROBE_TAG!r}) in place. Delete it manually "
                f"via DELETE {DATA_PRODUCTS_PATH}/{native_id} when done.",
                file=sys.stderr,
            )
        else:
            try:
                await connector.lifecycle.delete(ref)
                print(f"[tenant_probe] deleted throwaway product {native_id!r}.", file=sys.stderr)
            except ConnectorError as exc:
                print(
                    f"[tenant_probe] WARNING: could not delete throwaway product "
                    f"{native_id!r} (name={probe_name!r}, tag={PROBE_TAG!r}): {exc}. "
                    f"Delete it by hand: DELETE {DATA_PRODUCTS_PATH}/{native_id}.",
                    file=sys.stderr,
                )
                report.add(
                    "QLIK-ROLE-PERMISSION-STRINGS",
                    "fail",
                    f"cleanup delete() was rejected: {exc} — the throwaway product may "
                    "still exist in the tenant.",
                )


async def _probe_patch_etag_and_content_type(
    http: HttpEndpoint, native_id: str, report: ProbeReport
) -> None:
    """The two-PATCH conflict test: proves QLIK-DP-ETAG-PATCH directly."""
    url = f"{DATA_PRODUCTS_PATH}/{native_id}"
    stale_etag = await _current_etag(http, native_id)
    if stale_etag is None:
        report.add(
            "QLIK-DP-PATCH-204-ETAG",
            "inconclusive",
            "no ETag available after create to start from.",
        )
        report.add("QLIK-DP-PATCH-CONTENT-TYPE", "skipped", "no ETag to guard the PATCH with.")
        report.add("QLIK-DP-ETAG-PATCH", "skipped", "no ETag to make stale.")
        return

    body_1 = [{"op": "replace", "path": "/description", "value": "qlabs-tenant-probe: step 1"}]
    try:
        response_1 = await http.patch(url, json=body_1, headers={"if-match": stale_etag})
    except httpx.HTTPStatusError as exc:
        report.add(
            "QLIK-DP-PATCH-CONTENT-TYPE",
            "fail",
            f"first PATCH (application/json body) was rejected: HTTP {exc.response.status_code}.",
        )
        report.add("QLIK-DP-PATCH-204-ETAG", "skipped", "first PATCH failed.")
        report.add("QLIK-DP-ETAG-PATCH", "skipped", "first PATCH failed.")
        return

    report.add(
        "QLIK-DP-PATCH-CONTENT-TYPE",
        "pass",
        f"first PATCH accepted with application/json (HTTP {response_1.status_code}).",
    )
    fresh_etag = response_1.headers.get("etag")
    report.add(
        "QLIK-DP-PATCH-204-ETAG",
        "pass" if fresh_etag else "fail",
        (
            f"PATCH response (HTTP {response_1.status_code}) carried etag={fresh_etag!r}."
            if fresh_etag
            else f"PATCH response (HTTP {response_1.status_code}) carried no ETag header."
        ),
    )

    # The second PATCH deliberately reuses the now-stale `stale_etag`, not `fresh_etag`.
    body_2 = [{"op": "replace", "path": "/description", "value": "qlabs-tenant-probe: step 2"}]
    try:
        await http.patch(url, json=body_2, headers={"if-match": stale_etag})
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 412:
            report.add(
                "QLIK-DP-ETAG-PATCH",
                "pass",
                "second PATCH with the now-stale if-match returned 412 — Qlik honors "
                "if-match on data-product PATCH.",
            )
        else:
            report.add(
                "QLIK-DP-ETAG-PATCH",
                "inconclusive",
                f"second PATCH with a stale if-match returned HTTP {status}, not 412 — "
                "cannot confirm ETag enforcement from this status alone.",
            )
        return
    report.add(
        "QLIK-DP-ETAG-PATCH",
        "fail",
        "second PATCH with a stale if-match SUCCEEDED — Qlik does not appear to enforce "
        "if-match on data-product PATCH. This means concurrent writers can silently "
        "clobber each other's changes.",
    )


async def _probe_tags_charset(
    writer: Any, ref: IdentityRef, http: HttpEndpoint, native_id: str, report: ProbeReport
) -> None:
    etag = await _current_etag(http, native_id)
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(
                field="tags",
                mode=FieldUpdateMode.REPLACE,
                value=[{"key": PROBE_TAG, "value": "a=b"}],
            )
        ],
        expected_revision=etag,
    )
    try:
        await writer.update(ref, diff)
    except ConnectorError as exc:
        report.add(
            "QLIK-TAGS-CHARSET-CAP",
            "fail",
            f"writing a tag whose flattened value contains '=' was rejected: {exc}",
        )
        return
    report.add(
        "QLIK-TAGS-CHARSET-CAP",
        "pass",
        "a tag flattened to 'qlabs-tenant-probe=a=b' was accepted by the PATCH endpoint.",
    )


async def _probe_dataset_resolution(
    writer: Any,
    ref: IdentityRef,
    http: HttpEndpoint,
    native_id: str,
    sample_dataset_name: str | None,
    report: ProbeReport,
) -> None:
    if sample_dataset_name is None:
        report.add(
            "QLIK-DATASETIDS-RESOURCEID",
            "skipped",
            "no --sample-dataset-name given.",
        )
        report.add(
            "QLIK-ITEMS-NAME-FILTER-SEMANTICS",
            "skipped",
            "no --sample-dataset-name given.",
        )
        return

    # Substring-filter check (QLIK-ITEMS-NAME-FILTER-SEMANTICS): query with a strict
    # substring of the real name and see whether the server-side filter still finds it.
    if len(sample_dataset_name) > 2:
        substring = sample_dataset_name[1:-1]
        status, body = await _get_status(
            http, ITEMS_PATH, params={"resourceType": "dataset", "name": substring, "limit": 50}
        )
        found = False
        if status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
            found = any(
                isinstance(item, dict) and item.get("name") == sample_dataset_name
                for item in body["data"]
            )
        report.add(
            "QLIK-ITEMS-NAME-FILTER-SEMANTICS",
            "inconclusive",
            f"filtering by the substring {substring!r} of {sample_dataset_name!r} "
            f"{'returned' if found else 'did not return'} the full-name match "
            f"(HTTP {status}) — read.py's client-side exact-match re-check makes this "
            "informational only, not load-bearing.",
        )
    else:
        report.add(
            "QLIK-ITEMS-NAME-FILTER-SEMANTICS", "skipped", "sample dataset name too short to test."
        )

    neutral_id = uuid.uuid4()
    etag = await _current_etag(http, native_id)
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(
                field="dataset_refs",
                mode=FieldUpdateMode.REPLACE,
                value=[str(neutral_id)],
            )
        ],
        expected_revision=etag,
    )
    try:
        result = await writer.update(
            ref, diff, dataset_names={neutral_id: sample_dataset_name}
        )
    except ConnectorError as exc:
        report.add(
            "QLIK-DATASETIDS-RESOURCEID",
            "fail",
            f"attaching the sample dataset by name-match failed: {exc}",
        )
        return

    if "dataset_refs" in result.written_fields:
        report.add(
            "QLIK-DATASETIDS-RESOURCEID",
            "pass",
            f"the sample dataset {sample_dataset_name!r} resolved and was accepted as a "
            "datasetIds member. This confirms Qlik accepted the id our resolver chose — "
            "manually confirm in the Qlik hub UI that the product actually shows this "
            "dataset as a member, not just an accepted id string.",
        )
    else:
        report.add(
            "QLIK-DATASETIDS-RESOURCEID",
            "inconclusive",
            f"the sample dataset {sample_dataset_name!r} did not resolve (name match "
            f"found nothing in the configured space) — detail: {result.detail}",
        )


async def _probe_owner_role_vocab(
    writer: Any,
    ref: IdentityRef,
    http: HttpEndpoint,
    native_id: str,
    sample_owner_email: str | None,
    report: ProbeReport,
) -> None:
    if sample_owner_email is None:
        report.add(
            "QLIK-KEYCONTACTS-ROLE-VOCAB", "skipped", "no --sample-owner-email given."
        )
        return

    for role in ("contact", "other"):
        etag = await _current_etag(http, native_id)
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[
                FieldChange(
                    field="owners",
                    mode=FieldUpdateMode.REPLACE,
                    value=[{"email": sample_owner_email, "role": role}],
                )
            ],
            expected_revision=etag,
        )
        try:
            result = await writer.update(ref, diff)
        except ConnectorError as exc:
            report.add(
                "QLIK-KEYCONTACTS-ROLE-VOCAB",
                "fail",
                f"role={role!r} was rejected: {exc}",
            )
            return
        if "owners" not in result.written_fields:
            report.add(
                "QLIK-KEYCONTACTS-ROLE-VOCAB",
                "inconclusive",
                f"the sample email did not resolve to a Qlik user — detail: {result.detail}",
            )
            return

    report.add(
        "QLIK-KEYCONTACTS-ROLE-VOCAB",
        "pass",
        "keyContacts role values 'contact' and 'other' were both accepted by the PATCH endpoint.",
    )


# --------------------------------------------------------------------------------------
# Databricks: read-only checks
# --------------------------------------------------------------------------------------


async def _run_databricks_checks(
    *,
    endpoint_key: str,
    catalog_name: str | None,
    schema_names: tuple[str, ...],
    report: ProbeReport,
) -> None:
    if catalog_name is None:
        for check_id in ("DBX-SQL-TAGS-READ",):
            report.add(
                check_id,
                "skipped",
                "no --databricks-catalog given; nothing to read tags from.",
            )
        return

    config = DatabricksConfig.for_endpoint(endpoint_key)
    if config.sql_warehouse_id is None:
        report.add(
            "DBX-SQL-TAGS-READ",
            "skipped",
            f"{endpoint_key.upper()}__SQL_WAREHOUSE_ID is not set — the same gate D6's "
            "capability manifest uses; nothing to probe without a warehouse.",
        )
        return

    token_client = httpx.AsyncClient()
    try:
        oauth_provider = build_oauth_provider(config, transport=token_client)
        http = HttpEndpoint(config.host, auth=oauth_provider)
        try:
            index = await read_catalog_tags(
                http,
                sql_warehouse_id=config.sql_warehouse_id,
                catalog_name=catalog_name,
                endpoint=endpoint_key,
                schema_names=schema_names or None,
            )
        except IdentifierError as exc:
            report.add(
                "DBX-SQL-TAGS-READ",
                "fail",
                f"catalog name {catalog_name!r} failed the identifier safety check before "
                f"any HTTP call: {exc}. See DBX-IDENTIFIER-CHARSET.",
            )
            return
        except ConnectorError as exc:
            report.add(
                "DBX-SQL-TAGS-READ",
                "fail",
                f"read_catalog_tags raised {type(exc).__name__}: {exc}",
            )
            return
        finally:
            await http.aclose()
    finally:
        await token_client.aclose()

    if index is None:
        report.add(
            "DBX-SQL-TAGS-READ",
            "inconclusive",
            "read_catalog_tags returned None unexpectedly (sql_warehouse_id was set).",
        )
        return

    schema_count = len(index.schema_tags)
    table_count = len(index.table_tags)
    report.add(
        "DBX-SQL-TAGS-READ",
        "pass",
        f"read_catalog_tags succeeded for catalog {catalog_name!r}: {schema_count} "
        f"schema(s) and {table_count} table(s) with tag rows parsed without error. This "
        "confirms DBX-SCHEMA-TAGS-COLUMNS, DBX-TABLE-TAGS-COLUMNS and "
        "DBX-STATEMENT-EXEC-RESPONSE-SHAPE together, and DBX-IDENTIFIER-CHARSET passed "
        "implicitly (the identifier was accepted).",
    )


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

_VERDICT_ORDER: dict[Verdict, int] = {"fail": 0, "inconclusive": 1, "skipped": 2, "pass": 3}


def _render_text_report(report: ProbeReport) -> str:
    lines: list[str] = []
    lines.append("=" * 88)
    lines.append("QLabs Catalog Sync — tenant probe report")
    lines.append("=" * 88)
    ordered = sorted(report.outcomes, key=lambda o: (_VERDICT_ORDER[o.verdict], o.id))
    for outcome in ordered:
        try:
            entry = by_id(outcome.id)
            must_verify = (
                " [MUST VERIFY BEFORE PRODUCTION]" if entry.must_verify_before_production else ""
            )
            summary = entry.summary
        except KeyError:
            must_verify = ""
            summary = "(no matching registry entry)"
        marker = {"pass": "PASS", "fail": "FAIL", "inconclusive": "????", "skipped": "SKIP"}[
            outcome.verdict
        ]
        lines.append(f"[{marker:5}] {outcome.id}{must_verify}")
        lines.append(f"         {summary}")
        lines.append(f"         -> {outcome.detail}")
        lines.append("")
    counts: dict[Verdict, int] = {"pass": 0, "fail": 0, "inconclusive": 0, "skipped": 0}
    for outcome in report.outcomes:
        counts[outcome.verdict] += 1
    lines.append("-" * 88)
    lines.append(
        f"{counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['inconclusive']} inconclusive, {counts['skipped']} skipped."
    )
    if counts["fail"]:
        lines.append(
            "At least one check FAILED. Do not treat this connector as production-ready "
            "until you understand why — see docs/tenant-verification.md."
        )
    lines.append(
        "Every registry item this run did not check (no probe_check, or --component "
        "excluded it) still needs the manual step in docs/tenant-verification.md before "
        "first production use."
    )
    return "\n".join(lines)


def _render_json_report(report: ProbeReport) -> str:
    payload = [
        {"id": outcome.id, "verdict": outcome.verdict, "detail": outcome.detail}
        for outcome in report.outcomes
    ]
    return json.dumps(payload, indent=2, sort_keys=False)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


async def _run_qlik(args: Args, report: ProbeReport) -> None:
    config = QlikConfig.for_endpoint(args.qlik_endpoint)
    ctx: ConnectorContext[QlikConfig] = ConnectorContext.build(
        config=config, endpoint=args.qlik_endpoint
    )
    connector = Connector()
    if args.include_destructive:
        connector.enabled_destructive_actions = frozenset({DestructiveAction.DELETE})
    await connector.setup(ctx)
    try:
        health = await connector.healthcheck()
        print(f"[tenant_probe] Qlik healthcheck: {health.state.value}", file=sys.stderr)
        await _run_qlik_readonly_checks(connector, report)
        if args.include_destructive:
            await _run_qlik_destructive_checks(
                connector,
                report,
                sample_dataset_name=args.sample_dataset_name,
                sample_owner_email=args.sample_owner_email,
                keep_probe_product=args.keep_probe_product,
            )
        else:
            for entry in REGISTRY:
                if entry.component.value == "qlik" and entry.id not in {
                    o.id for o in report.outcomes
                } and entry.probe_check == entry.id:
                    report.add(
                        entry.id,
                        "skipped",
                        "requires --include-destructive; not run in read-only mode.",
                    )
    finally:
        await connector.close()


async def _run(args: Args) -> ProbeReport:
    report = ProbeReport()
    if args.component in ("qlik", "both"):
        await _run_qlik(args, report)
    if args.component in ("databricks", "both"):
        await _run_databricks_checks(
            endpoint_key=args.databricks_endpoint,
            catalog_name=args.databricks_catalog,
            schema_names=args.databricks_schema,
            report=report,
        )
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.keep_probe_product and not args.include_destructive:
        print(
            "--keep-probe-product requires --include-destructive.",
            file=sys.stderr,
        )
        return 1
    try:
        report = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - the probe harness itself failed; report plainly
        print(f"[tenant_probe] FAILED TO RUN: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(_render_json_report(report) if args.json_output else _render_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
