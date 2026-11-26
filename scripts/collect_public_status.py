"""Capture official public pages without inventing promotional quote rows."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
from market_rates.capture import capture_sources
from market_rates.common import save
from market_rates.pipeline import evidence_index
from market_rates.workbook_policy import bank_in_scope

from market_rates.link_registry import rate_url,named_url

def public_sources():
    keys={'DBS':['DBS-public'],'POSB':[],'MARI':['MARI-product','MARI-promo'],'TRUST':['TRUST-public']}
    sources={};unavailable=[]
    for bank,names in keys.items():
        if not bank_in_scope(bank):continue
        try:urls=[rate_url('POSB','SGD','board')] if bank=='POSB' else [named_url(key) for key in names]
        except ValueError as exc:
            unavailable.append(dict(bank=bank,reason=str(exc)))
        else:sources[bank]=urls
    return sources,unavailable


def collect(out):
    from urllib.parse import urlsplit
    out=Path(out)
    if out.exists():raise ValueError('请选择新证据目录')
    sources,unavailable=public_sources()
    cfg=dict(sources=[dict(id='public-'+bank,bank=bank,enabled=True,urls=urls,
      domains=sorted({urlsplit(u).hostname for u in urls}),max_depth=0,max_pages=len(urls),product_ids=[],settle_ms=1500)
      for bank,urls in sources.items()])
    pages,errors=capture_sources(cfg,out/'evidence');evidence_index(pages,out/'evidence')
    save(out/'status.json',dict(captured_at=datetime.now(timezone.utc).isoformat(),pages=pages,errors=errors,
      sources=sources,unavailable_sources=unavailable,
      note='只登记官方页面证据；未生成报价，未宣称已覆盖 App、登录后或非公开优惠。'))
    print({'pages':len(pages),'errors':errors,'unavailable_sources':unavailable})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args();collect(a.out)
