"""Turning a batch of :class:`SyncRunReport` into one process exit code.

WP2 / T2.8. See ``cli/errors.py`` for what each code means. This module is the one
place that maps *report content* onto those codes, so ``run`` and ``dry-run`` agree on
exactly the same rule.
"""

from __future__ import annotations

from collections.abc import Sequence

from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncRunReport

from .errors import EXIT_ENDPOINT_UNREACHABLE, EXIT_INCOMPLETE, EXIT_OK

__all__ = ["classify_exit"]


def classify_exit(reports: Sequence[SyncRunReport]) -> int:
    """The worst exit code implied by ``reports``.

    A quarantined endpoint (an :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` or
    an exhausted :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` retry budget
    mid-cycle) always wins: it is a stronger claim than "some records failed", since it
    means the *endpoint* is the problem, not one record. Short of that, any cycle that
    is not a clean :attr:`~qlabs_catalog_sync.sync.loop.RunStatus.OK` (or the informational
    :attr:`~qlabs_catalog_sync.sync.loop.RunStatus.SKIPPED`, meaning this pair/entity-type
    combination simply is not scheduled), any non-fatal error, or any failed record marks
    the run incomplete.
    """
    worst = EXIT_OK
    for report in reports:
        if report.quarantined_endpoints:
            worst = max(worst, EXIT_ENDPOINT_UNREACHABLE)
            continue
        incomplete = (
            report.status in (RunStatus.PARTIAL, RunStatus.FAILED)
            or bool(report.errors)
            or report.count(RecordOutcome.FAILED) > 0
        )
        if incomplete:
            worst = max(worst, EXIT_INCOMPLETE)
    return worst
