"""Field diff engine.

WP7 / T7.2 (Opus). Computes minimal-mutation diffs, choosing full-replace vs
partial-patch per the target connector's capability manifest. Builds on the SDK
envelope/checksum utilities (T1.6) and feeds the sync loop (T2.4).

TODO(T7.2): implement the manifest-aware field diff engine.
"""
