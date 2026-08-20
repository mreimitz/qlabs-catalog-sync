"""Secret references and the environment secret backend (C2).

WP10 / T10.2. Parses ``EndpointRow.secret_ref`` (``"scheme:locator"``, e.g.
``"env:QLIK_ACME"``) and resolves it into what a connector's own ``ConfigModel``
needs at ``setup()`` — without ever handling, returning, or logging a secret value
anywhere outside a pydantic ``SecretStr``/``SecretBytes`` wrapper.

Reconciling two shapes
-----------------------

Two secret-handling shapes already exist in this codebase, and they are not the same:

* **T2.3** (``qlabs_catalog_sync.config``, unedited by this task): ``EndpointConfig.
  secrets`` is a **per-field** mapping — one entry per connector config field, e.g.
  ``{"client_secret": "CLIENT_SECRET"}`` — resolved field-by-field via
  :class:`~qlabs_catalog_sync.config.SecretBackend` / :meth:`~qlabs_catalog_sync.
  config.EndpointConfig.resolve`. That machinery is complete and is not duplicated
  here.
* **T10.1** (``qlabs_catalog_sync.configstore.models.EndpointRow``): ``secret_ref`` is
  a **single** nullable string column — one reference for the whole endpoint (decision
  C2: ``"env:QLIK_ACME"``, later ``"vault:kv/qlabs/qlik-acme"``). There is no per-field
  mapping in the schema at all; the models module's own docstring is explicit about
  why one column is enough: "resolved through the SDK's ``ConnectorConfig.
  for_endpoint`` prefix convention, one reference yields every credential field that
  endpoint's connector needs".

What ``env:QLIK_ACME`` means here, precisely: ``QLIK_ACME`` is an **endpoint-key
prefix** — the exact same ``endpoint`` argument ``ConnectorConfig.for_endpoint`` and
``SecretBackend.get_secret`` already take, normalized identically by both (letters/
digits kept, everything else collapsed to ``_``, upper-cased). It is deliberately
**not** required to equal ``EndpointRow.name``: decoupling "how this endpoint is
addressed in this system" from "which environment-variable prefix its secrets live
under" is the entire reason ``secret_ref`` is its own column rather than an implicit
reuse of the endpoint's own key — renaming an endpoint, or pointing two endpoints at
one shared credential, never has to touch an environment variable.

Because the schema stores no per-field mapping, this module *derives* one: every
``SecretStr``/``SecretBytes`` field a connector's ``ConfigModel`` declares is resolved
under its own (upper-cased) field name as the backend key — see
:func:`resolve_connector_kwargs`. This mirrors the *shape* T2.3's
``EndpointConfig.resolve`` already returns (``{**settings, **resolved-secret-fields}``,
exactly what ``ConnectorConfig.for_endpoint`` accepts as override kwargs) and reuses
its actual working parts — :class:`~qlabs_catalog_sync.config.SecretBackend`,
:class:`~qlabs_catalog_sync.config.EnvironmentSecretBackend` and
:class:`~qlabs_catalog_sync.config.SecretNotFoundError` — directly, rather than
re-reading ``os.environ`` a second time. It does not call ``EndpointConfig.resolve``
itself: that method requires a full ``EndpointConfig`` (including a non-blank
``connector`` name that has no natural value at this call site) and a hand-authored
``secrets`` dict — exactly the shape T10.1 deliberately stopped storing. The "work"
that instruction protects — the ``os.environ`` read and its error shape — lives one
level down, in ``EnvironmentSecretBackend.get_secret``, and that is what gets reused.

Nothing in this module ever returns a secret value except wrapped in the
``SecretStr``/``SecretBytes`` that :class:`~qlabs_catalog_sync.config.SecretBackend`
already produces (redacted from ``repr``/``str``/``model_dump`` by pydantic itself,
and from structured logs by the SDK's ``logging.redact_secrets`` — T1.7). The
resolve-status query (:func:`resolve_status`) goes further: it returns
:class:`SecretResolveStatus`, a frozen two-field dataclass that cannot carry a value
at all — there is no field on it capable of holding one, so this is enforced by the
type, not by a promise kept elsewhere in the code.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from pydantic import SecretBytes, SecretStr
from pydantic_settings import BaseSettings

from qlabs_catalog_sync.config import (
    EnvironmentSecretBackend,
    SecretBackend,
    SecretNotFoundError,
)
from qlabs_catalog_sync_sdk.config import ConnectorConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "SUPPORTED_SECRET_REF_SCHEMES",
    "SecretRef",
    "SecretRefFormatError",
    "SecretResolveStatus",
    "resolve_connector_kwargs",
    "resolve_status",
    "secret_field_names",
]

#: Schemes this module can resolve today. Decision C2 names ``"vault"`` as a later
#: scheme ("env:QLIK_ACME, later vault:kv/qlabs/qlik-acme"); until a Vault-backed
#: ``SecretBackend`` exists, a ``"vault:..."`` reference is rejected at parse time as
#: unsupported (see :class:`SecretRefFormatError`) rather than accepted and left to
#: fail confusingly later.
SUPPORTED_SECRET_REF_SCHEMES: frozenset[str] = frozenset({"env"})


class SecretRefFormatError(ValueError):
    """A ``"scheme:locator"`` secret reference string is malformed or unsupported.

    Carries ``raw`` (the offending input) as a structured attribute — mirroring
    ``qlabs_catalog_sync.config.SecretNotFoundError``'s style — in addition to a
    message that names both the input and what was expected of it.
    """

    def __init__(self, raw: str, reason: str) -> None:
        super().__init__(f"invalid secret reference {raw!r}: {reason}")
        self.raw = raw


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A parsed ``"scheme:locator"`` secret reference (C2), e.g. ``"env:QLIK_ACME"``.

    ``locator`` is an opaque, scheme-specific string; see the module docstring for
    what it means for the ``"env"`` scheme (the only one implemented today).
    """

    scheme: str
    locator: str

    @classmethod
    def parse(cls, raw: str) -> SecretRef:
        """Parse ``raw``, raising :class:`SecretRefFormatError` naming ``raw`` for
        anything malformed: no ``":"``, an empty scheme or locator, leading/trailing/
        embedded whitespace anywhere in the string, or a scheme outside
        :data:`SUPPORTED_SECRET_REF_SCHEMES`.
        """
        if raw == "":
            raise SecretRefFormatError(raw, "must not be empty")
        if raw != raw.strip():
            raise SecretRefFormatError(raw, "must not have leading or trailing whitespace")
        if ":" not in raw:
            raise SecretRefFormatError(raw, "expected 'scheme:locator', found no ':'")
        scheme, _, locator = raw.partition(":")
        if not scheme:
            raise SecretRefFormatError(raw, "scheme before ':' must not be empty")
        if not locator:
            raise SecretRefFormatError(raw, "locator after ':' must not be empty")
        if any(part.isspace() or " " in part or "\t" in part for part in (scheme, locator)):
            raise SecretRefFormatError(raw, "scheme and locator must not contain whitespace")
        if scheme not in SUPPORTED_SECRET_REF_SCHEMES:
            supported = ", ".join(sorted(SUPPORTED_SECRET_REF_SCHEMES))
            raise SecretRefFormatError(
                raw, f"unsupported scheme {scheme!r}; supported schemes: {supported}"
            )
        return cls(scheme=scheme, locator=locator)


@dataclass(frozen=True, slots=True)
class SecretResolveStatus:
    """The result of a resolve-status query: exactly ``resolvable`` and ``reason``.

    Deliberately minimal — no field here can hold a resolved secret value.
    ``tests/configstore/test_secrets.py`` pins the field set itself, so a future
    change that adds e.g. a ``value`` field fails that test rather than silently
    creating a way for a secret to escape through this type.
    """

    resolvable: bool
    reason: str


def _secret_type_of(annotation: Any) -> type[SecretStr] | type[SecretBytes] | None:
    """``SecretStr``/``SecretBytes`` when ``annotation`` is one of them (optionally
    wrapped in a union with ``None``, e.g. ``SecretBytes | None``); ``None`` otherwise.
    """
    if annotation is SecretStr or annotation is SecretBytes:
        return annotation
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _secret_type_of(arg)
            if found is not None:
                return found
    return None


@dataclass(frozen=True, slots=True)
class _SecretField:
    """One secret-typed field on a connector's ``ConfigModel``: its name, whether
    pydantic considers it required, and which secret wrapper type it declares."""

    name: str
    required: bool
    secret_type: type[SecretStr] | type[SecretBytes]


def _secret_fields(config_model: type[BaseSettings]) -> list[_SecretField]:
    """Every field on ``config_model`` typed ``SecretStr``/``SecretBytes``.

    This is how "one reference yields every credential field that endpoint's
    connector needs" (``configstore/models.py``) is implemented: the database schema
    stores no per-field secrets mapping, so one is derived here from the connector's
    own ``ConfigModel`` instead of requiring a caller to repeat each connector's
    secret field names by hand. Required-ness matters for resolution: an *optional*
    secret field (a default of ``None``, e.g. a bearer refresh token some tenants
    don't use) with nothing in the backend is not a resolution failure — see
    :func:`resolve_connector_kwargs` and :func:`resolve_status`. ``secret_type``
    matters too: :class:`~qlabs_catalog_sync.config.SecretBackend` always hands back a
    ``SecretStr`` (see its protocol signature) — pydantic does not implicitly convert
    that into a ``SecretBytes``-typed field, so a ``SecretBytes`` field's value is
    re-wrapped into the type it actually declares (see ``resolve_connector_kwargs``),
    never left as a bare string.
    """
    fields: list[_SecretField] = []
    for name, field_info in config_model.model_fields.items():
        secret_type = _secret_type_of(field_info.annotation)
        if secret_type is not None:
            fields.append(
                _SecretField(name=name, required=field_info.is_required(), secret_type=secret_type)
            )
    return fields


def secret_field_names(config_model: type[BaseSettings]) -> frozenset[str]:
    """The names of every ``SecretStr``/``SecretBytes`` field ``config_model`` declares.

    Public because two different questions need the *same* answer and must never be
    able to disagree about it:

    * **which fields this module resolves** from a :class:`SecretRef` (see
      :func:`resolve_connector_kwargs` / :func:`resolve_status`), and
    * **which keys the configuration service refuses** in an endpoint's plain
      ``settings`` dict, because a credential smuggled in there would land in the one
      column decision C2 promises can never hold one
      (``configstore.service._validate_endpoint_settings``).

    T10.3 originally carried its own copy of this reflection, since the version here
    was private. Two implementations of "what counts as a secret field" drifting apart
    would mean the set of fields rejected inline and the set actually resolved stop
    matching — a credential could be accepted into ``settings`` for a field this module
    would then also read from the environment. One implementation, exported.
    """
    return frozenset(field.name for field in _secret_fields(config_model))


def _require_supported_scheme(ref: SecretRef) -> None:
    if ref.scheme not in SUPPORTED_SECRET_REF_SCHEMES:
        supported = ", ".join(sorted(SUPPORTED_SECRET_REF_SCHEMES))
        raise SecretRefFormatError(
            f"{ref.scheme}:{ref.locator}",
            f"unsupported scheme {ref.scheme!r}; supported schemes: {supported}",
        )


def resolve_connector_kwargs(
    ref: SecretRef,
    config_model: type[ConnectorConfig],
    *,
    settings: Mapping[str, Any] | None = None,
    backend: SecretBackend | None = None,
) -> dict[str, Any]:
    """Resolve ``ref`` into ``ConnectorConfig.for_endpoint`` override kwargs.

    Returns ``{**settings, **secret_fields}``: ``settings`` (the endpoint's plain,
    non-secret configuration — ``EndpointRow.settings`` / ``EndpointConfig.settings``)
    passes through unchanged, and every ``SecretStr``/``SecretBytes`` field
    ``config_model`` declares (see :func:`_secret_fields`) is resolved through
    ``backend`` (:class:`~qlabs_catalog_sync.config.EnvironmentSecretBackend` by
    default), keyed on ``ref.locator`` as the backend's ``endpoint`` argument — the
    exact prefix convention ``ConnectorConfig.for_endpoint`` itself uses. The result is
    exactly what ``config_model.for_endpoint(ref.locator, **result)`` accepts:

        kwargs = resolve_connector_kwargs(ref, QlikConfig, settings=row.settings)
        config = QlikConfig.for_endpoint(ref.locator, **kwargs)

    An *optional* secret field (pydantic considers it not required — a default of
    ``None``) that ``backend`` has no value for is simply omitted from the result, so
    ``for_endpoint`` falls back to that field's own default; it is not a resolution
    failure. A *required* secret field with no value is: this raises
    :class:`SecretRefFormatError` if ``ref.scheme`` is not supported (only ``"env"``
    today), or :class:`~qlabs_catalog_sync.config.SecretNotFoundError` — unchanged
    from T2.3, naming the endpoint/key/backend and never a value — for the first
    required field ``backend`` cannot resolve. A live credential IS returned here,
    always wrapped in ``SecretStr``/``SecretBytes``, and only from here — see
    :func:`resolve_status` for a query that never returns one at all.
    """
    _require_supported_scheme(ref)
    active_backend: SecretBackend = backend if backend is not None else EnvironmentSecretBackend()
    resolved: dict[str, Any] = dict(settings) if settings is not None else {}
    for field in _secret_fields(config_model):
        try:
            value = active_backend.get_secret(endpoint=ref.locator, key=field.name)
        except SecretNotFoundError:
            if field.required:
                raise
            continue
        resolved[field.name] = (
            value
            if field.secret_type is SecretStr
            else SecretBytes(value.get_secret_value().encode())
        )
    return resolved


def resolve_status(
    ref: SecretRef,
    config_model: type[ConnectorConfig],
    *,
    backend: SecretBackend | None = None,
) -> SecretResolveStatus:
    """Report whether ``ref`` resolves every secret field ``config_model`` declares,
    without ever raising and without ever returning a value.

    Attempts the same per-field resolution :func:`resolve_connector_kwargs` performs,
    through the same :class:`~qlabs_catalog_sync.config.SecretBackend`, but discards
    every resolved value immediately — only ``bool``/``str`` ever cross into the
    returned :class:`SecretResolveStatus`. Only *required* secret fields (pydantic's
    ``field_info.is_required()``) can make this unresolvable; an optional secret field
    with nothing in the backend is not a problem, mirroring
    :func:`resolve_connector_kwargs`. An unsupported scheme or a
    :class:`~qlabs_catalog_sync.config.SecretNotFoundError` (whose own message already
    excludes values, naming only the endpoint/key/expected-variable) becomes
    ``resolvable=False`` with a ``reason`` built from that same value-free
    information — never a raised exception.
    """
    if ref.scheme not in SUPPORTED_SECRET_REF_SCHEMES:
        supported = ", ".join(sorted(SUPPORTED_SECRET_REF_SCHEMES))
        return SecretResolveStatus(
            resolvable=False,
            reason=f"scheme {ref.scheme!r} is not yet supported; supported schemes: {supported}",
        )

    active_backend: SecretBackend = backend if backend is not None else EnvironmentSecretBackend()
    secret_fields = _secret_fields(config_model)
    problems: list[str] = []
    found = 0
    for field in secret_fields:
        try:
            active_backend.get_secret(endpoint=ref.locator, key=field.name)
        except SecretNotFoundError as exc:
            if field.required:
                problems.append(str(exc))
        else:
            found += 1

    if problems:
        return SecretResolveStatus(resolvable=False, reason="; ".join(problems))
    if not secret_fields:
        return SecretResolveStatus(
            resolvable=True, reason="connector declares no secret fields to resolve"
        )
    return SecretResolveStatus(
        resolvable=True,
        reason=(
            f"resolved {found} of {len(secret_fields)} secret field(s) via the environment "
            "backend (every required field resolved)"
        ),
    )
