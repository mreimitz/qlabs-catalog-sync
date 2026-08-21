"""The ACCOUNT_USAGE lag knobs are operator-tunable, and the two places they live agree.

``read.py`` carries the defaults as module constants (it is where the arithmetic lives);
``SnowflakeConfig`` carries them as fields (it is what an operator edits). ``auth.py``
cannot import ``read.py`` -- ``read.py`` imports ``auth.py`` -- so the two sets of
defaults are written out separately, and this file is what stops them drifting apart.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from qlabs_connector_snowflake.auth import SnowflakeConfig
from qlabs_connector_snowflake.read import (
    ACCOUNT_USAGE_LAG,
    DEFAULT_RESCAN_OVERLAP,
    DEFAULT_WATERMARK_SAFETY_MARGIN,
)


def _config(**overrides: object) -> SnowflakeConfig:
    return SnowflakeConfig(
        organization="acme",
        account="prod",
        user="sync_svc",
        private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
        **overrides,  # type: ignore[arg-type]
    )


def test_the_config_defaults_match_the_read_module_constants() -> None:
    config = _config()

    assert timedelta(seconds=config.account_usage_safety_margin_seconds) == (
        DEFAULT_WATERMARK_SAFETY_MARGIN
    )
    assert timedelta(seconds=config.rescan_overlap_seconds) == DEFAULT_RESCAN_OVERLAP


def test_the_default_margin_is_the_pessimistic_lag_figure() -> None:
    """RS-05 1.4 says "up to roughly two hours for many views"; the default assumes three.

    Assuming too little loses changes silently; assuming too much only widens a query.
    """
    assert DEFAULT_WATERMARK_SAFETY_MARGIN == ACCOUNT_USAGE_LAG == timedelta(hours=3)


def test_an_operator_can_widen_the_margin_for_a_slower_tenant() -> None:
    config = _config(account_usage_safety_margin_seconds=21_600, rescan_overlap_seconds=1_800)

    assert config.account_usage_safety_margin_seconds == 21_600
    assert config.rescan_overlap_seconds == 1_800


def test_a_negative_margin_is_refused() -> None:
    with pytest.raises(ValueError):
        _config(account_usage_safety_margin_seconds=-1)
