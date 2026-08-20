"""QLabs Catalog Sync — Snowflake connector (WP6).

Read-only source connector: reads objects plus listings/shares into neutral entities.
It never writes. Depends only on the SDK plus ``snowflake-connector-python``.
Registered as ``snowflake`` under the ``qlabs_catalog_sync.connectors`` entry-point
group.
"""


class Connector:
    """Placeholder Snowflake connector.

    WP6 / T6.x. Resolvable target for the ``snowflake`` entry point. It will subclass
    the SDK ``Connector`` ABC and implement read paths only: auth (T6.1), read-only
    manifest (T6.2), list_changed (T6.3), read (T6.4), and source-to-neutral mapping
    (T6.5). Write methods stay unimplemented.
    """

    name = "snowflake"
    # TODO(T6.x): subclass qlabs_catalog_sync_sdk.Connector; read-only paths only.
