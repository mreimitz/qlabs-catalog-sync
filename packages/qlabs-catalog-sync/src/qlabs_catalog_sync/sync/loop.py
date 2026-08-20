"""Upstream sync loop.

WP2 / T2.4 (Opus). The core cycle: poll a source -> read into neutral envelopes ->
resolve identity -> checksum diff -> write to Qlik -> persist -> advance watermark,
as one transaction, with idempotent skip on a no-op re-run.

TODO(T2.4): implement the transactional upstream sync loop.
"""
