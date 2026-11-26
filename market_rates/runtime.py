"""Portable paths and first-run configuration for the source distribution."""
from pathlib import Path
import json
import os
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]

def project_path(value):
    path=Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path.resolve() if path.is_absolute() else (ROOT/path).resolve()

def bank_links():
    return project_path(os.environ.get('RATE_BANK_LINKS','inputs/bank-links.xlsx'))

def ensure_configuration():
    for folder in ('runs','outputs','data','history'):(ROOT/folder).mkdir(exist_ok=True)
    for name in ('project','input-workbooks','format-templates','runtime','ollama.runtime'):
        dest=ROOT/'config'/(name+'.local.json')
        source=ROOT/'config'/(name+'.example.json')
        if not dest.exists():
            # Exclusive create keeps existing personal settings intact.
            try:
                with dest.open('x',encoding='utf-8') as stream:stream.write(source.read_text(encoding='utf-8-sig'))
            except FileExistsError:pass

def runtimes():
    return dict(validation_python=sys.executable,python=sys.executable)

def render_pdf(source,target,scale_to=1800):
    import pymupdf
    with pymupdf.open(source) as document:
        if not len(document):raise ValueError('预览PDF没有页面')
        page=document[0];scale=scale_to/max(page.rect.width,page.rect.height)
        page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).save(str(Path(target).with_suffix('.png')))

def ollama_binary():
    config=ROOT/'config/ollama.runtime.local.json'
    data=json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    name=os.environ.get('RATE_OLLAMA_BINARY') or data.get('binary')
    if name:
        found=shutil.which(name)
        candidate=found or str(project_path(name))
        return candidate if Path(candidate).is_file() else None
    return shutil.which('ollama')
