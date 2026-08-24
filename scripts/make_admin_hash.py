"""Produce the console administrator's password hash (decision C7).

The console is configured with a *hash*, never a password: a plaintext password in a pod
spec, a CI variable store, ``docker inspect`` output or ``/proc/self/environ`` is directly
reusable by anyone who can read it, while a scrypt hash is not. That is the whole reason
this script exists — the password should never have to be stored anywhere the deployment
can read it.

Run it once per deployment::

    uv run python scripts/make_admin_hash.py

It prompts for the password (through ``getpass``, so it is never echoed to the terminal and
never lands in shell history), and prints the environment-variable line to put in your
deployment's secret store::

    QLABS_CONSOLE_ADMIN__PASSWORD_HASH='$scrypt$...'

Set that, and optionally ``QLABS_CONSOLE_ADMIN__USERNAME`` (it defaults to ``admin``), and
``qlabs-catalog-sync serve`` will start. Without it the service refuses to start at all
rather than serving an unauthenticated console — see ``api/auth.py``.

The password is never printed, never logged, and never written to a file by this script.

If the deployment is only ever your own machine -- a local Docker container, a debug run --
you can skip this script and set ``QLABS_CONSOLE_ADMIN__PASSWORD`` to the password itself
instead; the service hashes it at startup and logs a warning that it did. The hash is still
the right choice anywhere the environment is a shared secret store, and it wins if both are
set.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from qlabs_catalog_sync.api.auth import (
    ADMIN_PASSWORD_HASH_KEY,
    ADMIN_SECRET_ENDPOINT,
    ADMIN_USERNAME_KEY,
    DEFAULT_ADMIN_USERNAME,
    AuthConfigurationError,
    ScryptParams,
    hash_password,
)


def _env_name(key: str) -> str:
    """The environment variable the shipped secret backend reads ``key`` from."""
    return f"{ADMIN_SECRET_ENDPOINT.upper()}__{key.upper()}"


def _read_password(*, confirm: bool) -> str:
    """Prompt twice and refuse a mismatch, so a typo cannot lock the operator out.

    A hash is not reversible: a mistyped password produces a perfectly valid hash for a
    password nobody knows, and the only way out is generating a new one. Confirming costs
    one extra prompt and removes that failure entirely.
    """
    password = getpass.getpass("Administrator password: ")
    if not confirm:
        return password
    again = getpass.getpass("Confirm password: ")
    if password != again:
        raise SystemExit("passwords did not match; nothing was written")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--username",
        default=DEFAULT_ADMIN_USERNAME,
        help=(
            "Administrator username to print alongside the hash "
            f"(default: {DEFAULT_ADMIN_USERNAME})."
        ),
    )
    parser.add_argument(
        "--log-n",
        type=int,
        default=ScryptParams().log_n,
        help=(
            "scrypt cost exponent. The default is one of OWASP's listed configurations; "
            "raise it to make verification more expensive. The value is carried inside the "
            "hash, so changing it needs no code change."
        ),
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Prompt for the password once instead of twice. Not recommended.",
    )
    args = parser.parse_args(argv)

    try:
        password = _read_password(confirm=not args.no_confirm)
        digest = hash_password(password, params=ScryptParams(log_n=args.log_n))
    except AuthConfigurationError as exc:
        # The message never echoes the password - see hash_password's own docstring.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print()
    print("# Put these in your deployment's environment or secret manager.")
    print("# The password itself does not need to be stored anywhere.")
    print(f"{_env_name(ADMIN_PASSWORD_HASH_KEY)}='{digest}'")
    if args.username != DEFAULT_ADMIN_USERNAME:
        print(f"{_env_name(ADMIN_USERNAME_KEY)}='{args.username}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
