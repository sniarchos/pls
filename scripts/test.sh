#!/usr/bin/env bash
# Run all the unit tests. See test_*.py files in the tests/ directory.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v "$@"
