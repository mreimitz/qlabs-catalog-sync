"""Typed SDK exceptions.

WP1 / T1.7 (Sonnet). The shared exception hierarchy connectors raise so the engine
reacts uniformly (retry vs skip vs fail): TransientError, AuthError, NotFound,
ConflictError, CapabilityError. Connectors must not invent their own hierarchies.

TODO(T1.7): define the typed exception classes.
"""
