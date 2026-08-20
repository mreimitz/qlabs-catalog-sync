"""Engine config & secrets loading.

WP2 / T2.3 (Sonnet). Loads tenants/endpoints and secrets via pydantic-settings with
pluggable secret backends, and binds validated per-connector config. Builds on the
SDK config base (T1.7).

TODO(T2.3): implement engine settings and secret-backend wiring.
"""
