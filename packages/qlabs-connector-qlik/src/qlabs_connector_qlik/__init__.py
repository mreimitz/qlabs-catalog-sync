"""QLabs Catalog Sync — Qlik connector (WP3).

The sole WRITE connector in v1: metadata flows from source catalogs into Qlik, and
Qlik is the only write target. Depends only on the SDK plus httpx. Registered via the
``qlabs_catalog_sync.connectors`` entry-point group as ``qlik``.
"""


class Connector:
    """Placeholder Qlik connector.

    WP3 / T3.x. Resolvable target for the ``qlik`` entry point. It will subclass the
    SDK ``Connector`` ABC (from ``qlabs_catalog_sync_sdk``) and implement full CRUD
    into Qlik: auth (T3.1), capability manifest (T3.2), read (T3.3), create (T3.4),
    JSON-Patch update (T3.5), glossary (T3.6), and delete/lifecycle (T3.7).
    """

    name = "qlik"
    # TODO(T3.x): subclass qlabs_catalog_sync_sdk.Connector and implement CRUD.
