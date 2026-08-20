"""Object builders and review-file helpers shared by the identity tests.

A plain module rather than fixtures: these are pure constructors, and reading a test is
easier when the objects it is about are built inline instead of arriving as fixtures.
``conftest.py`` puts this directory on ``sys.path`` (pytest runs with
``--import-mode=importlib``, which deliberately does not).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qlabs_catalog_sync.identity import BootstrapReport, CatalogObject, IdentityResolver, NaturalKey
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef

SOURCE_ENDPOINT = "databricks"
SOURCE_TENANT = "dbx-account-a"
TARGET_ENDPOINT = "qlik"
TARGET_TENANT = "qlik-acme"


def catalog_object(
    *,
    endpoint: str,
    tenant_id: str,
    native_key: str,
    name: str,
    parent_path: tuple[str, ...] = (),
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    secondary_keys: dict[str, str] | None = None,
) -> CatalogObject:
    return CatalogObject(
        identity=IdentityRef(
            endpoint=endpoint,
            entity_type=entity_type,
            native_key=native_key,
            tenant_id=tenant_id,
            secondary_keys=secondary_keys or {},
        ),
        natural_key=NaturalKey(name=name, entity_type=entity_type, parent_path=parent_path),
    )


def dbx(
    native_key: str,
    name: str,
    *,
    parent_path: tuple[str, ...] = ("main",),
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    tenant_id: str = SOURCE_TENANT,
) -> CatalogObject:
    """A Databricks-side object.

    ``native_key`` is a stable id, not the display name -- RS-03 section 4 ("prefer stable
    machine keys over human names, which can be renamed") and what makes the rename tests
    meaningful. D1: a UC schema's parent path is its catalog.
    """
    return catalog_object(
        endpoint=SOURCE_ENDPOINT,
        tenant_id=tenant_id,
        native_key=native_key,
        name=name,
        parent_path=parent_path,
        entity_type=entity_type,
    )


def qlik(
    native_key: str,
    name: str,
    *,
    parent_path: tuple[str, ...] = ("main",),
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    tenant_id: str = TARGET_TENANT,
    secondary_keys: dict[str, str] | None = None,
) -> CatalogObject:
    """A Qlik-side candidate."""
    return catalog_object(
        endpoint=TARGET_ENDPOINT,
        tenant_id=tenant_id,
        native_key=native_key,
        name=name,
        parent_path=parent_path,
        entity_type=entity_type,
        secondary_keys=secondary_keys,
    )


async def run_bootstrap(
    resolver: IdentityResolver,
    sources: list[CatalogObject],
    candidates: list[CatalogObject],
    **kwargs: Any,
) -> BootstrapReport:
    """``resolver.bootstrap`` with the fixture endpoints filled in."""
    return await resolver.bootstrap(
        source_objects=sources,
        target_candidates=candidates,
        target_endpoint=TARGET_ENDPOINT,
        target_tenant_id=TARGET_TENANT,
        **kwargs,
    )


def read_review_json(path: Path) -> dict[str, Any]:
    """The review file as raw JSON, the way a human's editor sees it."""
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def find_entry(document: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    for proposal in document["proposals"]:
        if proposal["proposal_id"] == proposal_id:
            entry: dict[str, Any] = proposal
            return entry
    raise AssertionError(f"no proposal {proposal_id!r} in the review file")


def edit_review_json(path: Path, proposal_id: str, **fields: Any) -> None:
    """Edit one proposal in the file the way a human would, then write it back."""
    document = read_review_json(path)
    find_entry(document, proposal_id).update(fields)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
