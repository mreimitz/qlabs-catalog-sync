"""Typed SDK exceptions.

WP1 / T1.7. The shared exception hierarchy connectors raise so the engine reacts
uniformly instead of parsing message strings: :class:`TransientError` (retry),
:class:`AuthError` (quarantine the endpoint), :class:`NotFound` (skip),
:class:`ConflictError` (re-read and re-diff — Qlik returns HTTP 412 on an ETag
mismatch), and :class:`CapabilityError` (the plan was wrong; never retry). All five
share the :class:`ConnectorError` base, so the engine can catch one type when it only
needs to know "something about this operation failed" and inspect further from there.

Connectors must raise these — never invent per-connector exception hierarchies (see
the RM-01 agent guide's conventions section).
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "AuthError",
    "CapabilityError",
    "ConflictError",
    "ConnectorError",
    "NotFound",
    "TransientError",
]


class ConnectorError(Exception):
    """Base for every typed error the SDK defines.

    Carries enough structure that the engine does not have to parse message text:

    * ``endpoint`` — the connector/endpoint key the error came from (e.g. ``"qlik"``
      or a per-tenant key like ``"qlik_acme"``), matching the key used in config, the
      IdentityMap, and logs.
    * ``entity_type`` — the neutral entity type involved, if any. Typed as ``str``
      rather than importing ``models.EntityType`` to keep this module dependency-free;
      ``EntityType`` is a ``StrEnum`` so its members satisfy this field directly.
    * ``cause`` — the original exception this wraps (an ``httpx`` error, a vendor SDK
      exception, ...), also set as ``__cause__`` so tracebacks chain normally.
    * ``retryable`` — a class-level default (overridable per instance via the
      constructor is deliberately *not* offered — retryability is a property of the
      error *kind*, not of one occurrence) telling the engine whether attempting the
      operation again is ever appropriate. ``True`` covers both "retry as-is after a
      backoff" (:class:`TransientError`) and "retry after corrective action"
      (:class:`ConflictError`'s re-read-and-re-diff); ``False`` means the engine must
      not blindly retry (:class:`AuthError`, :class:`NotFound`, :class:`CapabilityError`).

    Subclasses may add their own structured attributes (see each class below); all of
    them accept and forward ``endpoint``/``entity_type``/``cause`` unchanged.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        entity_type: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.entity_type = entity_type
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    def __repr__(self) -> str:
        parts: dict[str, Any] = {"endpoint": self.endpoint, "entity_type": self.entity_type}
        if self.cause is not None:
            parts["cause"] = repr(self.cause)
        detail = ", ".join(f"{key}={value!r}" for key, value in parts.items() if value is not None)
        suffix = f" ({detail})" if detail else ""
        return f"{type(self).__name__}({self.message!r}){suffix}"


class TransientError(ConnectorError):
    """A retryable failure: network blip, timeout, HTTP 429/5xx.

    ``retry_after_seconds`` carries a server-supplied hint (e.g. a ``Retry-After``
    header) when one is available, so the engine's backoff can honor it instead of
    guessing.
    """

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        entity_type: str | None = None,
        cause: BaseException | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint, entity_type=entity_type, cause=cause)
        self.retry_after_seconds = retry_after_seconds


class AuthError(ConnectorError):
    """Authentication or authorization failed against the endpoint.

    Not retryable: the engine quarantines the endpoint (stops scheduling cycles
    against it) rather than retrying with the same bad credentials.
    """

    retryable: ClassVar[bool] = False


class NotFound(ConnectorError):
    """The requested entity does not exist at the endpoint.

    Not retryable: the engine skips this item for the current cycle rather than
    retrying a lookup that will not start succeeding on its own. ``native_key``
    records the identity that was not found, when available.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        entity_type: str | None = None,
        cause: BaseException | None = None,
        native_key: str | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint, entity_type=entity_type, cause=cause)
        self.native_key = native_key


class ConflictError(ConnectorError):
    """An optimistic-concurrency write was rejected (Qlik: HTTP 412 on ETag mismatch).

    Retryable in the specific sense that the engine's reaction is automated recovery,
    not abandonment: re-read the current state, re-diff against desired state, and
    retry the write against the fresh revision — never a blind resend of the same
    request. ``expected_revision``/``actual_revision`` carry the ETag or revision
    values involved, when the connector has them, so the engine does not need to
    reparse an HTTP error body to log or reason about the mismatch.
    """

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        entity_type: str | None = None,
        cause: BaseException | None = None,
        expected_revision: str | None = None,
        actual_revision: str | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint, entity_type=entity_type, cause=cause)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class CapabilityError(ConnectorError):
    """The engine asked a connector to do something its capability manifest forbids.

    Raised when the engine's plan itself was wrong — most commonly a write to a field
    the manifest declares ``ro``/``na``. Never retryable: retrying sends the exact same
    invalid request again. This is a planning bug, not a transient condition, and the
    conformance kit (T1.8) asserts connectors raise it in exactly this situation.
    ``field`` is the neutral field name the engine tried to write,
    ``capability_mode`` is the manifest mode that forbade it (``"ro"`` or ``"na"``), and
    ``operation`` is what was attempted (``"create"``, ``"update"``, ``"delete"``, ...),
    when the connector has them to hand.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        entity_type: str | None = None,
        cause: BaseException | None = None,
        field: str | None = None,
        capability_mode: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint, entity_type=entity_type, cause=cause)
        self.field = field
        self.capability_mode = capability_mode
        self.operation = operation
