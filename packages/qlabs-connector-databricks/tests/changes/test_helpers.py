"""``derive_tenant_id`` — the fallback tenant boundary when the engine has not set
``ctx.tenant`` (see the orchestrator wiring note in ``changes.py``'s module docstring
and the T4.3 report: this connector's ``__init__.py`` is not owned by this task)."""

from __future__ import annotations

from qlabs_connector_databricks.changes import derive_tenant_id
from qlabs_connector_databricks.config import DatabricksConfig


def test_derive_tenant_id_strips_the_https_scheme() -> None:
    config = DatabricksConfig(
        host="https://acme.cloud.databricks.com",
        client_id="sp-1",
        client_secret="secret",
    )

    assert derive_tenant_id(config) == "acme.cloud.databricks.com"
