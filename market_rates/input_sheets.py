"""Independent Excel input sheets; existing report/rainbow layouts are untouched."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import json
import re
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from .common import load

CHANNELS={'online':'在线','branch_or_online':'分行 / 在线','staff_instruction':'分行 / 客户经理指令','branch_or_mobile':'分行 / RHB Mobile','branch_or_instruction':'分行 / 指令表单','hlf_digital':'HLF Digital','unknown':'待核实','branch':'分行','online_banking|sc_mobile':'网银 / SC Mobile'}
AUDIENCES={'personal':'Personal','preferred':'Preferred','premier':'Premier','private':'Private','premier_elite':'Premier Elite','premier_wealth':'Premier（财富/FX资格）','premier_standard':'Premier（无财富持仓）','all':'所有客群','unknown':'待核实'}

def number(value,missing='未列明'):
    return float(Decimal(str(value))) if value is not None else missing

def source_urls(row,pages):
    return '\n'.join(dict.fromkeys(pages.get(e['page_id'],{}).get('url','') for e in row.get('evidence',[]) if pages.get(e['page_id'],{}).get('url')))

def create(out,kind):
    out=Path(out);p=load(out/'table-validation-plan.json')
    rows=p['details']
    if not rows:raise ValueError('没有报价明细，不能创建空输入表')
    if [r['input_row'] for r in rows]!=list(range(4,len(rows)+4)):raise ValueError('输入行号必须连续且从第4行开始')
    wb=Workbook();s=wb.active;s.title=p['input_sheet'];s.sheet_view.showGridLines=False
    pages={}
    if kind!='validation':pages={x['id']:x for x in load(Path(p['source_run'])/'run.json')['pages']}
    if kind=='validation':
        columns=13;widths=[8,42,12,15,24,17,17,12,17,15,15,26,15,3,34,80,70]
        title=p['collection_label']+' · SGD促销文字提取明细（待复核）'
        subtitle='汇编 '+p['as_of']+'；证据日期：'+'；'.join(f'{b} {d}' for b,d in p['bank_dates'].items())+'。未采集银行保留历史样本，不代表全市场最新排名。'
        headers=['银行','产品','实际期限','客群','渠道','起存金额(SGD)','单笔上限(SGD)','年利率','新资金要求','开始日','结束日','验证状态','证据日期']
        for r in rows:
            status='已人工处理；见修正记录' if r.get('manual_reviewed') else '已人工确认；沿用采集结果' if r.get('human_reviewed') else '计息口径待人工核实' if r['rate_basis']=='unknown' else '文字来源已核对；条款待复核'
            if r.get('reference_quote'):status='最新公开参考；原公告已截止，保留原期限'
            values=[r['bank'],r['product_name'],f"{r['tenor_value']}{r['tenor_unit']}",AUDIENCES.get(r['audience'],r['audience']),CHANNELS.get(r['channel'],r['channel']),number(r['amount_min'],'待核实'),number(r['amount_max']),number(r['rate_pct'])/100,{'yes':'需要','no':'不限新资金','unknown':'见条款待核'}[r['fresh_funds']],date.fromisoformat(r['valid_from']) if r['valid_from'] else '未列明/待核',date.fromisoformat(r['valid_to']) if r['valid_to'] else '未列明',status,p['bank_dates'][r['bank']]]
            put(s,r['input_row'],values)
        put(s,3,['产品','官方来源','条件原文/待复核'],15)
        seen=set();line=4
        for g in p['groups']:
            if g['product_id'] in seen:continue
            seen.add(g['product_id']);put(s,line,[g['product_name'],'\n'.join(p['bank_urls'].get(g['bank'],[])),'\n'.join(dict.fromkeys(d['conditions'] for d in rows if d['product_id']==g['product_id']))],15);line+=1
    elif kind=='board':
        columns=18;widths=[9,9,9,28,11,23,16,17,16,23,13,15,15,35,50,70,22,17]
        title=p['as_of']+' '+' / '.join(p['banks'])+' 挂牌明细'
        subtitle='本页为报价输入。按既定表格规则，利率数字统一按百分数使用；不要求原文出现p.a.。I列为可插表数值，J列保留原始数字。原CNH在调研和彩虹表中使用CNY，明细保留CNH；不采集JPY。'
        headers=['银行','币种','类别','产品','实际期限','客群 / 渠道','金额下限','金额上限','利率','原始数值 / 单位','金额币种','生效日','采集日','核验状态','条件','来源','金额边界','银行标注日期']
        for r in rows:
            put(s,r['input_row'],[r['bank'],r['currency'],'挂牌',r['product_name'],f"{r['tenor_value']}{r['tenor_unit']}",r['audience']+' / '+r['channel'],number(r['amount_min']),number(r['amount_max']),number(r['rate_pct'])/100 if r['insertable'] else '-',r['rate_pct']+'%',r['amount_currency']+('等值' if r['amount_is_equivalent'] else ''),r['valid_from'] or '未列明',r.get('evidence_date') or p['as_of'],'用户已确认' if r.get('manual_reviewed') else '文字核验通过，已插表' if r['insertable'] else r.get('hold_reason') or '暂不插表，见条件',r['conditions'],source_urls(r,pages),('含' if r['min_inclusive'] else '不含')+'下限；'+('含' if r['max_inclusive'] else '不含')+'上限',r.get('source_date') or '未列明'])
    elif kind=='fx':
        columns=20;widths=[10,10,10,24,10,28,16,16,14,14,14,14,14,40,65,65,14,30,16,16]
        title=p['as_of']+' 外币促销明细'
        subtitle='I列为本期展示报价，J列为原始值；存在已核验DBS促销时I列=S列挂牌+T列加点。只使用各产品最新公告并保留有效期，截止后明确标注参考。按实际期限保留各产品、客群及金额档；Personal最高值仅取普通个人合资格报价。CNH按CNY显示。'
        headers=['银行','原币种','展示币种','产品','实际期限','客群 / 渠道','金额下限','金额上限','本期利率','原始利率','金额币种','生效日','截止日','插表状态','条件','来源网址','采集日','核验方式','DBS当期挂牌','DBS促销加点']
        for r in rows:
            n=r['input_row'];put(s,n,[r['bank'],r['currency'],r['display_currency'],r['product_name'],f"{r['tenor_value']}{r['tenor_unit']}",r['audience']+' / '+r['channel'],number(r['amount_min']),number(r['amount_max']),number(r['rate_pct'])/100 if r['insertable'] else '-',number(r['rate_pct'])/100,r['amount_currency']+('等值' if r['amount_is_equivalent'] else ''),date.fromisoformat(r['valid_from']) if r['valid_from'] else '未列明',date.fromisoformat(r['valid_to']) if r['valid_to'] else '未列截止日','已核验，可插表' if r['insertable'] else r['hold_reason'],r['conditions'],source_urls(r,pages),date.fromisoformat(p['as_of']),r['verification'],None,None])
            if r['bank']=='DBS' and r['insertable']:
                m=re.search(r'当期挂牌([\d.]+)%＋加点([\d.]+)%',r['conditions'])
                if not m:raise ValueError('Missing DBS calculation')
                s.cell(n,19,float(Decimal(m[1])/100));s.cell(n,20,float(Decimal(m[2])/100));s.cell(n,9,f'=SUM(S{n}:T{n})')
            if r.get('reference_quote'):s.cell(n,14,'最新公开参考报价；公告已截止');s.cell(n,15,f"{r['conditions']}；公告期 {r['valid_from']} 至 {r['valid_to']}；按既定策略插表")
        s.freeze_panes='A4'
    else:raise ValueError('Unknown input sheet kind')
    end=len(rows)+3
    for i,width in enumerate(widths,1):s.column_dimensions[get_column_letter(i)].width=width
    s.merge_cells(start_row=1,start_column=1,end_row=1,end_column=columns);s.cell(1,1,title)
    s.merge_cells(start_row=2,start_column=1,end_row=2,end_column=columns);s.cell(2,1,subtitle);put(s,3,headers)
    for row in s.iter_rows(min_row=1,max_row=max(end,s.max_row),max_col=len(widths)):
        for c in row:c.font=Font(name='Arial' if kind=='validation' else 'Microsoft YaHei',size=11 if kind=='validation' else 10);c.alignment=Alignment(vertical='center',wrap_text=True)
    for n in range(1,end+1):s.row_dimensions[n].height=42 if kind=='validation' else 48 if kind=='board' else 64
    s.row_dimensions[1].height=32;s.row_dimensions[2].height=60 if kind=='validation' else 42;s.row_dimensions[3].height=32
    for row in s.iter_rows(min_row=3,max_row=end,max_col=columns):
        for c in row:
            if kind!='fx':c.border=Border(**{side:Side(style='thin',color='CBD5DB') for side in ('left','right','top','bottom')})
    for c in s[3][:columns]:c.fill=PatternFill('solid',fgColor='173D54' if kind=='validation' else 'DDEBF7');c.font=Font(name='Microsoft YaHei',size=10,bold=True,color='FFFFFF' if kind=='validation' else '000000')
    s.cell(1,1).font=Font(name='Microsoft YaHei',size=15,bold=True,color='173D54')
    for r in rows:
        n=r['input_row']
        for c in ([6,7] if kind=='validation' else [7,8]):s.cell(n,c).number_format='#,##0.##'
        for c in ([8] if kind=='validation' else [9] if kind=='board' else [9,10,19,20]):s.cell(n,c).number_format='0.00%' if kind=='validation' else '0.0000%'
        for c in ([10,11] if kind=='validation' else [12,13,17] if kind=='fx' else []):s.cell(n,c).number_format='yyyy-mm-dd'
        if kind=='validation':
            for c,key,inc in [(6,'amount_min','min_inclusive'),(7,'amount_max','max_inclusive')]:
                if r[key] is not None:s.cell(n,c).number_format='"'+(('≥' if r[inc] else '>') if c==6 else ('≤' if r[inc] else '<'))+'"#,##0'+('.00' if Decimal(str(r[key]))%1 else '')
        s.cell(n,12 if kind=='validation' else 14).fill=PatternFill('solid',fgColor='FFF2CC')
    if kind=='fx':
        for i,status in enumerate(p.get('source_statuses',[])):
            n=end+2+i;s.merge_cells(start_row=n,start_column=1,end_row=n,end_column=20);s.cell(n,1,p['as_of']+' 来源状态：'+status['note']);s.cell(n,1).alignment=Alignment(wrap_text=True);s.row_dimensions[n].height=42
    filename={'validation':'validation-inputs.xlsx','board':'board-inputs.xlsx','fx':'fx-inputs.xlsx'}[kind]
    wb.save(out/filename)
    if kind=='validation':
        # This preview is diagnostic only; Excel handles final formula calculation.
        (out/'input-inspect.jsonl').write_text('\n'.join(json.dumps([c.value for c in row],ensure_ascii=False,default=str) for row in s.iter_rows(min_row=3,max_row=min(10,end),max_col=13)),encoding='utf-8')
    return out/filename

def put(sheet,row,values,start=1):
    for col,value in enumerate(values,start):
        cell=sheet.cell(row,col,value)
        # Evidence is text, never an executable spreadsheet formula.
        if isinstance(value,str):cell.data_type='s'
