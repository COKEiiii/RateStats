"""Small OS-specific adapters used by the local workflows."""
from __future__ import annotations

import os
import json
from pathlib import Path,PureWindowsPath
import shutil
import ssl
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def canonical(path: str | Path) -> Path:
    """Resolve symlinks such as macOS /var -> /private/var consistently."""
    return Path(path).expanduser().resolve(strict=False)


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(canonical(left))) == os.path.normcase(str(canonical(right)))


def available_output(path: str | Path | None, *, root: Path = ROOT) -> Path | None:
    """Use only output plans available on this host; keep migrated pointers untouched."""
    if not path:return None
    if PureWindowsPath(str(path)).is_absolute():return None
    candidate=Path(path).expanduser()
    if not candidate.is_absolute():candidate=root/candidate
    candidate=canonical(candidate)
    return candidate if (candidate/'table-validation-plan.json').is_file() else None


def open_path(path: str | Path) -> None:
    target = str(canonical(path))
    if sys.platform == "darwin":
        subprocess.run(["open", target], check=True)
    else:
        subprocess.run(["xdg-open", target], check=True)


def excel_command(operation: str, *args: str | Path) -> list[str]:
    """Build a command for the macOS Excel backend."""
    return [sys.executable, "-m", "scripts.excel_native", operation, *map(str, args)]


def prepare_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    values = dict(os.environ if env is None else env)
    try:
        import certifi
        values.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass
    values.setdefault("PYTHONUTF8", "1")
    values.setdefault("OLLAMA_NO_CLOUD", "1")
    return values


def pdf_converter() -> str | None:
    """Find the preview converter even under Finder's minimal PATH."""
    configured=os.environ.get('RATE_SOFFICE')
    config_path=ROOT/'config/excel.runtime.local.json'
    if not configured and config_path.exists():
        configured=json.loads(config_path.read_text(encoding='utf-8-sig')).get('pdf_converter')
    if configured:
        path=canonical(configured)
        if not path.is_file() or not os.access(path,os.X_OK):
            raise FileNotFoundError('Configured PDF preview converter is unavailable: '+str(path))
        return str(path)
    installed=Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    if installed.is_file():return str(installed)
    return shutil.which('soffice')


def start_local_model(log_path: str | Path, timeout: int = 90, env: dict[str, str] | None = None) -> dict:
    """Verify or start the configured loopback Ollama without killing other services."""
    from urllib.error import URLError
    from urllib.request import urlopen
    import certifi
    from market_rates.common import load
    from market_rates.runtime import ROOT, ollama_binary, project_path

    config = load(ROOT / "config/project.local.json")
    runtime_file = ROOT / "config/ollama.runtime.local.json"
    runtime = load(runtime_file) if runtime_file.exists() else {}
    base = os.environ.get("RATE_OLLAMA_URL") or config.get("models", {}).get("base_url") or "http://127.0.0.1:11434"
    from urllib.parse import urlsplit
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Only a loopback HTTP Ollama address is allowed")

    def version():
        context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(base.rstrip("/") + "/api/version", timeout=2, context=context) as response:
            value = json.loads(response.read())
        expected = runtime.get("version")
        if expected and value.get("version") != expected:
            raise RuntimeError(f"Configured Ollama version {expected} differs from running {value.get('version')}")
        return value

    try:
        current = version()
        return {"status": "reused", **current}
    except (URLError, TimeoutError, ConnectionError):
        pass

    binary = ollama_binary()
    if not binary or not Path(binary).is_file():
        raise FileNotFoundError("Install Ollama or set a valid local binary in config/ollama.runtime.local.json")
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    child_env = prepare_environment(env)
    child_env.update(OLLAMA_HOST=parsed.netloc, OLLAMA_NO_CLOUD="1", OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1")
    if runtime.get("model_directory"):
        child_env["OLLAMA_MODELS"] = str(project_path(runtime["model_directory"]))
    with log.open("ab") as output:
        process = subprocess.Popen([binary, "serve"], cwd=ROOT, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    import time
    for _ in range(max(1, timeout * 2)):
        if process.poll() is not None:
            raise RuntimeError(f"Ollama exited during startup; see {log}")
        try:
            current = version()
            save_service = {"pid": process.pid, "binary": binary, **current}
            from market_rates.common import save
            save(ROOT / "data/local-model/service.json", save_service)
            return {"status": "started", **current}
        except (URLError, TimeoutError, ConnectionError):
            import time
            time.sleep(0.5)
    raise TimeoutError(f"Ollama did not become ready; see {log}")
