"""TGOS package: batch reservations, exchange files, and strict result imports.

The implementation is split across ``batches``, ``exchange``, and ``validate``.
This module re-exports the names used by other modules and tests.
"""

from ..contracts.validate import TAIWAN_BOUNDS
from .batches import (  # noqa: F401
    UNCARRIED,
    _artifact_paths,
    _round_robin,
    _write_state,
    load_state,
    prepare_tgos,
    repair_prepared_exchange,
    revoke_alias,
    transition_batch,
)
from .exchange import _write_exchange, exchange_folder_name, tgos_log_date  # noqa: F401
from .validate import _rebuild_resolution, _tgos_coordinate, import_tgos  # noqa: F401

__all__ = [
    "import_tgos",
    "load_state",
    "prepare_tgos",
    "repair_prepared_exchange",
    "revoke_alias",
    "tgos_log_date",
    "transition_batch",
    "exchange_folder_name",
    "UNCARRIED",
    "TAIWAN_BOUNDS",
]
