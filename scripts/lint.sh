#!/bin/bash
# Runs all code checks (ruff + shellcheck). Used by the pre-commit hook and CI.
set -e
cd "$(git rev-parse --show-toplevel)"

# Prefer tools from the project venv, fall back to PATH (CI)
[ -d .venv/bin ] && PATH="$PWD/.venv/bin:$PATH"

for tool in ruff shellcheck; do
    if ! command -v "$tool" &>/dev/null; then
        echo "[lint] '$tool' not found. Run: pip install -r requirements-dev.txt" >&2
        exit 1
    fi
done

echo "[lint] ruff..."
ruff check .

echo "[lint] shellcheck..."
git ls-files -z '*.sh' '*.hook' | xargs -0 shellcheck --severity=warning

echo "[lint] All checks passed."
