"""Scheduler.

WP2 / T2.6 (Sonnet). APScheduler 3.x AsyncIOScheduler (or a plain asyncio loop)
driving per-source cadence with jitter and ``max_instances=1`` so a source never runs
concurrently with itself.

TODO(T2.6): implement the async scheduler.
"""
