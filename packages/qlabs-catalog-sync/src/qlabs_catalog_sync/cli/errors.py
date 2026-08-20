"""Exit codes and the CLI's own error type.

WP2 / T2.8. A CI pipeline branches on the process exit code, so it is a deliberate,
documented contract rather than an accident of whatever exception happened to escape:

* :data:`EXIT_OK` -- every cycle (or identity operation) run by this invocation
  completed cleanly: committed, nothing outstanding, no errors.
* :data:`EXIT_INCOMPLETE` -- the process ran to completion but something about the
  *work* did not finish: a cycle came back :attr:`~qlabs_catalog_sync.sync.loop.
  RunStatus.PARTIAL` or :attr:`~qlabs_catalog_sync.sync.loop.RunStatus.FAILED` without
  an endpoint quarantine, a record failed, or the run collected non-fatal errors. This
  is "ran but some records failed."
* :data:`EXIT_CONFIG_ERROR` -- the config file, an endpoint's settings/secrets, or a
  CLI argument was invalid; nothing was attempted against a live endpoint. This is also
  Click's own default exit code for a usage error (an unreadable ``--config`` path, a
  bad ``--entity-type`` choice), so the two are deliberately the same number.
* :data:`EXIT_ENDPOINT_UNREACHABLE` -- a configured endpoint could not be reached or
  authenticated (connector ``setup()``/``healthcheck()`` raised, or a cycle quarantined
  an endpoint after an :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` or an
  exhausted :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` retry budget).

:class:`CliError` is how every command signals one of the non-zero codes deliberately,
instead of an uncaught traceback: it is a :class:`click.ClickException`, so Click prints
``Error: <message>`` to stderr and exits with :attr:`CliError.exit_code` -- never stdout,
which is reserved for the human-readable plan/report a person is meant to read.
"""

from __future__ import annotations

import click

__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_ENDPOINT_UNREACHABLE",
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "CliError",
]

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG_ERROR = 2
EXIT_ENDPOINT_UNREACHABLE = 3


class CliError(click.ClickException):
    """A deliberate CLI failure carrying one of this module's exit codes.

    Raise this instead of letting a library exception escape uncaught whenever the
    situation is one this module's exit-code contract already names -- config invalid,
    a connector unreachable, and so on -- so the person running the command gets a
    clean, one-line ``Error: ...`` message and a documented exit code rather than a
    Python traceback.
    """

    def __init__(self, message: str, *, exit_code: int = EXIT_CONFIG_ERROR) -> None:
        super().__init__(message)
        # click.ClickException.exit_code is a ClassVar (default 1); overriding it per
        # instance is exactly how Click itself expects a caller to pick an exit code
        # (it is read back as `exc.exit_code` in Click's own error handling), so this
        # is a safe, deliberate override -- mypy's ClassVar check does not know that.
        self.exit_code = exit_code  # type: ignore[misc]
