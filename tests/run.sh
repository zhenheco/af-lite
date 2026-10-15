#!/usr/bin/env bash
set -euo pipefail
TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TEST_ROOT"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests -p 'test_*.py' -v
