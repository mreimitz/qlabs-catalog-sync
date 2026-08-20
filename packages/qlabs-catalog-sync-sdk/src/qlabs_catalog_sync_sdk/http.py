"""HTTP helper — httpx wrapper with retry/backoff and pagination.

WP1 / T1.4 (Sonnet). Provides an httpx-based endpoint wrapper (base URL, auth
injection, timeouts, connection pooling), tenacity-driven retry/backoff that honors
429/Retry-After, and pagination helpers. Connectors use this instead of calling
httpx directly.

TODO(T1.4): implement the HttpEndpoint wrapper and retry/pagination helpers.
"""
