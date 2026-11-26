#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  if [[ -e .venv ]]; then
    backup=".venv.incompatible-backup"
    [[ ! -e "$backup" ]] || { echo "Refusing to replace existing $backup" >&2; exit 1; }
    mv .venv "$backup"
  fi
  command -v python3 >/dev/null || { echo "Install Python 3.11 or newer first." >&2; exit 1; }
  python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
  python3 -m venv .venv
fi

export SSL_CERT_FILE="${SSL_CERT_FILE:-$(python3 -m certifi 2>/dev/null || true)}"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m playwright install chromium
.venv/bin/python -c 'from market_rates.runtime import ensure_configuration; ensure_configuration()'
.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.platform_runtime import pdf_converter
from market_rates.common import save
converter=pdf_converter()
config=Path('config/excel.runtime.local.json')
if converter and not config.exists():save(config,{'pdf_converter':converter})
PY
echo "Mac environment installed. Ollama and Microsoft Excel must be available."
