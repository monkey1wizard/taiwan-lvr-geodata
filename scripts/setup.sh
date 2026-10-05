#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo 'Install uv before running setup.' >&2; exit 1; }
uv sync --locked --group dev --python 3.13.16
uv run --locked --python 3.13.16 python scripts/build_fixtures.py
uv run --locked --python 3.13.16 python -m pytest -q
