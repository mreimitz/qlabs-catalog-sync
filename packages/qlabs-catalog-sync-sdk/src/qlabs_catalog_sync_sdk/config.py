"""Config base and connector context.

WP1 / T1.7 (Sonnet). Provides the pydantic-settings ``ConnectorConfig`` base that
connectors subclass to declare their config and secrets, plus ``ConnectorContext``
(bound logger, validated config, HTTP endpoint) injected by the engine. Connectors
must not read the environment directly.

TODO(T1.7): implement ConnectorConfig and ConnectorContext.
"""
