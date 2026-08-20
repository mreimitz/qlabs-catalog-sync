"""``QlikWriter.update`` — what it refuses, and that it refuses before touching the wire.

The closed eight-path enum is the whole safety story of this method, so every refusal is
asserted together with **zero requests**: a field the manifest declares ``ro``/``na``; a
field whose native path is outside the enum (``status`` -> the activate action, D7;
``placement`` -> the move action); a diff that would need more than 8 operations; and a
value that is not the JSON projection of the neutral field it names. Also covers the HTTP
classification the update path inherits from T3.1 — 403 -> ``AuthError``, 429 retried.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConnectorError,
    TransientError,
)
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import (
    DataProductStatus,
    FieldChange,
    FieldDiff,
    Tag,
    TextField,
)
from qlabs_connector_qlik.write import (
    MAX_UPDATE_OPERATIONS,
    PATCH_PATH_FOR_FIELD,
    QlikWriter,
)

from .conftest import (
    PRODUCT_URL,
    SPACE_ID,
    change,
    diff,
    mock_datasets_by_name,
    mock_patch,
    mock_users,
    owner,
    patch_body,
    product_ref,
    refs,
)

#: A manifest that declares every neutral data-product field writable through the PATCH
#: endpoint and widens the path enum to include ``/spaceId``. Used to reach the code paths
#: the real (honest) manifest can never reach, so the guards are provably guards and not
#: dead code that happens never to run.
PERMISSIVE_MANIFEST = CapabilityManifest(
    entities={
        EntityType.DATA_PRODUCT: EntityCapability(
            identity_keys=["id"],
            fields={
                name: FieldCapability.rw(writable_via="rest-patch", partial_update=False)
                for name in (
                    "name",
                    "description",
                    "documentation",
                    "status",
                    "owners",
                    "tags",
                    "dataset_refs",
                    "glossary_term_refs",
                    "placement",
                )
            },
            allowed_update_paths=[*PATCH_PATH_FOR_FIELD.values()],
            max_update_operations=8,
        )
    }
)


async def test_a_field_declared_ro_is_refused_with_no_request_issued(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``placement`` is ``ro`` in the real manifest — Qlik moves spaces by action, not PATCH."""
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(product_ref(), diff(change("placement", "other-space")))

    assert excinfo.value.field == "placement"
    assert excinfo.value.capability_mode == "ro"
    assert excinfo.value.operation == "update"
    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 0


async def test_a_field_declared_na_is_refused_with_no_request_issued(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D5: ``glossary_term_refs`` is ``na``, even though ``/glossaryIds`` is in the enum."""
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(), diff(change("glossary_term_refs", refs(2)))
        )

    assert excinfo.value.field == "glossary_term_refs"
    assert excinfo.value.capability_mode == "na"
    assert len(respx_mock.calls) == 0


async def test_a_field_the_manifest_never_mentions_is_refused_as_na(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Silence is never permission — an undeclared field is treated exactly like ``na``."""
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(),
            FieldDiff(
                entity_type=EntityType.DATA_PRODUCT,
                changes=[FieldChange(field="classifications", value=["pii"])],
            ),
        )

    assert excinfo.value.capability_mode == "na"
    assert len(respx_mock.calls) == 0


async def test_status_is_refused_because_activation_is_an_action_not_a_patch_path(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D7: activation is opt-in and is the ``/actions/activate`` endpoint's job (T3.7)."""
    mock_patch(respx_mock)
    activate = respx_mock.post(f"{PRODUCT_URL}/actions/activate")
    writer = make_writer(manifest=PERMISSIVE_MANIFEST)

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(), diff(change("status", DataProductStatus.ACTIVE))
        )

    assert excinfo.value.field == "status"
    assert excinfo.value.operation == "update"
    assert "decision D7" in str(excinfo.value)
    assert not activate.called
    assert len(respx_mock.calls) == 0


async def test_a_writable_field_mapping_outside_the_enum_is_refused(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``placement`` maps to ``/spaceId``, which Qlik's PATCH enum does not contain.

    Declared ``rw`` here so the manifest gate lets it through and the *enum* check is the
    thing being tested, rather than the ``ro`` check standing in for it.
    """
    mock_patch(respx_mock)
    narrowed = PERMISSIVE_MANIFEST.model_copy(deep=True)
    narrowed.entities[EntityType.DATA_PRODUCT].allowed_update_paths = [
        path for path in PATCH_PATH_FOR_FIELD.values() if path != "/spaceId"
    ]
    writer = make_writer(manifest=narrowed)

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(product_ref(), diff(change("placement", SPACE_ID)))

    assert excinfo.value.field == "placement"
    assert "/spaceId" in str(excinfo.value)
    assert "outside the closed path enum" in str(excinfo.value)
    assert len(respx_mock.calls) == 0


async def test_a_path_inside_the_enum_with_no_value_builder_is_refused_not_invented(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A manifest can widen the enum; it cannot conjure a Qlik glossary-id resolver (D5)."""
    mock_patch(respx_mock)
    writer = make_writer(manifest=PERMISSIVE_MANIFEST)

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(), diff(change("glossary_term_refs", refs(1)))
        )

    assert excinfo.value.field == "glossary_term_refs"
    assert "decision D5" in str(excinfo.value)
    assert len(respx_mock.calls) == 0


async def test_a_diff_for_the_wrong_entity_type_is_refused(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    writer = make_writer()
    mismatched = FieldDiff(
        entity_type=EntityType.DATASET,
        changes=[FieldChange(field="name", value="orders")],
    )

    with pytest.raises(CapabilityError):
        await writer.update(product_ref(), mismatched)

    assert len(respx_mock.calls) == 0


async def test_updating_a_dataset_is_refused_because_every_dataset_field_is_read_only(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2 again, from the update side: this connector resolves datasets, never writes them."""
    mock_patch(respx_mock)
    writer = make_writer()
    ref = product_ref(entity_type=EntityType.DATASET, native_key="ds-orders")

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            ref,
            FieldDiff(
                entity_type=EntityType.DATASET,
                changes=[FieldChange(field="name", value="orders")],
            ),
        )

    assert excinfo.value.entity_type == EntityType.DATASET.value
    assert excinfo.value.operation == "update"
    assert len(respx_mock.calls) == 0


async def test_a_diff_over_the_operation_cap_is_refused_rather_than_split(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Splitting would ship a second batch guarded by an ETag the first batch killed."""
    mock_patch(respx_mock)
    # A cap of 2 makes the refusal reachable; with the real 8-path enum the neutral model
    # can only ever reach 7, so the guard is otherwise unreachable by construction.
    capped = PERMISSIVE_MANIFEST.model_copy(deep=True)
    capped.entities[EntityType.DATA_PRODUCT].max_update_operations = 2
    writer = make_writer(manifest=capped)

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(),
            diff(
                change("name", "Renamed"),
                change("description", TextField.plain("desc")),
                change("tags", [Tag(key="sales")]),
            ),
        )

    message = str(excinfo.value)
    assert "needs 3 JSON Patch operations" in message
    assert "at most 2" in message
    assert "refuses rather than splitting" in message
    assert len(respx_mock.calls) == 0


async def test_a_diff_exactly_at_the_operation_cap_is_sent_as_one_request(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The cap is a limit, not a threshold: 8 operations still go out in one PATCH."""
    mock_patch(respx_mock)
    capped = PERMISSIVE_MANIFEST.model_copy(deep=True)
    capped.entities[EntityType.DATA_PRODUCT].max_update_operations = 2
    writer = make_writer(manifest=capped)

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed"), change("description", TextField.plain("desc"))),
    )

    assert len(patch_body(respx_mock)) == 2
    assert result.outcome is WriteOutcome.UPDATED


async def test_the_real_manifest_declares_the_documented_eight_operation_cap(
    make_writer: Callable[..., QlikWriter],
) -> None:
    """The wire fact behind the guard, read off the manifest the connector actually ships."""
    writer = make_writer()
    capability = writer.manifest.entity_capability(EntityType.DATA_PRODUCT)

    assert capability is not None
    assert capability.max_update_operations == MAX_UPDATE_OPERATIONS == 8
    assert len(capability.allowed_update_paths or []) == 8


async def test_api_consumable_refs_are_refused_without_a_dataset_refs_change(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Qlik's subset rule cannot be honored against target state this connector never read."""
    mock_patch(respx_mock)
    member = refs(1)[0]
    writer = make_writer(identity_map={member: "ds-customers"})

    with pytest.raises(CapabilityError) as excinfo:
        await writer.update(
            product_ref(),
            diff(change("name", "Renamed")),
            api_consumable_refs=[member],
        )

    assert "/apiConsumableDatasetIds" in str(excinfo.value)
    assert len(respx_mock.calls) == 0


async def test_api_consumable_refs_ride_along_with_the_dataset_ids_replace(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Both operations in one PATCH, with the subset enforced against what is sent."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    kept, dropped = refs(2)
    writer = make_writer(identity_map={kept: "ds-kept"})

    result = await writer.update(
        product_ref(),
        diff(change("dataset_refs", [kept, dropped])),
        api_consumable_refs=[kept, dropped],
    )

    body = patch_body(respx_mock)
    assert body == [
        {"op": "replace", "path": "/datasetIds", "value": ["ds-kept"]},
        {"op": "replace", "path": "/apiConsumableDatasetIds", "value": ["ds-kept"]},
    ]
    assert result.detail is not None
    assert "apiConsumableDatasetIds" in result.detail


async def test_a_blank_name_replacement_is_refused_before_any_request(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Name is Qlik's only required field — a sync must never blank it."""
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(ConnectorError) as excinfo:
        await writer.update(product_ref(), diff(change("name", "   ")))

    assert "cannot be blanked" in str(excinfo.value)
    assert len(respx_mock.calls) == 0


async def test_a_null_name_replacement_is_refused(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(ConnectorError):
        await writer.update(product_ref(), diff(change("name", None)))

    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("description", 17),
        ("tags", "sales"),
        ("tags", [{"not_a": "tag"}]),
        ("owners", {"userId": "user-ada"}),
        ("owners", [{"role": "owner"}]),
        ("dataset_refs", "one-id"),
        ("dataset_refs", [42]),
    ],
)
async def test_a_value_that_is_not_the_neutral_json_projection_is_refused(
    respx_mock: object,
    make_writer: Callable[..., QlikWriter],
    field_name: str,
    value: object,
) -> None:
    """A malformed diff value is a caller bug, not something to coerce into a write."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {})
    writer = make_writer()

    with pytest.raises(ConnectorError):
        await writer.update(
            product_ref(),
            FieldDiff(
                entity_type=EntityType.DATA_PRODUCT,
                changes=[FieldChange(field=field_name, value=value)],  # type: ignore[arg-type]
            ),
        )

    assert not any(call.request.method == "PATCH" for call in respx_mock.calls)


async def test_a_dataset_ref_that_is_not_a_neutral_id_is_refused(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``dataset_refs`` are engine neutral ids; a raw Qlik id must never be sent through."""
    mock_patch(respx_mock)
    writer = make_writer()

    with pytest.raises(ConnectorError) as excinfo:
        await writer.update(
            product_ref(),
            FieldDiff(
                entity_type=EntityType.DATA_PRODUCT,
                changes=[FieldChange(field="dataset_refs", value=["6672d8b7a182224cbb3f1c26"])],
            ),
        )

    assert "not a neutral entity id" in str(excinfo.value)
    assert not any(call.request.method == "PATCH" for call in respx_mock.calls)


async def test_a_403_raises_auth_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02 section 5: updating needs write permission in the product's space."""
    respx_mock.patch(PRODUCT_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    writer = make_writer()

    with pytest.raises(AuthError) as excinfo:
        await writer.update(product_ref(), diff(change("name", "Renamed")))

    assert excinfo.value.entity_type == EntityType.DATA_PRODUCT.value
    assert excinfo.value.retryable is False
    # A permission problem is never retried by HttpEndpoint — exactly one attempt.
    assert len(respx_mock.calls) == 1


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.patch(PRODUCT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(204),
        ]
    )
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed")))

    assert len(respx_mock.calls) == 3
    assert result.outcome is WriteOutcome.UPDATED
    # Every attempt carried the same precondition — a retry is not an unguarded resend.
    assert all(call.request.headers["if-match"] for call in respx_mock.calls)


async def test_a_5xx_that_outlives_the_retries_raises_transient_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.patch(PRODUCT_URL).mock(return_value=httpx.Response(503))
    writer = make_writer()

    with pytest.raises(TransientError):
        await writer.update(product_ref(), diff(change("name", "Renamed")))

    assert len(respx_mock.calls) == 3


async def test_a_404_raises_not_found_rather_than_a_conflict(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The 412 interception must not swallow the statuses ``auth.py`` already classifies."""
    from qlabs_catalog_sync_sdk.exceptions import NotFound

    respx_mock.patch(PRODUCT_URL).mock(return_value=httpx.Response(404))
    writer = make_writer()

    with pytest.raises(NotFound):
        await writer.update(product_ref(), diff(change("name", "Renamed")))


async def test_an_unmatched_owner_never_reaches_the_detail_as_an_email(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Same secrecy rule as create: a run report carries no owner email address."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(
            change(
                "owners",
                [
                    owner("ada@acme.example", display_name="Ada Lovelace"),
                    owner("ghost@acme.example", display_name="Grace Ghost"),
                ],
            )
        ),
    )

    assert result.detail is not None
    assert "Grace Ghost (not_found)" in result.detail
    assert "ghost@acme.example" not in result.detail
    assert "ada@acme.example" not in result.detail
