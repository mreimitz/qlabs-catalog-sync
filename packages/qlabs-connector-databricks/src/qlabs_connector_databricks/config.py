"""Databricks connector config.

WP4 / T4.1 (Sonnet). The ``ConnectorConfig`` subclass the engine builds and injects
via ``DatabricksConfig.for_endpoint(...)`` (T1.7): workspace host, OAuth M2M
service-principal credentials, and an optional SQL warehouse id.

``sql_warehouse_id`` is deliberately optional and its presence is load-bearing (RM-01
decision D6): Unity Catalog object tags are readable only through
``INFORMATION_SCHEMA.*_TAGS`` over the Statement Execution API, which needs a SQL
warehouse to run against. T4.2's capability manifest declares ``tags`` supported only
when :attr:`DatabricksConfig.has_sql_warehouse` is true, and ``na`` otherwise — this
module's job is only to make that presence/absence unambiguous, not to act on it.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator

from qlabs_catalog_sync_sdk.config import ConnectorConfig

__all__ = ["DatabricksConfig"]

#: The path of the Databricks OIDC token endpoint, relative to the workspace host. See
#: ``planning/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md``
#: section 3.2: ``POST https://<workspace-host>/oidc/v1/token``.
OIDC_TOKEN_PATH = "/oidc/v1/token"

#: The OAuth scope requested for the client-credentials grant. Unity Catalog REST and
#: the Statement Execution API are both reachable under the blanket ``all-apis`` scope;
#: Databricks does not offer a narrower scope for this combination.
OAUTH_SCOPE = "all-apis"


class DatabricksConfig(ConnectorConfig):
    """Workspace host, OAuth M2M service-principal credentials, and an optional
    SQL warehouse.

    Loaded via :meth:`~qlabs_catalog_sync_sdk.config.ConnectorConfig.for_endpoint`, so a
    deployment configures this through ``<ENDPOINT>__HOST``, ``<ENDPOINT>__CLIENT_ID``,
    ``<ENDPOINT>__CLIENT_SECRET`` and, optionally, ``<ENDPOINT>__SQL_WAREHOUSE_ID`` —
    never read from ``os.environ`` directly by connector code.
    """

    host: str = Field(
        description=(
            "The workspace host, e.g. 'https://adb-1234567890123456.7.azuredatabricks.net'. "
            "Do not append '/api' — REST paths are joined onto this as-is."
        )
    )
    client_id: str = Field(
        min_length=1,
        description="The OAuth M2M service principal's application (client) id. Not a secret.",
    )
    client_secret: SecretStr = Field(description="The OAuth M2M service principal's client secret.")
    sql_warehouse_id: str | None = Field(
        default=None,
        description=(
            "A SQL warehouse id used to read Unity Catalog tags via "
            "INFORMATION_SCHEMA.*_TAGS over the Statement Execution API (decision D6). "
            "Omit to leave tag reads declared 'na' in the capability manifest."
        ),
    )

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host must not be empty")
        if not value.startswith("https://"):
            raise ValueError(
                f"host must be an https:// workspace URL (got {value!r}); "
                "do not append '/api' to it"
            )
        return value.rstrip("/")

    @field_validator("sql_warehouse_id")
    @classmethod
    def _validate_sql_warehouse_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("sql_warehouse_id must not be blank; omit the field instead")
        return value

    @property
    def has_sql_warehouse(self) -> bool:
        """True when a SQL warehouse is configured for the SQL-gated tag read path."""
        return self.sql_warehouse_id is not None

    @property
    def token_url(self) -> str:
        """The full OAuth M2M token endpoint URL for this workspace."""
        return f"{self.host}{OIDC_TOKEN_PATH}"
