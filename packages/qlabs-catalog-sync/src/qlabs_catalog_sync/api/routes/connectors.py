"""Connector discovery routes (C6, WP12/T12.3): ``GET {API_PREFIX}/connectors``.

C6 in full: *"'Install an endpoint' means registering an instance of a connector that is
already present. The console lists what entry-point discovery found in the running
image..."* -- this module is exactly that listing and nothing more. It never fetches,
installs or executes anything: :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry`
was already built at process startup (WP2/T2.1, entry-point discovery over the
``qlabs_catalog_sync.connectors`` group), and reading it here is two side-effect-free
operations -- ``registry.names()``/``registry.broken()`` (pure lookups) and, for each
already-loaded connector class, instantiating it (``Connector.__init__`` only checks a
class attribute, per its own docstring) and asking it for its capability manifest.

**A manifest is not always available from an unconfigured connector class, and this
route must not pretend otherwise.** RS-08's connector lifecycle is *discover, configure,
``setup``, then ``capabilities``* -- so a connector whose manifest genuinely varies with
its resolved configuration is entitled to refuse before ``setup()``. The Databricks
connector does exactly that (``qlabs_connector_databricks``: D6 makes ``tags`` readable
only when a SQL warehouse is configured, so there is no honest answer to give yet), and
this route lists connector *classes*, which by definition have no configuration.

Until this was fixed, ``GET /connectors`` raised ``RuntimeError`` straight through the
generic 500 handler on any image with the Databricks connector installed -- which is
every real image, Databricks being the MVP's source. Every test of this route built its
registry from ``FakeConnector``, whose ``capabilities()`` needs no ``setup()``, so
nothing caught it. ``tests/api/test_connectors_real_registry.py`` is the regression
test, and it drives the **real** entry-point registry rather than a fake, precisely
because a fake is what hid this. A connector that cannot answer yet is reported as
``available`` with ``manifest`` unset and ``manifest_unavailable_reason`` explaining
why -- never as broken (it loaded fine) and never as a 500.

**Broken connectors are listed, not hidden.** ``qlabs_catalog_sync.discovery``'s own
module docstring is explicit about why a connector whose entry point failed to load, gate
or name-match is *recorded*, never silently dropped: "a config that actually names a
broken endpoint gets a clear... error distinguishable from 'connector X is not installed
at all' -- which is exactly the distinction an operator needs". The console needs the same
distinction *before* an operator tries to register an endpoint against it, so every
:class:`~qlabs_catalog_sync.discovery.BrokenConnector` entry appears in this list with its
``distribution``/``stage``/``reason``, right alongside the connectors that loaded cleanly.

**The capability manifest is serialized in the shape ``scripts/gen_capability_matrix.py``
already produces** for ``docs/capability-matrix.json`` (``concurrency`` plus, per entity
type in :class:`~qlabs_catalog_sync_sdk.models.EntityType`'s own declaration order,
``supported``/``identity_keys``/``supports_events``/``allowed_update_paths``/
``max_update_operations``/``fields``, each field keyed by name to
``mode``/``writable_via``/``partial_update``/``normalized_by_target``) -- deliberately
reused rather than reinvented, so the console and that generated file never describe one
manifest two different ways. That script is a standalone build tool outside this package's
import graph, so the shape is reproduced here structurally (same field names, same
per-entity-type ordering) rather than imported from it.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, JsonValue

from qlabs_catalog_sync.configstore.secrets import secret_field_names
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase, Connector
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, ConcurrencyMode, FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import EntityType

from ..errors import API_ERROR_RESPONSES

__all__ = ["CapabilityManifestOut", "build_connectors_router", "capability_manifest_out"]


# --------------------------------------------------------------------------------------
# Response models -- see the module docstring for why this shape mirrors
# scripts/gen_capability_matrix.py's ``_field_to_json``/``_entity_to_json``/
# ``_manifest_to_json`` exactly.
# --------------------------------------------------------------------------------------


class FieldCapabilityOut(BaseModel):
    """One neutral field's write posture at this connector, per
    :class:`~qlabs_catalog_sync_sdk.manifest.FieldCapability`."""

    model_config = ConfigDict(frozen=True)

    mode: FieldCapabilityMode
    writable_via: str | None
    partial_update: bool
    normalized_by_target: bool


class EntityCapabilityOut(BaseModel):
    """One entity type's capability, per
    :class:`~qlabs_catalog_sync_sdk.manifest.EntityCapability`."""

    model_config = ConfigDict(frozen=True)

    supported: bool
    identity_keys: list[str]
    supports_events: bool
    allowed_update_paths: list[str] | None
    max_update_operations: int | None
    fields: dict[str, FieldCapabilityOut]


class CapabilityManifestOut(BaseModel):
    """A connector's whole capability manifest, per
    :class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest`. Keyed by
    :class:`~qlabs_catalog_sync_sdk.models.EntityType` value (``"data_product"``, ...),
    same as ``docs/capability-matrix.json``."""

    model_config = ConfigDict(frozen=True)

    concurrency: ConcurrencyMode
    entities: dict[str, EntityCapabilityOut]


class ConnectorInfo(BaseModel):
    """One entry point discovery found (C6): either a usable connector with its live
    capability manifest, or a broken one with why -- never both, never neither."""

    model_config = ConfigDict(frozen=True)

    name: str
    available: bool
    manifest: CapabilityManifestOut | None = None
    """The connector's live ``capabilities()``. Set only when ``available`` -- and even
    then, ``None`` when the connector cannot describe itself until it is configured, in
    which case ``manifest_unavailable_reason`` says so. See the module docstring."""
    config_schema: dict[str, JsonValue] | None = None
    """The JSON Schema of this connector's own ``ConfigModel`` -- what an endpoint's
    ``settings`` must look like -- with every secret-typed property **removed**, not merely
    flagged. C2: an endpoint holds a named secret *reference*, never a value, and the server
    rejects an inline secret in ``settings`` (``InlineSecretRejectedError``). Shipping a
    schema the console could render a credential input from would invite exactly the shape
    the server refuses, so the stripping happens here rather than being left to every client
    to remember. ``None`` when the connector is unavailable.

    Set only when ``available``. The console generates its settings form from this instead of
    hardcoding a field list per connector, which would be wrong the day a connector changes."""
    config_secret_fields: list[str] = []
    """The property names removed from ``config_schema`` because they are secret-typed
    (pydantic renders a ``SecretStr`` as ``format: password``/``writeOnly``). Named, not
    hidden: an operator needs to know the connector *has* a ``client_secret`` and that it
    comes from the bound secret reference -- otherwise the generated form looks incomplete
    and they go looking for somewhere to type it. Never accompanied by a value."""
    manifest_unavailable_reason: str | None = None
    """Set only when ``available`` and ``manifest`` is ``None``: why this connector
    cannot report a capability manifest from an unconfigured class. Human-readable and
    safe to render as-is. The console must show this rather than an empty manifest --
    "this connector describes itself once configured" and "this connector supports
    nothing" are opposite facts."""
    distribution: str | None = None
    """Set only when not ``available`` -- the installed distribution that claims this
    entry-point name (``qlabs_catalog_sync.discovery.BrokenConnector.distribution``)."""
    broken_stage: str | None = None
    """Set only when not ``available``: ``"load"``, ``"contract"`` or ``"name_mismatch"``
    (``qlabs_catalog_sync.discovery.BrokenConnector.stage``)."""
    broken_reason: str | None = None
    """Set only when not ``available`` -- human-readable, safe to render as-is."""


def capability_manifest_out(manifest: CapabilityManifestBase) -> CapabilityManifestOut:
    """Serialize ``manifest`` in ``gen_capability_matrix.py``'s own shape.

    Public because ``routes/endpoints.py``'s ``/{name}/manifest`` serializes the same
    manifest for a *configured* endpoint -- one serializer, so the two routes can never
    describe one manifest two different ways.

    Every connector in this codebase declares the SDK's concrete
    :class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest` (T1.3) -- the only
    subclass of :class:`~qlabs_catalog_sync_sdk.contract.CapabilityManifestBase` this
    repository defines -- so narrowing to it here is the same assumption
    ``gen_capability_matrix.py`` itself makes, not a new one.
    """
    if not isinstance(manifest, CapabilityManifest):
        raise TypeError(
            f"connector declared a {type(manifest).__name__!r} capability manifest; "
            "this route only knows how to serialize "
            "qlabs_catalog_sync_sdk.manifest.CapabilityManifest"
        )
    return CapabilityManifestOut(
        concurrency=manifest.concurrency,
        entities={
            entity_type.value: EntityCapabilityOut(
                supported=capability.supported,
                identity_keys=list(capability.identity_keys),
                supports_events=capability.supports_events,
                allowed_update_paths=capability.allowed_update_paths,
                max_update_operations=capability.max_update_operations,
                fields={
                    field_name: FieldCapabilityOut(
                        mode=field_capability.mode,
                        writable_via=field_capability.writable_via,
                        partial_update=field_capability.partial_update,
                        normalized_by_target=field_capability.normalized_by_target,
                    )
                    for field_name, field_capability in sorted(capability.fields.items())
                },
            )
            for entity_type in EntityType
            if (capability := manifest.entities.get(entity_type)) is not None
        },
    )


def _property_is_secret_shaped(prop: object) -> bool:
    """Whether one JSON Schema property renders a pydantic secret.

    A required ``SecretStr`` renders flat -- ``{"format": "password", "writeOnly": true}`` --
    but an **optional** one (``SecretStr | None``, which is how a connector spells a
    credential that is only set on one of two auth routes) renders those markers one level
    down, inside ``anyOf``. Checking only the top level therefore missed exactly the fields
    most likely to exist: Databricks' ``client_secret`` and ``token``. Missing one is not a
    cosmetic slip -- the console then renders it as an ordinary settings input, inviting an
    operator to type a credential into a field meant to be stored in the clear.

    So the composition keywords are searched too, and either marker alone counts: being wrong
    in the permissive direction here costs a field moved out of the settings form, and being
    wrong in the other direction costs a credential in plaintext.
    """
    if not isinstance(prop, dict):
        return False
    if prop.get("format") == "password" or prop.get("writeOnly"):
        return True
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = prop.get(keyword)
        if isinstance(branches, list) and any(
            _property_is_secret_shaped(branch) for branch in branches
        ):
            return True
    return False


def _split_secret_properties(
    schema: dict[str, Any],
    declared_secret_fields: frozenset[str] = frozenset(),
) -> tuple[dict[str, JsonValue], list[str]]:
    """Split a ``ConfigModel`` JSON Schema into (schema without secret properties, the names
    removed).

    ``declared_secret_fields`` is the authority:
    :func:`~qlabs_catalog_sync.configstore.secrets.secret_field_names` reads the model's own
    field *types*, which is the same single definition the configuration service refuses
    inline secrets by and the resolver looks up credentials by. Deriving this list a second
    way, from the rendered schema, is how the three could ever disagree -- and a field this
    route failed to call secret while the service still refuses it inline is a form that
    invites input the server will always reject.

    The rendered-schema scan (:func:`_property_is_secret_shaped`) is kept as well, unioned in,
    for a ``ConfigModel`` whose secret-ness is expressed in a way the type walk does not see.
    Two independent detectors, union rather than intersection: the failure this must not have
    is a credential-shaped field left in the settings form.

    ``required`` is filtered to match -- a required property that is not in ``properties``
    would make every generated form permanently invalid.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return cast(dict[str, JsonValue], schema), []

    secret_names = sorted(
        name
        for name, prop in properties.items()
        if name in declared_secret_fields or _property_is_secret_shaped(prop)
    )
    if not secret_names:
        return cast(dict[str, JsonValue], schema), []

    stripped = dict(schema)
    stripped["properties"] = {
        name: prop for name, prop in properties.items() if name not in secret_names
    }
    required = schema.get("required")
    if isinstance(required, list):
        stripped["required"] = [name for name in required if name not in secret_names]
    return cast(dict[str, JsonValue], stripped), secret_names


def _config_schema_for(
    connector_cls: type[Connector],
) -> tuple[dict[str, JsonValue] | None, list[str]]:
    """This connector's settings schema, secret properties removed. Never raises: a connector
    whose ``ConfigModel`` cannot produce a schema simply reports none, exactly as one that
    cannot produce a capability manifest does -- one connector must never take the listing
    down with it."""
    try:
        raw = connector_cls.ConfigModel.model_json_schema()
    except Exception:  # noqa: BLE001 -- a connector's ConfigModel may fail for any reason
        return None, []
    return _split_secret_properties(raw, secret_field_names(connector_cls.ConfigModel))


def _describe_available(registry: ConnectorRegistry, name: str) -> ConnectorInfo:
    """Describe one connector that discovery loaded successfully.

    Asking an unconfigured connector class for its manifest is allowed to fail -- see the
    module docstring. When it does, this reports the connector as ``available`` (it loaded;
    an operator can register an endpoint against it) with no manifest and a reason, rather
    than letting the exception reach the generic 500 handler and taking the whole listing
    down with it. One connector that cannot describe itself must never hide every other
    connector in the image.
    """
    connector_cls = registry.get_connector(name)
    config_schema, secret_fields = _config_schema_for(connector_cls)
    try:
        manifest = connector_cls().capabilities()
    except Exception as exc:  # noqa: BLE001 -- a connector may refuse for any reason
        return ConnectorInfo(
            name=name,
            available=True,
            manifest=None,
            config_schema=config_schema,
            config_secret_fields=secret_fields,
            manifest_unavailable_reason=(
                "this connector reports what it supports only once an endpoint using it "
                "has been configured, because its capabilities depend on that "
                f"configuration. Register an endpoint to see its manifest. ({exc})"
            ),
        )
    return ConnectorInfo(
        name=name,
        available=True,
        manifest=capability_manifest_out(manifest),
        config_schema=config_schema,
        config_secret_fields=secret_fields,
    )


def build_connectors_router(registry: ConnectorRegistry) -> APIRouter:
    """Build the ``/connectors`` router over an already-built ``registry``.

    Mirrors ``api.auth._build_auth_router``'s shape: a factory taking its one
    dependency explicitly, called once from :func:`~qlabs_catalog_sync.api.app.create_app`
    -- never a module-level router reading global state.
    """
    router = APIRouter(prefix="/connectors", tags=["connectors"])

    @router.get(
        "",
        response_model=list[ConnectorInfo],
        responses=API_ERROR_RESPONSES,
        summary="List connectors discovery found in this running image",
    )
    async def list_connectors() -> list[ConnectorInfo]:
        """Everything entry-point discovery found (C6): every successfully registered
        connector with its live capability manifest, plus every entry point discovery
        could not turn into one, with its reason. Installs, fetches and executes
        nothing beyond what process startup already did -- see the module docstring.
        """
        available = [_describe_available(registry, name) for name in registry.names()]
        broken = [
            ConnectorInfo(
                name=broken.name,
                available=False,
                distribution=broken.distribution,
                broken_stage=broken.stage,
                broken_reason=broken.reason,
            )
            for broken in registry.broken().values()
        ]
        return sorted(available + broken, key=lambda info: info.name)

    return router
