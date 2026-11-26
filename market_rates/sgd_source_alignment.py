"""Bind literal table cells using each extraction lane's own evidence only."""
import re
from copy import deepcopy
from decimal import Decimal

def align_tables(offers,terms,pages,lane):
    if lane!='llm':raise ValueError('Only the text LLM lane is supported')
    changes=[]
    # The printed tenor is a rowspan. A continuation amount row must inherit
    # that tenor, never become the next tenor or disappear from the quote set.
    from .source_inventory import (
        BOC_SGD_BUNDLE, boc_bundle_inventory, boc_bundle_terms, boc_inventory, boc_period,
        verify_boc_inventory,
    )
    for page in pages:
        if page.get('bank')!='BOC':continue
        for unit in page.get('units',[]):
            if unit.get('kind')!='rates':continue
            for pid in unit['product_ids']:
                originals=[r for r in offers if r['product_id']==pid]
                if not originals:raise ValueError('BOC source product lacks model extraction: '+pid)
                term_labels={page['id']+':'+str(i+1) for i,u in enumerate(page.get('units',[])) if u.get('kind')=='terms' and pid in u.get('product_ids',[])}
                own_terms=[t for t in terms if t['product_id']==pid and t['page_id']==page['id'] and t['locator'] in term_labels]
                if pid == BOC_SGD_BUNDLE:
                    if len(own_terms) != 1:
                        raise ValueError('BOC SmartSaver on-page terms missing or ambiguous')
                    period, minimum, percentage, savings_minimum, evidence = boc_bundle_terms(pages)
                    note = own_terms[0]
                    rebuilt = []
                    for slot in boc_bundle_inventory(unit['text']):
                        if slot['amount_min'] != minimum:
                            raise ValueError('BOC SmartSaver table and PDF minimums disagree')
                        row = deepcopy(originals[0])
                        row.update(
                            tenor_value=slot['tenor_value'], tenor_unit='M', amount_min=minimum,
                            amount_max=None, min_inclusive=True, max_inclusive=False,
                            rate_pct=slot['rate_pct'], audience='personal', channel='branch', fresh_funds='no',
                            conditions=(f'组合有效年利率：{slot["effective"]}%；附加存款比例：{percentage}%'
                                        f'；SmartSaver 须另存至少 S${savings_minimum} 并保留至定存到期'),
                            valid_from=period[0], valid_to=period[1],
                            evidence=[
                                dict(page_id=page['id'], quote=slot['quote'], locator='SmartSaver bundle rate row'),
                                dict(page_id=page['id'], quote=note['text'], locator=note['locator']),
                                *evidence,
                            ],
                        )
                        rebuilt.append(row)
                    changes.append(dict(bank='BOC', product_id=pid, field='source_table_grid',
                                        before=len(originals), after=len(rebuilt), lane=lane))
                    offers[:] = [r for r in offers if r['product_id'] != pid] + rebuilt
                    continue
                condition_lines=[line.strip() for t in own_terms for line in t['text'].splitlines() if line.strip()]
                if pid=='boc-sgd-mobile':condition_lines=[line for line in condition_lines if not line.startswith('**')]
                if pid=='boc-sgd-welcome':condition_lines=[line for line in condition_lines if '本次个人定期存款促销利率有效期' not in line]
                period=None
                if own_terms:
                    period=boc_period('\n'.join(t['text'] for t in own_terms),pid)
                rebuilt=[]
                for n,slot in enumerate(boc_inventory(unit['text'])):
                    row=deepcopy(originals[0])
                    row.update(tenor_value=slot['tenor_value'],tenor_unit='M',amount_min=slot['amount_min'],amount_max=slot['amount_max'],
                               min_inclusive=True,max_inclusive=slot['amount_max'] is not None,
                               rate_pct=slot['rate_pct'],audience='personal',channel='online',
                               conditions='；'.join(condition_lines),
                               evidence=[dict(page_id=page['id'],quote=slot['quote'],locator=f'{pid}/source-table-row-{n+1}')])
                    for t in own_terms:
                        row['evidence'].append(dict(page_id=page['id'],quote=t['text'],locator=t['locator']))
                    if period:row.update(valid_from=period[0],valid_to=period[1])
                    rebuilt.append(row)
                changes.append(dict(bank='BOC',product_id=pid,field='source_table_grid',before=len(originals),after=len(rebuilt),lane=lane))
                offers[:]=[r for r in offers if r['product_id']!=pid]+rebuilt
    verify_boc_inventory(offers,pages)
    from .source_inventory import singfinance_inventory
    for page in pages:
        if page.get('bank')!='SingFinance':continue
        for unit in page.get('units',[]):
            if unit.get('kind')!='rates':continue
            for pid in unit['product_ids']:
                originals=[r for r in offers if r['product_id']==pid]
                if not originals:raise ValueError('SingFinance model extraction missing: '+pid)
                rebuilt=[]
                for n,slot in enumerate(singfinance_inventory(unit['text'])):
                    row=deepcopy(originals[0]);quote=slot.pop('quote')
                    row.update(slot,tenor_unit='M',amount_max=None,min_inclusive=True,max_inclusive=False,
                               evidence=[dict(page_id=page['id'],quote=quote,locator=f'{pid}/source-grid-{n+1}')]+[e for e in originals[0]['evidence'] if 'terms' in e['locator']])
                    rebuilt.append(row)
                offers[:]=[r for r in offers if r['product_id']!=pid]+rebuilt
                changes.append(dict(bank='SingFinance',product_id=pid,field='source_table_grid',before=len(originals),after=len(rebuilt),lane=lane))
    icbc=[r for r in offers if r['bank']=='ICBC']
    if icbc:
        rebuilt=[]
        for t in terms:
            text=re.sub(r'\s+',' ',t['text']).strip()
            match=re.search(r'(Counter|E-Banking) Promotion Rates',text,re.I)
            if not match:continue
            channel='branch' if match[1].lower()=='counter' else 'online'
            heads=re.findall(r'SGD([\d,.]+K?)\s*\(inclusive\)\s*(?:to SGD([\d,.]+K?)|& Above)',text,re.I)
            expected=2 if channel=='branch' else 3
            if len(heads)!=expected:raise ValueError('ICBC amount columns ambiguous')
            def money(s):
                return str(int(Decimal(s.rstrip('Kk').replace(',',''))*(1000 if s.lower().endswith('k') else 1))) if s else None
            templates=[r for r in icbc if any(e['page_id']==t['page_id'] for e in r['evidence'])]
            if not templates:raise ValueError('ICBC own table lacks source template')
            pattern=r'(\d+)\s+(months?|years?)\s+'+r'\s+'.join([r'([\d.]+)%']*expected)
            rows=list(re.finditer(pattern,text,re.I))
            if not rows:raise ValueError('ICBC empty rate grid')
            for m in rows:
                tenor=int(m[1])*(12 if m[2].lower().startswith('year') else 1)
                for (lo,hi),rate in zip(heads,m.groups()[2:]):
                    template=next((r for r in templates if r['channel']==channel and r['amount_min']==money(lo)),templates[0])
                    row=deepcopy(template)
                    row.update(tenor_value=tenor,tenor_unit='M',channel=channel,amount_min=money(lo),amount_max=money(hi),min_inclusive=True,max_inclusive=False,rate_pct=str(Decimal(rate).normalize()))
                    row['evidence'].append(dict(page_id=t['page_id'],quote=m[0],locator=t['locator']+' / own table column '+lo))
                    rebuilt.append(row)
        if rebuilt:
            changes.append(dict(bank='ICBC',field='table_grid',before=len(icbc),after=len(rebuilt),lane=lane,source='own text or screenshot transcript'))
            offers[:]=[r for r in offers if r['bank']!='ICBC']+rebuilt
    units=[(p,u) for p in pages for u in p.get('units',[]) if u['kind']=='rates' and u['product_ids']==['cimb-sgd-online']]
    cimb=[r for r in offers if r['product_id']=='cimb-sgd-online']
    if units or cimb:
        if len(units)!=1:raise ValueError('CIMB needs one unambiguous own-text rate table')
        page,unit=units[0];text=unit['text'];grid={}
        headings=re.findall(r'\b(PERSONAL|PREFERRED)\s+BANKING\b',text,re.I)
        if len(headings)!=2 or {h.lower() for h in headings}!={'personal','preferred'}:
            raise ValueError('CIMB customer columns missing or ambiguous')
        for m in re.finditer(r'(\d+)\s+Months?\s+([\d.]+)\s+([\d.]+)(?=\s|$)',text,re.I):
            for audience,rate in zip([h.lower() for h in headings],m.groups()[1:]):
                key=(int(m[1]),audience)
                if key in grid:raise ValueError('Duplicate CIMB tenor row')
                grid[key]=(str(Decimal(rate).normalize()),m[0])
        # CIMB's HTML table can extract cleanly while a small local model swaps
        # a continuation cell onto the wrong tenor (or omits that cell). The
        # captured, page-bound table is the authoritative grid, so rebuild this
        # product's tenor/audience/rate slots from it instead of rejecting an
        # otherwise complete bank batch. Keep the model's audience templates
        # for the non-grid terms and conditions, and require one template for
        # each audience printed by the source.
        templates={audience:next((r for r in cimb if r['audience']==audience),None)
                   for audience in {h.lower() for h in headings}}
        if not grid or any(template is None for template in templates.values()):
            raise ValueError('CIMB source table or audience extraction is incomplete')
        rebuilt=[]
        audience_order=[h.lower() for h in headings]
        for (tenor,audience),(rate,quote) in sorted(grid.items(),key=lambda item:(item[0][0],audience_order.index(item[0][1]))):
            row=deepcopy(templates[audience])
            row.update(tenor_value=tenor,tenor_unit='M',audience=audience,rate_pct=rate)
            row['evidence']=[e for e in row['evidence'] if e['page_id']!=page['id']]
            row['evidence'].append(dict(page_id=page['id'],quote=quote,
                                        locator='source table row / '+audience))
            rebuilt.append(row)
        if len(rebuilt)!=len(grid):
            raise ValueError('CIMB source table did not produce unique customer/tenor slots')
        changes.append(dict(bank='CIMB',product_id='cimb-sgd-online',field='source_table_grid',
                            before=len(cimb),after=len(rebuilt),lane=lane,
                            source='captured own-text rate table'))
        offers[:]=[r for r in offers if r['product_id']!='cimb-sgd-online']+rebuilt
    for row in offers:
        if row['product_id']=='citi-private-investment-bundle':
            if row['audience']!='private':
                changes.append(dict(product=row['product_id'],field='audience',before=row['audience'],after='private',lane=lane,source='own private investment bundle product category'))
            row['audience']='private'
        if row['product_id']=='cimb-sgd-online' and lane=='llm':
            key=(row['tenor_value'],row['audience'])
            if row['tenor_unit']!='M' or key not in grid:raise ValueError('CIMB rate row association missing')
            rate,quote=grid[key]
            if row['rate_pct']!=rate:
                changes.append(dict(product=row['product_id'],tenor=row['tenor_value'],audience=row['audience'],field='rate_pct',before=row['rate_pct'],after=rate,lane=lane,source='own captured text grid'))
                row['rate_pct']=rate
                row['evidence'].append(dict(page_id=page['id'],quote=quote,locator='source table row / '+row['audience']))
        if row['bank']=='HLF':
            own=[t for t in terms if t['product_id']==row['product_id'] and t['page_id']==row['evidence'][0]['page_id']]
            matches=[]
            for t in own:
                table=re.search(r'Deposit Amount(.*?)Published rates',t['text'],re.S|re.I)
                if not table:continue
                for m in re.finditer(r'S\$([\d,]+)\s+to\s*(<|≤)\s*S\$([\d,]+)\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%',table[1]):
                    if Decimal(m[1].replace(',',''))!=Decimal(row['amount_min'] or '-1'):continue
                    rate_map=dict(zip([6,9,12],map(Decimal,m.groups()[3:])))
                    if row['tenor_value'] not in rate_map or rate_map[row['tenor_value']]!=Decimal(row['rate_pct']):raise ValueError('HLF own transcript rate does not match tier')
                    matches.append((m[3].replace(',',''),m[2]=='≤',t,m[0]))
            if len(matches)>1:raise ValueError('Ambiguous HLF own transcript range')
            if matches:
                maximum,inclusive,t,quote=matches[0]
                if (row['amount_max'],row['max_inclusive'])!=(maximum,inclusive):
                    changes.append(dict(product=row['product_id'],tenor=row['tenor_value'],field='amount_max',before=row['amount_max'],after=maximum,lane=lane,source='own text' if lane=='llm' else 'own screenshot transcript'))
                    row['amount_max']=maximum;row['max_inclusive']=inclusive
                    row['evidence'].append(dict(page_id=t['page_id'],quote=quote,locator=t['locator']+' / deposit tier'))
    return changes
