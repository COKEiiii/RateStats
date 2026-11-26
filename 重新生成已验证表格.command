#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
exec .venv/bin/python -m scripts.run_macos sgd-rebuild
