#!/usr/bin/env bash
# Linting script: auto-fixes run first, then checkers report remaining issues.
# Install: pip install isort mypy ruff pylint

set -uo pipefail

TARGETS=(cogs config database services utils bot.py fun.py moderation.py watchdog.py)
FAILED=0

echo "==> isort (auto-fix import ordering)"
isort "${TARGETS[@]}" || FAILED=1

echo ""
echo "==> ruff format (auto-format)"
ruff format "${TARGETS[@]}" || FAILED=1

echo ""
echo "==> ruff check --fix (auto-fix lint rules)"
ruff check --fix "${TARGETS[@]}" || FAILED=1

echo ""
echo "==> mypy (type checking)"
mypy "${TARGETS[@]}" || FAILED=1

echo ""
echo "==> pylint (lint report)"
.venv/bin/pylint "${TARGETS[@]}" || FAILED=1

exit $FAILED

