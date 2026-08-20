"""The ``TENANT_UNVERIFIED`` registry (T8.6).

WP8 / T8.6. This build has **no live Databricks workspace or Qlik tenant** (RM-01
``decision-databricks-to-qlik-mvp.md``: "The MVP is also built without live tenants ...
Behavior only a real tenant can confirm is registered in a ``TENANT_UNVERIFIED`` list
with a probe script and a human-run checklist, rather than assumed."). Every connector
module built against that constraint says so in its own docstring — grep either
connector package for ``TENANT_UNVERIFIED`` and you will find the same sentences this
module quotes back. What this module adds is not new information: it is the single,
importable, structured place those scattered admissions collect into, so a *program*
(this package's ``scripts/tenant_probe.py``, one directory up from ``packages/``) and a
*person* (``docs/tenant-verification.md``) can both walk the same list instead of one
restating the other by hand.

**How this registry was built.** Every entry below traces to a real sentence in the code
this build produced — ``manifest.py``, ``read.py``, ``resolve.py``, ``write.py`` and
``lifecycle.py`` in this package, plus ``sql_tags.py`` and ``changes.py`` in
``qlabs_connector_databricks`` (this module holds pure data about that package, never an
import of it — the hard dependency rule in the root ``CLAUDE.md`` says a connector
depends only on the SDK and its own vendor libraries, and that rule is honored here even
though this is a registry rather than a runtime code path). A few entries
(:data:`AssumptionStatus.LIVE` items marked in each entry's ``source`` as "audit
finding") were **not** flagged with the literal ``TENANT_UNVERIFIED`` marker by the
module that relies on them — they surfaced only by reading the code's own reasoning
against what RS-02/RS-01 actually document, which is exactly the audit this task's brief
asked for ("a piece of code relying on an unverified assumption that is *not* in the
registry is the actual danger"). Each such case is called out explicitly so the mismatch
this task asked to be reported is visible in the data itself, not just in a PR
description.

**Reading the fields.** :class:`AssumptionStatus` says whether v1 code actually reaches
the assumption today:

* :attr:`~AssumptionStatus.LIVE` — a call the engine can reach in v1 depends on this
  being true. These are what :attr:`UnverifiedAssumption.must_verify_before_production`
  is mostly ``True`` for.
* :attr:`~AssumptionStatus.DORMANT` — implemented, correct by construction, but gated
  off in v1 by a decision (D4's destructive-action opt-in, D7's activation opt-in) that
  nothing in this build's engine wiring turns on yet. Wrong today costs nothing; wrong
  when a future task flips the opt-in could cost a lot, so it stays registered.
* :attr:`~AssumptionStatus.UNUSED` — no code path reaches it at all. The three glossary
  entries are this: ``glossary.py`` is a ``TODO(T3.6)`` stub, decision D5 puts the Qlik
  glossary write path on the RM-05 board, and these three items are registered **because
  RS-02 already flagged them**, not because anything in this build depends on them. Said
  plainly per this task's brief: "these may be registered-but-unused; say so rather than
  implying they are live."
* :attr:`~AssumptionStatus.OPERATIONAL` — not a code assumption at all; a deployment
  precondition (the Qlik service-account role strings, Databricks rate-limit headroom)
  that no module can check for itself.

:attr:`UnverifiedAssumption.probe_check` names the id ``scripts/tenant_probe.py`` reports
results under, when the item can be safety exercised by an automated probe at all;
``None`` means the checklist's "how to verify" step is manual (see
``docs/tenant-verification.md``) — usually because verifying it safely needs a second
tenant, a second space, deliberately unauthorized credentials, or a sustained load test,
none of which a safety-first probe should launch against a customer's tenant by default.

:func:`validate_registry` is this module's only form of self-test: T8.6 owns no test
directory (every other task's tests live under its own ``packages/*/tests``, and this
task's ``owns_paths`` names exactly ``unverified.py``, ``scripts/tenant_probe.py`` and
``docs/tenant-verification.md`` — no test path), so the registry stays self-validating by
running its own consistency check at import time instead: unique, well-formed ids; no
blank fields; ``relied_on`` never empty (even a dormant/unused entry must say *why* it
has nothing to point at, in its own prose). Any accidental copy-paste duplicate id or
half-filled entry fails loudly the moment this module is imported — by
``scripts/tenant_probe.py``, by a REPL, or by a future test suite that does get a path of
its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "REGISTRY",
    "AssumptionStatus",
    "Component",
    "UnverifiedAssumption",
    "by_component",
    "by_id",
    "by_status",
    "must_verify_before_production",
    "validate_registry",
    "with_probe",
]

_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+)+$")


class Component(StrEnum):
    """Which connector package an assumption belongs to.

    Both live under this one registry (see the module docstring's "how this registry was
    built") even though only ``qlik`` is this package — the registry is data, not a
    runtime dependency on ``qlabs_connector_databricks``.
    """

    QLIK = "qlik"
    DATABRICKS = "databricks"


class AssumptionStatus(StrEnum):
    """Whether v1 code actually depends on this assumption today. See the module
    docstring for what each value means and why it matters for prioritizing the
    checklist."""

    LIVE = "live"
    DORMANT = "dormant"
    UNUSED = "unused"
    OPERATIONAL = "operational"


@dataclass(frozen=True, slots=True)
class UnverifiedAssumption:
    """One documented-but-unconfirmed vendor behavior this build relies on, or might.

    Every field is required and non-blank (enforced by :func:`validate_registry`) — an
    entry that cannot fill in "where it is relied on" or "what breaks if wrong" is not
    ready to be registered, it is a placeholder.
    """

    #: Stable, human-referenceable id (e.g. ``"QLIK-DP-ETAG-PATCH"``). Cited by
    #: ``scripts/tenant_probe.py``'s ``--check`` flag and by
    #: ``docs/tenant-verification.md``'s checklist rows.
    id: str
    component: Component
    status: AssumptionStatus
    #: One line, for a table row or a probe's summary output.
    summary: str
    #: What is assumed, and what in RS-01/RS-02 does (or does not) support it.
    assumption: str
    #: ``"module:function/class.method"`` strings — every place in the codebase that
    #: depends on this assumption holding. Never empty: a dormant/unused entry still
    #: states in prose that nothing relies on it yet, and why.
    relied_on: tuple[str, ...]
    #: What the code does today, given the assumption (its behavior, not its intent).
    current_behavior: str
    #: What breaks, and how badly, if the assumption is wrong.
    consequence: str
    #: How a human (or the probe) checks this against a real tenant.
    verification: str
    #: The id of the ``scripts/tenant_probe.py`` check that exercises this, or ``None``
    #: when verification cannot be safely automated (see the module docstring).
    probe_check: str | None
    #: Citation into the research/plan documents this build was built against.
    source: str
    #: Whether this must be confirmed before this connector writes into a real
    #: customer's tenant. Mostly ``True`` for :attr:`AssumptionStatus.LIVE` entries in
    #: the write path; mostly ``False`` for dormant/unused/perf-only ones — see each
    #: entry's ``consequence`` for why.
    must_verify_before_production: bool


# ----------------------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------------------

REGISTRY: Final[tuple[UnverifiedAssumption, ...]] = (
    # -- Qlik: the data-product write path (create/update) — the load-bearing cluster --
    UnverifiedAssumption(
        id="QLIK-DP-ETAG-PATCH",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether the data-products PATCH endpoint honors if-match/ETag concurrency.",
        assumption=(
            "RS-02 documents an optional if-match ETag header for glossary/category/term "
            "writes but is silent on whether the Data Products PATCH endpoint enforces one "
            "at all: 'concurrency control there relies on the changelog rather than ETags' "
            "per the readiness note. The connector assumes sending if-match with a stale "
            "revision makes Qlik reject the request with HTTP 412."
        ),
        relied_on=(
            "manifest.py:qlik_capability_manifest (declares concurrency=ConcurrencyMode.ETAG "
            "for DATA_PRODUCT)",
            "write.py:QlikWriter.update / QlikWriter._send_patch (sends if-match when a "
            "revision is known)",
            "write.py:QlikWriter._recover_from_conflict (assumes HTTP 412 is how a stale "
            "write is signalled, and retries exactly once)",
            "read.py:_map_data_product (captures the resource ETag as source_revision on "
            "every field envelope, seeding the next diff's expected_revision)",
        ),
        current_behavior=(
            "write.py sends if-match on every PATCH that has a revision to send, and "
            "auth.py has no 412 branch by design — write.py._send intercepts 412 locally "
            "and raises ConflictError, which triggers one re-read/re-diff/re-apply cycle. A "
            "diff with no known revision is applied unguarded, with a "
            "qlik.write.update.unguarded warning."
        ),
        consequence=(
            "If Qlik does not enforce if-match at all, two engine cycles racing on the same "
            "product silently last-write-wins: the earlier write is clobbered with no "
            "error, no retry, and nothing in the run report — the exact lost update the "
            "ETag concurrency declaration exists to prevent. If Qlik instead rejects an "
            "unrecognized if-match header outright, every update() call fails until the "
            "header is removed."
        ),
        verification=(
            "scripts/tenant_probe.py --include-destructive creates a throwaway product, "
            "issues two PATCHes back to back with the same (now-stale) if-match value, and "
            "reports whether the second one comes back 412."
        ),
        probe_check="QLIK-DP-ETAG-PATCH",
        source="RS-02 qlik-two-way-sync-readiness.md, 'ETags / optimistic concurrency'",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-DP-CREATE-ETAG",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether the data-products POST (create) response carries an ETag header.",
        assumption="RS-02 documents no ETag on the data-products POST response at all.",
        relied_on=(
            "write.py:QlikWriter.create (WriteResult.created(..., "
            "source_revision=response.headers.get('etag')))",
        ),
        current_behavior=(
            "create() captures whatever ETag header comes back and reports None otherwise; "
            "it never fabricates a revision from another field (e.g. updatedAt)."
        ),
        consequence=(
            "If create never returns an ETag, the very first update() the engine issues for "
            "a freshly created product runs unguarded (no if-match) — the concurrency "
            "protection is absent for exactly the object most likely to be edited "
            "immediately after creation."
        ),
        verification="Same probe run as QLIK-DP-ETAG-PATCH: inspect the create response headers.",
        probe_check="QLIK-DP-CREATE-ETAG",
        source="write.py module docstring, point 6; RS-02 section 2",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-DP-PATCH-204-ETAG",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether a successful PATCH's 204 No Content response carries a fresh ETag.",
        assumption=(
            "The PATCH endpoint is documented to return 204 No Content; RS-02 says nothing "
            "about whether a 204 carries response headers at all, let alone an ETag naming "
            "the new revision."
        ),
        relied_on=(
            "write.py:QlikWriter.update (WriteResult.updated(..., "
            "source_revision=response.headers.get('etag')) after a successful PATCH)",
            "write.py:QlikWriter._recover_from_conflict (same pattern on the retry response)",
        ),
        current_behavior=(
            "update() reads response.headers.get('etag') off the 204 response and reports "
            "it as the new source_revision; None is accepted silently."
        ),
        consequence=(
            "If a 204 never carries an ETag, source_revision becomes None after every "
            "successful update, so every subsequent update to that product also runs "
            "unguarded — the ETag concurrency story would only ever protect the first write "
            "after a read, never a chain of updates, and nothing today would notice."
        ),
        verification="Same probe run: inspect the first successful PATCH's response headers.",
        probe_check="QLIK-DP-PATCH-204-ETAG",
        source=(
            "audit finding — not flagged with the literal TENANT_UNVERIFIED marker in "
            "write.py; added to the registry by this task's own read of the code against "
            "RS-02's silence on 204 response headers"
        ),
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-DP-PATCH-CONTENT-TYPE",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether the PATCH endpoint accepts Content-Type: application/json.",
        assumption=(
            "RS-02 says the endpoint 'uses JSON Patch (array of operations)' but never "
            "names a media type; the documented create call on the same family is "
            "application/json. The connector sends application/json (httpx's default for "
            "json=), not the more RFC-6902-idiomatic application/json-patch+json."
        ),
        relied_on=("write.py:QlikWriter._send_patch",),
        current_behavior=(
            "Every PATCH is sent with whatever Content-Type httpx's json= kwarg produces "
            "(application/json)."
        ),
        consequence=(
            "If Qlik strictly requires application/json-patch+json, every update() call "
            "fails (415/400) — the entire write-after-create half of the sync loop breaks "
            "until the one-line Content-Type change write.py's own docstring already "
            "anticipates is applied."
        ),
        verification="Same probe run: the first PATCH either succeeds or fails on this exact axis.",
        probe_check="QLIK-DP-PATCH-CONTENT-TYPE",
        source="write.py module docstring, point 14; RS-02 section 2",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-TAGS-CHARSET-CAP",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether Qlik constrains tags[] characters (= in particular) or array length.",
        assumption=(
            "RS-02 documents neither constraint. A valued neutral Tag is flattened to "
            "'key=value' before being sent, so a valued tag's wire form always contains at "
            "least one '='."
        ),
        relied_on=("write.py:_tag_values (used by _build_create_body and _patch_tags)",),
        current_behavior=(
            "Any Tag with a non-null value is sent as key=value, unconditionally, with no "
            "client-side character or length validation."
        ),
        consequence=(
            "If Qlik rejects '=' (or any character a source tag value happens to contain) "
            "or enforces a length/count cap, the entire create or update request that "
            "included that tag fails — not just the one tag — for any Databricks UC object "
            "whose tags carry a value (D6 tags are key/value)."
        ),
        verification=(
            "Probe (--include-destructive) attaches a tag with '=' in its flattened value "
            "to the throwaway product and reports whether the write is accepted."
        ),
        probe_check="QLIK-TAGS-CHARSET-CAP",
        source="write.py module docstring, point 4",
        must_verify_before_production=True,
    ),
    # -- Qlik: dataset and owner resolution (D2/D3) -------------------------------------
    UnverifiedAssumption(
        id="QLIK-DATASETIDS-RESOURCEID",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether the datasetIds wire value is the Items-API resourceId, not the item id.",
        assumption=(
            "RS-02 section 4.1 names resourceId as the dataset-CRUD/datasetIds key, and "
            "section 1.1 as 'the id of the underlying resource', but never shows a "
            "datasetIds entry paired against its source item's resourceId in the same "
            "worked example — the equivalence is inferred, not witnessed."
        ),
        relied_on=(
            "resolve.py:_dataset_id_of (prefers resourceId, falls back to the Items-API "
            "item id)",
            "write.py:QlikWriter._apply_datasets / _apply_dataset_update (send whatever "
            "_dataset_id_of returned as datasetIds)",
        ),
        current_behavior=(
            "Every dataset member resolved by name (tier 2) sends resourceId (or, when "
            "absent, the item id) as its datasetIds entry."
        ),
        consequence=(
            "If resourceId is not the value datasetIds actually wants, a created/updated "
            "product either silently associates with the wrong dataset (if the id happens "
            "to validate but means something else) or the request is rejected outright — "
            "undermining D2's whole point (never guess a dataset reference) at the "
            "field-value level even though the field-name level refuses to guess."
        ),
        verification=(
            "Probe (--include-destructive --sample-dataset-name <name>) includes a real "
            "dataset as a member of the throwaway product and re-reads it to confirm "
            "datasetIds round-trips; full confidence still needs a manual check in the Qlik "
            "hub UI that the product shows the intended dataset as a member, not just an "
            "accepted id."
        ),
        probe_check="QLIK-DATASETIDS-RESOURCEID",
        source="resolve.py module docstring, point 2",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-ITEMS-NAME-FILTER-SEMANTICS",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Items-API name filter match semantics (exact vs. substring/fuzzy) undocumented.",
        assumption=(
            "The Items API list endpoint is filterable by name (RS-02 section 3.5), but "
            "its match semantics are not documented."
        ),
        relied_on=("resolve.py:_resolve_dataset_name / _list_items_by_name",),
        current_behavior=(
            "Server results are always re-filtered client-side to an exact string match "
            "before being counted; two or more exact matches is reported ambiguous, never "
            "guessed."
        ),
        consequence=(
            "Self-mitigated by construction — the worst case if the server filter is "
            "fuzzier than expected is fetching a few extra candidates per lookup, not a "
            "wrong resolution. Worth confirming to know whether the filter narrows "
            "anything at all: a purely decorative filter would mean every name lookup "
            "silently degrades toward a full-space scan on a large tenant."
        ),
        verification=(
            "Probe (--include-destructive --sample-dataset-name <name>) issues a filter "
            "with a value that is a strict substring of the real dataset's name and reports "
            "whether the substring alone returns it."
        ),
        probe_check="QLIK-ITEMS-NAME-FILTER-SEMANTICS",
        source="resolve.py module docstring, point 3",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-USERS-FILTER-SYNTAX",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="The /api/v1/users filter syntax (filter=email eq '<email>') is inferred.",
        assumption=(
            "RS-02 documents GET /api/v1/users only as a bare path. The SCIM-style "
            "filter=<field> eq '<value>' syntax is documented for the same endpoint family "
            "in RS-09 (access control), and resolve.py extrapolates it to "
            "filter=email eq '<email>'."
        ),
        relied_on=("resolve.py:_list_users_by_email",),
        current_behavior=(
            "Every owner-email lookup sends filter=email eq '<email>'; results are "
            "re-checked client-side (case-insensitive exact email match) before being "
            "trusted, exactly like the dataset name filter."
        ),
        consequence=(
            "If the filter parameter is wrong, the request may 400 outright (owner "
            "resolution fails hard for every product with owners — D3's whole keyContacts "
            "path breaks) or be silently ignored (the tenant's entire user directory comes "
            "back unfiltered every call; the client-side re-check still resolves correctly "
            "but at a cost that grows with tenant size and eats into the Tier-1, 1000/min "
            "rate budget)."
        ),
        verification=(
            "Probe issues the filter with an email that cannot match a real user and "
            "reports whether the request succeeds (200, even with zero results) or is "
            "rejected (400) as evidence the parameter is recognized."
        ),
        probe_check="QLIK-USERS-FILTER-SYNTAX",
        source="resolve.py module docstring, point 4",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-KEYCONTACTS-ROLE-VOCAB",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="The exact keyContacts[].role string vocabulary Qlik's wire accepts.",
        assumption=(
            "RS-02 documents role: string optional with no enum; its worked examples show "
            "only 'owner' and 'steward'. resolve.py's KeyContact assumes Qlik's vocabulary "
            "is exactly PartyRole's four lowercase values (owner/steward/contact/other) "
            "'by construction'."
        ),
        relied_on=(
            "resolve.py:KeyContact.role / QlikReferenceResolver.resolve_owners",
            "write.py:QlikWriter._apply_owners / _apply_owner_update (send "
            "KeyContact.as_json()['role'] verbatim)",
        ),
        current_behavior=(
            "Every resolved owner's neutral PartyRole.value is sent as-is as the "
            "keyContacts role string, for all four neutral roles."
        ),
        consequence=(
            "'owner' and 'steward' are attested by RS-02's own examples, so lower risk; "
            "'contact' and 'other' are unconfirmed — if Qlik rejects an unrecognized role "
            "value, the whole create/update request (not just that one contact) fails for "
            "any product whose owners include a CONTACT- or OTHER-role party."
        ),
        verification=(
            "Probe (--include-destructive --sample-owner-email <email>) sets that user's "
            "keyContacts role to 'contact' then 'other' on the throwaway product and "
            "reports whether each write is accepted."
        ),
        probe_check="QLIK-KEYCONTACTS-ROLE-VOCAB",
        source=(
            "audit finding — resolve.py's KeyContact docstring states the assumption ('by "
            "construction') without a literal TENANT_UNVERIFIED marker; flagged by this task"
        ),
        must_verify_before_production=True,
    ),
    # -- Qlik: read path -----------------------------------------------------------------
    UnverifiedAssumption(
        id="QLIK-DP-LIST-ENDPOINT",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether a bare GET /api/data-governance/data-products collection endpoint exists.",
        assumption=(
            "RS-02's CRUD reference for data products documents only POST (create) and GET "
            ".../{id} (read one); it never explicitly shows a bare collection GET. read.py "
            "assumes standard REST convention gives one, paginated the same way as "
            "everything else (a data array plus a links object)."
        ),
        relied_on=("read.py:_iter_data_product_changes (http.paginate_cursor('GET', "
                    "DATA_PRODUCTS_PATH, ...))",),
        current_behavior=(
            "list_changed(DATA_PRODUCT) calls the bare collection GET; nothing else in this "
            "module depends on it — read_data_product (a single documented GET .../{id}) "
            "does not need it."
        ),
        consequence=(
            "read.py's own docstring calls this 'the single riskiest assumption in this "
            "module.' If the endpoint does not exist or paginates differently, "
            "list_changed for DATA_PRODUCT fails outright, and the engine has no way to "
            "enumerate existing Qlik-side products it does not already hold in the "
            "IdentityMap — drift detection and update-path diffing break, while create() "
            "(which never lists) keeps working."
        ),
        verification=(
            "Probe issues one page-size-1 GET against the collection endpoint and reports "
            "the raw status and whether the response parses as {data: [...], links: {...}}."
        ),
        probe_check="QLIK-DP-LIST-ENDPOINT",
        source="read.py module docstring, point 7",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-DATASET-RESOURCE-ATTRS",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether every Items-API dataset item carries resourceAttributes.secureQri.",
        assumption=(
            "read.py assumes resourceAttributes.secureQri (or the legacy qri) is present "
            "on the Items-API detail GET for every dataset; when absent it falls back to "
            "the item id."
        ),
        relied_on=(
            "read.py:_map_dataset (identity fallback chain: secure_qri -> legacy_qri -> "
            "item_id)",
        ),
        current_behavior=(
            "A missing resourceAttributes never raises; the dataset's native identity "
            "silently degrades to the Items-API item id instead of the documented durable "
            "key."
        ),
        consequence=(
            "A dataset keyed on the item id instead of secureQri may not compare stable "
            "across whatever the item id's own lifecycle is (RS-02 documents secureQri, "
            "not the item id, as the forward-looking durable key) — a false 'new dataset' "
            "could appear in the IdentityMap on a later cycle if the item id and secureQri "
            "diverge in stability, breaking re-run idempotency for that one dataset."
        ),
        verification=(
            "Probe reads a sample of real dataset items via the Items API and reports how "
            "many carry resourceAttributes.secureQri."
        ),
        probe_check="QLIK-DATASET-RESOURCE-ATTRS",
        source="read.py module docstring, point 1",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-DP-STATUS-ACTIVATED-SIGNAL",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether deactivated reports anything distinguishing it from never-activated.",
        assumption=(
            "Qlik's only documented lifecycle signal is the boolean activated (plus "
            "activatedAt/activatedOn); read.py maps activated:true -> ACTIVE and "
            "activated:false -> DRAFT, treating 'deactivated' and 'never activated' as the "
            "same neutral status."
        ),
        relied_on=(
            "read.py:_map_data_product (values['status'] = ACTIVE if raw['activated'] else "
            "DRAFT)",
        ),
        current_behavior=(
            "A previously-active, now-deactivated product reads back as DRAFT, identical to "
            "a product that was never activated."
        ),
        consequence=(
            "If/when a future status-reconciliation task (D7) is wired up, a source that "
            "still says 'active' would see a Qlik-side DRAFT and re-issue an activate "
            "action indistinguishably from a first activation — which may be the intended "
            "source-wins behavior, or may re-trigger tenant-wide discoverability the "
            "operator deliberately turned off. Today, with no reconciliation wired in v1, "
            "this is dormant risk, not an active bug."
        ),
        verification=(
            "Probe reports the activated value (and activatedOn/activatedAt when present) "
            "for a sample of existing products, as supporting evidence only; full "
            "confirmation needs a live activate-then-deactivate-then-read cycle, which the "
            "probe does not perform automatically (see docs/tenant-verification.md)."
        ),
        probe_check="QLIK-DP-STATUS-ACTIVATED-SIGNAL",
        source="read.py module docstring, point 5",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="QLIK-LIST-CHANGED-FULL-SCAN",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether a changed-since filter exists that would make list_changed incremental.",
        assumption=(
            "RS-02 documents sort on Items updatedAt but not a confirmed changed-since "
            "filter for either the Items API or the data-products list endpoint."
        ),
        relied_on=(
            "read.py:list_changed / _iter_data_product_changes / _iter_dataset_changes",
        ),
        current_behavior=(
            "Every list_changed call performs a full scan of the relevant listing endpoint "
            "and reports every object as UPSERT, regardless of since; next_watermark is the "
            "constant opaque cursor 'full-scan'."
        ),
        consequence=(
            "Not a correctness risk — the engine's checksum-based idempotency makes an "
            "unnecessary re-read a no-op — but a cost/latency one: O(n) reads every cycle "
            "instead of true incrementality, growing with catalog size and eating into the "
            "Tier-2 (100 req/min) rate budget."
        ),
        verification=(
            "Manual: check the Qlik developer docs / OpenAPI spec for a documented "
            "updatedAt-since query parameter on the Items and data-products list endpoints. "
            "Not automated by the probe."
        ),
        probe_check=None,
        source="read.py module docstring, point 6",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-PAGE-SIZE-DEFAULT",
        component=Component.QLIK,
        status=AssumptionStatus.LIVE,
        summary="Whether Qlik documents a bound on cursor-pagination page size (limit).",
        assumption=(
            "RS-02 does not document a bound; DEFAULT_PAGE_SIZE=100 is a conservative, "
            "undocumented default."
        ),
        relied_on=(
            "read.py:DEFAULT_PAGE_SIZE (used by list_changed, and by resolve.py's dataset/"
            "user lookups)",
        ),
        current_behavior="Every paginated call requests limit=100 unless a caller overrides it.",
        consequence=(
            "If the tenant's real maximum is smaller, a page request could 400 or be "
            "silently server-capped; if larger, this is only conservative and costs extra "
            "round trips. Low risk either way — the pagination helper already walks "
            "multiple pages."
        ),
        verification=(
            "Manual: check Qlik developer docs for a documented limit maximum on the "
            "Items/data-products/users list endpoints, or send a request with a very large "
            "limit and see whether it is rejected or capped. Not automated by the probe."
        ),
        probe_check=None,
        source="read.py module docstring, DEFAULT_PAGE_SIZE",
        must_verify_before_production=False,
    ),
    # -- Qlik: lifecycle actions (dormant in v1 — D4/D7) ---------------------------------
    UnverifiedAssumption(
        id="QLIK-MOVE-IDENTITY-STABILITY",
        component=Component.QLIK,
        status=AssumptionStatus.DORMANT,
        summary="Whether a data product's id/qri survive a .../actions/move unchanged.",
        assumption=(
            "RS-02 says a move 'patches the space, not the identifier', which lifecycle.py "
            "treats as strongly implied but not explicitly documented."
        ),
        relied_on=("lifecycle.py:LifecycleActions.move / _identity_after_move",),
        current_behavior=(
            "move() returns the same IdentityRef it was given by default, but inspects the "
            "response body (when present) and rebuilds the ref around a different id if the "
            "body disagrees, logging qlik.lifecycle.move.identity_changed."
        ),
        consequence=(
            "Dormant in v1: decision D4 means the engine has no code path that calls "
            "delete()/lifecycle actions at all, and LifecycleActions.enabled_actions "
            "defaults to empty regardless of what the engine tries. If a future task wires "
            "move in and the identity genuinely changes without the response body "
            "disclosing it, the engine's IdentityMap would silently point at a native key "
            "that no longer resolves — every subsequent write to that product would 404."
        ),
        verification=(
            "Manual: perform a move on a real (disposable) product between two spaces and "
            "confirm the id/qri in a follow-up GET. Not automated by the probe — this "
            "requires a second target space id and exercises a destructive lifecycle "
            "action the probe does not perform by design (see docs/tenant-verification.md)."
        ),
        probe_check=None,
        source="lifecycle.py module docstring, 'Identity across a move'",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-QRI-CROSS-TENANT",
        component=Component.QLIK,
        status=AssumptionStatus.DORMANT,
        summary="Whether qri/secureQri stay stable if a resource is copied to a different tenant.",
        assumption=(
            "RS-02 documents no cross-tenant preservation guarantee; QRIs are "
            "tenant-and-platform scoped."
        ),
        relied_on=(
            "no v1 code path migrates a resource across tenants; every IdentityRef this "
            "connector mints already carries tenant_id alongside the native key (read.py, "
            "resolve.py, write.py, lifecycle.py) as RS-02's own mitigation recommends",
        ),
        current_behavior=(
            "Identity is always scoped by (endpoint, entity_type, native_key, tenant_id) "
            "together, never native_key alone."
        ),
        consequence=(
            "None in v1 — no feature migrates a resource between tenants. Relevant only if "
            "a future roadmap item adds cross-tenant replication; the per-tenant scoping "
            "already in place is the correct defensive posture regardless of how this "
            "resolves."
        ),
        verification=(
            "Manual, and only relevant if cross-tenant migration is ever built. Not "
            "automated by the probe."
        ),
        probe_check=None,
        source="resolve.py / read.py module docstrings; RS-02 section 3",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-ACTIVATE-NONMANAGED-STATUS",
        component=Component.QLIK,
        status=AssumptionStatus.DORMANT,
        summary="The HTTP status Qlik returns when activate targets a non-managed space.",
        assumption=(
            "RS-02 documents activation as 'managed space only' but no captured page "
            "documents the rejection status for a non-managed target."
        ),
        relied_on=(
            "lifecycle.py:LifecycleActions._send_activate (treats any unclassified 4xx as "
            "likely this precondition and says so explicitly)",
        ),
        current_behavior=(
            "401/403/404/429 are classified normally; any other 4xx below 500 is wrapped in "
            "a ConnectorError naming the precondition explicitly as the likely cause."
        ),
        consequence=(
            "Dormant in v1 — D7 makes activation opt-in and nothing in the engine calls it "
            "yet. The risk is a misleading error message (blaming 'not managed' for what is "
            "actually some other 4xx) once activation is wired in, sending an operator "
            "chasing the wrong fix."
        ),
        verification=(
            "Manual: attempt to activate a data product into a space that is not managed, "
            "on a disposable tenant, and record the status code. Not automated by the "
            "probe — activation makes a product tenant-wide discoverable, so this script "
            "does not exercise it by design."
        ),
        probe_check=None,
        source="lifecycle.py module docstring, 'Activation and a non-managed target space'",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-DEACTIVATE-BODY",
        component=Component.QLIK,
        status=AssumptionStatus.DORMANT,
        summary="Whether Qlik's deactivate action accepts an empty JSON object {} as its body.",
        assumption="RS-02 documents the endpoint but no body fields for it.",
        relied_on=("lifecycle.py:LifecycleActions.deactivate",),
        current_behavior="POSTs {} rather than omitting the body.",
        consequence=(
            "Dormant in v1 (same reasoning as activate). If wrong, deactivate() would 400 "
            "the first time anything calls it."
        ),
        verification="Manual, paired with the activate check above. Not automated by the probe.",
        probe_check=None,
        source="lifecycle.py module docstring, \"Deactivate's request body\"",
        must_verify_before_production=False,
    ),
    # -- Qlik: operational precondition, not a code path ---------------------------------
    UnverifiedAssumption(
        id="QLIK-ROLE-PERMISSION-STRINGS",
        component=Component.QLIK,
        status=AssumptionStatus.OPERATIONAL,
        summary="The exact custom-role permission strings a Qlik sync service account needs.",
        assumption=(
            "RS-02 section 5 confirms which broad permission category each operation needs "
            "(create/update/move/activate/consume) but not the precise named custom role on "
            "a specific tenant; the help.qlik.com role matrix is the authoritative source "
            "and 'should be confirmed on the target tenant.'"
        ),
        relied_on=(
            "none directly in code — no module enumerates or checks role strings. Every "
            "write call implicitly depends on whatever identity backs "
            "QlikConfig.client_id/client_secret (config.py, auth.py) holding the right "
            "roles in the target space; a missing permission surfaces only as a 401/403 "
            "AuthError at the moment of the first call that needs it.",
        ),
        current_behavior=(
            "The connector does not validate permissions ahead of time; auth.py's "
            "classify_response_error turns any 401/403 into AuthError uniformly, whichever "
            "specific permission was actually missing."
        ),
        consequence=(
            "A service account with an incomplete role assignment fails at first use — "
            "possibly mid-rollout, on whichever operation happens to need the missing grant "
            "first — rather than being caught during setup."
        ),
        verification=(
            "Probe's --include-destructive run exercises create, update, and delete against "
            "the configured space and reports which (if any) came back as an auth failure — "
            "the closest thing to a permission smoke test this script can safely offer. The "
            "full named-role confirmation is still a manual step against help.qlik.com's "
            "permission matrix for the specific tenant."
        ),
        probe_check="QLIK-ROLE-PERMISSION-STRINGS",
        source="RS-02 qlik-two-way-sync-readiness.md, section 5",
        must_verify_before_production=True,
    ),
    # -- Qlik: glossary — registered, unused (D5 / Track B) ------------------------------
    UnverifiedAssumption(
        id="QLIK-GLOSSARY-PATCH-PATH-ENUM",
        component=Component.QLIK,
        status=AssumptionStatus.UNUSED,
        summary="The exact per-field JSON Pointer path enum a glossary-term PATCH accepts.",
        assumption=(
            "RS-02's captured reference page was truncated before naming which paths (e.g. "
            "/name, /description, /categories, /tags, /stewards, /relatesTo) are "
            "individually patchable for a term."
        ),
        relied_on=(
            "none — glossary.py is an unimplemented TODO(T3.6) stub; decision D5 puts the "
            "Qlik glossary write path on the RM-05 (Track B) board, not this build.",
        ),
        current_behavior="No code in this build sends a glossary-term PATCH.",
        consequence=(
            "None in v1 — registered for Track B's benefit, not because anything today "
            "depends on it."
        ),
        verification=(
            "Defer to Track B (RM-05): re-capture the qlik.dev glossaries reference in full "
            "(RS-02 flags it as truncated) before T3.6 is implemented."
        ),
        probe_check=None,
        source="RS-02 qlik-two-way-sync-readiness.md section 2, 'Glossary term — PATCH / PUT'",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-GLOSSARY-CHANGE-STATUS-BODY",
        component=Component.QLIK,
        status=AssumptionStatus.UNUSED,
        summary="The request body key for the glossary term change-status action.",
        assumption=(
            "RS-02 could not confirm whether the body is {\"status\": \"verified\"} or "
            "{\"type\": \"verified\"}."
        ),
        relied_on=("none — same TODO(T3.6) stub as above; decision D5.",),
        current_behavior="No code in this build calls this action.",
        consequence="None in v1.",
        verification="Defer to Track B (RM-05).",
        probe_check=None,
        source="RS-02 qlik-two-way-sync-readiness.md section 2, 'Glossary term — change-status'",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="QLIK-GLOSSARY-LINKS-PAYLOAD",
        component=Component.QLIK,
        status=AssumptionStatus.UNUSED,
        summary="The exact POST /links request body vs. the inline linksTo shape.",
        assumption="RS-02 notes the overlap between the two was not fully captured.",
        relied_on=("none — same TODO(T3.6) stub; decision D5.",),
        current_behavior="No code in this build calls this endpoint.",
        consequence="None in v1.",
        verification="Defer to Track B (RM-05).",
        probe_check=None,
        source="RS-02 qlik-two-way-sync-readiness.md section 4, 'Term-to-resource links'",
        must_verify_before_production=False,
    ),
    # -- Databricks: the SQL tag read path (D6) ------------------------------------------
    UnverifiedAssumption(
        id="DBX-SCHEMA-TAGS-COLUMNS",
        component=Component.DATABRICKS,
        status=AssumptionStatus.LIVE,
        summary="The exact column set of INFORMATION_SCHEMA.SCHEMA_TAGS.",
        assumption="RS-01 section 1.3 names the table but not its columns.",
        relied_on=(
            "qlabs_connector_databricks.sql_tags:_SCHEMA_TAGS_COLUMNS / read_catalog_tags "
            "(SELECTs catalog_name, schema_name, tag_name, tag_value by name, positionally "
            "indexed)",
        ),
        current_behavior=(
            "The SQL statement names these four columns explicitly; a mismatch surfaces as "
            "a FAILED statement or a parse error, never a silently wrong mapping."
        ),
        consequence=(
            "If the real column names differ, every catalog with a SQL warehouse "
            "configured fails its tag read entirely (D6's tags pipeline breaks for that "
            "catalog) until the column list is corrected — but the failure is loud (FAILED "
            "statement), not silent data corruption."
        ),
        verification=(
            "Probe (--component databricks --databricks-catalog <name>) runs "
            "read_catalog_tags and reports success/failure."
        ),
        probe_check="DBX-SQL-TAGS-READ",
        source="sql_tags.py module docstring, assumption 1",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="DBX-TABLE-TAGS-COLUMNS",
        component=Component.DATABRICKS,
        status=AssumptionStatus.LIVE,
        summary="The exact column set of INFORMATION_SCHEMA.TABLE_TAGS.",
        assumption=(
            "Same caveat as SCHEMA_TAGS, for table_name plus catalog_name/schema_name/"
            "tag_name/tag_value."
        ),
        relied_on=(
            "qlabs_connector_databricks.sql_tags:_TABLE_TAGS_COLUMNS / read_catalog_tags",
        ),
        current_behavior="Same as DBX-SCHEMA-TAGS-COLUMNS, for the table-level query.",
        consequence="Same failure mode as DBX-SCHEMA-TAGS-COLUMNS, scoped to table tags.",
        verification="Same probe run as DBX-SCHEMA-TAGS-COLUMNS (one call reads both tables).",
        probe_check="DBX-SQL-TAGS-READ",
        source="sql_tags.py module docstring, assumption 2",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="DBX-STATEMENT-EXEC-RESPONSE-SHAPE",
        component=Component.DATABRICKS,
        status=AssumptionStatus.LIVE,
        summary=(
            "The Statement Execution API's response shape (status.state, result.data_array, "
            "chunk continuation)."
        ),
        assumption="RS-01 documents the endpoint's existence, not its response schema in detail.",
        relied_on=(
            "qlabs_connector_databricks.sql_tags (the statement submit/poll/result-"
            "collection functions)",
        ),
        current_behavior=(
            "The module polls status.state until it leaves {PENDING, RUNNING}, reads rows "
            "from result.data_array, and follows result.next_chunk_internal_link for "
            "multi-chunk results."
        ),
        consequence=(
            "A wrong field name means either the poll loop never recognizes a terminal "
            "state (exhausts max_poll_attempts, raises TransientError every time) or "
            "successfully-returned rows are silently missed (result.data_array misread as "
            "empty) — the latter is the more dangerous failure, since it looks like 'this "
            "catalog has no tags' rather than a hard error, reintroducing exactly the "
            "ambiguity D6 exists to avoid one layer up."
        ),
        verification=(
            "Same probe run as DBX-SCHEMA-TAGS-COLUMNS: any parse failure in the "
            "row-collection path surfaces as an exception, and a nonempty result on a "
            "catalog known to carry tags is the positive confirmation."
        ),
        probe_check="DBX-SQL-TAGS-READ",
        source="sql_tags.py module docstring, assumption 3",
        must_verify_before_production=True,
    ),
    UnverifiedAssumption(
        id="DBX-ERROR-CODE-AUTH-VOCAB",
        component=Component.DATABRICKS,
        status=AssumptionStatus.LIVE,
        summary="The status.error.error_code substrings assumed to signal a permission failure.",
        assumption=(
            "PERMISSION/UNAUTHENTICATED/FORBIDDEN/ACCESS_DENIED are assumed markers; RS-01 "
            "does not enumerate Statement Execution error codes."
        ),
        relied_on=(
            "qlabs_connector_databricks.sql_tags:_looks_like_auth_failure / "
            "_AUTH_ERROR_CODE_MARKERS",
        ),
        current_behavior=(
            "A FAILED statement is routed to AuthError only if its error_code contains one "
            "of these four substrings; otherwise it becomes TransientError."
        ),
        consequence=(
            "A genuine permission failure whose error_code uses different wording is "
            "misclassified as TransientError — the engine retries a call that will never "
            "succeed instead of quarantining the endpoint. Wasted retry traffic against the "
            "warehouse, not data corruption."
        ),
        verification=(
            "Manual: deliberately query a catalog/warehouse combination the configured "
            "account lacks permission on and record the error_code Databricks returns. Not "
            "run by default — it requires a second, intentionally-unauthorized catalog to "
            "be meaningful, which this probe does not assume exists."
        ),
        probe_check=None,
        source="sql_tags.py module docstring, assumption 4",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="DBX-IDENTIFIER-CHARSET",
        component=Component.DATABRICKS,
        status=AssumptionStatus.LIVE,
        summary="The real UC identifier charset vs. this module's conservative validator.",
        assumption=(
            "RS-01 pins the tag-key charset but not the catalog/schema/table identifier "
            "charset."
        ),
        relied_on=("qlabs_connector_databricks.sql_tags:_IDENTIFIER_RE / IdentifierError",),
        current_behavior=(
            "A catalog name containing any character outside letters/digits/underscore is "
            "refused before any HTTP call, with IdentifierError."
        ),
        consequence=(
            "False-negative risk only, never a SQL-injection-shaped one: a legitimately "
            "named catalog with (say) a hyphen would have its tags refused as unreadable "
            "even though the object itself is fine. Safe by construction, possibly "
            "over-conservative."
        ),
        verification=(
            "Probe (--component databricks --databricks-catalog <name>) reports whether the "
            "named catalog passes the identifier check; a real catalog with non-alphanumeric "
            "characters that needs tag reads is direct evidence the validator needs "
            "widening."
        ),
        probe_check="DBX-SQL-TAGS-READ",
        source="sql_tags.py module docstring, assumption 5",
        must_verify_before_production=False,
    ),
    UnverifiedAssumption(
        id="DBX-RATE-LIMIT-CADENCE",
        component=Component.DATABRICKS,
        status=AssumptionStatus.OPERATIONAL,
        summary="Databricks' rate-limit behavior under the sync loop's chosen poll cadence.",
        assumption=(
            "Not captured in RS-01 in enough detail to size the scheduler's per-pair "
            "cadence against; listed in the implementation plan's 'Known-unverified "
            "behavior' as shipping in the MVP as a documented assumption."
        ),
        relied_on=(
            "no single call site — a property of the combination of the engine's per-pair "
            "scheduler cadence (T2.6, qlabs_catalog_sync, not owned by this connector) and "
            "every Databricks connector call issued each cycle (changes.py's paging, "
            "read.py, sql_tags.py)",
        ),
        current_behavior=(
            "HttpEndpoint retries 429s with Retry-After-aware backoff (T1.4), so a single "
            "rate-limit hit is absorbed; nothing paces requests proactively to stay under a "
            "budget."
        ),
        consequence=(
            "At a high enough polling frequency or catalog size, the connector could spend "
            "a meaningful fraction of its cycle time in backoff, or in the worst case never "
            "catch up — a throughput/latency risk, not a correctness one (retried requests "
            "still eventually succeed or the cycle fails loudly with TransientError)."
        ),
        verification=(
            "Manual: load-test against a real workspace at the intended production cadence "
            "and catalog size, and watch for sustained 429s. Not automated by the probe — a "
            "deliberate rate-limit stress test is exactly the kind of load a safety-first "
            "probe should not launch against a customer's tenant by default."
        ),
        probe_check=None,
        source="implementation-plan.md, 'Known-unverified behavior'",
        must_verify_before_production=False,
    ),
)


# ----------------------------------------------------------------------------------
# Lookups — the "a program can consume this too" half of the module docstring's promise
# ----------------------------------------------------------------------------------


def by_id(assumption_id: str) -> UnverifiedAssumption:
    """The single entry with this id. Raises ``KeyError`` if it does not exist."""
    for entry in REGISTRY:
        if entry.id == assumption_id:
            return entry
    raise KeyError(f"no registered assumption with id {assumption_id!r}")


def by_component(component: Component) -> tuple[UnverifiedAssumption, ...]:
    """Every entry for one connector package, in registry order."""
    return tuple(entry for entry in REGISTRY if entry.component is component)


def by_status(status: AssumptionStatus) -> tuple[UnverifiedAssumption, ...]:
    """Every entry with this :class:`AssumptionStatus`, in registry order."""
    return tuple(entry for entry in REGISTRY if entry.status is status)


def with_probe() -> tuple[UnverifiedAssumption, ...]:
    """Every entry ``scripts/tenant_probe.py`` can automatically exercise."""
    return tuple(entry for entry in REGISTRY if entry.probe_check is not None)


def must_verify_before_production() -> tuple[UnverifiedAssumption, ...]:
    """Every entry the checklist should treat as a hard gate, in registry order."""
    return tuple(entry for entry in REGISTRY if entry.must_verify_before_production)


# ----------------------------------------------------------------------------------
# Self-validation — see the module docstring for why this runs at import time
# ----------------------------------------------------------------------------------


def validate_registry() -> None:
    """Check :data:`REGISTRY` for internal consistency.

    Raises ``ValueError`` on the first problem found: a duplicate or malformed id, a
    blank required field, or an empty ``relied_on`` tuple. Called at the bottom of this
    module so any accidental copy-paste error fails the moment ``unverified.py`` is
    imported — by ``scripts/tenant_probe.py``, by a REPL, or by a future test suite.
    """
    seen_ids: set[str] = set()
    for entry in REGISTRY:
        if not _ID_PATTERN.match(entry.id):
            raise ValueError(f"malformed assumption id: {entry.id!r}")
        if entry.id in seen_ids:
            raise ValueError(f"duplicate assumption id: {entry.id!r}")
        seen_ids.add(entry.id)

        if not isinstance(entry.component, Component):
            raise ValueError(f"{entry.id}: component must be a Component member")
        if not isinstance(entry.status, AssumptionStatus):
            raise ValueError(f"{entry.id}: status must be an AssumptionStatus member")

        for field_name in (
            "summary",
            "assumption",
            "current_behavior",
            "consequence",
            "verification",
            "source",
        ):
            value = getattr(entry, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{entry.id}: {field_name!r} must be a non-blank string")

        if not entry.relied_on:
            raise ValueError(
                f"{entry.id}: relied_on must never be empty — a dormant/unused entry still "
                "has to say, in prose, why nothing depends on it yet"
            )
        for site in entry.relied_on:
            if not isinstance(site, str) or not site.strip():
                raise ValueError(f"{entry.id}: relied_on entries must be non-blank strings")

        if entry.probe_check is not None and not entry.probe_check.strip():
            raise ValueError(f"{entry.id}: probe_check must be None or a non-blank string")


validate_registry()
