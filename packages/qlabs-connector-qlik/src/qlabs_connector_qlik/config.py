"""Qlik connector configuration.

WP3 / T3.1. :class:`QlikConfig` declares what one configured Qlik tenant endpoint needs:
the tenant base URL, OAuth2 machine-to-machine client-credentials, and the target Qlik
space that receives synced data products. The connector never creates Qlik datasets
(decision D2 in ``decision-databricks-to-qlik-mvp.md``) — it only resolves them, and
creates data products, inside this one space — so ``space_id`` is the only "where do
writes land" field this config needs; there is no Databricks-shaped field here.

Subclasses the SDK's ``ConnectorConfig`` (T1.7) rather than reading the environment
directly: the engine builds and validates one instance per configured endpoint via
``QlikConfig.for_endpoint("<endpoint-key>", ...)``, which reads ``<ENDPOINT>__<FIELD>``
environment variables (e.g. an endpoint key of ``"qlik"`` reads ``QLIK__CLIENT_SECRET``).
``client_secret`` is a pydantic ``SecretStr`` so it is redacted from ``repr()``/``str()``/
``model_dump()`` by construction; it is unwrapped exactly once, in ``auth.py``, to hand
the raw value to the SDK's OAuth2 provider.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator

from qlabs_catalog_sync_sdk.config import ConnectorConfig

__all__ = ["TOKEN_PATH", "QlikConfig"]

#: Qlik Cloud's OAuth2 client-credentials token endpoint, relative to the tenant base URL
#: (RS-02 qlik-catalog-api-reference.md section 3.2). Every Qlik Cloud tenant exposes it
#: at this fixed path, so it is not configurable.
TOKEN_PATH = "/oauth/token"


class QlikConfig(ConnectorConfig):
    """Configuration for one Qlik Cloud tenant endpoint.

    ``base_url`` is the tenant's own base, e.g. ``https://acme.eu.qlikcloud.com`` — never
    include ``/api/v1``, ``/api/data-governance`` or ``/oauth/token``; the connector
    appends those itself. OAuth2 client-credentials (machine-to-machine) is the only auth
    method T3.1 wires up: RS-02 section 3.2 recommends it over long-lived API keys for
    backend integrations, and it is what lets the connector run unattended.
    """

    base_url: str = Field(min_length=1)
    """The tenant base, e.g. ``https://acme.eu.qlikcloud.com`` (no trailing slash, no
    path segment)."""

    client_id: str = Field(min_length=1)
    """The OAuth2 client-credentials client id (``/api/v1/oauth-clients``)."""

    client_secret: SecretStr
    """The OAuth2 client-credentials client secret. Never logged, never rendered in
    ``repr()``/``str()``/``model_dump()`` — see the module docstring."""

    scope: str | None = "user_default"
    """Space-delimited OAuth2 scope(s) sent with the token request. Qlik grants a client
    no access at all with no scope (RS-02 two-way-sync-readiness notes section 5), so
    this defaults to ``user_default`` (act-as-the-user) rather than ``None``."""

    space_id: str = Field(min_length=1)
    """The target Qlik space: data products are created in it and dataset references are
    resolved against datasets already present in it (decision D2 — the connector never
    creates Qlik datasets). Deliberately the only "where do writes land" field; this is a
    Qlik concept; it has no Databricks-shaped equivalent and none belongs here."""

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        stripped = value.rstrip("/")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an absolute http(s) URL, got {value!r}")
        return stripped

    @property
    def token_url(self) -> str:
        """The tenant's OAuth2 token endpoint, derived from :attr:`base_url`."""
        return f"{self.base_url}{TOKEN_PATH}"
