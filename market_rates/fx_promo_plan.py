"""Preserve current board/SGD workbooks and update their FX promotion regions."""
import re
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
from datetime import datetime,timedelta
from .common import load,save,digest
from .xlsx_read import read_xlsx,merged_ranges
from .board_plan import col,cell_number,tenor_label,formula,amount
from .board_wide_plan import canonical_bank
from .rules import active,main_offer,COLORS
from .quote_selection import personal_offer,is_personal,caption_is_personal
from .quote_layout import tenor_order
from .manual_review import effective_offers
from .table_validation import sha
from .workbook_policy import presentation_currency,workbook_policy,quote_in_scope,promo_reference_allowed
from .board_cleanup import scb_audience
from .sheet_names import unused_sheet_name
from .fx_product_rows import consolidate_usd,source_rows,reuse_product_row

def tt(r):return str(r['tenor_value'])+r['tenor_unit']

def unmatched_caption(old_label):
    # Compatibility with older native checkpoints that used placeholders.
    if not str(old_label or '').strip():return ''
    return '本期未列此旧产品或档位'+(' / unknown；原专属报价仅保留历史' if not caption_is_personal(old_label) else '')

def rank_public_groups(groups,records,currency,tenor):
    """Keep all of a bank's products together at its retail winner's rank."""
    winners={bank:personal_offer([r for r in records if r['bank']==bank and
        r.get('display_currency',r['currency'])==currency and tt(r)==tenor]) for bank in {g['bank'] for g in groups}}
    for group in groups:
        winner=winners[group['bank']]
        group['rank']=float(Decimal(winner['rate_pct'])) if winner else -1
    groups.sort(key=lambda g:(-g['rank'],g['order'],g['bank'],
        bool(winners[g['bank']] and g['product_id']!=winners[g['bank']]['product_id']),g['product_id']))

def rainbow_public_cells(plan,*,formulas=False):
    """Exact current promo cells; unlisted cells in these regions must be blank."""
    cells={(2,1):'USD Promo Rate',(23+plan['usd_delta'],1):'CNY Promo Rate（含CNH）'}
    for title in plan.get('extra_titles',[]):cells[title['row'],1]=title['currency']+' Promo Rate '+plan['as_of']
    for block in plan['rainbow_blocks']:
        c=block['col'];header=block['start']-1
        if block['tenor'] is not None:
            for offset,value in enumerate((block['tenor'],'最高报价','金额要求')):cells[header,c+offset]=value
        for group in block['groups']:
            first=group['target_row'];cells[first,c]=group['bank']
            for offset,item in enumerate(group['rows']):
                cells[first+offset,c+1]=item['formula'] if formulas else item['expected']
                cells[first+offset,c+2]=item['amount']
    return cells

def rainbow_public_regions(plan,last_row,last_col):
    usd,cny=plan['usd_delta'],plan['cny_delta']
    last=max([last_row]+[b['end'] for b in plan['rainbow_blocks']])
    width=max([last_col]+[b['col']+2 for b in plan['rainbow_blocks']])
    return [(2,21+usd,width),(23+usd,29+usd+cny,width),(123+usd+cny,last,width)]
def bank_span(bank,anchor,end,inserts):
    """Preserve a tenor's bank span, including newly added amount tiers."""
    shift=lambda row:row+sum(i['count'] for i in inserts if i['before']<=row)
    last=max([shift(end)]+[i['target_row']+i['count']-1 for i in inserts
             if i['bank']==bank and i['anchor']==anchor and not i['new_bank']])
    return dict(bank=bank,start=shift(anchor),end=last)

def publication_notice(offer,as_of):
    expired=bool(offer.get('valid_to') and offer['valid_to']<as_of)
    period=str(offer.get('valid_from') or '未注明起日')+'–'+str(offer.get('valid_to') or '未列截止日')
    return ('最新公开参考报价；公告期 '+period+'（公告已截止，按用户确认保留）'
            if expired else '最新公告报价；公告期 '+period)

def caption(r,short=False):
    note=r['conditions']
    if short:
        note={'BEA':'达SGD500,000等值须另询银行','CIMB':'须有对应外币账户；到期挂牌续存','RHB':'新旧资金；含合资格续存','SBI':'每人同币种合计须小于1,000,000','SCB':'外部新资金；持有至到期','ICBC':'按金额档位及渠道适用','HSBC':'新资金；USD为非App渠道公布报价' if r['currency']=='USD' else '对应币种新资金'}.get(r['bank'],note)
        if r['bank']=='CITI':note='须先换汇；经客户顾问办理' if 'conversion' in r['product_id'] else '现有客户带入新资金；新户先建立财富关系'
        if r['bank']=='DBS':note=re.search(r'当期挂牌.*?（区分大小写）',note)[0]
    if r.get('reference_quote'):note+='；公告期 '+(r['valid_from'] or '未注明起日')+'–'+r['valid_to']+'（最新公开参考，公告已截止）'
    return r['product_name']+' / '+r['audience']+'\n'+amount(r)+'\n'+note
def plan(source,out,base):
    root=Path(source).resolve();run=load(root/'run.json');offers,blocked,receipts=effective_offers(run)
    from .llm_validation import require_complete_sources
    require_complete_sources(run)
    if blocked:raise ValueError('Unresolved FX facts')
    out=Path(out).resolve();sheet='外币促销_'+run['as_of'].replace('-','');report=base['report_output'];rainbow=base['rainbow_output']
    b=read_xlsx(report,merge_anchors_only=True);bf=read_xlsx(report,formulas=True,merge_anchors_only=True);rb=read_xlsx(rainbow,merge_anchors_only=True);details=[]
    sheet=unused_sheet_name(sheet,b,rb)
    for r in sorted(offers,key=lambda r:(presentation_currency(r['currency']),r['bank'],r['tenor_value'],r['product_id'],r['audience'],Decimal(r['amount_min'] or 0))):
        if not quote_in_scope(r):continue
        reason=''
        reference=promo_reference_allowed(r,run['as_of'])
        if not active(r,run['as_of']) and not reference:reason='促销未生效或已截止'

        details.append(dict(r,input_row=len(details)+4,display_currency=presentation_currency(r['currency']),insertable=not reason,hold_reason=reason,reference_quote=bool(reference),verification='本地文字来源核验通过'))
    for d in run['deferred']:
        r=d['offer'];details.append(dict(r,input_row=len(details)+4,display_currency=presentation_currency(r['currency']),insertable=False,hold_reason=d['reason'],verification=d['verification']))
    valid=[r for r in details if r['insertable']];product_merges=[]
    def select(bank,cur,t=None):return [r for r in valid if r['bank']==bank and r['display_currency']==cur and (not t or tt(r)==t)]
    def entry(rows):
        raw=[source for row in rows for source in source_rows(row)]
        return dict(formula=formula(raw,sheet),expected=float(max(Decimal(r['rate_pct']) for r in rows)/100) if rows else '-',input_rows=sorted({r['input_row'] for r in raw}))
    histories=[];product_reuse=[];product_history_preservation=[]
    for name,cur,bc,cc in [('USD促销','USD',2,4),('CNY促销','CNY',1,3),('其他外币促销利率','multi',2,4)]:
        s=b[name];oldmax=max(int(re.search(r'\d+',k)[0]) for k,v in s.items() if v is not None);sections=[];currency=cur
        for row in range(1,oldmax+1):
            v=s.get(col(bc)+str(row),'')
            for zh,fx in [('澳元','AUD'),('欧元','EUR'),('英镑','GBP'),('新西兰元','NZD')]:
                if zh in str(v):currency=fx
            t=tenor_label(v)
            if t:sections.append((row,t,currency))
        dv=cell_number(s.get(col(cc)+str(sections[0][0]+1)));previous=(datetime(1899,12,30)+timedelta(days=dv)).date().isoformat()
        if previous>run['as_of']:raise ValueError('Cannot insert an older period')
        inserts=[];existing=[];bank_bounds=[];history_candidates=[]
        for si,(start,t,currency) in enumerate(sections):
            stop=sections[si+1][0]-1 if si+1<len(sections) else oldmax
            # Currency title between tenor blocks is not a bank or a data row.
            stop=min([r-1 for r in range(start+2,stop+1) if '促销' in str(s.get(col(bc)+str(r),''))]+[stop])
            anchors=[(r,canonical_bank(s[col(bc)+str(r)])) for r in range(start+2,stop+1) if s.get(col(bc)+str(r))]
            seen=set()
            for ai,(ar,bank) in enumerate(anchors):
                end=anchors[ai+1][0]-1 if ai+1<len(anchors) else stop
                while end>ar and s.get(col(bc+1)+str(end)) is None and s.get(col(cc)+str(end)) is None:end-=1
                if bank in ['ICBC','SCB']:bank_bounds.append((bank,ar,end))
                for rr in range(ar,end+1):
                    old_label=s.get(col(bc+1)+str(rr),'')
                    # A blank current rate marks an unmatched row. Preserve
                    # the old caption so later date comparisons retain identity.
                    existing.append(dict(old_row=rr,bank=bank,formula='-',expected='-',amount=str(old_label or '').strip()))
                if bank in seen:continue
                seen.add(bank);new=[];used=set()
                labels={x:s.get(col(bc+1)+str(x)) for x in range(ar,end+1)}
                for r in select(bank,currency,t):
                    label=caption(r);matched,reason=reuse_product_row(r,labels,label,used)
                    if matched is not None:
                        used.add(matched)
                        next(x for x in existing if x['old_row']==matched).update(entry([r]),amount=label,input_row=r['input_row'])
                        product_reuse.append(dict(sheet=name,bank=bank,tenor=t,old_row=matched,old_label=labels[matched],new_label=label,reason=reason,input_rows=entry([r])['input_rows']))
                        for old_row,old_label in labels.items():
                            if old_row!=matched and reuse_product_row(r,{old_row:old_label},label)[0] is not None:
                                history_candidates.append(dict(sheet=name,bank=bank,tenor=t,old_row=old_row,active_old_row=matched,old_label=old_label))
                    else:new.append(dict(amount=label,**entry([r])))
                if new:inserts.append(dict(before=end+1,anchor=ar,bank=bank,label=s[col(bc)+str(ar)],rows=new,count=len(new),new_bank=False))
            for bank in sorted({r['bank'] for r in valid if r['display_currency']==currency and tt(r)==t}-seen):
                rows=[dict(amount=caption(r),**entry([r])) for r in select(bank,currency,t)]
                # Insert a legitimately new bank once into this tenor section.
                inserts.append(dict(before=stop+1,anchor=anchors[-1][0],bank=bank,label=bank,rows=rows,count=len(rows),new_bank=True))
        currencies=sorted({r['display_currency'] for r in valid}) if cur=='multi' else [cur]
        if cur=='multi':currencies=[c for c in currencies if c not in ('USD','CNY')]
        labels={'AUD':'澳元','NZD':'新西兰元','EUR':'欧元','GBP':'英镑','CAD':'加元','HKD':'港币','CHF':'瑞士法郎'}
        for currency in currencies:
            missing=sorted({tt(r) for r in valid if r['display_currency']==currency}-{t for _,t,c in sections if c==currency},key=tenor_order)
            for t in missing:
                rs=[r for r in valid if r['display_currency']==currency and tt(r)==t]
                label='Tenor: '+t[:-1]+(' months' if t.endswith('M') else ' days')
                inserts.append(dict(before=oldmax+1,anchor=sections[0][0]+2,bank='',label=label,
                    currency_label=labels.get(currency,currency)+'促销利率' if cur=='multi' else '',
                    rows=[dict(bank=r['bank'],amount=caption(r),**entry([r])) for r in rs],
                    count=len(rs)+2,new_bank=True,kind='tenor',currency=currency))
        # Several new banks can share an insertion point; coalesce position order.
        inserts.sort(key=lambda i:(i['before'],i['new_bank'],i['bank']))
        shift=lambda row:row+sum(i['count'] for i in inserts if i['before']<=row)
        for i,ins in enumerate(inserts):
            ins['target_row']=ins['before']+sum(j['count'] for j in inserts[:i] if j['before']<=ins['before']);ins['target_anchor']=shift(ins['anchor'])
            if ins['new_bank']:ins['target_anchor']=ins['target_row']
        for e in existing:e['address']=col(cc)+str(shift(e['old_row']))
        snapshots=[]
        delta_cols={re.match(r'[A-Z]+',a)[0] for a,v in s.items() if v in ['当日最高报价变动','当日增幅']}
        for addr,f in bf[name].items():
            if not isinstance(f,str) or not f.startswith('='):continue
            letters,row=re.fullmatch(r'([A-Z]+)(\d+)',addr).groups();c=0
            for ch in letters:c=c*26+ord(ch)-64
            if c>=cc and letters not in delta_cols:snapshots.append(dict(address=addr,value=cell_number(s.get(addr))))
        assigned={e['old_row'] for e in existing if e.get('input_rows')}
        audited=set()
        for item in history_candidates:
            old_row=item['old_row']
            if old_row in assigned or old_row in audited:continue
            audited.add(old_row)
            values={}
            active_values={}
            for address,value in s.items():
                letters,num=re.fullmatch(r'([A-Z]+)(\d+)',address).groups();column=0
                for letter in letters:column=column*26+ord(letter)-64
                if column<cc+(0 if previous!=run['as_of'] else 1) or letters in delta_cols:continue
                numeric_value=cell_number(value)
                if not isinstance(numeric_value,(float,int)):continue
                if int(num)==old_row:values[letters]=numeric_value
                elif int(num)==item['active_old_row']:active_values[letters]=numeric_value
            conflicts=[column for column,value in values.items() if column in active_values and abs(value-active_values[column])>1e-10]
            product_history_preservation.append(dict(item,address=col(cc)+str(shift(old_row)),active_address=col(cc)+str(shift(item['active_old_row'])),current_expected='-',history_values=values,conflicting_history_columns=conflicts,reason='同类旧行本期留空；历史观察完整保留，格式对齐版可归档，不能覆盖不同历史报价'))
        final_max=max([shift(oldmax)]+[i['target_row']+i['count']-1 for i in inserts])
        histories.append(dict(sheet=name,currency=cur,title=('其他外币' if cur=='multi' else cur)+'促销 '+run['as_of'],bank_col=bc,current_col=cc,insert_date=previous!=run['as_of'],date_rows=[shift(r+1) for r,_,_ in sections],old_max_row=oldmax,max_row=final_max,inserts=inserts,existing=existing,snapshots=snapshots,merges=merged_ranges(report)[name],bank_merges=[bank_span(bank,ar,end,inserts) for bank,ar,end in bank_bounds]))
    # Existing USD/CNY rainbow product groups, stable ties by last week's bank order.
    s=rb['USD Rate + Other Currency Rates'];blocks=[]
    for cur,header,start,end in [('USD',3,4,21),('CNY',24,25,29)]:
        for c,t in [(c,s.get(col(c)+str(header))) for c in [1,5,9,13,17]]+[(21+4*i,t) for i,t in enumerate(sorted({tt(r) for r in valid if r['display_currency']==cur}-{s.get(col(c)+str(header)) for c in [1,5,9,13,17]},key=tenor_order))]:
            oldorder=[canonical_bank(s[col(c)+str(r)]) for r in range(start,end+1) if s.get(col(c)+str(r))]
            groups=defaultdict(list)
            for r in valid:
                if r['display_currency']==cur and tt(r)==t:groups[(r['bank'],r['product_id'])].append(r)
            gs=[]
            for (bank,pid),rows in groups.items():
                win=main_offer(rows);ordered=[win]+sorted([r for r in rows if r is not win],key=lambda r:(r['audience']!='personal',-Decimal(r['rate_pct']),Decimal(r['amount_min'] or 0),r['channel']))
                gs.append(dict(bank=bank,product_id=pid,rank=float(Decimal(personal_offer(rows)['rate_pct'])) if personal_offer(rows) else -1,order=oldorder.index(bank) if bank in oldorder else len(oldorder),rows=[dict(amount=caption(r,True),**entry([r])) for r in ordered]))
            rank_public_groups(gs,valid,cur,t)
            blocks.append(dict(currency=cur,col=c,tenor=t,start=start,old_end=end,groups=gs,color=COLORS.get(t,'E8F4FD')))
    usd_delta=max([sum(len(g['rows']) for g in bl['groups'])-18 for bl in blocks if bl['currency']=='USD']+[0]);cny_delta=max([sum(len(g['rows']) for g in bl['groups'])-5 for bl in blocks if bl['currency']=='CNY']+[0])
    for bl in blocks:
        delta=usd_delta if bl['currency']=='CNY' else 0;bl['start']+=delta;bl['end']=bl['old_end']+delta+(cny_delta if bl['currency']=='CNY' else usd_delta);n=bl['start']
        for g in bl['groups']:g['target_row']=n;n+=len(g['rows'])
    # Keep the existing mixed comparison and add promo-only groups for the four
    # foreign currencies, including published 12M offers not in its 1/3/6M matrix.
    extra_titles=[];nextrow=123+usd_delta+cny_delta
    for currency in sorted({r['display_currency'] for r in valid}-{'USD','CNY'}):
        extra_titles.append(dict(row=nextrow,currency=currency));start=nextrow+2;largest=1
        for i,t in enumerate(sorted({'1M','3M','6M','12M'}|{tt(r) for r in valid if r['display_currency']==currency},key=tenor_order)):
            c=1+4*i
            grouped=defaultdict(list)
            for r in valid:
                if r['display_currency']==currency and tt(r)==t:grouped[(r['bank'],r['product_id'])].append(r)
            order=['BOC','CIMB','RHB','BEA','CITI','HSBC'];gs=[]
            for (bank,pid),rows in grouped.items():
                win=main_offer(rows);ordered=[win]+[r for r in rows if r is not win]
                gs.append(dict(bank=bank,product_id=pid,rank=float(Decimal(personal_offer(rows)['rate_pct'])) if personal_offer(rows) else -1,order=order.index(bank) if bank in order else len(order),rows=[dict(amount=caption(r,True),**entry([r])) for r in ordered]))
            rank_public_groups(gs,valid,currency,t);n=start
            for g in gs:g['target_row']=n;n+=len(g['rows'])
            largest=max(largest,n-start);blocks.append(dict(currency=currency,col=c,tenor=t,start=start,end=n-1 if n>start else start,groups=gs,color=COLORS.get(t,'E8F4FD'),extra=True))
        nextrow=start+largest+2
    summary=[]
    for row in range(29,38):
        bank=canonical_bank(b['最高报价汇总 '].get('B'+str(row),''))
        for c,t in enumerate(['1M','3M','6M','9M','12M'],3):summary.append(dict(address=col(c)+str(row),**entry([r for r in select(bank,'USD',t) if is_personal(r['audience'])])))
        rs=select(bank,'USD');summary.extend([dict(address='H'+str(row),formula='各产品门槛见USD促销',expected='各产品门槛见USD促销'),dict(address='I'+str(row),formula='公告已截止，本期无有效报价' if bank=='BOC' else '普通个人最高报价；金额、渠道及条件见同条明细',expected='公告已截止，本期无有效报价' if bank=='BOC' else '普通个人最高报价；金额、渠道及条件见同条明细')])
    # DBS has a spare summary row; no displacement of board tables.
    summary.append(dict(address='B38',formula='DBS/POSB',expected='DBS/POSB'))
    for c,t in enumerate(['1M','3M','6M','9M','12M'],3):summary.append(dict(address=col(c)+'38',**entry([r for r in select('DBS','USD',t) if is_personal(r['audience'])])))
    summary+= [dict(address='H38',formula='USD10,000–500,000；按挂牌档位',expected='USD10,000–500,000；按挂牌档位'),dict(address='I38',formula='挂牌＋加点；须输入优惠码',expected='挂牌＋加点；须输入优惠码')]
    for row in range(29,39):
        bank='DBS' if row==38 else canonical_bank(b['最高报价汇总 '].get('B'+str(row),''))
        selected=[(t,personal_offer(select(bank,'USD',t))) for t in ['1M','3M','6M','9M','12M']]
        selected=[(t,r) for t,r in selected if r]
        minimum='\n'.join(t+'：'+amount(r) for t,r in selected) or '-'
        note='\n'.join(t+'：'+r['conditions']+'；'+publication_notice(r,run['as_of']) for t,r in selected) or ('当前无独立外币促销；本期已检查官方公开入口' if bank=='DBS' else '本期无合资格普通个人报价')
        for address,value in [('H'+str(row),minimum),('I'+str(row),note)]:
            next(c for c in summary if c['address']==address).update(formula=value,expected=value)
    mixed=[];offset=usd_delta+cny_delta
    # Existing area explicitly says promotion first, otherwise board. Refresh
    # fallback from already checked board details rather than old copied values.
    for row in range(106,119):
        bank=canonical_bank(s.get('A'+str(row),''));bank='HLB' if bank=='HL Bank' else bank
        notes=[]
        for cur,c in [('AUD',2),('NZD',5),('EUR',8),('GBP',11)]:
            for j,t in enumerate(['1M','3M','6M']):
                rs=select(bank,cur,t);basis='促销'
                if rs:win=personal_offer(rs);value=entry([win] if win else [])
                else:
                    rs=[r for r in base['details'] if r['insertable'] and r['bank']==bank and r['display_currency']==cur and tt(r)==t];basis='挂牌'
                    win=personal_offer(rs);value=dict(formula=formula([win],base['input_sheet']) if win else '-',expected=float(Decimal(win['rate_pct'])/100) if win else '-')
                mixed.append(dict(address=col(c+j)+str(row+offset),**value))
                if win:notes.append((cur,t,basis))
        note_lines=[]
        for currency in ['AUD','NZD','EUR','GBP']:
            for basis in ['促销','挂牌']:
                ts=[t for c,t,basis_ in notes if c==currency and basis_==basis]
                if ts:note_lines.append(currency+' '+('/'.join(t[:-1] for t in ts))+'M '+basis)
        if bank=='CITI':note_lines.append('促销须先换汇')
        mixed.extend([dict(address='N'+str(row+offset),formula='各币种门槛见调研/明细',expected='各币种门槛见调研/明细'),dict(address='O'+str(row+offset),formula='\n'.join(note_lines),expected='\n'.join(note_lines))])
    # Four-currency highest quotations in the report mirror the promo-only scope.
    summary.append(dict(address='B75',formula='HSBC',expected='HSBC'))
    fx_notes={
        'BOC':('—','公告已截止，本期无有效促销'),
        'CIMB':('各币种10,000–1,000,000','在线；须已有对应外币账户'),
        'RHB':('各币种5,000起','个人；新/现有资金及合资格续期'),
        'CITI':('USD50,000–5,000,000等值','先换汇；经Treasury Sales/Advisor；上限按主客户合计'),
        'BEA':('各币种金额门槛见明细','Tier Rates沿用原表分类；等值SGD500,000起须议价'),
        'HSBC':('各币种30,000起','新资金；非App报价，App以确认页为准'),
    }
    for row in [69,70,71,72,73,75]:
        bank='HSBC' if row==75 else canonical_bank(b['最高报价汇总 '].get('B'+str(row),''))
        for cur,c in [('AUD',3),('NZD',6),('EUR',9),('GBP',12)]:
            for j,t in enumerate(['1M','3M','6M']):summary.append(dict(address=col(c+j)+str(row),**entry([r for r in select(bank,cur,t) if is_personal(r['audience'])])))
        for c,v in zip(['O','P'],fx_notes[bank]):summary.append(dict(address=c+str(row),formula=v,expected=v))
    for addr,v in [('F74','本期门槛与渠道见各币种促销明细'),('I74','')]:summary.append(dict(address=addr,formula=v,expected=v))
    # An independent summary row for each actual bank/currency/tenor, including
    # groups absent from the historical summary template.
    summary_start=max(int(re.search(r'\d+',a)[0]) for a in b['最高报价汇总 '])+4
    for c,v in enumerate(['银行','币种','实际期限','Personal最高值','金额及渠道','条件及来源'],2):
        summary.append(dict(address=col(c)+str(summary_start),formula=v,expected=v))
    keys=sorted({(r['bank'],r['display_currency'],tt(r)) for r in valid},key=lambda k:(k[1],tenor_order(k[2]),k[0]))
    for row,(bank,cur,t) in enumerate(keys,summary_start+1):
        win=personal_offer(select(bank,cur,t))
        values=[bank,cur,t,entry([win] if win else []),amount(win) if win else '-',(win['conditions']+'；'+publication_notice(win,run['as_of'])+'；'+'；'.join(dict.fromkeys(e['page_id'] for e in win['evidence']))) if win else '无合资格普通个人报价']
        for c,v in enumerate(values,2):summary.append(dict(address=col(c)+str(row),**v) if isinstance(v,dict) else dict(address=col(c)+str(row),formula=v,expected=v))
    coverage=[]
    for addr,bank in b['Bank List'].items():
        if not re.fullmatch(r'B\d+',addr):continue
        bank=canonical_bank(bank)
        categories={('D' if r['display_currency']=='USD' else 'E' if r['display_currency']=='CNY' else 'F') for r in details if r['bank']==bank}
        for c in sorted(categories):
            target=c+addr[1:]
            if not b['Bank List'].get(target):continue
            coverage.append(dict(address=target,formula=int(run['as_of'].replace('-','')),expected=int(run['as_of'].replace('-','')),comment='采集检查日期；'+('公告已截止，明细保留，本期促销填 -。' if bank=='BOC' else '本期报价见对应外币促销及报价明细；网页来源日期单独保留。')))
    p=dict(kind='fx-promo',source_run=str(root),as_of=run['as_of'],banks=sorted(run['bank_dates']),input_sheet=sheet,details=details,histories=histories,rainbow_blocks=blocks,extra_titles=extra_titles,usd_delta=usd_delta,cny_delta=cny_delta,mixed_cells=mixed,summary=summary,manual_receipts=receipts,report_template=report,rainbow_template=rainbow,report_sha256=sha(report),rainbow_sha256=sha(rainbow),report_output=str(out/(run['as_of'].replace('-','')+'_外币促销合并_调研.xlsx')),rainbow_output=str(out/(run['as_of'].replace('-','')+'_外币促销合并_彩虹表.xlsx')),base_plan=str(Path(base['output'])/'table-validation-plan.json'),output=str(out))
    p['product_merges']=product_merges;p['product_reuse']=product_reuse;p['product_history_preservation']=product_history_preservation
    p['coverage']=coverage;p['bank_dates']=run['bank_dates'];p['limitations']=['仅使用各产品最新公告；已截止公告按确认规则标注为最新公开参考，保留原有效期。','现有挂牌与SGD促销保留；各产品、客群、渠道和金额档逐条展示，期限按实际值分组。','输入利率调整后关联数值自动更新；请通过本地重新生成更新彩虹表排序。','Personal最高值仅从personal和普通个人可参与的all报价选择；专属客群完整展示但不参与此最高值。']
    boc=[r for r in valid if r['bank']=='BOC']
    if boc:
        notice=publication_notice(boc[0],run['as_of'])
        p['limitations'][0]='中行'+notice
        for c in coverage:
            if '公告已截止' in c.get('comment',''):c['comment']='采集检查日期；'+notice
        for row in range(29,38):
            if b['最高报价汇总 '].get('B'+str(row))=='BOC':next(c for c in summary if c['address']=='I'+str(row)).update(formula=notice+'；各期限金额、渠道及条件见明细',expected=notice+'；各期限金额、渠道及条件见明细')
        for c in summary:
            if c['address']=='P69':c.update(formula=notice,expected=notice)
            if c['address']=='O69':c.update(formula='AUD/NZD/EUR 5,000；GBP 2,000起',expected='AUD/NZD/EUR 5,000；GBP 2,000起')
        for c in mixed:
            if c['address']=='O'+str(106+offset):c['formula']+='\n公告期 '+str(boc[0]['valid_from'])+'–'+str(boc[0]['valid_to']);c['expected']=c['formula']
    # Preserve the existing rainbow tenor groups. Longer CNY tenors may appear
    # in the existing research sections and details, without adding new groups.
    displayed={(bl['currency'],bl['tenor']) for bl in blocks}
    for r in details:r['rainbow_insertable']=r['insertable'] and (r['display_currency'],tt(r)) in displayed
    p['source_statuses']=run.get('source_statuses',[])
    p['limitations'] += [s['note'] for s in p['source_statuses']]
    out.mkdir(parents=True,exist_ok=False);save(out/'table-validation-plan.json',p);return p
