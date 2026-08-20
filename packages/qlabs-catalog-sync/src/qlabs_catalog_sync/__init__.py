"""QLabs Catalog Sync engine.

WP2 & WP7 (`qlabs-catalog-sync`). The long-running service that discovers connectors
at runtime, polls source catalogs, resolves identity, diffs against target state, and
writes to Qlik (the sole write target in v1). Depends only on the SDK; it never
imports a connector directly (connectors are found via the
``qlabs_catalog_sync.connectors`` entry-point group).
"""

__all__: list[str] = []
