"""Foreign-currency promotions from checked literal tables. No remote model calls."""
import re
import unicodedata
from urllib.parse import urlsplit
from datetime import datetime
from decimal import Decimal
from .schema import normalize
from .workbook_policy import presentation_currency

def numeric(text):
    m=re.search(r'[+-]?\d[\d,]*(?:\.\d+)?',text)
    if not m:raise ValueError('Missing numeric value: '+text)
    return str(Decimal(m[0].replace(',','')))

def tenor(text):
    n=int(numeric(text))
    return (n*12,'M') if 'year' in text.lower() else (n*7,'D') if 'week' in text.lower() else (n,'M')

def rate_columns(labels,values,bank):
    """Never let zip silently discard an added/missing source column."""
    if not labels or len(labels)!=len(values):
        raise ValueError(f'{bank} promotion tenor/rate column count changed: {len(labels)} != {len(values)}')
    return zip(labels,values)

def icbc_amount_bands(headers,minimum,currency):
    """Read all published bands; ambiguous or overlapping boundaries must fail."""
    number=r'(\d[\d,]*(?:\.\d+)?[KM]?)'
    prefix=r'(?:'+('(?:RMB|CNY)' if currency=='CNY' else 'USD')+r')?'
    atom=prefix+number
    def value(raw):
        return Decimal(raw.rstrip('KM').replace(',',''))*({'K':1000,'M':1000000}.get(raw[-1],1))
    bands=[]
    for header in headers:
        text=re.sub(r'\s+','',unicodedata.normalize('NFKC',header)).upper()
        below=re.fullmatch(r'BELOW'+atom+r'(?:\(EXCLUSIVE\))?',text)
        interval=re.fullmatch(atom+r'(?:\(INCLUSIVE\))?(?:TO|[-–])'+atom+r'(?:\(EXCLUSIVE\))?',text)
        above=re.fullmatch(atom+r'(?:\(INCLUSIVE\))?(?:&|AND)?ABOVE',text)
        if below and not bands:lo,hi=Decimal(minimum),value(below[1])
        elif interval:lo,hi=value(interval[1]),value(interval[2])
        elif above:lo,hi=value(above[1]),None
        else:raise ValueError('ICBC amount tiers/header not understood: '+header)
        if lo<Decimal(minimum) or (hi is not None and lo>=hi) or (bands and bands[-1][1]!=lo):
            raise ValueError('ICBC amount tiers overlap, gap or invalid minimum')
        bands.append((lo,hi))
    if not bands:raise ValueError('ICBC amount tiers missing')
    return bands

def icbc_minimums(section):
    text=' '.join(e['quote'] for e in section.get('extra_evidence',[]))
    currency='(?:RMB|CNY)' if section['currency']=='CNY' else 'USD'
    amounts=[]
    for channel in [r'E[- ]banking',r'over\s+counter']:
        found=re.findall(channel+r'[^.;；]{0,90}?'+currency+r'\s*(\d[\d,]*(?:\.\d+)?)',text,re.I)
        values={Decimal(x.replace(',','')) for x in found}
        if len(values)!=1:raise ValueError('ICBC channel minimum missing or ambiguous: '+channel)
        amounts.append(next(iter(values)))
    return amounts

def promotion_dates(s):
    if s['bank']=='BOC':
        for e in s.get('extra_evidence',[]):
            if '本次个人定期存款促销利率有效期' in e['quote']:
                dates=re.findall(r'(20\d{2})年(\d{1,2})月(\d{1,2})日',e['quote'])
                if len(dates)==2:return tuple(datetime(int(y),int(m),int(d)).date().isoformat() for y,m,d in dates)
        raise ValueError('Missing BOC ordinary promotion validity')
    pattern=r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})\b'
    if s['bank'] in ['BEA','ICBC','SBI']:return None,None
    quotes=sorted(s.get('extra_evidence',[]),key=lambda e:'terms' not in e['page_id'])
    for e in quotes:
        q=e['quote'];compact=re.sub(r'\s+',' ',q)
        if not (re.search(r'promot',q,re.I) and re.search(r'valid from|available from|commences on|begins|(?:promotion|promotional period)\s+starts on',compact,re.I)):continue
        matches=re.findall(pattern,q,re.I);dates=[]
        for d,m,y in matches:
            dates.append(datetime.strptime(f'{d} {m[:3]} {y}','%d %b %Y').date().isoformat())
        if dates:return dates[0],dates[1] if len(dates)>1 else None
    if s['bank']=='CITI' and s['layout'].endswith('new-funds'):
        for e in quotes:
            m=re.search(r'ends\s+'+pattern,e['quote'],re.I)
            if m:return None,datetime.strptime(f'{m[1]} {m[2][:3]} {m[3]}','%d %b %Y').date().isoformat()
    raise ValueError('Promotion validity must be read from current evidence: '+s['id'])

def normalize_fx(s,board=()):
    bank=s['bank'];layout=s['layout'];grid=s['rows'];out=[]
    if layout=='fx-promo-source-unavailable':
        validate_source_absence(s)
        return []
    evidence=[e for e in s.get('extra_evidence',[]) if e['quote'].strip()]
    def add(cur,label,value,lo=None,hi=None,*,audience='personal',channel='未列明',name='外币定存促销',fresh='unknown',conditions='',start=None,end=None,amount_cur=None,equivalent=False,ui=True,product=None,extra=None):
        if cur=='JPY':return
        n,u=tenor(label);r=dict(bank=bank,product_id=product or s['product_id'],product_name=name,currency=cur,rate_type='promo',tenor_value=n,tenor_unit=u,audience=audience,channel=channel,amount_min=str(lo) if lo is not None else None,amount_max=str(hi) if hi is not None else None,min_inclusive=True,max_inclusive=ui if hi is not None else False,amount_currency=amount_cur or cur,amount_is_equivalent=equivalent,fresh_funds=fresh,conditions=conditions or '按官网公布的外币定存条件',rate_pct=numeric(value),rate_basis='annual_nominal',valid_from=start,valid_to=end,availability='available',evidence=[dict(page_id=s['page_id'],quote=' | '.join([cur,label,value]),locator=s['id'])]+evidence+(extra or []))
        if s.get('source_date'):r['evidence'].append(dict(page_id=s['page_id'],quote='银行标注日期 '+s['source_date'],locator=s['id']+'/source-date'))
        if r['fresh_funds']=='required':r['fresh_funds']='yes'
        out.append(normalize(r));return out[-1]
    if bank=='BOC':
        names={'美元':'USD','澳元':'AUD','新西兰元':'NZD','欧元':'EUR','英镑':'GBP','人民币':'CNY'};cur=None;term=None;lo=None
        for physical in grid:
            row=list(physical)
            if row[0] in names:cur=names[row.pop(0)];lo=None
            if cur is None:continue
            if '个月' in row[0]:term=int(numeric(row.pop(0)))
            if len(row)==2:lo=numeric(row.pop(0))
            if len(row)!=1 or lo is None:raise ValueError('BOC merged-cell state changed')
            add(cur,str(term)+' months',row[0],lo,channel='手机银行',name='手机银行普通促销',product='boc-fx-mobile',conditions='普通新存单；须已有活期账户；非新客户产品')
        for r in out:
            higher=[x for x in out if x['currency']==r['currency'] and x['tenor_value']==r['tenor_value'] and Decimal(x['amount_min'])>Decimal(r['amount_min'])]
            if higher:r['amount_max']=min(higher,key=lambda x:Decimal(x['amount_min']))['amount_min'];r['max_inclusive']=False
    elif bank=='BEA':
        for row in grid[2:]:
            if len(row)<4:raise ValueError('BEA tier shape changed')
            for label,value in rate_columns(grid[1][1:],row[3:],bank):
                add(row[1],label,value,numeric(row[2]),name='Tier Rates',conditions='官网Tier Rates，沿用原调研分类；达到SGD500,000等值须另询银行，不能直接套用本表')
    elif bank=='CIMB':
        for row in grid[2:]:
            for label,value in rate_columns(grid[1],row[1:],bank):add(row[0],label,value,10000,1000000,channel='在线CIMB Clicks',start='2026-09-01',end='2026-09-30',conditions='须有对应外币活期/储蓄账户；个人或联名；到期按挂牌续存')
    elif bank=='RHB':
        if 'minimum' not in grid[0][-1].lower():raise ValueError('RHB minimum column moved')
        for row in grid[1:]:
            for label,value in rate_columns(grid[0][1:-1],row[1:-1],bank):add(row[0],label,value,numeric(row[-1]),start='2026-08-03',conditions='个人客户；新存及符合条件的到期续存；新旧资金均可')
    elif bank=='SBI':
        for row in grid[1:]:
            if len(row)!=4 or row[0].strip()!='USD':raise ValueError('SBI USD promotion row shape/currency changed')
            add('USD',row[1],row[2],numeric(row[3]),1000000,ui=False,channel='柜台/客户经理',conditions='新存和续存均可；每名客户同币种存款合计必须小于1,000,000，不是每笔额度')
        if not out:raise ValueError('SBI promotion has no rate rows')
    elif bank=='SCB':
        for row in grid[1:]:
            if len(row)!=3:raise ValueError('SCB promotion row shape changed')
            rates=re.findall(r'(Personal Banking|Priority Private Banking|Priority Banking)\s*:\s*([\d.]+)%',row[2])
            unmatched=re.sub(r'(Personal Banking|Priority Private Banking|Priority Banking)\s*:\s*([\d.]+)%','',row[2])
            if not rates or len({a for a,_ in rates})!=len(rates) or re.search(r'[\d%]',unmatched):raise ValueError('SCB customer rows missing, duplicated or unrecognized')
            for label,value in rates:add('USD',row[0],value,numeric(row[1]),audience={'Personal Banking':'personal','Priority Banking':'premier','Priority Private Banking':'private'}[label],channel='网银/手机',fresh='required',start='2026-09-01',end='2026-09-30',conditions=label+'；外部新资金，不含30天内提取再存；优惠须持有至到期')
        if not out:raise ValueError('SCB promotion has no rate rows')
    elif bank=='ICBC':
        cur=s['currency'];terms=grid[0][1:] if cur=='CNY' else [r[0] for r in grid[2:]]
        electronic_min,counter_min=icbc_minimums(s)
        headers=[r[0] for r in grid[1:]] if cur=='CNY' else grid[1][1:]
        bands=icbc_amount_bands(headers,electronic_min,cur)
        if cur=='USD':
            if not headers or any(len(r)!=len(headers)+1 for r in grid[2:]):raise ValueError('ICBC USD amount tier/rate column count changed')
        for bi,(lo,hi) in enumerate(bands):
            if cur=='CNY' and len(grid[bi+1])-1!=len(terms):raise ValueError('ICBC CNY tenor/rate column count changed')
            for ti,label in enumerate(terms):
                value=grid[bi+1][ti+1] if cur=='CNY' else grid[ti+2][bi+1]
                counter=hi is None or hi>counter_min
                add(cur,label,value,lo,hi,ui=False,channel='电子银行/柜台' if counter else '电子银行',conditions=f'电子银行{electronic_min:,}起存；'+(f'柜台{counter_min:,}起存；金额档位以本表上下界为准' if counter else f'本档仅适用电子银行，柜台{counter_min:,}起存'))
    elif bank=='HSBC':
        if s['currency']=='USD':
            aud=layout.split('-')[-1];audiences={'elite':'premier_elite','wealth':'premier_wealth','premier':'premier_standard','personal':'personal'}
            for row in grid[1:]:
                if len(row)!=2:raise ValueError('HSBC USD tenor/rate row shape changed')
                add('USD',row[0],row[1],30000,audience=audiences[aud],channel='分行/客户经理/电话/邮件',fresh='required',start='2026-09-01',end='2026-09-30',conditions='USD30,000新资金；本行为非App渠道公布报价；App实际报价以确认页为准'+('；须满足财富持有或指定外汇交易资格' if aud=='wealth' else ''))
        else:add(s['currency'],'3 months',grid[0][0],30000,channel='手机/分行',fresh='required',start='2026-09-01',end='2026-09-30',conditions='对应币种30,000新资金；3个月；App报价以提交时确认为准')
    elif bank=='CITI':
        if layout.endswith('conversion'):
            for row in grid[1:]:
                if len(row)!=2:raise ValueError('Citi conversion rate columns changed')
                add(row[0],'1 month',row[1],50000,5000000,amount_cur='USD',equivalent=True,channel='Treasury Sales Officer/客户顾问',name='换汇定存促销',start='2026-09-01',end='2026-09-30',conditions='必须从另一币种换汇并通过客户顾问办理，非网银/App；须有Citi活期或储蓄账户；新旧资金均可；上限为每名主客户总额度')
        elif layout.endswith('new-funds'):
            usd=[r for r in grid if re.match(r'^USD\b',r[0].strip())]
            if len(usd)!=1:raise ValueError('Citi USD row missing or ambiguous')
            row=usd[0]
            headers=[r for r in grid if all(re.search(r'\d+\s*Months?',x,re.I) for x in r)]
            labels=headers[0] if len(headers)==1 else grid[0][1:]
            if not (0<len(row)-1<=len(labels)):raise ValueError('Citi tenor/rate columns changed')
            grouped='For Existing Customers only' in grid[0]
            for index,(label,value) in enumerate(zip(labels,row[1:])):
                if not value.strip() or value.strip() in ['-','—']:continue
                condition='现有Citi客户须带入新资金；新客户须先建立Citigold/Private Client财富关系；详细新资金认定以官网条款为准'
                if grouped and index>0:condition='仅现有Citi客户；须带入新资金；详细新资金认定以官网条款为准'
                add('USD',label,value,5000,5000000,fresh='required',name='新资金定存促销',conditions=condition)
    elif bank=='DBS':
        start,end=promotion_dates(s)
        terms=' '.join(e['quote'] for e in evidence)
        if not re.search(r'no less than\s+USD\s*10,000',terms,re.I) or not re.search(r'no more than\s+USD\s*500,000',terms,re.I):
            raise ValueError('DBS campaign placement bounds changed; do not reuse old bonus rules')
        for row in grid[1:]:
            if len(row)!=4:raise ValueError('DBS bonus promotion row shape changed')
            n,u=tenor(row[0]);bonus=Decimal(numeric(row[1]));candidates=[r for r in board if r['bank']=='DBS' and r['currency']=='USD' and r['tenor_value']==n and r['tenor_unit']==u]
            if not candidates:raise ValueError('DBS verified current board dependency missing')
            for r in candidates:
                lo=max(Decimal(r['amount_min'] or 0),Decimal(10000));hi=min(Decimal(r['amount_max'] or 500000),Decimal(500000));ui=r['max_inclusive'] if r['amount_max'] is not None and Decimal(r['amount_max'])<=500000 else True
                if lo>hi or (lo==hi and not ui):continue
                add('USD',row[0],str(Decimal(r['rate_pct'])+bonus),lo,hi,ui=ui,channel='digibank网银/手机',start=start,end=end,name='挂牌加点促销',conditions=f"现有DBS/POSB个人客户；当期挂牌{r['rate_pct']}%＋加点{bonus}%；优惠码{row[3]}（区分大小写）；每笔10,000–500,000，可多笔",extra=r['evidence'])
    else:raise ValueError('Unsupported FX adapter '+bank)
    expected_count=None
    if bank=='SBI':expected_count=len(grid)-1
    elif bank=='SCB':expected_count=sum(row[2].count('%') for row in grid[1:])
    elif bank=='RHB':expected_count=sum(len(grid[0])-2 for r in grid[1:] if r[0]!='JPY')
    elif bank=='CIMB':expected_count=sum(len(grid[1]) for r in grid[2:] if r[0]!='JPY')
    elif bank=='BEA':expected_count=sum(len(grid[1])-1 for r in grid[2:] if r[1]!='JPY')
    elif bank=='HSBC' and s['currency']=='USD':expected_count=len(grid)-1
    elif bank=='ICBC':expected_count=(len(grid)-1)*(len(grid[0])-1) if s['currency']=='CNY' else (len(grid[1])-1)*(len(grid)-2)
    elif bank=='CITI' and layout.endswith('conversion'):expected_count=sum(r[0]!='JPY' for r in grid[1:])
    if expected_count is not None and len(out)!=expected_count:
        raise ValueError(f'{bank} source quote coverage incomplete: {len(out)} of {expected_count}')
    start,end=promotion_dates(s)
    for r in out:r['valid_from']=start;r['valid_to']=end
    return out

def dbs_missing_marker(url,text):
    """Recognize only observed official error routes with their literal marker."""
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='www.dbs.com.sg' or parsed.username or parsed.password:return None
    marker={'/personal/default.page':'Page you are trying to access is invalid',
            '/personal/404.page':'Page not found'}.get(parsed.path)
    return marker if marker and marker in [line.strip() for line in text.splitlines()] else None


def validate_source_absence(s):
    if s.get('status')=='no_current_promotion':
        from .workbook_policy import workbook_policy
        known=workbook_policy().get('known_no_promotion',{}).get('DBS-others-promo',{})
        parsed=urlsplit(s.get('resolved_url',''))
        if not (known and s['bank']=='DBS' and s['currency']=='USD' and not s['rows'] and s.get('task_ids')
                and s.get('confirmation_date')==known['confirmed_on']
                and parsed.scheme=='https' and parsed.hostname=='www.dbs.com.sg'
                and parsed.path.rstrip('/')=='/personal/accounts/fixed-deposit'
                and s.get('public_text') and 'Fixed Deposit' in s['public_text'] and not dbs_inline_promotion(s['public_text'])
                and not dbs_promo_links(s.get('public_links',[]))):
            raise ValueError('Unverified current no-promotion status')
        return dict(bank='DBS',currency='USD',status='no_current_promotion',note=known['note'],page_id=s['page_id'])
    if not (s['bank']=='DBS' and s['currency']=='USD' and s.get('status')=='source_unavailable'
            and urlsplit(s.get('requested_url','')).scheme=='https' and urlsplit(s.get('requested_url','')).hostname=='www.dbs.com.sg'
            and not s['rows'] and s.get('task_ids')
            and any(dbs_missing_marker(s.get('resolved_url',''),e['quote']) for e in s.get('extra_evidence',[]))):
        raise ValueError('Unverified promotion source absence')
    return dict(bank='DBS',currency='USD',status='source_unavailable',note='DBS原促销链接已失效，本期未取得独立促销报价；挂牌利率见USD挂牌。',page_id=s['page_id'])


def dbs_promo_links(links):
    """New official FX campaign links require collection, never a silent '-'."""
    return [a for a in links if urlsplit(a.get('url','')).hostname=='www.dbs.com.sg'
            and '/promotion/' in urlsplit(a.get('url','')).path
            and re.search(r'USD|foreign|currency|(?:^|[/_-])(?:usdfd|fxfd)(?:[/?_-]|$)',a.get('text','')+' '+a.get('url',''),re.I)]


def dbs_inline_promotion(text):
    return bool(re.search(r'Bonus interest on top of board|USD Fixed Deposit Promotion|USD\s+(?:time|fixed) deposit\s+promotional',text,re.I))
