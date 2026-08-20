"""Auth providers base.

WP1 / T1.5 (Sonnet). Base auth providers for API key, OAuth2 machine-to-machine,
and JWT/key-pair, plus an in-memory token cache with refresh. Connectors select
and configure a provider; secrets never get logged or persisted.

TODO(T1.5): implement the auth provider base classes and token cache.
"""
