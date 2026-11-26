"""Aggregate already verified source waves without repeating inference."""
import argparse,hashlib,shutil
from datetime import datetime
from pathlib import Path
from market_rates.common import save,digest
from market_rates.board_wave import validate_wave,replace_verified_sections,wave_source_hash
from market_rates.pipeline import evidence_index

def ensure_merged(waves,out,work):
    """An interrupted output is not complete merely because run.json exists."""
    out=Path(out).resolve();work=Path(work).resolve()
    if out.parent!=work:raise ValueError('Merged output must be a direct child of this workflow')
    if out.exists():
        try:return validate_wave(out)
        except (ValueError,OSError):
            archive=work/(out.name+'-failed-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            if archive.resolve().parent!=work:raise ValueError('Merge archive escaped workflow')
            out.rename(archive)
    return merge(waves,out)

def merge(waves,out):
    out=Path(out);sources=[(Path(p),validate_wave(p)) for p in waves]
    out.mkdir(parents=True,exist_ok=False);(out/'evidence').mkdir()
    r=dict(id=out.name,as_of=max(r['as_of'] for _,r in sources),bank_dates={},pages=[],sections=[],tasks=[],errors=[],demo=False,verification_sources=[],source_file_hashes={})
    checks=[]
    from market_rates.common import load
    for path,source in sources:
        r['verification_sources'].append(dict(path=str(path.resolve()),run_sha256=hashlib.sha256((path/'run.json').read_bytes()).hexdigest()))
        for key in ['pages','sections','tasks']:r[key]+=source[key]
        r['bank_dates'].update(source['bank_dates']);checks+=load(path/'wave-checks.json')['checks']
        for name,expected in source.get('source_file_hashes',{}).items():
            if name in r['source_file_hashes'] and r['source_file_hashes'][name]!=expected:
                raise ValueError('Conflicting verified source file: '+name)
            r['source_file_hashes'][name]=expected
        for file in (path/'evidence').iterdir():
            if not file.is_file() or file.name=='index.html':continue
            dest=out/'evidence'/file.name
            if dest.exists() and dest.read_bytes()!=file.read_bytes():raise ValueError('Evidence name conflict')
            shutil.copy2(file,dest)
    for key in ['pages','sections','tasks']:
        ids=[x['id'] for x in r[key]]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate '+key+' identifiers')
    r['evidence_hash']=digest(r['pages']);r['task_hash']=digest(r['tasks'])
    save(out/'run.json',r);save(out/'wave-checks.json',dict(passed=True,extraction_mode='llm_only',source_hash=wave_source_hash(r),task_hash=r['task_hash'],checks=checks,errors=[],aggregated=True))
    evidence_index(r['pages'],out/'evidence');validate_wave(out);return r

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--waves',nargs='+',required=True);a.add_argument('--out',required=True);x=a.parse_args();merge(x.waves,x.out)
