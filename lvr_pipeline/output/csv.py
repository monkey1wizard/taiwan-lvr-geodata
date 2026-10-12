"""CSV output: UTF-8, header row, `\\n` line endings, longitude and latitude instead of geometry."""

from __future__ import annotations

import csv as _csv
from pathlib import Path

from .rows import CSV_COLUMNS


class CsvOutput:
    def __init__(self, path: Path, *, bom: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8-sig" if bom else "utf-8", newline="")
        self.writer = _csv.writer(self.stream, lineterminator="\n")
        self.writer.writerow(CSV_COLUMNS)
        self.rows = 0

    def add(self, row):
        self.writer.writerow(["" if row.get(n) is None else row[n] for n in CSV_COLUMNS])
        self.rows += 1

    def close(self):
        self.stream.close()
