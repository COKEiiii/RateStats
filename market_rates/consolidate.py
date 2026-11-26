"""Combine disjoint, pinned pilot runs without approving their review items."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import shutil
import html
from .common import load,save,digest
from .failure_summary import record_failure
from .pipeline import check_evidence,evidence_index,evaluate
from .multi_bank_validation import matched_offers
from .table_validation import sha
from .store import Store

def qualify(root,allow_agreement=False):
    root=Path(root);run=load(root/'run.json');check_evidence(run,root/'evidence')
    good,bad=matched_offers(run)
    expected_banks=set(load(root/'config.snapshot.json')['expected_banks'])
    capture_errors=[error for error in run.get('errors',[])
                    if 'http://' in error or 'https://' in error or '来源发现达到上限' in error]
    if capture_errors:
        for error in capture_errors:
            bank=next((name for name in sorted(expected_banks,key=len,reverse=True)
                       if error.startswith((name+' ',name+'来源发现'))),None)
            record_failure(error,bank=bank,step='采集',substep='合并银行',log=root/'run.json')
        raise ValueError('仍有网页或附件采集失败：'+str(root))
    actual_banks={r['bank'] for r in good}
    incomplete=[p for p in run['pages'] if not p.get('complete') or not p.get('ok')]
    if actual_banks!=expected_banks or incomplete:
        for bank in sorted(expected_banks-actual_banks):
            record_failure('本批缺少通过核验的报价',bank=bank,step='核验',substep='合并银行',log=root/'run.json')
        for page in incomplete:
            record_failure('来源证据未完整采集（'+str(page.get('id','来源页'))+'）',
                           bank=page.get('bank'),step='采集',substep='合并银行',log=root/'run.json')
        raise ValueError('银行或证据覆盖不足：'+str(root))
    if bad or not good or any(not run[l]['coverage_complete'] for l in ['llm']):
        for key in bad:
            bank=key.split('/',1)[0]
            record_failure('报价来源核验未通过（'+key+'）',bank=bank if bank in expected_banks else None,
                           step='核验',substep='合并银行',log=root/'run.json')
        raise ValueError('文字结果缺失或来源检查不通过，保留复核报告并停止插表：'+str(root))
    from .manual_review import effective_offers
    receipts=effective_offers(run)[2]
    if receipts:
        return dict(kind='human_reviewed_overlay',manual_revisions=[r['revision'] for r in receipts],
            lanes={l:dict(expected=len(good),matched=len(good),unexpected=[]) for l in ['llm']})
    b=load(root/'benchmark.json') if (root/'benchmark.json').exists() else None
    if b and b.get('run_sha256')==sha(root/'run.json') and b.get('evidence_hash')==run['evidence_hash']:
        if set(b.get('lanes',{}))!={'llm'}:raise ValueError('不能继承旧文字核验记录')
        if any(v['matched']!=v['expected'] or v['unexpected'] for v in b['lanes'].values()):raise ValueError('原文基准未通过')
        return dict(kind=b.get('kind','frozen_source_reference'),lanes=b['lanes'])
    if not allow_agreement:raise ValueError('缺少对应本次证据的原文基准：'+str(root))
    message='本次完成文字LLM结构、来源和覆盖检查，不等同于原文人工基准通过。完整条款仍待人工复核。'
    (root/'agreement.html').write_text('<!doctype html><meta charset="utf-8"><h1>文字来源检查</h1><p>'+html.escape(message)+'</p><p>通过检查报价：'+str(len(good))+'</p><a href="review.html">人工复核</a>',encoding='utf8')
    return dict(kind='text_source_checks',lanes={l:dict(expected=len(good),matched=len(good),unexpected=[]) for l in ['llm']})

def consolidate(folders,out,label,allow_agreement=False,coverage=None):
    out=Path(out)
    if out.exists():raise ValueError('合并目录已存在，请使用新目录')
    prepared=[(Path(f),qualify(f,allow_agreement)) for f in folders]
    banks=set();pages=[];metadata=[];errors=[];sources=[];dates={};cfg=None
    lanes={l:dict(offers=[],inventory=[],coverage_complete=True,unreadable=[]) for l in ['llm']}
    for root,gate in prepared:
        r=load(root/'run.json');c=load(root/'config.snapshot.json')
        if banks.intersection(c['expected_banks']):raise ValueError('同一银行存在多个来源批次，拒绝静默覆盖')
        banks.update(c['expected_banks']);dates.update({b:r['as_of'] for b in c['expected_banks']})
        if cfg is None:cfg=deepcopy(c);cfg.update(products=[],sources=[],expected_banks=[])
        for key in ['products','sources','expected_banks']:cfg[key]+=deepcopy(c[key])
        cfg.setdefault('tenor_map',{}).update(c.get('tenor_map',{}))
        pages+=r['pages'];metadata+=r['metadata'];errors+=r['errors']
        for lane in lanes:
            for field in ['offers','inventory','unreadable']:lanes[lane][field]+=deepcopy(r[lane][field])
        sources.append(dict(path=str(root.resolve()),as_of=r['as_of'],banks=c['expected_banks'],run_sha256=sha(root/'run.json'),result_sha256=sha(root/'result.json'),qualification=gate))
    if len({p['id'] for p in pages})!=len(pages):raise ValueError('证据标识冲突')
    out.mkdir(parents=True);(out/'evidence').mkdir()
    for root,_ in prepared:
        for src in (root/'evidence').iterdir():
            if not src.is_file() or src.name in ['index.html','capture.json']:continue
            dst=out/'evidence'/src.name
            if dst.exists() and sha(dst)!=sha(src):raise ValueError('证据文件名冲突：'+src.name)
            if not dst.exists():shutil.copy2(src,dst)
    as_of=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    cfg['pilot']=dict(no_publication=True,scope=label)
    run=dict(id=out.name,as_of=as_of,demo=False,pages=pages,metadata=metadata,errors=errors,evidence_hash=digest(pages),pilot=cfg['pilot'],collection_label=label,bank_dates=dates,source_runs=sources,**lanes)
    # Coverage is a read-only status inventory. It cannot supply quotes or relax qualify().
    run['coverage']=coverage or []
    save(out/'config.snapshot.json',cfg);save(out/'run.json',run);evidence_index(pages,out/'evidence')
    store=Store(out/'pilot.sqlite3')
    try:evaluate(out,store)
    finally:store.close()
    benchmark=dict(kind='consolidated_qualifications',evidence_hash=run['evidence_hash'],run_sha256=sha(out/'run.json'),source_runs=sources,
       lanes={l:dict(expected=sum(s['qualification']['lanes'][l]['expected'] for s in sources),matched=sum(s['qualification']['lanes'][l]['matched'] for s in sources),unexpected=[]) for l in lanes})
    save(out/'benchmark.json',benchmark)
    return run
