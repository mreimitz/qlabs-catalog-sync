"""QLabs Catalog Sync SDK — the public contract for the sync engine and connectors.

WP1 (`qlabs-catalog-sync-sdk`). This package is the single public surface: it defines
the neutral metadata model, the connector contract (ABC), the capability manifest,
shared HTTP/auth helpers, the field envelope/checksum utilities, config/exception
types, and the conformance kit. Connectors and the engine import everything from here;
the SDK depends on neither the engine nor any connector.

The full public surface (model types, ``Connector`` ABC, ``CapabilityManifest``, etc.)
is re-exported here as the individual modules are implemented in WP1
(T1.1-T1.9). See the RS-08 connector SDK spec.
"""

# Contract version constant + compatibility gate (T1.9). Bump on breaking changes.
CONTRACT_VERSION = "0.1.0"

# Entry-point group connectors register under; the engine discovers via this group (T1.9).
CONNECTOR_ENTRY_POINT_GROUP = "qlabs_catalog_sync.connectors"

# TODO(T1.1-T1.9): re-export the public surface (neutral model, Connector ABC,
# CapabilityManifest, HttpEndpoint, ConnectorConfig, ConnectorContext, exceptions)
# from the sibling modules as they are implemented.
__all__ = ["CONTRACT_VERSION", "CONNECTOR_ENTRY_POINT_GROUP"]
