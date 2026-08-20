"""Config-error paths: a missing file, and an invalid pair, both fail clearly with exit 2."""

from __future__ import annotations

import json
from pathlib import Path

from cli_helpers import write_engine_config
from click.testing import CliRunner

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.errors import EXIT_CONFIG_ERROR


def test_missing_config_file_fails_clearly(
    runner: CliRunner, tmp_path: Path, state_db_url: str
) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    result = runner.invoke(cli, ["--state-db", state_db_url, "run", "--config", str(missing)])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "does-not-exist.yaml" in result.output


def test_invalid_config_names_the_pair_and_field(
    runner: CliRunner, tmp_path: Path, state_db_url: str
) -> None:
    """A pair pointing a non-Qlik endpoint as its target violates config.py's direction
    guardrail; the resulting error must name the offending pair and say why, intact."""
    config = {
        "endpoints": {
            "dbx": {"connector": "databricks"},
            "not_qlik": {"connector": "not-qlik"},
        },
        "pairs": [
            {
                "name": "bad-pair",
                "source": "dbx",
                "target": "not_qlik",
                "catalog_schema_patterns": ["sales.*"],
                "target_space": "Sales",
                "entity_types": ["data_product"],
            }
        ],
    }
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(config))

    result = runner.invoke(cli, ["--state-db", state_db_url, "run", "--config", str(config_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "bad-pair" in result.output
    assert "not_qlik" in result.output
    assert "qlik" in result.output


def test_unknown_pair_name_fails_clearly(
    runner: CliRunner, tmp_path: Path, state_db_url: str
) -> None:
    config_path = write_engine_config(tmp_path, pair_name="real-pair")
    result = runner.invoke(
        cli,
        ["--state-db", state_db_url, "run", "--config", str(config_path), "--pair", "no-such-pair"],
    )
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "no-such-pair" in result.output
    assert "real-pair" in result.output
