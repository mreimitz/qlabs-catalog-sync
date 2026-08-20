"""Run history: what each sync cycle did, recorded and made queryable.

WP11 / T11.4. Three tables (``qlabs_catalog_sync.runs.models``) migrated by
``packages/qlabs-catalog-sync/src/qlabs_catalog_sync/alembic/versions/0003_run_history.py``
(``down_revision = "0002"``, T10.1's configuration schema), sharing ``Base``/
``Base.metadata`` with the T2.2 state-store tables and the T10.1 configuration tables
rather than a fourth declarative base, and a recorder
(``qlabs_catalog_sync.runs.recorder.RunRecorder``) that turns a real
:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport` -- the sync loop's own, already-
complete description of one cycle -- into those rows.

This task owns the schema and the recorder only. See
``qlabs_catalog_sync.runs.recorder``'s module docstring for exactly where and how the
sync loop's caller (``SyncScheduler``, in ``qlabs_catalog_sync.scheduler``) should be
wired to drive it -- that wiring is not built here.
"""

from qlabs_catalog_sync.runs.models import (
    RunErrorRow,
    RunItemRow,
    RunItemUnresolvedFieldRow,
    RunRecordStatus,
    RunRow,
)
from qlabs_catalog_sync.runs.recorder import (
    RunErrorRecord,
    RunItemRecord,
    RunRecord,
    RunRecorder,
    is_reportable,
)

__all__ = [
    "RunErrorRecord",
    "RunErrorRow",
    "RunItemRecord",
    "RunItemRow",
    "RunItemUnresolvedFieldRow",
    "RunRecord",
    "RunRecordStatus",
    "RunRecorder",
    "RunRow",
    "is_reportable",
]
