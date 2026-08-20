"""Qlik write path — create / update / delete (sole write target).

WP3 / T3.4, T3.5, T3.7 (Opus/Sonnet). create() data products + datasets/items
(neutral -> Qlik payloads, T3.4); update() via JSON Patch replace-only with a closed
path enum, full-replace arrays, ETag if-match, and max-8-ops batching (T3.5);
delete()/lifecycle actions such as deactivate/move (T3.7). See the RS-02
sync-readiness note.

TODO(T3.4/T3.5/T3.7): implement create, JSON-Patch update, and delete/lifecycle.
"""
