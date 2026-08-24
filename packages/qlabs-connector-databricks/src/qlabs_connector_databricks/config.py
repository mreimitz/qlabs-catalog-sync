"""Databricks connector config.

WP4 / T4.1 (Sonnet). The ``ConnectorConfig`` subclass the engine builds and injects
via ``DatabricksConfig.for_endpoint(...)`` (T1.7): workspace host, credentials, and an
optional SQL warehouse id.

**Two credential routes, exactly one of them set.** An OAuth M2M service principal
(``client_id`` + ``client_secret``) is the documented default and what Databricks itself
recommends. A personal access token (``token``) is the alternative, because a PAT is one
click in the workspace UI where a service principal needs a workspace admin, and an
operator standing an endpoint up for the first time should not need the admin before
anything works. Databricks documents PATs as legacy; this connector offers both and
prefers neither at runtime -- configuring both is a validation error rather than a silent
precedence rule, so a leftover ``client_secret`` can never quietly override a PAT
somebody just added, or the reverse.

Downstream, the choice is invisible: ``auth.build_auth_provider`` turns either into a
bearer token, and the ``WorkspaceClient`` this connector builds has always been a
bearer-token (``auth_type="pat"``) client.

``sql_warehouse_id`` is deliberately optional and its presence is load-bearing (RM-01
decision D6): Unity Catalog object tags are readable only through
``INFORMATION_SCHEMA.*_TAGS`` over the Statement Execution API, which needs a SQL
warehouse to run against. T4.2's capability manifest declares ``tags`` supported only
when :attr:`DatabricksConfig.has_sql_warehouse` is true, and ``na`` otherwise — this
module's job is only to make that presence/absence unambiguous, not to act on it.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator, model_validator

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
    """Workspace host, one of two credential routes, and an optional SQL warehouse.

    Loaded via :meth:`~qlabs_catalog_sync_sdk.config.ConnectorConfig.for_endpoint`, so a
    deployment configures this through ``<ENDPOINT>__HOST``, then either
    ``<ENDPOINT>__CLIENT_ID`` + ``<ENDPOINT>__CLIENT_SECRET`` or ``<ENDPOINT>__TOKEN``,
    and optionally ``<ENDPOINT>__SQL_WAREHOUSE_ID`` — never read from ``os.environ``
    directly by connector code.
    """

    # A cross-field check reports the *model's* input, which is the whole settings dict --
    # raw secret values included, before SecretStr ever wraps them. Field-level errors
    # report only their own field and never had this problem, so it arrived with
    # _exactly_one_credential_route below (tests/auth/test_pat.py pins it). Hiding inputs
    # on this model rather than rewording one validator makes it structural: no future
    # validator here can echo a credential either.
    model_config = ConnectorConfig.model_config | {"hide_input_in_errors": True}

    host: str = Field(
        description=(
            "The workspace host, e.g. 'https://adb-1234567890123456.7.azuredatabricks.net'. "
            "Do not append '/api' — REST paths are joined onto this as-is."
        )
    )
    client_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The OAuth M2M service principal's application (client) id. Not a secret. "
            "Set this with 'client_secret', or set 'token' instead -- not both."
        ),
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description=(
            "The OAuth M2M service principal's client secret. Set this with 'client_id', "
            "or set 'token' instead -- not both."
        ),
    )
    token: SecretStr | None = Field(
        default=None,
        description=(
            "A Databricks personal access token, sent as 'Authorization: Bearer <token>'. "
            "The alternative to the 'client_id'/'client_secret' service principal -- set "
            "one route or the other, not both. Easier to obtain (one click in the "
            "workspace UI, no admin needed); Databricks documents PATs as legacy and "
            "recommends the service principal for anything long-lived."
        ),
    )
    sql_warehouse_id: str | None = Field(
        default=None,
        description=(
            "A SQL warehouse id used to read Unity Catalog tags via "
            "INFORMATION_SCHEMA.*_TAGS over the Statement Execution API (decision D6). "
            "Omit to leave tag reads declared 'na' in the capability manifest."
        ),
    )

    catalog_schema_patterns: list[str] = Field(
        default_factory=lambda: ["*.*"],
        description=(
            "Endpoint-level allow-list of `catalog.schema` glob patterns this connector "
            "may read at all. Defaults to everything the service principal can see; the "
            "per-pair selector (SyncPairConfig.catalog_schema_patterns, decision D1) is "
            "applied by the engine on top of this and is the one an operator normally "
            "edits. Present here because Connector.read(ref) receives only a ref."
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

    @model_validator(mode="after")
    def _exactly_one_credential_route(self) -> DatabricksConfig:
        """Require the OAuth pair or the token, never both and never neither.

        Deliberately *after* field validation and deliberately silent about values: the
        message names the fields to set, and a rejected config is the single most likely
        place a secret gets printed, because the error goes straight to the operator's
        screen and pydantic quotes input values by default.
        """
        has_oauth = self.client_id is not None or self.client_secret is not None
        has_token = self.token is not None

        if has_oauth and has_token:
            raise ValueError(
                "configure exactly one credential route: either 'client_id' + "
                "'client_secret' (OAuth M2M service principal) or 'token' (personal "
                "access token) -- both are set"
            )
        if not has_oauth and not has_token:
            raise ValueError(
                "configure exactly one credential route: either 'client_id' + "
                "'client_secret' (OAuth M2M service principal) or 'token' (personal "
                "access token) -- neither is set"
            )
        if has_oauth and (self.client_id is None or self.client_secret is None):
            missing = "client_secret" if self.client_id is not None else "client_id"
            raise ValueError(
                f"the OAuth M2M service principal needs both 'client_id' and "
                f"'client_secret'; {missing!r} is missing (or set 'token' instead)"
            )
        return self

    @property
    def uses_personal_access_token(self) -> bool:
        """True when this endpoint authenticates with a PAT rather than a service
        principal. The connector logs this once at setup so a deployment can see which
        route an endpoint actually took."""
        return self.token is not None

    @property
    def has_sql_warehouse(self) -> bool:
        """True when a SQL warehouse is configured for the SQL-gated tag read path."""
        return self.sql_warehouse_id is not None

    @property
    def token_url(self) -> str:
        """The full OAuth M2M token endpoint URL for this workspace.

        Meaningful only on the service-principal route; the PAT route never calls it.
        """
        return f"{self.host}{OIDC_TOKEN_PATH}"
