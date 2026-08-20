"""Entry-point discovery + connector registry + SDK version gate.

WP2 / T2.1 (Sonnet). Discovers installed connectors via the
``qlabs_catalog_sync.connectors`` entry-point group, builds the registry, and gates
each connector against the SDK contract version. The engine never imports a connector
module directly.

TODO(T2.1): implement entry-point discovery, registry, and version gate.
"""
