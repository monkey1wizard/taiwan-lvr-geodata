"""R07-12i: every DuckDB spill lives in a known directory, and its peak size is measured.

measured_spill creates a unique directory under a caller-chosen parent (inside the run or the
command's own output area, never the system temp), samples its byte size on a daemon thread, and
removes it on exit. Each exit also feeds a module-level accumulator with two scopes, so callers can
report the largest spill seen while building one stage ("build") or one pipeline step ("step"):

    reset_peak("build") at Stage start; peak_bytes("build") when Stage.finish writes quality.json.
    reset_peak("step") at the start of a run_full step; peak_bytes("step") when the step is recorded.

The accumulator is per process and the pipeline runs steps one at a time, so no locking by scope is
needed beyond the lock around the update. Peaks are sampled, not exact: a spill that grows and
shrinks between two samples can be missed, which is why the sample interval is reported too.
"""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_INTERVAL_SECONDS = 2.0
_SCOPES = ("build", "step")
_lock = threading.Lock()
_peaks = {scope: 0 for scope in _SCOPES}
_intervals = {scope: DEFAULT_INTERVAL_SECONDS for scope in _SCOPES}


def reset_peak(scope: str) -> None:
    with _lock:
        _peaks[scope] = 0
        _intervals[scope] = DEFAULT_INTERVAL_SECONDS


def peak_bytes(scope: str) -> int:
    with _lock:
        return _peaks[scope]


def sample_interval(scope: str) -> float:
    with _lock:
        return _intervals[scope]


def _record(peak: int, interval: float) -> None:
    with _lock:
        for scope in _SCOPES:
            if peak >= _peaks[scope]:
                _peaks[scope] = peak
            _intervals[scope] = max(_intervals[scope], interval)


def tree_bytes(root: Path) -> int:
    """Total size of the files under root; entries that vanish while walking are skipped."""
    total, pending = 0, [str(root)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class _Measure:
    def __init__(self, path: Path, interval: float):
        self.path, self.interval, self.peak_bytes = path, interval, 0
        self._stop = threading.Event()

    def sample(self) -> None:
        self.peak_bytes = max(self.peak_bytes, tree_bytes(self.path))

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()


@contextmanager
def measured_spill(parent: Path, prefix: str, interval: float = DEFAULT_INTERVAL_SECONDS):
    """Yield an object with .path (a new directory under parent), .peak_bytes and .interval."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"{prefix}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    measure = _Measure(path, interval)
    thread = threading.Thread(target=measure.run, daemon=True)
    thread.start()
    try:
        yield measure
    finally:
        measure._stop.set()
        thread.join()
        measure.sample()
        shutil.rmtree(measure.path, ignore_errors=True)
        _record(measure.peak_bytes, interval)
