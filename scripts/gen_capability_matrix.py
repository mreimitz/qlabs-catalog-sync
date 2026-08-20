#!/usr/bin/env python3
"""Generate ``docs/capability-matrix.json`` from the connectors' live capability manifests.

WP9 / T9.4. RS-08 section 10 states the intent directly: "Connector docs are generated
from the ``CapabilityManifest``, so the capability matrix per catalog is always accurate
and published." A hand-written matrix drifts from the manifest the moment someone edits
a ``FieldCapability`` in ``manifest.py`` and forgets the doc; this script makes that
impossible by construction — the committed JSON *is* the manifest, serialized, and
nothing else populates it.

Sources, both called live, never re-typed:

* ``qlabs_connector_qlik.manifest.qlik_capability_manifest()`` — the sole v1 write
  connector. Its manifest never varies with config (see that function's own docstring),
  so it is represented once.
* ``qlabs_connector_databricks.manifest.manifest_for_config(...)`` — a v1 source
  connector whose manifest genuinely varies with runtime config (decision D6): ``tags``
  (and, through the same tag surface, ``classifications``) are declared ``ro`` only when
  a SQL warehouse is configured, ``na`` otherwise. Absence of a warehouse is a real,
  supported choice — "not read at all", not "read as empty" — so this script builds
  *both* config shapes (``sql_warehouse_configured`` true and false) and represents both
  under the ``databricks`` connector rather than picking one, which would silently hide
  the other from every reader of the committed file.

Usage::

    uv run python scripts/gen_capability_matrix.py            # write docs/capability-matrix.json
    uv run python scripts/gen_capability_matrix.py --check     # exit 1 if the file is stale
    uv run python scripts/gen_capability_matrix.py --out PATH  # write somewhere else

``--check`` is the CI gate: it regenerates the matrix in memory and diffs it against
what is on disk without touching the file, so a manifest edited without regenerating
the committed JSON fails the build instead of silently drifting.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, EntityCapability, FieldCapability
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_connector_databricks.config import DatabricksConfig
from qlabs_connector_databricks.manifest import manifest_for_config
from qlabs_connector_qlik.manifest import qlik_capability_manifest

#: Default location of the generated matrix, relative to the repository root.
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "capability-matrix.json"

#: A minimally valid ``DatabricksConfig`` needs these three fields regardless of
#: ``sql_warehouse_id`` — see ``qlabs_connector_databricks.config.DatabricksConfig`` and
#: the equivalent ``build_config`` helper in that package's own manifest tests (same
#: ``dict[str, Any]`` typing, for the same reason: pydantic coerces a plain ``str`` into
#: ``client_secret``'s ``SecretStr`` at validation time, which a narrower static type
#: for this dict cannot express). The values themselves are never used for anything but
#: constructing the config object.
_DATABRICKS_CONFIG_BASE: dict[str, Any] = {
    "host": "https://example.cloud.databricks.com",
    "client_id": "capability-matrix-generator",
    "client_secret": "unused",
}


def _field_to_json(field: FieldCapability) -> dict[str, Any]:
    """One :class:`FieldCapability` as a plain, stable JSON object."""
    return {
        "mode": field.mode.value,
        "writable_via": field.writable_via,
        "partial_update": field.partial_update,
        "normalized_by_target": field.normalized_by_target,
    }


def _entity_to_json(entity: EntityCapability) -> dict[str, Any]:
    """One :class:`EntityCapability` as a plain, stable JSON object.

    Fields are sorted by name so the committed file's diffs are field-level, not an
    artifact of whatever order a manifest happened to declare its ``fields`` dict in.
    """
    return {
        "supported": entity.supported,
        "identity_keys": list(entity.identity_keys),
        "supports_events": entity.supports_events,
        "allowed_update_paths": entity.allowed_update_paths,
        "max_update_operations": entity.max_update_operations,
        "fields": {
            name: _field_to_json(field) for name, field in sorted(entity.fields.items())
        },
    }


def _manifest_to_json(manifest: CapabilityManifest) -> dict[str, Any]:
    """One :class:`CapabilityManifest` as a plain JSON object.

    Entities are emitted in :class:`EntityType`'s own declaration order (data product,
    dataset, glossary term, category) rather than dict-insertion order, so the shape is
    identical regardless of how a connector's ``manifest.py`` built the ``entities``
    dict.
    """
    return {
        "concurrency": manifest.concurrency.value,
        "entities": {
            entity_type.value: _entity_to_json(manifest.entities[entity_type])
            for entity_type in EntityType
            if entity_type in manifest.entities
        },
    }


def build_matrix() -> dict[str, Any]:
    """Build the whole capability matrix from the two connectors' live manifests.

    Every value below comes from calling the real manifest-building function — nothing
    here is a copy of a field name or a capability mode typed by hand.
    """
    qlik_manifest = qlik_capability_manifest()

    databricks_with_warehouse = manifest_for_config(
        DatabricksConfig(**_DATABRICKS_CONFIG_BASE, sql_warehouse_id="capability-matrix-probe")
    )
    databricks_without_warehouse = manifest_for_config(
        DatabricksConfig(**_DATABRICKS_CONFIG_BASE, sql_warehouse_id=None)
    )

    return {
        "$schema_note": (
            "Generated by scripts/gen_capability_matrix.py from the live "
            "qlabs_connector_qlik.manifest.qlik_capability_manifest() and "
            "qlabs_connector_databricks.manifest.manifest_for_config() calls. "
            "Do not hand-edit -- re-run the script instead; `--check` fails CI on drift."
        ),
        "connectors": {
            "qlik": {
                "role": "write (the only v1 write target)",
                "config_dependent": False,
                "manifest": _manifest_to_json(qlik_manifest),
            },
            "databricks": {
                "role": "read (v1 source)",
                "config_dependent": True,
                "config_dependent_note": (
                    "decision D6: tags (and, through the same tag surface, "
                    "classifications) are readable ('ro') only when the endpoint has a "
                    "SQL warehouse configured (DATABRICKS__SQL_WAREHOUSE_ID / "
                    "sql_warehouse_id). Absent, they are 'na' -- not read at all, not "
                    "read as empty. Both shapes are represented below; pick the one "
                    "that matches a given endpoint's actual config."
                ),
                "variants": {
                    "sql_warehouse_configured": _manifest_to_json(databricks_with_warehouse),
                    "no_sql_warehouse": _manifest_to_json(databricks_without_warehouse),
                },
            },
        },
    }


def render(matrix: dict[str, Any]) -> str:
    """The matrix as the exact bytes this script writes/compares — stable, sorted keys."""
    return json.dumps(matrix, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gen_capability_matrix.py",
        description=(
            "Generate docs/capability-matrix.json from the live Qlik and Databricks "
            "capability manifests. Use --check in CI to fail on drift."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the matrix (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write anything. Exit 1 (with a diff-shaped message) if the "
            "regenerated matrix differs from what is already at --out, exit 0 if it "
            "matches exactly."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    rendered = render(build_matrix())

    if args.check:
        if not args.out.exists():
            print(f"[gen_capability_matrix] {args.out} does not exist -- run without --check.")
            return 1
        on_disk = args.out.read_text(encoding="utf-8")
        if on_disk == rendered:
            print(f"[gen_capability_matrix] {args.out} matches the live manifests.")
            return 0
        print(
            f"[gen_capability_matrix] {args.out} is STALE -- it does not match what the "
            "live qlik/databricks manifests generate right now. Run "
            "`uv run python scripts/gen_capability_matrix.py` (no --check) to regenerate "
            "it, then commit the result.",
            file=sys.stderr,
        )
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"[gen_capability_matrix] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
