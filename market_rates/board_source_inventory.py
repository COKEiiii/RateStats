"""Reconstruct legacy board adapters from captured grids, never model offers."""
import re

from .board_batch import reference,normalize_table
from .board_extractor import table_rows,tenor,rate,number,literal_date
from .schema import normalize


def direct_board_rows(pages):
    index={p['id']:p for p in pages};rows=[]
    for page in pages:
        if not page.get('grid') or not page['id'].endswith('-rates') or page.get('rate_type')!='board':continue
        bank,cur=page['bank'],page['currency']
        if bank not in ('BOC','HLB','SBI'):continue
        terms=index.get(page['id'].removesuffix('-rates')+'-terms')
        if bank=='BOC' or (bank=='HLB' and cur not in ('SGD','USD')):
            if not terms:raise ValueError('Board source eligibility grid missing: '+page['id'])
            packet=dict(bank=bank,currency=cur,pages=[page['id'],terms['id']],layout='horizontal' if bank=='BOC' else 'vertical')
            offers,_=normalize_table(packet,reference(packet,index),index,'llm');rows.extend(offers)
            continue
        if bank=='HLB':
            if not terms:raise ValueError('HLB placement terms missing')
            if cur=='SGD':
                tiers=[(number(m),None,ch,'SGD',False) for m,ch in re.findall(r'S\$([\d,]+)\s*\((online|branch)\)',terms['text'],re.I)]
                if len(tiers)!=2:raise ValueError('HLB SGD channel minima changed')
            else:
                packet=dict(bank=bank,currency=cur,pages=[page['id'],terms['id']],layout='vertical')
                parsed=reference(packet,index)
                from .board_batch import minimums
                tiers=[(str(m) if m is not None else None,None,ch,ac,eq) for m,ch,ac,eq in minimums(bank,cur,parsed['minimum_text'])]
            date=re.search(r'Effective Date:\s*(\d{2}-\w{3}-\d{4})',page['text'])
            if not date:raise ValueError('HLB source date missing')
            starts=literal_date(date[1])
        else:
            bounds=next((re.fullmatch(r'\s*([\d,]+)\s*(?:to|-|–)\s*<\s*([\d,]+(?:\s*mio)?)\s*',cell,re.I) for gridrow in page['grid'] for cell in gridrow if re.fullmatch(r'\s*([\d,]+)\s*(?:to|-|–)\s*<\s*([\d,]+(?:\s*mio)?)\s*',cell,re.I)),None)
            date=re.search(r'with effect from\s+(\d{2}[-.]\w+[-.]\d{4})',page['text'],re.I)
            if not bounds or not date:raise ValueError('SBI amount/date source headers changed')
            tiers=[(number(bounds[1]),number(bounds[2]),'unknown',cur,False)];starts=literal_date(date[1])
        basis='annual_nominal' if re.search(r'p\.?\s*a\.?|per annum|annual',page['text'],re.I) else 'unknown'
        for source in table_rows(page):
            term=tenor(source['tenor_text']);value=rate(source['rate_text'])
            if term is None or value is None:continue
            for minimum,maximum,channel,amount_currency,equivalent in tiers:
                rows.append(normalize(dict(bank=bank,product_id=bank.lower()+'-'+cur.lower()+'-board',product_name=bank+' '+cur+' Board Fixed Deposit',
                    currency=cur,rate_type='board',tenor_value=term[0],tenor_unit=term[1],audience='all',channel=channel,
                    amount_min=minimum,amount_max=maximum,min_inclusive=True,max_inclusive=False,amount_currency=amount_currency,amount_is_equivalent=equivalent,
                    fresh_funds='unknown',conditions='挂牌报价；完整条款待复核'+('；原表未明确年化单位，禁止自动填入年利率表' if basis=='unknown' else ''),
                    rate_pct=value,rate_basis=basis,valid_from=starts,valid_to=None,availability='available',
                    evidence=[dict(page_id=page['id'],quote=source['rate_text'],locator=page['id'])]+([dict(page_id=terms['id'],quote=terms['text'],locator=terms['id'])] if terms else []))))
    return rows
