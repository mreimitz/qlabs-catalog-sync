"""Secret references and the environment secret backend (C2), WP10 / T10.2.

Three things this suite has to prove, per the task's definition of done:

* a reference resolves to a live credential usable for connector setup
  (:func:`resolve_connector_kwargs`, backed by a real ``ConnectorConfig.for_endpoint``
  round trip, not just an internal dict);
* :func:`resolve_status` returns only a boolean and a reason -- pinned by
  ``test_secret_resolve_status_field_set_cannot_carry_a_value``, the "dishonest case"
  that fails the moment a future ``value=`` field sneaks onto
  :class:`~qlabs_catalog_sync.configstore.secrets.SecretResolveStatus``;
* no secret value ever appears in a log record or a serialized response --
  ``test_a_resolved_secret_never_appears_in_real_log_output`` exercises the engine's
  *real* structlog processor chain (``qlabs_catalog_sync.observability.
  REDACTION_TEST_PROCESSORS`` -- the exact processors ``configure_logging`` wires in
  production, reused here rather than re-declared), not a stub of it.

A distinctive sentinel value (``_SENTINEL``) stands in for a live credential
throughout: it cannot occur by accident, so any assertion that it is absent from some
text is a real assertion, not a coincidence.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
import structlog.testing
from pydantic import SecretBytes, SecretStr

from qlabs_catalog_sync.config import SecretNotFoundError
from qlabs_catalog_sync.configstore.secrets import (
    SUPPORTED_SECRET_REF_SCHEMES,
    SecretRef,
    SecretRefFormatError,
    SecretResolveStatus,
    resolve_connector_kwargs,
    resolve_status,
    secret_field_names,
)
from qlabs_catalog_sync.observability import REDACTION_TEST_PROCESSORS, get_logger
from qlabs_catalog_sync_sdk.config import ConnectorConfig

#: Stands in for a live credential value. Distinctive enough that its accidental
#: presence anywhere (a log line, a repr, a JSON dump) can only mean a real leak.
_SENTINEL = "sk-t10-2-do-not-leak-9f3e2c7d4b1a6e58"

#: A second, distinct sentinel -- used to prove that a failure/reason naming *one*
#: missing field never accidentally echoes a *sibling* field's real value.
_SIBLING_SENTINEL = "sk-t10-2-sibling-value-should-never-appear-either"


class _ExampleConnectorConfig(ConnectorConfig):
    """A minimal ``ConnectorConfig`` subclass standing in for a real connector's
    ``ConfigModel`` (shaped like ``QlikConfig``/``DatabricksConfig``): one plain field,
    two secret fields of the two supported secret types.
    """

    base_url: str
    client_secret: SecretStr
    refresh_token: SecretBytes | None = None


class _NoSecretsConnectorConfig(ConnectorConfig):
    """A ``ConfigModel`` that declares no secret-typed field at all."""

    base_url: str = "https://example.qlikcloud.com"


# --------------------------------------------------------------------------------------
# SecretRef.parse
# --------------------------------------------------------------------------------------


def test_parse_accepts_an_env_scheme_reference() -> None:
    ref = SecretRef.parse("env:QLIK_ACME")

    assert ref == SecretRef(scheme="env", locator="QLIK_ACME")
    assert ref.scheme in SUPPORTED_SECRET_REF_SCHEMES


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "QLIK_ACME",  # no colon at all
        ":QLIK_ACME",  # empty scheme
        "env:",  # empty locator
        "vault:kv/qlabs/qlik-acme",  # a real C2 scheme, just not implemented yet
        "ldap:cn=qlik,dc=acme",  # a scheme this module has never heard of
        " env:QLIK_ACME",  # leading whitespace
        "env:QLIK_ACME ",  # trailing whitespace
        "env: QLIK_ACME",  # whitespace right after the colon
        "env:QLIK ACME",  # whitespace inside the locator
        "e n v:QLIK_ACME",  # whitespace inside the scheme
    ],
)
def test_parse_rejects_malformed_or_unsupported_references(raw: str) -> None:
    with pytest.raises(SecretRefFormatError) as excinfo:
        SecretRef.parse(raw)

    assert excinfo.value.raw == raw
    assert raw in str(excinfo.value) or repr(raw) in str(excinfo.value)


def test_parse_error_is_a_value_error() -> None:
    """Callers that only know to catch ``ValueError`` still catch this."""
    with pytest.raises(ValueError):
        SecretRef.parse("not-a-reference")


# --------------------------------------------------------------------------------------
# resolve_connector_kwargs -- resolution to connector-setup kwargs
# --------------------------------------------------------------------------------------


def test_resolve_connector_kwargs_merges_settings_and_secret_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )

    assert kwargs["base_url"] == "https://acme.eu.qlikcloud.com"
    assert isinstance(kwargs["client_secret"], SecretStr)
    assert kwargs["client_secret"].get_secret_value() == _SENTINEL
    # refresh_token is optional and unset in the environment: resolving it eagerly
    # would raise, so the DoD ("a reference resolves to a live credential") only
    # requires the *required* secret fields to resolve. See the "missing" test below
    # for the case where a required field's env var is absent.


def test_resolve_connector_kwargs_skips_an_unset_optional_secret_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh_token`` is optional (``SecretBytes | None = None``) on
    ``_ExampleConnectorConfig``. Its env var being unset must not fail resolution --
    only *required* secret fields do that (see the module docstring's "one reference
    yields every credential field" note and the docstring on ``resolve_connector_kwargs``).
    """
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    monkeypatch.delenv("QLIK_ACME__REFRESH_TOKEN", raising=False)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )

    assert "refresh_token" not in kwargs
    assert kwargs["client_secret"].get_secret_value() == _SENTINEL


def test_resolve_connector_kwargs_resolves_an_optional_secret_field_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    monkeypatch.setenv("QLIK_ACME__REFRESH_TOKEN", _SIBLING_SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )

    assert isinstance(kwargs["refresh_token"], SecretBytes)
    assert kwargs["refresh_token"].get_secret_value() == _SIBLING_SENTINEL.encode()


def test_resolve_status_ignores_an_unset_optional_secret_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    monkeypatch.delenv("QLIK_ACME__REFRESH_TOKEN", raising=False)
    ref = SecretRef.parse("env:QLIK_ACME")

    status = resolve_status(ref, _ExampleConnectorConfig)

    assert status.resolvable is True


def test_resolve_connector_kwargs_produces_a_live_connector_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the resolved kwargs are exactly what ``for_endpoint`` accepts, and
    the resulting object holds the real, usable credential -- the DoD's "a reference
    resolves to a live credential for connector setup", proven, not asserted."""
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )
    config = _ExampleConnectorConfig.for_endpoint(ref.locator, **kwargs)

    assert config.base_url == "https://acme.eu.qlikcloud.com"
    assert config.client_secret.get_secret_value() == _SENTINEL


def test_resolution_depends_only_on_the_locator_not_any_endpoint_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciliation this module makes: ``secret_ref``'s locator is the
    environment-variable prefix, decoupled from whatever an operator names the
    endpoint (``EndpointRow.name``). ``resolve_connector_kwargs`` never receives an
    endpoint name at all -- only the parsed reference -- so two differently-named
    endpoints sharing one ``secret_ref`` must resolve the same credential.
    """
    monkeypatch.setenv("SHARED_TENANT__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:SHARED_TENANT")

    kwargs_for_acme = resolve_connector_kwargs(ref, _ExampleConnectorConfig, settings={"base_url": "https://a"})
    kwargs_for_other = resolve_connector_kwargs(ref, _ExampleConnectorConfig, settings={"base_url": "https://b"})

    assert kwargs_for_acme["client_secret"].get_secret_value() == _SENTINEL
    assert kwargs_for_other["client_secret"].get_secret_value() == _SENTINEL


def test_resolve_connector_kwargs_raises_secret_not_found_when_env_var_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QLIK_ACME__CLIENT_SECRET", raising=False)
    ref = SecretRef.parse("env:QLIK_ACME")

    with pytest.raises(SecretNotFoundError) as excinfo:
        resolve_connector_kwargs(ref, _ExampleConnectorConfig, settings={"base_url": "https://a"})

    assert excinfo.value.endpoint == "QLIK_ACME"
    assert excinfo.value.key == "client_secret"
    assert excinfo.value.backend == "environment"
    assert "QLIK_ACME__CLIENT_SECRET" in str(excinfo.value)
    assert _SENTINEL not in str(excinfo.value)


def test_resolve_connector_kwargs_error_never_echoes_a_sibling_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two secret fields; one is set (to a sentinel), the other is missing. The
    failure raised for the missing one must never mention the *other* field's real
    value -- proving the "no value in any exception message" half of the DoD, not
    just the trivial case where nothing was ever set at all."""
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SIBLING_SENTINEL)
    monkeypatch.delenv("QLIK_ACME__REFRESH_TOKEN", raising=False)

    class _TwoSecretFieldsConfig(ConnectorConfig):
        base_url: str = "https://a"
        client_secret: SecretStr
        refresh_token: SecretBytes

    ref = SecretRef.parse("env:QLIK_ACME")

    with pytest.raises(SecretNotFoundError) as excinfo:
        resolve_connector_kwargs(ref, _TwoSecretFieldsConfig)

    assert excinfo.value.key == "refresh_token"
    assert _SIBLING_SENTINEL not in str(excinfo.value)


def test_resolve_connector_kwargs_rejects_a_reference_with_an_unsupported_scheme() -> None:
    # Bypass SecretRef.parse (which already rejects this) to prove the resolution
    # function itself is defensive, not merely relying on callers to have parsed.
    ref = SecretRef(scheme="vault", locator="kv/qlabs/qlik-acme")

    with pytest.raises(SecretRefFormatError):
        resolve_connector_kwargs(ref, _ExampleConnectorConfig)


# --------------------------------------------------------------------------------------
# resolve_status -- reports resolvable/unresolvable, never a value, never raises
# --------------------------------------------------------------------------------------


def test_resolve_status_reports_resolvable_without_returning_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    status = resolve_status(ref, _ExampleConnectorConfig)

    assert status.resolvable is True
    assert _SENTINEL not in status.reason


def test_resolve_status_reports_unresolvable_and_names_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QLIK_ACME__CLIENT_SECRET", raising=False)
    ref = SecretRef.parse("env:QLIK_ACME")

    status = resolve_status(ref, _ExampleConnectorConfig)

    assert status.resolvable is False
    assert "QLIK_ACME__CLIENT_SECRET" in status.reason
    assert _SENTINEL not in status.reason


def test_resolve_status_never_raises_for_an_unsupported_scheme() -> None:
    ref = SecretRef(scheme="vault", locator="kv/qlabs/qlik-acme")

    status = resolve_status(ref, _ExampleConnectorConfig)

    assert status.resolvable is False
    assert "vault" in status.reason


def test_resolve_status_is_resolvable_when_the_connector_declares_no_secret_fields() -> None:
    ref = SecretRef.parse("env:QLIK_ACME")

    status = resolve_status(ref, _NoSecretsConnectorConfig)

    assert status.resolvable is True


# --------------------------------------------------------------------------------------
# The dishonest case: SecretResolveStatus cannot be widened into a value carrier
# --------------------------------------------------------------------------------------


def test_secret_resolve_status_field_set_cannot_carry_a_value() -> None:
    """A future change that adds e.g. ``value: SecretStr | None`` to
    ``SecretResolveStatus`` must fail this test, not slide through unnoticed."""
    field_names = {f.name for f in dataclasses.fields(SecretResolveStatus)}
    field_types = {f.name: f.type for f in dataclasses.fields(SecretResolveStatus)}

    assert field_names == {"resolvable", "reason"}
    assert field_types == {"resolvable": "bool", "reason": "str"}


def test_secret_resolve_status_is_frozen() -> None:
    status = SecretResolveStatus(resolvable=True, reason="ok")

    with pytest.raises(dataclasses.FrozenInstanceError):
        status.reason = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# No secret value survives repr / str / serialization of anything this module returns
# --------------------------------------------------------------------------------------


def test_no_returned_object_serializes_the_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )
    config = _ExampleConnectorConfig.for_endpoint(ref.locator, **kwargs)
    status = resolve_status(ref, _ExampleConnectorConfig)

    surfaces = [
        repr(ref),
        str(ref),
        repr(status),
        str(status),
        json.dumps(dataclasses.asdict(status), default=str),
        json.dumps(dataclasses.asdict(ref), default=str),
        repr(kwargs),
        str(kwargs),
        json.dumps(kwargs, default=str),
        repr(config),
        str(config),
        json.dumps(config.model_dump(), default=str),
        config.model_dump_json(),
    ]

    for surface in surfaces:
        assert _SENTINEL not in surface, surface


# --------------------------------------------------------------------------------------
# No secret value survives the engine's real structlog processor chain
# --------------------------------------------------------------------------------------


def test_a_resolved_secret_never_appears_in_real_log_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uses the engine's *real* redaction processors (``qlabs_catalog_sync.
    observability.REDACTION_TEST_PROCESSORS`` -- the same chain ``configure_logging``
    wires for the whole process, T2.7), not a stand-in for them, per
    ``tests/observability/test_redaction_and_context_emit.py``'s own convention."""
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    ref = SecretRef.parse("env:QLIK_ACME")

    kwargs = resolve_connector_kwargs(
        ref, _ExampleConnectorConfig, settings={"base_url": "https://acme.eu.qlikcloud.com"}
    )
    status = resolve_status(ref, _ExampleConnectorConfig)

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        get_logger().info(
            "endpoint secret resolved",
            endpoint="qlik_acme",
            ref=ref,
            status=status,
            resolved_kwargs=kwargs,
        )

    (entry,) = entries
    assert _SENTINEL not in str(entry)
    assert entry["resolved_kwargs"]["client_secret"] != _SENTINEL
    assert entry["status"] == status  # the status object itself, untouched -- it never held a value


def test_secret_not_found_failure_path_never_appears_in_log_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unresolvable path (DoD: "still does not raise or leak" from the caller's
    point of view, once turned into a status) logged end to end."""
    monkeypatch.delenv("QLIK_ACME__CLIENT_SECRET", raising=False)
    ref = SecretRef.parse("env:QLIK_ACME")

    status = resolve_status(ref, _ExampleConnectorConfig)
    assert status.resolvable is False

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        get_logger().warning("endpoint secret unresolved", endpoint="qlik_acme", status=status)

    (entry,) = entries
    assert _SENTINEL not in str(entry)
    assert "QLIK_ACME__CLIENT_SECRET" in entry["status"].reason


# --------------------------------------------------------------------------------------
# secret_field_names is the ONE reflection both the resolver and the config service use
# --------------------------------------------------------------------------------------


def test_secret_field_names_matches_what_resolution_actually_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exported reflection names exactly the fields resolution populates.

    T10.3's configuration service refuses an endpoint ``settings`` key that names a
    connector secret field (a credential smuggled into the one column decision C2
    promises can never hold one), and it decides *which* keys those are by calling
    :func:`secret_field_names`. This module decides which fields to *resolve* from a
    :class:`SecretRef` using the same function. The two answers must be the same set:
    if resolution read a field the service did not refuse, a credential could be
    accepted into ``settings`` for a field also read from the environment.

    This test is the probe that keeps them joined. It fails if the reflection and the
    resolver ever come apart -- which is exactly what happened while the reflection was
    private and T10.3 carried its own copy.
    """
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", _SENTINEL)
    monkeypatch.setenv("QLIK_ACME__REFRESH_TOKEN", _SIBLING_SENTINEL)

    declared = secret_field_names(_ExampleConnectorConfig)
    resolved = resolve_connector_kwargs(
        SecretRef.parse("env:QLIK_ACME"),
        _ExampleConnectorConfig,
        settings={"base_url": "https://acme.example"},
    )

    # every declared secret field was populated by resolution ...
    assert declared <= set(resolved)
    # ... and resolution added nothing beyond the declared secrets and the settings.
    assert set(resolved) - {"base_url"} == declared
    # a plain, non-secret field is never mistaken for a secret one.
    assert "base_url" not in declared


def test_secret_field_names_ignores_plain_fields_and_finds_optional_ones() -> None:
    """A field is secret because of its declared type, not its name or its required-ness."""
    declared = secret_field_names(_ExampleConnectorConfig)
    assert "client_secret" in declared  # required SecretStr
    assert "refresh_token" in declared  # optional SecretBytes | None
    assert "base_url" not in declared  # plain str, however credential-ish it looked
