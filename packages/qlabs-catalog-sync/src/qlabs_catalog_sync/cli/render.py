"""Human-readable rendering of a :class:`SyncRunReport` and identity review state.

WP2 / T2.8. This is a *different* artifact from the JSON plan
(:meth:`~qlabs_catalog_sync.sync.loop.SyncRunReport.to_json`, written verbatim to the
plan file by ``dry-run``): it is prose for a person skimming a terminal, not a document
for a machine to parse. Counts first, then the exceptional things (failures, orphans,
dropped/withheld fields, held watermarks) before the routine creates/updates, because
those are what a reviewer actually needs to see without scrolling.
"""

from __future__ import annotations

from collections.abc import Sequence

from qlabs_catalog_sync.identity import BootstrapReport, MatchProposal, ProposalStatus
from qlabs_catalog_sync.sync.loop import RecordOutcome, RecordReport, RunStatus, SyncRunReport

__all__ = [
    "render_bootstrap_text",
    "render_proposals_text",
    "render_report_text",
    "render_summary_text",
]

_STATUS_SEVERITY: dict[RunStatus, int] = {
    RunStatus.OK: 0,
    RunStatus.SKIPPED: 0,
    RunStatus.PARTIAL: 1,
    RunStatus.FAILED: 2,
}


def render_report_text(report: SyncRunReport) -> str:
    """One pair/entity-type cycle, rendered for a terminal: counts, then detail."""
    mode = "dry-run" if report.dry_run else "apply"
    lines: list[str] = [
        f"=== {report.pair}: {report.source_endpoint} -> {report.target_endpoint} "
        f"/ {report.entity_type.value} ({mode}) ===",
        f"status={report.status.value} committed={report.committed} "
        f"watermark_advanced={report.watermark_advanced} "
        f"duration={report.duration_seconds:.2f}s pages={report.pages} "
        f"has_more={report.has_more}",
        "counts: "
        + " ".join(
            f"{label}={report.count(outcome)}"
            for label, outcome in (
                ("created", RecordOutcome.CREATED),
                ("written", RecordOutcome.WRITTEN),
                ("unchanged", RecordOutcome.UNCHANGED),
                ("no_op", RecordOutcome.NO_OP),
                ("skipped", RecordOutcome.SKIPPED),
                ("orphaned", RecordOutcome.ORPHANED),
                ("filtered", RecordOutcome.FILTERED),
                ("failed", RecordOutcome.FAILED),
            )
        )
        + f" (read={report.read_count})",
    ]

    if report.errors:
        lines.append(f"\nerrors ({len(report.errors)}):")
        for error in report.errors:
            lines.append(
                f"  ! [{error.kind}] {error.message} "
                f"(endpoint={error.endpoint}, operation={error.operation}, "
                f"fatal={error.fatal})"
            )

    if report.quarantined_endpoints:
        lines.append(f"\nquarantined endpoints: {list(report.quarantined_endpoints)}")

    failed = _records(report, RecordOutcome.FAILED)
    if failed:
        lines.append(f"\nfailed ({len(failed)}):")
        for record in failed:
            lines.append(f"  x {record.native_key}  {record.detail or ''}")

    orphaned = _records(report, RecordOutcome.ORPHANED)
    if orphaned:
        lines.append(f"\norphaned -- gone at the source, never deleted ({len(orphaned)}):")
        for record in orphaned:
            lines.append(f"  ! {record.native_key}")

    creates = _records(report, RecordOutcome.CREATED)
    if creates:
        lines.append(f"\ncreates ({len(creates)}):")
        for record in creates:
            lines.append(_render_record_detail(record, symbol="+"))

    updates = _records(report, RecordOutcome.WRITTEN)
    if updates:
        lines.append(f"\nupdates ({len(updates)}):")
        for record in updates:
            lines.append(_render_record_detail(record, symbol="~"))

    skipped = _records(report, RecordOutcome.SKIPPED)
    if skipped:
        lines.append(f"\nskipped ({len(skipped)}):")
        for record in skipped:
            reason = record.reason.value if record.reason is not None else "?"
            lines.append(f"  - {record.native_key}  reason={reason}  {record.detail or ''}")

    if report.watermark_held_by:
        lines.append(
            f"\nwatermark held at {report.watermark_before!r} by "
            f"{len(report.watermark_held_by)} record(s) with outstanding work: "
            f"{list(report.watermark_held_by)}"
        )

    return "\n".join(lines)


def _records(report: SyncRunReport, outcome: RecordOutcome) -> list[RecordReport]:
    return [record for record in report.records if record.outcome is outcome]


def _render_record_detail(record: RecordReport, *, symbol: str) -> str:
    target = f" -> {record.target_native_key}" if record.target_native_key else " (new)"
    line = f"  {symbol} {record.native_key}{target}"
    line += f"\n      changed:  {list(record.changed_fields)}"
    if record.written_fields:
        line += f"\n      written:  {list(record.written_fields)}"
    if record.dropped:
        line += "\n      dropped:  " + ", ".join(
            f"{field.field} ({field.reason.value})" for field in record.dropped
        )
    if record.withheld:
        line += "\n      withheld: " + ", ".join(
            f"{field.field} ({field.reason})" for field in record.withheld
        )
    if record.target_skipped_fields:
        line += f"\n      target could not resolve: {list(record.target_skipped_fields)}"
    return line


def render_summary_text(reports: Sequence[SyncRunReport]) -> str:
    """One line covering every pair/entity-type run in this invocation."""
    if not reports:
        return "=== summary: no sync pairs/entity types were run ==="
    worst = max((report.status for report in reports), key=lambda status: _STATUS_SEVERITY[status])
    total_writes = sum(report.write_count for report in reports)
    total_errors = sum(len(report.errors) for report in reports)
    total_failed = sum(report.count(RecordOutcome.FAILED) for report in reports)
    return (
        f"\n=== summary: {len(reports)} run(s), overall status={worst.value}, "
        f"writes={total_writes}, errors={total_errors}, failed_records={total_failed} ==="
    )


def render_proposals_text(proposals: Sequence[MatchProposal]) -> str:
    """Identity bootstrap proposals, for ``identity-confirm list``."""
    if not proposals:
        return "no matching identity proposals"
    lines = [f"{len(proposals)} proposal(s):"]
    for proposal in proposals:
        lines.append(
            f"\n[{proposal.proposal_id}] status={proposal.status.value} "
            f"decision={proposal.decision.value}"
        )
        lines.append(f"  source: {proposal.source.natural_key.display}")
        if proposal.status is ProposalStatus.AMBIGUOUS:
            lines.append(
                "  candidates (ambiguous -- pick one with --candidate): "
                + ", ".join(c.identity.native_key for c in proposal.candidates)
            )
        elif proposal.candidates:
            lines.append(
                "  candidate: " + ", ".join(c.identity.native_key for c in proposal.candidates)
            )
        lines.append(f"  rationale: {proposal.rationale}")
    return "\n".join(lines)


def render_bootstrap_text(report: BootstrapReport) -> str:
    """The result of ``identity-confirm bootstrap`` -- nothing here was bound."""
    return (
        f"=== identity bootstrap: {report.review_path} ===\n"
        f"considered={report.considered} proposed={len(report.proposed)} "
        f"ambiguous={len(report.ambiguous)} unmatched={len(report.unmatched)} "
        f"already_bound={len(report.already_bound)} superseded={len(report.superseded)}\n"
        "nothing was bound -- review the file, then `identity-confirm confirm <id>` "
        "or `identity-confirm apply`"
    )
