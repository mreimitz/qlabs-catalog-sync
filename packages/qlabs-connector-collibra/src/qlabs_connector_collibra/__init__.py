"""QLabs Catalog Sync — Collibra connector (WP5).

Read-only source connector and the clean glossary source for Qlik: reads data products
and business terms (with relations preserved). It never writes. Depends only on the SDK
plus httpx. Registered as ``collibra`` under the ``qlabs_catalog_sync.connectors``
entry-point group.
"""


class Connector:
    """Placeholder Collibra connector.

    WP5 / T5.x. Resolvable target for the ``collibra`` entry point. It will subclass the
    SDK ``Connector`` ABC and implement read paths only: auth (T5.1), read-only manifest
    (T5.2), list_changed (T5.3), read (T5.4), and relation-graph source-to-neutral
    mapping (T5.5). Write methods stay unimplemented.
    """

    name = "collibra"
    # TODO(T5.x): subclass qlabs_catalog_sync_sdk.Connector; read-only paths only.
