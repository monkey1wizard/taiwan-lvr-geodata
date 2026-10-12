"""GeoJSON output (RFC 7946): one FeatureCollection per month and category, streamed."""

from __future__ import annotations

import json
from pathlib import Path

from .rows import ATTRIBUTE_NAMES


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class GeoJsonOutput:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8", newline="\n")
        self.stream.write('{"type":"FeatureCollection","features":[')
        self.rows = 0

    def add(self, row):
        feature = {
            "type": "Feature",
            "id": f"{row['transaction_id']}#{row['door_index']}",
            "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]},
            "properties": {name: row.get(name) for name in ATTRIBUTE_NAMES},
        }
        self.stream.write(("," if self.rows else "") + "\n" + _dumps(feature))
        self.rows += 1

    def close(self):
        self.stream.write("\n]}\n")
        self.stream.close()
