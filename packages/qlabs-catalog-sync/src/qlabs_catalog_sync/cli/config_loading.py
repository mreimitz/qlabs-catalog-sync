"""Loading :class:`EngineConfig` and resolving credentials, with CLI-shaped errors.

WP2 / T2.8. ``config.py`` (T2.3) already raises precise, pair-and-field-named errors --
a :class:`pydantic.ValidationError` from :meth:`EngineConfig.load` for a malformed pair
or a direction-guardrail violation, :class:`~qlabs_catalog_sync.config.
SecretNotFoundError` from :meth:`EngineConfig.resolve_credentials` for a missing secret.
This module's only job is to let that text reach the user **intact** while giving it a
CLI exit code (:data:`~qlabs_catalog_sync.cli.errors.EXIT_CONFIG_ERROR`) instead of a
bare Python traceback -- never to re-word or summarize what T2.3 already said precisely.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from qlabs_catalog_sync.config import EngineConfig, SecretNotFoundError

from .errors import EXIT_CONFIG_ERROR, CliError

__all__ = ["load_engine_config", "resolve_credentials"]

_VALUE_ERROR_PREFIX = "Value error, "


def load_engine_config(config_path: str | Path) -> EngineConfig:
    """:meth:`EngineConfig.load`, with validation failures turned into a :class:`CliError`.

    ``config_path`` is expected to already exist -- the ``run``/``dry-run``/
    ``identity-confirm bootstrap`` commands declare ``--config`` as a
    ``click.Path(exists=True)``, so Click itself produces the "missing config file"
    error (also exit code 2) before this function is ever called.
    """
    try:
        return EngineConfig.load(config_file=config_path)
    except ValidationError as exc:
        raise CliError(_format_validation_error(exc), exit_code=EXIT_CONFIG_ERROR) from exc
    except ValueError as exc:
        raise CliError(
            f"invalid config file {str(config_path)!r}: {exc}", exit_code=EXIT_CONFIG_ERROR
        ) from exc


def resolve_credentials(engine_config: EngineConfig) -> dict[str, dict[str, object]]:
    """:meth:`EngineConfig.resolve_credentials`, with a missing secret as a :class:`CliError`."""
    try:
        return engine_config.resolve_credentials()
    except SecretNotFoundError as exc:
        raise CliError(f"invalid config: {exc}", exit_code=EXIT_CONFIG_ERROR) from exc


def _format_validation_error(exc: ValidationError) -> str:
    """Every ``ValueError`` message from ``config.py``'s validators, one per line.

    Strips pydantic's own ``"Value error, "`` prefix and the per-error type/URL
    boilerplate -- the substantive, pair-and-field-named text T2.3 wrote is kept
    verbatim; only pydantic's wrapper around it is removed.
    """
    messages: list[str] = []
    for error in exc.errors():
        message = str(error.get("msg", ""))
        if message.startswith(_VALUE_ERROR_PREFIX):
            message = message[len(_VALUE_ERROR_PREFIX) :]
        messages.append(message)
    if not messages:
        return f"invalid config: {exc}"
    return "invalid config:\n  - " + "\n  - ".join(messages)
