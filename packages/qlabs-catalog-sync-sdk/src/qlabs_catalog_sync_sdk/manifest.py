"""Capability manifest types.

WP1 / T1.3 (Sonnet). Defines CapabilityManifest, EntityCapability, and
FieldCapability (per-field ``mode`` of rw/ro/na), plus ``partial_update`` and the
connector ``concurrency`` model (etag/revision/none). The engine plans strictly
from the manifest, so connectors must declare capabilities honestly.

TODO(T1.3): define the manifest types.
"""
