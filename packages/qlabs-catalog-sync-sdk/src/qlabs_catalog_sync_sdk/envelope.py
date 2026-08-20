"""Field envelope and canonical checksum utilities.

WP1 / T1.6 (Opus). Deterministic normalization and stable checksums over field
envelopes so the engine can diff current vs desired state reproducibly. Correctness
here underpins idempotent, minimal-mutation writes.

TODO(T1.6): implement canonical normalization + checksum utilities.
"""
