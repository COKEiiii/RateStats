from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load(path):
    path=Path(path)
    if not path.exists() and path.name.endswith('.local.json'):
        root=Path(__file__).resolve().parents[1]
        candidate=path.resolve()
        if candidate.parent==root/'config':
            path=candidate.with_name(candidate.name.replace('.local.json','.example.json'))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def confined(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("证据文件路径越出采集目录")
    return path


def offline_replay(run):
    """Saved-evidence acceptance runs must not become daily production seeds."""
    return bool(run.get('offline_replay')) or any(
        isinstance(item,dict) and item.get('source_sha256') and
        item.get('method')=='Rebind saved model rows to archived literal source grids; no new capture or manual approval'
        for item in run.get('metadata',[]))
