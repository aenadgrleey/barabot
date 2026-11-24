"""DBOS lifecycle: workflow state lives in data/dbos.sqlite.

DBOS is process-global. `launch` must run before any service starts so
the scraper can enqueue from its first event, and `shutdown` runs after
every service has stopped.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dbos import DBOS, DBOSConfig
from dbos._error import DBOSAwaitedWorkflowCancelledError


class _CancelNoise(logging.Filter):
    """Drop the WARNING and ERROR traceback DBOS logs on every cancel.

    Cancelling a running workflow is how the debounce works, so these
    fire on every edit that lands while a listing is processing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and isinstance(record.exc_info[1], DBOSAwaitedWorkflowCancelledError):
            return False
        return "is no longer owned by this execution" not in record.getMessage()


def launch(system_db: Path, *, log_level: str = "WARNING") -> None:
    system_db.parent.mkdir(parents=True, exist_ok=True)
    config: DBOSConfig = {
        "name": "barabot",
        "system_database_url": f"sqlite:///{system_db}",
        "log_level": log_level,
    }
    dbos_log = logging.getLogger("dbos")
    if not any(isinstance(f, _CancelNoise) for f in dbos_log.filters):
        dbos_log.addFilter(_CancelNoise())
    DBOS(config=config)
    DBOS.launch()


def shutdown() -> None:
    DBOS.destroy(workflow_completion_timeout_sec=0)
