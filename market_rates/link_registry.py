"""Read current user-maintained bank URLs without modifying the source workbook."""
from pathlib import Path
import re
from urllib.parse import urlsplit
from .xlsx_read import read_xlsx,read_hyperlinks
from .scope import bank_name

OFFICIAL_DOMAINS={
 'BEA':['www.hkbea.com.sg','www.hkbea.com'],'BOC':['www.bankofchina.com'],'CIMB':['www.cimb.com.sg','www.cimbpreferred.com.sg'],
 'CITI':['www.citibank.com.sg'],'DBS':['www.dbs.com.sg'],'POSB':['www.posb.com.sg'],'HLB':['www.hlbank.com.sg'],
 'HLF':['www.hlf.com.sg'],'HSBC':['www.hsbc.com.sg'],'ICBC':['singapore.icbc.com.cn','www.icbc.com.sg','www.icbcs.com.sg','v.icbc.com.cn'],
 'Maybank':['www.maybank2u.com.sg','sslsecure.maybank.com.sg'],'OCBC':['www.ocbc.com'],'RHB':['rhbgroup.com.sg','www.rhbgroup.com.sg'],
 'SBI':['sg.statebank'],'SCB':['www.sc.com','av.sc.com'],'UOB':['www.uob.com.sg','www.uobgroup.com'],
 'SingFinance':['www.singfinance.com.sg'],'Singapura Finance':['www.singapurafinance.com.sg'],
 'MARI':['www.maribank.sg'],'TRUST':['trustbank.sg']}

def official_url(url,bank,domains=None):
    parsed=urlsplit(url or '')
    if parsed.scheme!='https' or parsed.hostname not in (domains or OFFICIAL_DOMAINS.get(bank_name(bank),[])) or parsed.username or parsed.password:
        raise ValueError(f'{bank} 必须填写已登记银行的官方 HTTPS 网址：{url}')
    return url

def source_id(bank,currency,kind):return bank_name(bank)+'-'+str(currency)+'-'+kind


def discovery_folder(root,key):
    """Resolve grouped and historical captures by source identity, never row number."""
    from .common import load
    root=Path(root)
    matches=[]
    for item in load(root/'status.json'):
        if any(source_id(r['bank'],r['currency_hint'],r['rate_type'])==key for r in item['registry']):
            matches.append(root/item['id'])
    if len(matches)!=1:raise ValueError('采集来源缺失或重复：'+key)
    return matches[0]


def entries(path):
    book=read_xlsx(path,merge_anchors_only=True);hyperlinks=read_hyperlinks(path) if Path(path).exists() else {};records=[]
    for sheet,cells in book.items():
        if sheet=='补充来源':continue
        bank=currency=None
        for row in range(2,max(int(re.search(r'\d+',k)[0]) for k in cells)+1):
            if cells.get(f'A{row}'):bank=bank_name(cells[f'A{row}']);currency=None
            currency=cells.get(f'B{row}') or currency
            category={'促销':'promo','挂牌':'board','board':'board'}.get(cells.get(f'C{row}'))
            if not bank or not category:continue
            cell=f'D{row}';label=cells.get(cell);target=hyperlinks.get(sheet,{}).get(cell)
            value=label.strip() if isinstance(label,str) else ''
            url=value if value.startswith(('http://','https://')) else target if value else None
            records.append(dict(bank=bank,currency_hint=currency,rate_type=category,url=url,
                source_id=source_id(bank,currency,category),
                cell=cell,sheet=sheet,workbook=str(Path(path).resolve()),display_value=label,
                hyperlink_target=target,status='registered' if url else 'missing_link'))
    return records


def bank_link(path, bank, currency, category, domains=None):
    category={'促销':'promo','挂牌':'board'}.get(category,category)
    matches=[r for r in entries(path) if r['bank']==bank_name(bank) and r['currency_hint']==currency and r['rate_type']==category]
    if len(matches)!=1:raise ValueError(f'{bank} {currency} {category} 链接缺失或不唯一，请检查利率链接表')
    record=matches[0]
    if not record['url']:
        raise ValueError(f"{bank_name(bank)} {currency} {category} 网址为空，请补齐利率链接表“{record['sheet']}”的 {record['cell']}：{record['workbook']}")
    official_url(record['url'],bank,domains)
    return record

def rate_url(bank,currency,category,path=None):
    from .runtime import bank_links
    return bank_link(path or bank_links(),bank,currency,category)['url']

def named_url(key,path=None):
    """Fixed supplementary sources live beside the main URLs, never in code."""
    from .runtime import bank_links
    path=Path(path or bank_links());book=read_xlsx(path);cells=book.get('补充来源',{})
    rows=sorted({int(re.search(r'\d+',c)[0]) for c in cells})
    matches=[r for r in rows if cells.get('C'+str(r))==key]
    if len(matches)!=1:raise ValueError('链接表“补充来源”的来源编号缺失或重复：'+key)
    r=str(matches[0]);url=cells.get('D'+r)
    return official_url(url,cells.get('A'+r))
