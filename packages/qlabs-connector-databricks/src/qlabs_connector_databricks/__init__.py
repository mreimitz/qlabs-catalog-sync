"""QLabs Catalog Sync — Databricks connector (WP4).

Read-only source connector: reads Unity Catalog objects plus shares/listings into
neutral entities. It never writes (Qlik is the sole write target in v1). Depends only
on the SDK plus ``databricks-sdk``. Registered as ``databricks`` under the
``qlabs_catalog_sync.connectors`` entry-point group.
"""


class Connector:
    """Placeholder Databricks connector.

    WP4 / T4.x. Resolvable target for the ``databricks`` entry point. It will subclass
    the SDK ``Connector`` ABC and implement read paths only: auth (T4.1), read-only
    capability manifest (T4.2), list_changed (T4.3), read (T4.4), and source-to-neutral
    mapping (T4.5). Write methods stay unimplemented; writable fields are declared
    ro/na in the manifest.
    """

    name = "databricks"
    # TODO(T4.x): subclass qlabs_catalog_sync_sdk.Connector; read-only paths only.
