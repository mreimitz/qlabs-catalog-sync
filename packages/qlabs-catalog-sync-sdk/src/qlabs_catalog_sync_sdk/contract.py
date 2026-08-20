"""Connector contract — the abstract base class every connector implements.

WP1 / T1.2 (Opus, foundational). Defines the ``Connector`` ABC and its surface:
``capabilities`` / ``setup`` / ``healthcheck`` / ``list_changed`` / ``read`` /
``create`` / ``update`` / ``delete``, plus the supporting types EntityType,
Watermark, ChangeRef, WriteResult, HealthStatus. See the RS-08 SDK spec.

TODO(T1.2): define the Connector ABC and supporting contract types.
"""
