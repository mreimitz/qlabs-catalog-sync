"""Snowflake read path.

WP6 / T6.3, T6.4 (Sonnet). list_changed via SHOW / INFORMATION_SCHEMA / ACCOUNT_USAGE
(note the ~2h ACCOUNT_USAGE lag) (T6.3); read() objects plus listings/shares into
neutral entities (T6.4).

TODO(T6.3/T6.4): implement list_changed and read().
"""
