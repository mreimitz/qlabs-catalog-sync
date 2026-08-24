"""``serve`` refuses to start without a usable administrator credential -- *clearly*.

C7 makes the refusal itself non-negotiable: no credential, no service. What these tests
pin down is the other half, the half a person actually experiences -- that the refusal
arrives as the CLI's documented ``Error: ...`` line and exit 2, not as a Python traceback.

This matters most for ``QLABS_CONSOLE_ADMIN__PASSWORD``, the plaintext alternative to a
configured hash: it is typed straight into a ``.env`` by a person, so the ways it goes
wrong are the ordinary human ones -- the variable left blank, the variable not set at all
-- and a traceback there reads as the software being broken rather than the ``.env`` line
needing a value.

There is no password-strength policy to test: any non-empty password is accepted (see
``tests/api/test_auth.py``). The empty string is the single refusal, and it is refused as
a misconfiguration rather than as a weak password.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cli_helpers import write_engine_config
from click.testing import CliRunner, Result

from qlabs_catalog_sync.api.auth import (
    ADMIN_PASSWORD_HASH_KEY,
    ADMIN_PASSWORD_KEY,
    ADMIN_SECRET_ENDPOINT,
)
from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_CONFIG_ERROR

PASSWORD_VARIABLE = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_KEY.upper()}"
HASH_VARIABLE = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_HASH_KEY.upper()}"
USERNAME_VARIABLE = f"{ADMIN_SECRET_ENDPOINT.upper()}__USERNAME"


@pytest.fixture(autouse=True)
def _no_inherited_admin_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer running these tests may well have a real credential exported."""
    for variable in (PASSWORD_VARIABLE, HASH_VARIABLE, USERNAME_VARIABLE):
        monkeypatch.delenv(variable, raising=False)


def _invoke_serve(
    runner: CliRunner, tmp_path: Path, state_db_url: str, cli_deps: CliDeps
) -> Result:
    """Invoke ``serve`` far enough to reach the credential check.

    ``--port 0`` is deliberate: if any of these ever *stopped* refusing, the assertion
    that follows would fail on a bound socket rather than hang the suite on a real one.
    """
    config = write_engine_config(tmp_path)
    return runner.invoke(
        cli,
        ["--state-db", state_db_url, "serve", "--config", str(config), "--port", "0"],
        obj=cli_deps,
    )


def test_an_empty_plaintext_password_is_a_clean_config_error(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    cli_deps: CliDeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PASSWORD_VARIABLE, "")

    result = _invoke_serve(runner, tmp_path, state_db_url, cli_deps)

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "empty" in result.output
    assert "Traceback" not in result.output


def test_the_empty_password_error_names_the_variable_to_edit(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    cli_deps: CliDeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Password must not be empty" is only actionable if it says *which* password."""
    monkeypatch.setenv(PASSWORD_VARIABLE, "")

    result = _invoke_serve(runner, tmp_path, state_db_url, cli_deps)

    assert PASSWORD_VARIABLE in result.output


def test_a_malformed_hash_error_does_not_echo_the_value(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    cli_deps: CliDeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that fails to parse as a hash is quite likely a password pasted into the
    wrong variable, and CLI output lands in scrollback and CI logs."""
    sentinel = "SENTINEL-not-a-hash-2b91d"
    monkeypatch.setenv(HASH_VARIABLE, sentinel)

    result = _invoke_serve(runner, tmp_path, state_db_url, cli_deps)

    assert sentinel not in result.output


def test_no_credential_at_all_is_a_clean_config_error(
    runner: CliRunner, tmp_path: Path, state_db_url: str, cli_deps: CliDeps
) -> None:
    """The C7 refusal is the same shape: an ``Error:`` line, exit 2, no traceback."""
    result = _invoke_serve(runner, tmp_path, state_db_url, cli_deps)

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "Traceback" not in result.output
    assert HASH_VARIABLE in result.output
    assert PASSWORD_VARIABLE in result.output
