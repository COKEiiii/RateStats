"""Independent literal source slots; coverage is never inferred from model rows."""
import re
from collections import Counter
from decimal import Decimal


BOC_SGD_BUNDLE = 'boc-sgd-smartsaver-bundle'


def boc_bundle_inventory(text):
    """Read the counter bundle's nominal and effective columns and shared minimum."""
    lines = [line for line in text.splitlines() if line.strip()]
    if (len(lines) < 3 or '定期和储蓄通账户组合促销利率' not in lines[0]
            or not re.fullmatch(r'年利率[^\t]*\t有效年利率[^\t]*\t起存金额[^\t]*新元[^\t]*', lines[1])):
        raise ValueError('BOC SmartSaver bundle column order or headers changed')
    slots = []
    minimum = None
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.split('\t') if cell.strip()]
        tenor = re.fullmatch(r'(\d+)\s*个?月', cells[0])
        if not tenor or len(cells) not in (3, 4) or any(not re.fullmatch(r'\d+(?:\.\d+)?%?', rate) for rate in cells[1:3]):
            raise ValueError('BOC SmartSaver bundle row unreadable: ' + line)
        if len(cells) == 4:
            amount = re.fullmatch(r'S\$\s*(\d[\d,]*)', cells[3])
            if not amount:
                raise ValueError('BOC SmartSaver bundle minimum unreadable: ' + line)
            minimum = amount[1].replace(',', '')
        if minimum is None:
            raise ValueError('BOC SmartSaver bundle minimum missing')
        nominal, effective = (Decimal(rate.rstrip('%')) for rate in cells[1:3])
        slots.append(dict(tenor_value=int(tenor[1]), amount_min=minimum, amount_max=None,
                          rate_pct=str(nominal), effective=str(effective), quote=line.strip()))
    if not slots or len({slot['tenor_value'] for slot in slots}) != len(slots):
        raise ValueError('BOC SmartSaver bundle has missing or duplicate rows')
    return slots


def boc_bundle_terms(pages):
    from .extraction_contract import compact, dates_in_quote

    terms = [page for page in pages if page.get('bank') == 'BOC' and
             page.get('terms_role') == 'primary' and page.get('product_ids') == [BOC_SGD_BUNDLE]]
    if len(terms) != 1:
        raise ValueError('BOC SmartSaver terms PDF missing or ambiguous')
    page = terms[0]
    text = compact(page['text'])
    period = re.search(
        r'The Promotion is valid from (\d{1,2} [A-Za-z]+ 20\d{2})(?: \([^)]+\))? '
        r'(?:and remains valid until such further date|(?:to|until) (\d{1,2} [A-Za-z]+ 20\d{2}))',
        text, re.I)
    placement = re.search(r'a minimum of S\$([\d,]+) in an SGD TD account\. '
                          r'Both fresh and existing funds are eligible, with no maximum placement limit', text, re.I)
    savings = re.search(r'balance equivalent to (\d+(?:\.\d+)?)% of the TD placement amount '
                        r'\(minimum S\$([\d,]+)\)', text, re.I)
    channel = re.search(r'available exclusively over the counter at all BOCSG branches', text, re.I)
    if not all((period, placement, savings, channel)):
        raise ValueError('BOC SmartSaver bundle period or eligibility terms changed')
    dates = tuple(next(iter(dates_in_quote(value))) if value else None for value in period.groups())
    evidence = [dict(page_id=page['id'], quote=match[0], locator='SmartSaver bundle terms')
                for match in (period, placement, savings, channel)]
    return dates, placement[1].replace(',', ''), savings[1], savings[2], evidence


def boc_period(text,product):
    prefix=r'\*\*\s*' if product=='boc-sgd-welcome' else r'本次个人定期存款促销利率有效期为'
    m=re.search(prefix+r'(20\d{2})年(\d{1,2})月(\d{1,2})日至(20\d{2})年(\d{1,2})月(\d{1,2})日',text)
    if not m:raise ValueError('BOC product-specific promotion period missing: '+product)
    from datetime import date
    nums=list(map(int,m.groups()))
    return date(*nums[:3]).isoformat(),date(*nums[3:]).isoformat()


def boc_inventory(text):
    """Read BOC's SGD mobile grids, including blank rowspan continuation cells."""
    if '年利率' not in text or '起存金额' not in text or '新元' not in text:
        raise ValueError('BOC table headers/currency missing')
    tenor = None
    slots = []
    for line in text.splitlines():
        cells = [x.strip() for x in line.split('\t') if x.strip()]
        if not cells:
            continue
        if cells[0] == '新元':
            cells = cells[1:]
        if cells and (m := re.fullmatch(r'(\d+)\s*个?月', cells[0])):
            tenor = int(m[1]); cells = cells[1:]
        if len(cells) != 2:
            continue
        amount, rate = cells
        if not re.fullmatch(r'\d[\d,]*(?:\.\d+)?(?:\s*[-–—]\s*\d[\d,]*(?:\.\d+)?)?', amount):
            continue
        if tenor is None or not re.fullmatch(r'\d+(?:\.\d+)?%?', rate):
            raise ValueError('BOC amount/rate row lacks an unambiguous tenor: '+line)
        bounds = re.split(r'\s*[-–—]\s*', amount.replace(',', ''))
        slots.append(dict(tenor_value=tenor, amount_min=str(Decimal(bounds[0])),
                          amount_max=str(Decimal(bounds[1])) if len(bounds)==2 else None,
                          rate_pct=str(Decimal(rate.rstrip('%'))), quote=line.strip()))
    if not slots:
        raise ValueError('BOC source contains no readable rate slots')
    identities=[(s['tenor_value'],s['amount_min'],s['amount_max']) for s in slots]
    if len(set(identities)) != len(identities):
        raise ValueError('BOC source contains duplicate tenor/amount slots')
    return slots


def verify_boc_inventory(offers, pages):
    audit=[]
    for page in pages:
        if page.get('bank') != 'BOC':
            continue
        for unit in page.get('units', []):
            if unit.get('kind') != 'rates':
                continue
            for product in unit['product_ids']:
                slots = boc_bundle_inventory(unit['text']) if product == BOC_SGD_BUNDLE else boc_inventory(unit['text'])
                expected=Counter((s['tenor_value'],s['amount_min'],s['amount_max'],Decimal(s['rate_pct'])) for s in slots)
                product_offers = [r for r in offers if r['product_id'] == product]
                actual=Counter((r['tenor_value'],r['amount_min'],r['amount_max'],Decimal(r['rate_pct'])) for r in product_offers)
                if actual != expected:
                    raise ValueError(f'BOC source coverage mismatch {product}: missing={list((expected-actual).elements())}; extra={list((actual-expected).elements())}')
                if product == BOC_SGD_BUNDLE:
                    from .schema import financial_terms
                    period, minimum, percentage, _, _ = boc_bundle_terms(pages)
                    effective = {s['tenor_value']: s['effective'] for s in slots}
                    if any(r['amount_min'] != minimum or r['channel'] != 'branch' or
                           financial_terms(r) != (effective[r['tenor_value']], percentage)
                           for r in product_offers):
                        raise ValueError('BOC SmartSaver bundle eligibility or effective-rate mismatch')
                else:
                    terms='\n'.join(u['text'] for u in page.get('units',[]) if u.get('kind')=='terms' and product in u.get('product_ids',[]))
                    period = boc_period(terms, product) if terms else None
                if period and any((r['valid_from'],r['valid_to']) != period for r in product_offers):
                    raise ValueError('BOC product period mismatch: '+product)
                audit.append(dict(bank='BOC',product_id=product,page_id=page['id'],expected=sum(expected.values()),verified=sum(actual.values()),passed=True))
    return audit


def singfinance_inventory(text):
    amounts=re.search(r'\$([\d,]+)\s*\t\s*\$([\d,]+)',text)
    if not amounts:raise ValueError('SingFinance minimum column headers missing')
    minimums=[str(Decimal(x.replace(',',''))) for x in amounts.groups()]
    out=[]
    for line in text.splitlines():
        cells=line.split('\t')
        if not cells or not (m:=re.fullmatch(r'(\d+)\s*months?',cells[0],re.I)):continue
        if len(cells)!=3:raise ValueError('SingFinance source column count changed')
        for minimum,cell in zip(minimums,cells[1:]):
            rate=re.fullmatch(r'(\d+\.\d+)\s*%\s*p\.?a\.?',cell.strip(),re.I)
            if not rate:raise ValueError('SingFinance rate cell unreadable')
            out.append(dict(tenor_value=int(m[1]),amount_min=minimum,rate_pct=rate[1],quote=line))
    if not out:raise ValueError('SingFinance source grid empty')
    return out


def verify_source_inventory(offers,pages):
    """Literal cell counts are independent of model-declared coverage.

    Count only isolated cells of the captured product units. HLF's calculator
    is outside its published grid. Inline headline sources have explicit %.
    BOC and SingFinance additionally bind the tenor and the minimum column.
    """
    audit=verify_boc_inventory(offers,pages)
    products={};associations={}
    for page in pages:
        if page.get('bank')=='BOC':continue
        if page.get('terms_role')=='primary' and not page.get('units'):
            text=page.get('text','')
            if page['bank']=='SCB' and 'TENOR' in text and 'PROMOTIONAL INTEREST RATE' in text:
                rates=[Decimal(x) for x in re.findall(r'(\d+\.\d+)\s*%',text.split('PROMOTIONAL INTEREST RATE',1)[1])]
                products.setdefault(('SCB','scb-sgd-fresh-funds'),[]).extend(rates)
            if page['bank']=='HLB' and text.startswith('[PDF page') and 'Promotional Interest Rate (p.a.)' in text:
                table=text.split('Promotional Interest Rate (p.a.)',1)[1].split('5.',1)[0]
                rates=[Decimal(x) for x in re.findall(r'(\d+\.\d+)\s*%',table)]
                products.setdefault(('HLB','hlb-sgd-promo'),[]).extend(rates)
            if 'cimb-preferred-welcome' in page.get('product_ids',[]):
                table=text.split('Welcome Promotion',1)[-1].split('5.',1)[0]
                rates=list(dict.fromkeys(Decimal(x) for x in re.findall(r'(\d+\.\d+)\s*%',table)))
                if len(rates)!=1:raise ValueError('CIMB welcome rate table changed')
                products.setdefault(('CIMB','cimb-preferred-welcome'),[]).extend(rates)
        for unit in page.get('units',[]):
            if unit.get('kind')!='rates':continue
            text=unit['text']
            if page['bank']=='HLF':
                text=text.split('Deposit Amount',1)[-1].split('Published rates',1)[0]
            cells=[x.strip() for x in re.split(r'[\t\n]',text) if x.strip()]
            pattern=r'(\d{1,2}\.\d+)\s*(?:%\s*(?:p\.?\s*a\.?)?)?'
            rates=[Decimal(m[1]) for cell in cells if (m:=re.fullmatch(pattern,cell,re.I))]
            if not rates:
                rates=[Decimal(x) for x in re.findall(r'(?<![\d.])(\d{1,2}\.\d+)\s*%',text)]
            if not rates:raise ValueError('Source rate inventory unreadable: '+page['bank']+' '+str(unit['product_ids']))
            if len(unit['product_ids'])!=1:raise ValueError('Rate inventory requires a product-bound unit')
            key=(page['bank'],unit['product_ids'][0]);products.setdefault(key,[]).extend(rates)
            if page['bank']=='HSBC':
                segment=('premier_elite' if 'Premier Elite' in text else 'premier_wealth' if 'with wealth holdings' in text else
                         'premier_standard' if 'without wealth holdings' in text else 'personal' if 'Personal Banking' in text else None)
                if segment is None:raise ValueError('HSBC source audience heading unreadable')
                entries=[(int(n),segment,Decimal(rate)) for n,rate in re.findall(r'(\d+)-month\s*\t\s*(\d+\.\d+)\s*%',text,re.I)]
                if not entries:raise ValueError('HSBC tenor/rate rows unreadable')
                associations.setdefault(key,[]).extend(entries)
            if page['bank']=='RHB':
                columns=re.findall(r'\b(Personal|Premier) Banking',text.split('Minimum Placement',1)[0],re.I)
                if len(columns)!=2:raise ValueError('RHB customer columns unreadable')
                for n,a,b,_ in re.findall(r'(\d+)-month\s+(\d+\.\d+)%\s+(\d+\.\d+)%\s+([\d,]+)',text,re.I):
                    associations.setdefault(key,[]).extend((int(n),audience.lower(),Decimal(rate)) for audience,rate in zip(columns,[a,b]))
                if not associations.get(key):raise ValueError('RHB source rows unreadable')
            if page['bank']=='SingFinance':
                slots=singfinance_inventory(unit['text'])
                expected=Counter((s['tenor_value'],s['amount_min'],Decimal(s['rate_pct'])) for s in slots)
                actual=Counter((r['tenor_value'],r['amount_min'],Decimal(r['rate_pct'])) for r in offers if r['product_id']==key[1])
                if expected!=actual:raise ValueError('SingFinance tenor/minimum/rate coverage mismatch')
    for (bank,product),rates in products.items():
        expected=Counter(rates);actual=Counter(Decimal(r['rate_pct']) for r in offers if r['bank']==bank and r['product_id']==product)
        if expected!=actual:
            raise ValueError(f'Source rate coverage mismatch {bank}/{product}: missing={list((expected-actual).elements())}; extra={list((actual-expected).elements())}')
        if (bank,product) in associations:
            actual=Counter((r['tenor_value'],r['audience'],Decimal(r['rate_pct'])) for r in offers if r['bank']==bank and r['product_id']==product)
            if actual!=Counter(associations[bank,product]):raise ValueError(f'Source tenor/audience/rate association mismatch {bank}/{product}')
        audit.append(dict(bank=bank,product_id=product,expected=sum(expected.values()),verified=sum(actual.values()),passed=True))
    verify_retail_slots(offers,pages)
    return audit


def retail_source_slots(bank,unit):
    """Bind printed tenor/rate columns and source-labelled amount/channel axes."""
    text=unit['text'];pid=unit['product_ids'][0];slots=[]
    def add(n,rate,**facts):slots.append(dict(tenor_value=int(n),rate_pct=Decimal(rate),**facts))
    money=lambda x:Decimal(x.replace(',',''))
    if bank=='OCBC':
        channel=None;minimum=None
        for line in text.splitlines():
            cells=[x.strip() for x in line.split('\t') if x.strip()]
            if cells and cells[0]=='Singapore Dollar':cells=cells[1:]
            if not cells or not cells[0].isdigit():continue
            n=cells.pop(0)
            if cells and cells[0] in ('Branch','Online'):channel=cells.pop(0).lower()
            if not cells or not (rate:=re.fullmatch(r'(\d+\.\d+)%',cells[0])):raise ValueError('OCBC rate row unreadable')
            if m:=re.search(r'S\$([\d,]+)',line):minimum=money(m[1])
            if channel is None or minimum is None:raise ValueError('OCBC merged row context missing')
            add(n,rate[1],channel=channel,amount_min=minimum)
    elif bank in ('SBI','UOB'):
        if bank=='SBI':
            for n,rate,minimum in re.findall(r'SGD\s+(\d+)-month\s+(\d+\.\d+)%\s+SGD\s+([\d,]+)',text,re.I):add(n,rate,amount_min=money(minimum))
        else:
            m=re.search(r'Minimum\s+S\$([\d,]+)',text,re.I)
            if not m:raise ValueError('UOB minimum placement header missing')
            for n,rate in re.findall(r'(\d+)-month\s+(?:Minimum\s+S\$[\d,]+\s+)?(\d+\.\d+)%',text,re.I):add(n,rate,amount_min=money(m[1]))
    elif bank=='Maybank' and pid=='maybank-sgd-standalone':
        m=re.search(r'Minimum placement of S\$([\d,]+)',text,re.I)
        if not m:raise ValueError('Maybank standalone minimum missing')
        for n,rate in re.findall(r'(\d+)\s+months?\s+Individual customers only\s+(\d+\.\d+)%',text,re.I):add(n,rate,amount_min=money(m[1]),audience='personal')
    elif bank=='BEA':
        for rate,n in re.findall(r'(\d+\.\d+)%\s*p\.a\.\*?\s+For\s+(\d+)\s+months?',text,re.I):add(n,rate)
    elif bank=='Singapura Finance':
        m=re.search(r'(?:minimum deposit amount.*?S\$|Tier\s*/\s*Tenure\s+S\$)([\d,]+)',text,re.I|re.S)
        if not m:raise ValueError('Singapura minimum column missing')
        for n,rate in re.findall(r'(\d+)\s+Months?\s+(\d+\.\d+)%',text,re.I):add(n,rate,amount_min=money(m[1]))
    elif bank=='CIMB' and pid=='cimb-wwfd-online':
        m=re.search(r'\$S\$([\d,]+)',text)
        if not m:raise ValueError('CIMB WWFD minimum header missing')
        for n,rate in re.findall(r'(\d+)\s+Months?\s+(\d+\.\d+)',text,re.I):add(n,rate,amount_min=money(m[1]),audience='personal')
    elif bank=='CITI':
        rates=re.findall(r'(?<![\d.])(\d+\.\d+)%',text)
        if pid=='citi-sgd-promo':
            tenors=re.findall(r'(\d+)\s+Months?',text,re.I);bounds=re.search(r'SGD\s*/\s*S\$([\d,]+)\s*[–-]\s*S\$([\d,]+)',text)
            if not bounds or len(tenors)!=len(rates):raise ValueError('Citi amount/tenor headers unreadable')
            for n,rate in zip(tenors,rates):add(n,rate,amount_min=money(bounds[1]),amount_max=money(bounds[2]))
        elif 'investment-bundle' in pid:
            ns=set(re.findall(r'SGD\s+(\d+)-month',text,re.I))
            if len(ns)!=1 or len(rates)!=2:raise ValueError('Citi investment columns unreadable')
            for rate,fresh in zip(rates,['yes','no']):add(next(iter(ns)),rate,audience='private' if 'private' in pid else 'preferred',fresh_funds=fresh)
    elif bank=='HLF':
        grid=text.split('Deposit Amount',1)[-1].split('Published rates',1)[0]
        header=grid.splitlines()[0];tenors=re.findall(r'(\d+)-Month',header,re.I)
        for line in grid.splitlines()[1:]:
            if not line.strip():continue
            cells=[x.strip() for x in line.split('\t') if x.strip()]
            if not cells or not cells[0].startswith('S$'):continue
            minimum=re.search(r'S\$([\d,]+)',cells[0]);rates=[re.fullmatch(r'(\d+\.\d+)%',x) for x in cells[1:]]
            if not minimum or len(rates)!=len(tenors) or not all(rates):raise ValueError('HLF amount/rate matrix changed')
            for n,rate in zip(tenors,rates):add(n,rate[1],amount_min=money(minimum[1]))
    return slots


def verify_retail_slots(offers,pages):
    expected={}
    for page in pages:
        for unit in page.get('units',[]):
            if unit.get('kind')!='rates' or len(unit['product_ids'])!=1:continue
            slots=retail_source_slots(page['bank'],unit)
            if slots:expected.setdefault((page['bank'],unit['product_ids'][0]),[]).extend(slots)
    for (bank,pid),slots in expected.items():
        fields=tuple(sorted(slots[0]))
        if any(set(s)!=set(fields) for s in slots):raise ValueError('Inconsistent source slot schema')
        def signature(r):
            return tuple(Decimal(str(r[k])) if k in ('rate_pct','amount_min','amount_max') and r[k] is not None else r[k] for k in fields)
        actual=[r for r in offers if r['bank']==bank and r['product_id']==pid]
        if Counter(map(signature,slots))!=Counter(map(signature,actual)):
            raise ValueError(f'Source tenor/amount/channel association mismatch: {bank}/{pid}')
