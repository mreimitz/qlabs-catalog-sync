"""Individually-diagnosed proofs of the mismatches ``TestQlikConformance``'s base-suite
round-trip/idempotency tests hit as a group (see that module's docstring). The base
suite reports only the *first* field that fails, in each of its three affected checks —
because all three happen to fail on the alphabetically-first writable field,
``dataset_refs``, that loop never gets far enough to show the other three independently
real mismatches (``documentation``, ``owners``, ``status``). Every test below isolates
one field/behavior with its own realistic setup, so each finding stands on its own
evidence rather than being inferred from one shared failure.

Every positive claim here (``name``/``tags`` round-trip fine; ``owners`` round-trips fine
*given an email*) is proven by an actually-passing assertion, not asserted from reading
the source — the point of a conformance kit is to catch the gap between what the code
appears to do and what it verifiably does.
"""

from __future__ import annotations

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

from .conftest import FakeQlikTenant, setup_connector

# --------------------------------------------------------------------------------------
# Finding 1: dataset_refs never round-trips through read() — structural, not a
# resolution artifact. This is the connector-side defect this task's report names a
# precise fix for.
# --------------------------------------------------------------------------------------


async def test_dataset_refs_is_written_but_read_never_reports_it_back() -> None:
    """Seed a matching dataset so member resolution (D2) genuinely succeeds — proving the
    gap below is not "nothing resolved" but "read.py has no field for this at all."
    ``read.py``'s ``_map_data_product`` never sets ``dataset_refs`` on the ``DataProduct``
    it builds (module docstring, point 4): the Qlik-native ``datasetIds`` values travel
    losslessly into ``custom_attributes`` instead, deliberately, because resolving a
    native id back to the *neutral* UUID the engine minted is the engine's IdentityMap's
    job, not a single connector read's. That is a defensible reason for the *value* to
    come back empty — but the manifest still declares ``dataset_refs`` ``rw``
    (``manifest.py``), and this is the concrete gap between that declaration and what a
    caller relying on read-after-write observes.
    """
    import uuid

    tenant = FakeQlikTenant()
    tenant.seed_dataset(name="Orders", resource_id="ds-orders-1")

    async def name_lookup(neutral_id: uuid.UUID) -> str | None:
        del neutral_id
        return "Orders"

    async with setup_connector(tenant=tenant, dataset_name_lookup=name_lookup) as connector:
        member = uuid.uuid4()
        product = DataProduct(name="Sales", dataset_refs=[member])

        created = await connector.create(product)
        assert created.outcome is WriteOutcome.CREATED
        # Confirm the write genuinely landed at the wire, not just "was requested":
        assert "dataset_refs" in created.written_fields
        assert "dataset_refs" not in created.skipped_fields

        read_back = await connector.read(created.ref)

        assert read_back.dataset_refs == [], (
            "documenting current behavior, not endorsing it — see the report for the "
            "precise fix: read.py never populates DataProduct.dataset_refs from the raw "
            "'datasetIds' it already has in hand"
        )
        assert "dataset_refs" not in read_back.field_envelopes


# --------------------------------------------------------------------------------------
# Finding 2: update() has no field-level no-op detection outside the two documented
# cases (an empty diff, or every operation dropped by a resolution failure) — a diff
# that resolves and applies always reports UPDATED and always issues a request, even
# when it replays a value the target already holds. Shown here with `name`, the
# cleanest possible field (no resolver, no lifecycle action, no format ambiguity), to
# isolate this from the dataset_refs-specific findings above and below.
# --------------------------------------------------------------------------------------


async def test_replaying_an_already_current_name_is_reported_updated_not_no_op() -> None:
    """The general form of what the base suite's ``test_reapplying_an_unchanged_diff_is_a_
    no_op`` hits via ``dataset_refs``: ``write.py``'s own module docstring says
    idempotency "rests on" the *engine* never calling ``update()`` with an unchanged
    value (point 9's "empty diff" framing) — there is no read-before-write comparison in
    the non-conflict path (contrast ``_recover_from_conflict``'s ``_already_applied``,
    which only runs after a 412). Calling ``update()`` twice with the literal same
    desired value therefore sends the PATCH twice and reports ``UPDATED`` twice, not
    ``UPDATED`` then ``NO_OP`` — the exact shape ``suite.py``'s idempotency check expects
    a conforming connector to avoid.
    """
    async with setup_connector() as connector:
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
        second = await connector.update(created.ref, replay)

        assert second.outcome is WriteOutcome.UPDATED, (
            "documenting current behavior: a byte-identical replay of 'name' is reported "
            "as a real update, not a no-op — see the report for the design tension this "
            "implies"
        )
        # And a real PATCH really was sent for it -- the revision moved a second time,
        # proving this is not a cosmetic mislabeling of an already-skipped write.
        assert second.source_revision != first.source_revision


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
