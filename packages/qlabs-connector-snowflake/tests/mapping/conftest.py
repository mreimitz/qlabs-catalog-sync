"""Shared fixtures for the Snowflake connector's mapping tests (T6.5).

Independent of ``tests/read/conftest.py`` (T6.4's own fixtures) on purpose, exactly as the
Databricks connector keeps its two suites apart: this suite tests ``mapping.py`` in
isolation against raw rows it builds itself, so it never depends on -- and can never
accidentally couple to -- ``read.py``'s statement plumbing or its private helpers.

The rows here are shaped the way each Snowflake surface actually returns them (RS-05
sections 1.4, 3.4, 3.6): ``INFORMATION_SCHEMA`` columns **upper-cased**,
``SHOW``/``DESCRIBE`` columns **lower-cased**. That split is not incidental -- it is the
thing ``mapping.lookup``'s case-insensitive matching exists for, so the fixtures preserve
it rather than normalizing it away.
"""

from __future__ import annotations

from typing import Any

from qlabs_connector_snowflake.mapping import TagReference

ENDPOINT = "snowflake"
TENANT_ID = "ACME-PRIMARY"


def make_raw_table(**overrides: Any) -> dict[str, Any]:
    """A realistic ``INFORMATION_SCHEMA.TABLES`` row (upper-cased columns)."""
    row: dict[str, Any] = {
        "TABLE_CATALOG": "SALES_DB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": "ORDERS",
        "TABLE_OWNER": "SALES_ENGINEER",
        "TABLE_TYPE": "BASE TABLE",
        "IS_TRANSIENT": "NO",
        "COMMENT": "Order header rows, one per checkout.",
        "CREATED": "1700000000.000000000",
        "LAST_ALTERED": "1700003600.000000000",
    }
    row.update(overrides)
    return row


def make_raw_view(**overrides: Any) -> dict[str, Any]:
    """A realistic ``INFORMATION_SCHEMA.VIEWS`` row -- note: no ``TABLE_TYPE`` column."""
    row: dict[str, Any] = {
        "TABLE_CATALOG": "SALES_DB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": "ORDERS_EU",
        "TABLE_OWNER": "SALES_ENGINEER",
        "IS_SECURE": "YES",
        "COMMENT": "EU-only projection of ORDERS.",
        "CREATED": "1700000000.000000000",
        "LAST_ALTERED": "1700003600.000000000",
    }
    row.update(overrides)
    return row


def make_raw_schema(**overrides: Any) -> dict[str, Any]:
    """A realistic ``INFORMATION_SCHEMA.SCHEMATA`` row."""
    row: dict[str, Any] = {
        "CATALOG_NAME": "SALES_DB",
        "SCHEMA_NAME": "PUBLIC",
        "SCHEMA_OWNER": "SYSADMIN",
        "IS_TRANSIENT": "NO",
        "IS_MANAGED_ACCESS": "NO",
        "COMMENT": "Conformed sales dimensions and facts.",
        "CREATED": "1700000000.000000000",
        "LAST_ALTERED": "1700003600.000000000",
    }
    row.update(overrides)
    return row


def make_raw_listing(**overrides: Any) -> dict[str, Any]:
    """A merged ``SHOW LISTINGS`` + ``DESCRIBE LISTING`` row (lower-cased columns).

    Carries the whole RS-05 section 2.2 metadata set -- ``categories``,
    ``business_needs``, ``data_attributes``, ``compliance_badges``, ``data_dictionary`` --
    because none of them has a neutral field and all of them must round-trip through
    ``custom_attributes``. ``targets`` marks this as a V1 listing (RS-05 section 2.3).
    """
    row: dict[str, Any] = {
        "global_name": "GZTSZAS2KH9",
        "name": "SALES_DAILY",
        "title": "Daily sales",
        "subtitle": "Daily sales by region, refreshed nightly",
        "description": "# Daily sales\n\nSales fact tables refreshed **daily**.",
        "owner": "SALES_PROVIDER",
        "comment": "Managed by the commercial analytics team.",
        "share": "SALES_S",
        "state": "PUBLISHED",
        "review_state": "APPROVED",
        "categories": ["BUSINESS"],
        "business_needs": [{"name": "Revenue reporting"}],
        "data_attributes": {"refresh_rate": "DAILY", "geography": ["EU"]},
        "compliance_badges": ["GDPR"],
        "data_dictionary": {
            "featured": {
                "database": "SALES_DB",
                "objects": [{"name": "ORDERS", "schema": "PUBLIC", "domain": "TABLE"}],
            }
        },
        "targets": {"accounts": ["Org1.Account1"]},
        "created_on": "1700000000.000000000",
        "updated_on": "1700003600.000000000",
    }
    row.update(overrides)
    return row


def user_tag(
    name: str = "COST_CENTER",
    value: str | None = "commerce",
    *,
    database: str = "GOVERNANCE",
    schema: str = "TAGS",
    column_name: str | None = None,
) -> TagReference:
    """One user-defined tag assignment (RS-05 section 3.4)."""
    return TagReference(
        tag_database=database,
        tag_schema=schema,
        tag_name=name,
        tag_value=value,
        object_database="SALES_DB",
        object_schema="PUBLIC",
        object_name="ORDERS",
        column_name=column_name,
        domain="TABLE",
    )


def system_tag(
    name: str = "PRIVACY_CATEGORY",
    value: str | None = "IDENTIFIER",
    *,
    column_name: str | None = "EMAIL",
) -> TagReference:
    """One ``SNOWFLAKE.CORE`` classification tag (RS-05 sections 1.3/4.2)."""
    return TagReference(
        tag_database="SNOWFLAKE",
        tag_schema="CORE",
        tag_name=name,
        tag_value=value,
        object_database="SALES_DB",
        object_schema="PUBLIC",
        object_name="ORDERS",
        column_name=column_name,
        domain="TABLE",
    )
