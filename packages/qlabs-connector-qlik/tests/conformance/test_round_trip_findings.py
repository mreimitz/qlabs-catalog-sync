"""Individually-diagnosed proofs of the mismatches ``TestQlikConformance``'s base-suite
round-trip/idempotency tests hit as a group (see that module's docstring). The base
suite reports only the *first* field that fails in each affected check, and every one of
them failed on the alphabetically-first writable field, ``dataset_refs``, so that loop
never got far enough to show the other independently real mismatches. Every test below
isolates one field or behavior with its own realistic setup, so each finding stands on
its own evidence rather than being inferred from one shared failure.

**Two of these findings were real connector defects and are now fixed.** Findings 1 and 2
have been rewritten to pin the *fixed* behavior, not the broken behavior they originally
documented — the point of pinning a defect is to notice when it comes back, which an
assertion still describing the bug cannot do. Neither the base suite nor any check here
was loosened to achieve that; what changed is the connector:

* **Finding 1 — ``read()`` never reported ``dataset_refs``,** though the manifest
  declares the field ``rw``. ``read.py`` now maps Qlik's native ``datasetIds`` back
  through an injected reverse identity seam (its module docstring, point 4). Both halves
  are pinned below: it round-trips when the seam is wired, and it is *not reported at
  all* — rather than reported empty — when it is not.
* **Finding 2 — ``update()`` was not idempotent.** ``write.py`` now issues one Tier-1
  ``GET`` before every PATCH and drops operations the product already carries (its module
  docstring, point 12a), so a byte-identical replay is a ``NO_OP`` with no write at all.

Findings 3 to 5 are **not** defects and are left exactly as they were: Qlik's ``readMe``
has no format concept, the base suite's synthetic owner sample carries no email, and
decision D7 makes activation opt-in. Each is a documented consequence, and each is proven
here by a passing assertion rather than asserted from reading the source — the point of a
conformance kit is to catch the gap between what the code appears to do and what it
verifiably does.
"""

from __future__ import annotations

import uuid

import respx

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    Party,
    PartyRole,
    TextField,
)

from .conftest import FakeQlikTenant, build_connector, register_routes, setup_connector

# --------------------------------------------------------------------------------------
# Finding 1 (was a defect, now fixed): dataset_refs is written by create()/update() and
# read back by read() — through the reverse identity seam, and only when that seam has an
# answer. Both halves are pinned: the round trip, and the honest silence without it.
# --------------------------------------------------------------------------------------


async def test_dataset_refs_round_trips_when_the_reverse_identity_seam_is_wired() -> None:
    """The fix for the original finding: ``read.py``'s ``_map_data_product`` now sets
    ``DataProduct.dataset_refs`` from the raw ``datasetIds`` it already had in hand,
    mapping each native id back through ``read.DatasetRefLookup`` — the mirror of the
    forward ``DatasetIdentityLookup`` ``resolve.py`` documents, defaulting to "no answer"
    the same way and answered by the same engine-owned IdentityMap. The manifest declares
    ``dataset_refs`` ``rw``; this is what makes that declaration true.
    """
    tenant = FakeQlikTenant()
    tenant.seed_dataset(name="Orders", resource_id="ds-orders-1")
    member = uuid.uuid4()

    async def name_lookup(neutral_id: uuid.UUID) -> str | None:
        del neutral_id
        return "Orders"

    async def ref_lookup(native_id: str) -> uuid.UUID | None:
        return member if native_id == "ds-orders-1" else None

    async with setup_connector(
        tenant=tenant, dataset_name_lookup=name_lookup, dataset_ref_lookup=ref_lookup
    ) as connector:
        product = DataProduct(name="Sales", dataset_refs=[member])

        created = await connector.create(product)
        assert created.outcome is WriteOutcome.CREATED
        # Confirm the write genuinely landed at the wire, not just "was requested":
        assert "dataset_refs" in created.written_fields
        assert "dataset_refs" not in created.skipped_fields

        read_back = await connector.read(created.ref)

        assert read_back.dataset_refs == [member]
        assert "dataset_refs" in read_back.field_envelopes
        # The raw native ids stay recoverable too: the mapping is lossy by construction
        # (an unbound id has no neutral counterpart), so 'datasetIds' is deliberately
        # *not* excluded from custom_attributes -- read.py's point 3 exclusion policy.
        assert read_back.custom_attributes["datasetIds"] == ["ds-orders-1"]


async def test_dataset_refs_is_not_reported_at_all_when_the_seam_has_no_answer() -> None:
    """The other half, and the reason this is not simply "report whatever resolved".

    With no reverse binding, the connector cannot state this product's membership. It
    therefore does not report the field at all — no value, no ``FieldEnvelope`` — rather
    than reporting ``[]``, which would assert "this product has no datasets" and is
    exactly the invention decision D2 forbids (and exactly the claim ``write.py`` refuses
    to *send* for the same reason, its point 10). The raw ids still travel in
    ``custom_attributes``, so nothing is lost, only left unclaimed.
    """
    tenant = FakeQlikTenant()
    tenant.seed_dataset(name="Orders", resource_id="ds-orders-1")

    async def name_lookup(neutral_id: uuid.UUID) -> str | None:
        del neutral_id
        return "Orders"

    # dataset_ref_lookup deliberately left at the connector's default: no binding known.
    async with setup_connector(tenant=tenant, dataset_name_lookup=name_lookup) as connector:
        created = await connector.create(DataProduct(name="Sales", dataset_refs=[uuid.uuid4()]))
        assert "dataset_refs" in created.written_fields

        read_back = await connector.read(created.ref)

        assert read_back.dataset_refs == []
        assert "dataset_refs" not in read_back.field_envelopes
        assert read_back.custom_attributes["datasetIds"] == ["ds-orders-1"]


# --------------------------------------------------------------------------------------
# Finding 2 (was a defect, now fixed): update() detects, before writing, that the product
# already carries every value the diff asks for. Shown with `name`, the cleanest possible
# field (no resolver, no lifecycle action, no format ambiguity), so nothing about this is
# a dataset_refs artifact.
# --------------------------------------------------------------------------------------


async def test_replaying_an_already_current_name_is_a_no_op_and_issues_no_patch(
    respx_mock: respx.MockRouter,
) -> None:
    """The general form of what the base suite's ``test_reapplying_an_unchanged_diff_is_a_
    no_op`` exercises via ``dataset_refs``. ``write.py`` used to have no read-before-write
    comparison outside ``_recover_from_conflict``'s post-412 ``_already_applied``, so
    calling ``update()`` twice with the literal same desired value sent the PATCH twice
    and reported ``UPDATED`` twice. It now runs that same comparison proactively (module
    docstring, point 12a): the replay costs one Tier-1 ``GET`` and issues no write.

    Built on :func:`~.conftest.build_connector`'s single router rather than
    :func:`~.conftest.setup_connector`'s nested one precisely so "no PATCH was sent" is a
    reliable count of real requests and not a respx routing artifact — see
    ``conftest.setup_connector``'s docstring for that caveat.
    """
    tenant = FakeQlikTenant()
    register_routes(respx_mock, tenant)
    connector = await build_connector()
    try:
        created = await connector.create(DataProduct(name="Original Name"))
        assert created.outcome is WriteOutcome.CREATED

        diff = FieldDiff(
            entity_type=created.ref.entity_type,
            changes=[
                FieldChange(field="name", mode=FieldUpdateMode.PATCH, value="New Name"),
            ],
            expected_revision=created.source_revision,
        )
        first = await connector.update(created.ref, diff)
        assert first.outcome is WriteOutcome.UPDATED

        replay = FieldDiff(
            entity_type=created.ref.entity_type,
            changes=[
                FieldChange(field="name", mode=FieldUpdateMode.PATCH, value="New Name"),
            ],
            expected_revision=first.source_revision,
        )
        patches_before = _patch_count(respx_mock)
        second = await connector.update(created.ref, replay)

        assert second.outcome is WriteOutcome.NO_OP
        assert second.written_fields == []  # a no-op that claimed a field would not validate
        assert second.detail is not None and "/name" in second.detail
        # No PATCH at all -- not "a PATCH that happened to change nothing".
        assert _patch_count(respx_mock) == patches_before
        # And the product's revision did not move, which is the check that would catch a
        # connector reporting NO_OP while still writing.
        assert second.source_revision == first.source_revision
    finally:
        await connector.close()


def _patch_count(respx_mock: respx.MockRouter) -> int:
    return sum(1 for call in respx_mock.calls if call.request.method == "PATCH")


# --------------------------------------------------------------------------------------
# Finding 3: `documentation` survives its *text* but not its *format* through Qlik's
# `readMe`. Not a bug in the sense of losing data -- Qlik's readMe has no format concept
# at all, it is simply always markdown -- but it means a TextField.plain documentation
# value never round-trips byte-identically, which is exactly the kind of thing this
# task exists to surface plainly rather than leave implicit.
# --------------------------------------------------------------------------------------


async def test_documentation_format_is_always_markdown_on_read_regardless_of_what_was_sent() -> (
    None
):
    async with setup_connector() as connector:
        product = DataProduct(name="Sales", documentation=TextField.plain("Plain body text."))
        created = await connector.create(product)
        assert "documentation" in created.written_fields

        read_back = await connector.read(created.ref)

        assert read_back.documentation is not None
        assert read_back.documentation.text == "Plain body text.", "the text itself is preserved"
        assert read_back.documentation.format.value == "markdown", (
            "read.py unconditionally maps Qlik's 'readMe' back as TextFormat.MARKDOWN "
            "(module docstring point 2's ETag reasoning has no equivalent note for this), "
            "so a TextField.plain(...) documentation value round-trips its text but not "
            "its declared format -- see the report"
        )
        assert to_json_value(product.documentation) != to_json_value(read_back.documentation)


# --------------------------------------------------------------------------------------
# Finding 4 (positive proof): owners DOES round-trip correctly when the Party actually
# carries an email -- contrasting with the base suite's synthetic sample, which never
# does (samples.py has no email generator), so the base suite's round-trip check can
# never exercise a real keyContacts write for this or any email-matched connector.
# --------------------------------------------------------------------------------------


async def test_owners_round_trips_when_the_party_carries_an_email() -> None:
    """A ``Party`` with an email genuinely reaches ``keyContacts`` and reads back with
    the resolved Qlik ``userId`` -- proving D3 works, not just that it refuses honestly.

    One more asymmetry worth naming precisely, surfaced by this very assertion rather
    than asserted from reading the source: ``read.py``'s ``_data_product_owners`` also
    turns the product's own ``ownerId`` (the Qlik-assigned creator -- here, this fake
    tenant's ``self.owner_id``, standing in for whatever service account authenticated
    the ``create()`` call) into a second, implicit ``OWNER``-role ``Party`` that the
    caller never wrote and has no way to influence via ``keyContacts`` at all. So even a
    correctly-resolved owner list never round-trips to *exactly* what was sent -- it
    round-trips to that plus one extra entry Qlik itself always contributes.
    """
    tenant = FakeQlikTenant()
    tenant.seed_user(email="steward@acme.example", user_id="user-conformance-1")

    async with setup_connector(tenant=tenant) as connector:
        owner = Party(email="steward@acme.example", role=PartyRole.OWNER)
        created = await connector.create(DataProduct(name="Sales", owners=[owner]))
        assert "owners" in created.written_fields
        assert "owners" not in created.skipped_fields

        read_back = await connector.read(created.ref)

        party_ids = {party.party_id for party in read_back.owners}
        assert party_ids == {"user-conformance-1", tenant.owner_id}, (
            "the caller-supplied owner resolves and round-trips, but the product's own "
            "Qlik-assigned ownerId always joins it as a second, caller-uncontrolled entry"
        )


async def test_owners_with_no_email_are_reported_unmatched_not_silently_dropped() -> None:
    """The base suite's actual sample shape (``samples.py``: ``Party(display_name=...,
    role=...)``, no email) -- proving D3's refusal is honest (reported, zero HTTP for the
    lookup it can't make), not merely "the round trip happens to fail.\""""
    async with setup_connector() as connector:
        owner = Party(display_name="Conformance Owner", role=PartyRole.OWNER)
        created = await connector.create(DataProduct(name="Sales", owners=[owner]))

        assert "owners" in created.skipped_fields
        assert created.detail is not None and "no_email" in created.detail


# --------------------------------------------------------------------------------------
# Finding 5: status is never applied at create, whatever the neutral value asks for --
# decision D7, already documented as deliberate; pinned here as a concrete proof rather
# than left as something the round-trip loop would have surfaced had it reached this far.
# --------------------------------------------------------------------------------------


async def test_status_active_at_create_is_skipped_and_reads_back_as_draft() -> None:
    async with setup_connector() as connector:
        product = DataProduct(name="Sales", status=DataProductStatus.ACTIVE)
        created = await connector.create(product)

        assert "status" in created.skipped_fields
        assert created.detail is not None and "decision D7" in created.detail

        read_back = await connector.read(created.ref)
        assert read_back.status is DataProductStatus.DRAFT


async def test_activation_is_refused_without_the_opt_in_even_though_status_asked_for_it() -> None:
    """The other half of D7: this connector cannot even reach ``/actions/activate`` on a
    default-constructed instance to make the skipped status above happen later, either."""
    async with setup_connector() as connector:
        created = await connector.create(DataProduct(name="Sales"))
        assert connector.lifecycle is not None
        try:
            await connector.lifecycle.activate(
                created.ref, name="Sales", managed_space_id="space-managed"
            )
        except CapabilityError:
            pass
        else:
            raise AssertionError("expected activate() to refuse on a default-built connector")
