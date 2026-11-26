"""One lossless public quote catalog and one retail selection for four deliveries."""
from collections import defaultdict,Counter
from decimal import Decimal
from pathlib import Path
import xml.etree.ElementTree as ET

from .common import load,save,digest
from .quote_selection import personal_offer,is_personal,comparison_eligible,summary_terms
from .quote_layout import tenor_order
from .report_xml import Book,StyleMap,M,col
from .workbook_policy import bank_in_scope,currency_in_scope,presentation_currency

PUBLIC='全部公开报价'
WINNERS='Personal最高值'
HEADERS=['报价编号','银行','币种','类型','实际期限','产品','客群','起存金额','金额上限','金额币种','下限包含','上限包含','利率(%)','渠道','新资金','条件','公告开始','公告截止','来源','显示状态','来源原币种']


def coverage_key(row):
    return (row['bank'],presentation_currency(row['currency']),row['rate_type'],row['product_id'],str(row['tenor_value'])+row['tenor_unit'],row['audience'],row['channel'])


def catalog(plan_path):
    plans={};visited=set()
    def walk(path):
        path=Path(path).resolve()
        if path in visited:return
        visited.add(path);p=load(path)
        kind=p.get('kind','sgd' if 'groups' in p else '')
        if kind in ('sgd','board-wide','fx-promo'):plans.setdefault(kind,p)
        parent=p.get('base_plan')
        if not parent and p.get('base_output'):parent=str(Path(p['base_output'])/'table-validation-plan.json')
        if parent and Path(parent).exists():walk(parent)
    walk(plan_path)
    if set(plans)!={'sgd','board-wide','fx-promo'}:raise ValueError('四份交付缺少同一条流水线的完整报价计划：'+str(set(plans)))
    records=[];coverage=[];source_counts=Counter();manual_adjustments=defaultdict(list)
    for kind,p in plans.items():
        source=load(Path(p['source_run'])/'run.json');pages={page['id']:page for page in source['pages']}
        source_counts.update(coverage_key(r) for r in source['llm']['offers'] if bank_in_scope(r['bank']) and currency_in_scope(r['currency']))
        from .weekly_history import identity
        originals={identity(r):r for r in source['llm']['offers']};manual_urls={}
        manual_ids=set()
        for receipt in p.get('manual_receipts',[]):
            if not receipt.get('author') or not receipt.get('reason') or receipt.get('action')=='reset':continue
            changed=[r for r in (originals.get(receipt['id']),receipt.get('offer')) if r]
            manual_ids.update(identity(r) for r in changed)
            note={k:receipt.get(k) for k in ('id','action','author','reason','source_url','at')}
            for key in {coverage_key(r) for r in changed}:manual_adjustments[key].append(note)
            for e in (receipt.get('offer') or {}).get('evidence',[]):
                if receipt.get('source_url'):manual_urls[e['page_id']]=receipt['source_url']
        for r in p['details']:
            if not bank_in_scope(r['bank']) or not currency_in_scope(r['currency']):continue
            row=dict(r);row['currency']=r.get('display_currency',r['currency'])
            row['source_currency']=r['currency'];row['source_urls']=list(dict.fromkeys(pages[e['page_id']]['url'] if e['page_id'] in pages else manual_urls[e['page_id']] for e in r['evidence']))
            row['quote_id']=digest({k:r.get(k) for k in ('bank','product_id','currency','rate_type','tenor_value','tenor_unit','audience','amount_min','amount_max','amount_currency','min_inclusive','max_inclusive','channel','fresh_funds','conditions','rate_pct','valid_from','valid_to')})
            row['display_status']=r.get('hold_reason') or ('本轮人工复核／修正；'+ '；'.join(v['reason'] for v in manual_adjustments.get(coverage_key(r),[])) if identity(r) in manual_ids else '本期来源已核验')
            records.append(row)
        from .source_inventory import verify_source_inventory
        source_audit=verify_source_inventory(source['llm']['offers'],source['pages']) if kind=='sgd' else []
        if kind!='sgd':
            from .llm_validation import validated_offers,normalized_row_hashes
            verified,blocked=validated_offers(source)
            if blocked:raise ValueError('公开报价覆盖核对未通过：'+str(blocked))
            expected=normalized_row_hashes(source)
            source_audit.append(dict(bank='ALL',product_id='ALL',expected=len(expected),verified=len(verified),passed=len(expected)==len(verified),method='hash-checked source grid normalization; exact row identity reconciliation'))
            if len(expected)!=len(verified):raise ValueError('来源报价行数与已核验明细不一致')
        coverage.extend(dict(stage=kind,**a) for a in source_audit)
    if len({r['quote_id'] for r in records})!=len(records):raise ValueError('公开报价集合存在重复身份')
    records.sort(key=lambda r:(r['rate_type'],r['currency'],tenor_order(str(r['tenor_value'])+r['tenor_unit']),r['bank'],r['product_id'],r['input_row']))
    groups=defaultdict(list)
    for r in records:groups[r['bank'],r['currency'],r['rate_type'],str(r['tenor_value'])+r['tenor_unit']].append(r)
    winners=[]
    for (bank,cur,kind,t),rows in groups.items():
        eligible=[r for r in rows if comparison_eligible(r)]
        win=personal_offer(eligible)
        tied=[r['quote_id'] for r in eligible if is_personal(r['audience']) and win and Decimal(r['rate_pct'])==Decimal(win['rate_pct'])]
        winners.append(dict(bank=bank,currency=cur,rate_type=kind,tenor=t,quote_id=win['quote_id'] if win else None,rate_pct=win['rate_pct'] if win else None,tied_quotes=tied))
    return dict(records=records,winners=winners,source_coverage=coverage,source_counts=source_counts,
                manual_adjustments=manual_adjustments,sgd_input_sheet=plans['sgd']['input_sheet'])


def values(row):
    return [row['quote_id'],row['bank'],row['currency'],row['rate_type'],str(row['tenor_value'])+row['tenor_unit'],row['product_name'],row['audience'],
            row['amount_min'],row['amount_max'],row['amount_currency'],str(row['min_inclusive']),str(row['max_inclusive']),row['rate_pct'],row['channel'],row['fresh_funds'],row['conditions'],row['valid_from'],row['valid_to'],'\n'.join(row['source_urls']),row['display_status'],row.get('source_currency',row['currency'])]


def write_catalog(path,cat):
    book=Book(path);styles=StyleMap(book,book)
    textstyle=styles.customize(0,format_code='General',wrap=True,alignment='left')
    for name in (PUBLIC,WINNERS):
        # The caller owns a staging copy. A user-selected prior delivery may
        # carry yesterday's derived audit tabs; rebuild them for this run.
        if name in book.sheets:book.drop_sheet(name)
    def table(name,headers,rows):
        sh=book.add_sheet(name)
        views=ET.Element(M+'sheetViews');view=ET.SubElement(views,M+'sheetView',workbookViewId='0',showGridLines='0')
        ET.SubElement(view,M+'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen');sh.insert_element(views)
        cols=ET.Element(M+'cols')
        for c in range(1,len(headers)+1):ET.SubElement(cols,M+'col',min=str(c),max=str(c),width='55' if c in (16,19) else '22',customWidth='1')
        sh.insert_element(cols)
        for r,row in enumerate([headers]+rows,1):
            for c,value in enumerate(row,1):sh.put(r,c,value if value is not None else '-',textstyle)
            sh.height(r,30 if r==1 else 60)
        sh.insert_element(ET.Element(M+'autoFilter',ref=f'A1:{col(len(headers))}{len(rows)+1}'))
    table(PUBLIC,HEADERS,[values(r) for r in cat['records']])
    index={r['quote_id']:r for r in cat['records']}
    rows=[]
    for w in cat['winners']:
        r=index.get(w['quote_id'])
        rows.append([w['bank'],w['currency'],w['rate_type'],w['tenor'],w['rate_pct'],w['quote_id'],r['amount_min'] if r else '-',r['amount_currency'] if r else '-',r['channel'] if r else '-',r['conditions'] if r else '无合资格Personal报价','\n'.join(r['source_urls']) if r else '-', '\n'.join(w['tied_quotes'])])
    table(WINNERS,['银行','币种','类型','实际期限','Personal最高利率(%)','获选报价编号','起存金额','金额币种','渠道','条件','来源','全部并列最高报价编号'],rows)
    book.save(path)
    saved=Book(path)
    for i,row in enumerate(cat['records'],2):
        if [saved.sheets[PUBLIC].value(i,c) for c in range(1,len(HEADERS)+1)]!=[str(x) if x is not None else '-' for x in values(row)]:
            raise ValueError('公开报价落盘回读不一致：'+row['quote_id'])
    for i,row in enumerate(rows,2):
        if [saved.sheets[WINNERS].value(i,c) for c in range(1,13)]!=[str(x) if x is not None else '-' for x in row]:
            raise ValueError('Personal获选记录落盘回读不一致：'+str(row[:4]))
    return dict(quotes=len(cat['records']),groups=len(cat['winners']),catalog_sha256=digest([values(r) for r in cat['records']]),selection_sha256=digest(rows),passed=True)


def align_sgd_summary(path,cat,*,preserve_quote_notes=False,source_sheet=None):
    """Use the canonical selected records for the summary's amount and terms."""
    import re
    from .scope import bank_name
    from .report_xml import numeric
    book=Book(path);sheet=next((s for n,s in book.sheets.items() if n.strip()=='最高报价汇总'),None)
    if sheet is None:return dict(skipped=True)
    headers=sorted(r for (r,c) in sheet.cells if c==2 and sheet.value(r,c)=='Bank')
    if not headers:raise ValueError('SGD summary header missing')
    first=headers[0];end=headers[1] if len(headers)>1 else sheet.maxrow+1
    columns={c:str(sheet.value(first,c)) for r,c in sheet.cells if r==first and re.fullmatch(r'\d+[MD]',str(sheet.value(r,c)))}
    index={r['quote_id']:r for r in cat['records']};selected=defaultdict(dict)
    for w in cat['winners']:
        if w['currency']=='SGD' and w['rate_type']=='promo':selected[w['bank']][w['tenor']]=index.get(w['quote_id'])
    styles=StyleMap(book,book);changes=[];checked=0
    for row in range(first+1,end):
        label=sheet.value(row,2)
        if not label or not re.fullmatch(r'[A-Za-z][A-Za-z /&.-]*',str(label)):continue
        bank=bank_name(label);banks=['DBS','POSB'] if label=='DBS/POSB' else [bank]
        terms=sorted({t for b in banks for t in selected[b]},key=tenor_order)
        picks={t:personal_offer([selected[b][t] for b in banks if selected[b].get(t)]) for t in terms}
        for column,tenor in columns.items():
            quote=picks.get(tenor);expected=Decimal(quote['rate_pct'])/100 if quote else None;actual=sheet.value(row,column)
            if bank_in_scope(bank) and ((expected is not None and (not numeric(actual) or abs(actual-expected)>Decimal('1e-12'))) or (expected is None and numeric(actual))):
                raise ValueError('SGD汇总最高值与统一获选记录不一致：'+str(label)+'/'+tenor)
            if expected is None and actual!='-':sheet.put(row,column,'-')
            checked+=1
        amount,note=summary_terms(list(picks.items()))
        if not bank_in_scope(bank):amount,note='-','本期不在采集范围'
        for column,value in ((10,amount),(11,note)):
            cell=sheet.cell(row,column);style=styles.customize(int(cell.get('s','0')),format_code='General',wrap=True,alignment='left')
            if sheet.value(row,column)!=value:changes.append(dict(row=row,column=column))
            sheet.put(row,column,value,style)
        sheet.height(row,min(409,max(float(sheet.rows[row].get('ht','20')),32*max(len(amount.splitlines()),len(note.splitlines())))))
    main_checks=[]
    if 'SGD促销' in book.sheets:
        from .report_format import row_contexts,row_groups,first_delta,unmerge_written_cells,fitted_height,width_map,set_columns
        main=book.sheets['SGD促销'];contexts,_,_=row_contexts(main,1);delta=first_delta(main)
        unmerge_written_cells(main,contexts,[delta+1]+([] if preserve_quote_notes else [delta+2]))
        for (_,tenor,bank),rows in row_groups(contexts).items():
            winner=selected[bank].get(tenor)
            selected_row=rows[0]
            expected=Decimal(winner['rate_pct'])/100 if winner else None
            if bank_in_scope(bank):
                if source_sheet:
                    # The raw sheet keeps each product's own MAX formula.
                    # Bind the winning record to its detail reference, even on a tie.
                    rate_rows=[(row,main.value(row,3)) for row in rows if numeric(main.value(row,3))]
                    actual=max((value for _,value in rate_rows),default=None)
                    if winner:
                        escaped=re.escape(source_sheet)
                        refs=rf"(?<![\w'])(?:'{escaped}'|{escaped})!\$?H\$?(\d+)(?!\d)"
                        matches=[row for row,value in rate_rows if abs(value-expected)<=Decimal('1e-12')
                                 and str(winner['input_row']) in re.findall(refs,main.cells[row,3].findtext(M+'f') or '')]
                        if len(matches)!=1:
                            raise ValueError('SGD主表未能定位统一获选报价所在产品行：'+bank+'/'+tenor)
                        selected_row=matches[0]
                else:
                    actual=main.value(selected_row,3)
                if ((expected is not None and (not numeric(actual) or abs(actual-expected)>Decimal('1e-12')))
                        or (expected is None and numeric(actual))):
                    raise ValueError('SGD主表最高值与统一获选记录不一致：'+bank+'/'+tenor)
            amount,note=summary_terms([(tenor,winner)])
            if winner:
                note=winner['product_name']+' / '+winner['audience']+'\n'+note.replace('完整条件及来源见报价明细。\n','')+'\n完整资格条件及来源见“全部公开报价”，报价编号：'+winner['quote_id']
            for row in rows:
                for column,value in ((delta+1,amount if row==selected_row else '-'),):
                    element=main.cell(row,column);style=styles.customize(int(element.get('s','0')),format_code='General',wrap=True,alignment='left')
                    main.put(row,column,value,style)
                if not preserve_quote_notes:
                    element=main.cell(row,delta+2);style=styles.customize(int(element.get('s','0')),format_code='General',wrap=True,alignment='left')
                    main.put(row,delta+2,note if row==selected_row else '',style)
            if not preserve_quote_notes:main.height(selected_row,fitted_height([amount,note],[28,70],min(409,float(main.rows[selected_row].get('ht','20')))))
            main_checks.append(dict(bank=bank,tenor=tenor,row=selected_row,quote_id=winner['quote_id'] if winner else None,amount=amount))
        widths=width_map(main)
        for column,width in ((delta+1,28),(delta+2,70)):widths[column]=dict(widths.get(column,{}),width=str(width),hidden='0',customWidth='1')
        set_columns(main,widths)
    book.save(path)
    return dict(passed=True,rates_checked=checked,updated_cells=changes,main_selection=main_checks)


def attach_four(files,plan_path,folder):
    cat=catalog(plan_path)
    summaries={
        'raw_report':align_sgd_summary(files['raw_report'],cat,source_sheet=cat.get('sgd_input_sheet')),
        'formatted_report':align_sgd_summary(files['formatted_report'],cat,preserve_quote_notes=True),
    }
    checks={name:write_catalog(path,cat) for name,path in files.items()}
    if len({c['catalog_sha256'] for c in checks.values()})!=1:raise ValueError('四份公开报价集合不一致')
    if len({c['selection_sha256'] for c in checks.values()})!=1:raise ValueError('四份Personal获选记录不一致')
    grouped=defaultdict(list)
    for r in cat['records']:grouped[coverage_key(r)].append(r)
    all_groups=set(grouped)|set(cat['source_counts'])
    audit=[dict(bank=k[0],currency=k[1],rate_type=k[2],product_id=k[3],tenor=k[4],audience=k[5],channel=k[6],source_quotes=cat['source_counts'].get(k,0),verified=len(grouped[k]),displayed=len(grouped[k]),
                reason='人工排除或更正；展示数量按核准记录调整' if cat['source_counts'].get(k,0)!=len(grouped[k]) and cat['manual_adjustments'].get(k) else '全部原始报价逐条列于四份文件的“全部公开报价”；比较区未适用项注明原因',
                selection_adjustment=cat['manual_adjustments'].get(k) or None) for k in sorted(all_groups)]
    if any(cat['source_counts'].get(k,0)!=len(grouped[k]) and not cat['manual_adjustments'].get(k) for k in all_groups):
        raise ValueError('来源报价数与已核验展示数存在未解释差异')
    result=dict(passed=True,files=checks,source_coverage=cat['source_coverage'],coverage=audit,winners=cat['winners'],summary_alignment=summaries)
    save(Path(folder)/'quote-coverage.audit.json',result)
    return result
