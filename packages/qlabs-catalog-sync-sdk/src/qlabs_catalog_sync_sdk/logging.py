"""Structured logging: secret redaction and the connector-bound logger.

WP1 / T1.7. Two things live here:

* :func:`redact_secrets` — a standalone `structlog
  <https://www.structlog.org/>`_ processor that strips secret material out of a log
  event before it leaves the process. It is a plain function so it composes into
  *any* processor chain, including one the engine (T2.7) assembles for its own
  production logging setup (JSON-to-stdout, OTel correlation, etc.) — this module
  deliberately never calls ``structlog.configure()`` itself, because mutating global
  structlog configuration is an application's call to make, not a library's.
* :func:`get_connector_logger` — builds the bound logger ``ConnectorContext`` hands to
  a connector. It wraps its own self-contained processor chain (via
  ``structlog.wrap_logger``) so redaction is unconditional: a connector's logger is
  never secret-safe *by convention only*, it is secret-safe *by construction*, even if
  nothing else in the process has configured structlog at all.

Redaction covers three shapes of secret, all recursively (nested dicts and lists, not
just the top level of the event):

1. Known secret-shaped keys (``token``, ``password``, ``secret``, ``api_key``,
   ``authorization``, ``client_secret``, ``private_key``, ...) — the value under a
   matching key is replaced outright, regardless of its own type.
2. Pydantic ``SecretStr``/``SecretBytes`` values — unwrapped and replaced, wherever
   they appear (pydantic's own ``repr``/``str`` already mask these, but a caller can
   still hand ``.get_secret_value()`` to a log call, so this is a second line of
   defense).
3. ``Authorization: Bearer <token>`` / bare ``Bearer``/``Basic`` credential material
   inside an otherwise-ordinary string value, e.g. a logged header line or an HTTP
   request repr.
"""

from __future__ import annotations

import logging as _stdlib_logging
import re
from collections.abc import Mapping
from typing import Any

import structlog
from pydantic import SecretBytes, SecretStr

__all__ = ["REDACTED", "get_connector_logger", "redact_secrets"]

REDACTED = "***REDACTED***"

# Key names (case-insensitive, substring match) whose *value* is always dropped,
# whatever type it is. Substring matching is deliberate: it also catches
# "clientSecret", "api-key", "x-api-key", "authorization_header", etc.
_SECRET_KEY_PATTERN = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|authorization|private[_-]?key"
    r"|credential|access[_-]?key|refresh[_-]?token)",
    re.IGNORECASE,
)

# A bearer/basic credential embedded in an otherwise-normal string, e.g.
# "Authorization: Bearer eyJhbGciOi..." or a bare "Bearer sk-...".
_CREDENTIAL_IN_STRING_PATTERN = re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE)


def _is_secret_key(key: object) -> bool:
    return isinstance(key, str) and _SECRET_KEY_PATTERN.search(key) is not None


def _redact_string(value: str) -> str:
    return _CREDENTIAL_IN_STRING_PATTERN.sub(lambda m: f"{m.group(1)} {REDACTED}", value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return REDACTED
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {
            key: (REDACTED if _is_secret_key(key) else _redact_value(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


def redact_secrets(
    logger: object, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """A structlog processor: recursively strip secret material from ``event_dict``.

    Safe to place in any structlog processor chain — it only inspects and rewrites
    values, never raises, and leaves non-secret keys/values (including nested
    non-secret structures) untouched.
    """
    return {
        key: (REDACTED if _is_secret_key(key) else _redact_value(value))
        for key, value in event_dict.items()
    }


def get_connector_logger(
    *, endpoint: str, tenant: str | None = None, **extra: Any
) -> structlog.stdlib.BoundLogger:
    """Build the bound logger a ``ConnectorContext`` hands to one connector.

    The returned logger is bound with ``endpoint`` (and ``tenant``, when given) plus
    any additional ``extra`` context, and every event it emits passes through
    :func:`redact_secrets` — this is guaranteed by the logger's own processor chain,
    independent of whatever global structlog configuration the host process does or
    does not have. Events render to a JSON string and are handed to a Python stdlib
    ``logging.Logger`` named ``qlabs.connector.<endpoint>``, so wherever the host
    process routes stdlib logging (stdout, a file, ``pytest``'s ``caplog``) is where
    connector logs end up, without this module dictating handler/formatter setup.
    """
    wrapped = structlog.wrap_logger(
        _stdlib_logging.getLogger(f"qlabs.connector.{endpoint}"),
        wrapper_class=structlog.stdlib.BoundLogger,
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
    )
    context: dict[str, Any] = {"endpoint": endpoint}
    if tenant is not None:
        context["tenant"] = tenant
    context.update(extra)
    bound: structlog.stdlib.BoundLogger = wrapped.bind(**context)
    return bound
