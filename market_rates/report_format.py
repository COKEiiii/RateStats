"""Repeatable, offline report finishing stage; no collection or model calls.

Public API: format_report(input_path, template_path, output_path, ...).
CLI: python -m market_rates.report_format --help
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .report_xml import Book, StyleMap, M, NS, address, col, pos, numeric, local
from .amount_rows import amount_identity
from .scope import bank_name
from .fx_product_rows import caption_identity,equal_rate_coverage
from .sgd_conditions import SGDConditions, summarize_sgd_conditions
from .quote_selection import personal_quote,caption_is_personal
from .workbook_policy import workbook_policy

GRID = {'SGD促销':(1,3), 'USD促销':(2,4), 'CNY促销':(1,3),
        '其他外币促销利率':(2,4), 'USD挂牌':(2,4), 'CNY挂牌':(1,3)}
DELTA_LABELS = {'当日增幅','当日最高报价变动'}
SOURCE_PATTERNS = (r'明细_\d{8}(?:_\d+)?',r'挂牌全批_\d{8}(?:_.+)?',r'外币促销_\d{8}(?:_.+)?')
CURRENCIES = {'澳元':'AUD','纽元':'NZD','新西兰元':'NZD','欧元':'EUR','英镑':'GBP',
              '加元':'CAD','港币':'HKD','美元':'USD','人民币':'CNY','新元':'SGD','瑞士法郎':'CHF'}


def text(value):
    return '' if value is None else str(value).strip()


def is_source(name):
    return any(re.fullmatch(p,name) for p in SOURCE_PATTERNS)


def money(value):
    if not numeric(value):
        return text(value)
    return f'{value:,.0f}' if value == int(value) else f'{value:,f}'


def parse_date(value):
    if numeric(value) and Decimal(30000) < value < Decimal(80000):
        return (datetime(1899,12,30)+timedelta(days=float(value))).date()
    match = re.fullmatch(r'(20\d{2})[-/]?(\d{2})[-/]?(\d{2})(?:T.*)?',text(value))
    try:
        return date(*map(int,match.groups())) if match else None
    except ValueError:
        return None


def serial(day):
    return Decimal((day-date(1899,12,30)).days)


def infer_date(book):
    values = []
    for name,(bankcol,current) in GRID.items():
        if name in book.sheets:
            sheet = book.sheets[name]
            for r in range(1,min(sheet.maxrow,12)+1):
                if text(sheet.value(r,bankcol)) in ('银行','日期','Bank'):
                    value = parse_date(sheet.value(r,current))
                    if value:
                        values.append(value)
                    break
    if not values or len(set(values)) != 1:
        raise ValueError(f'Current collection date is missing or inconsistent: {values}; check the input report')
    return values[0]


def customer_is_personal(audience):
    from .quote_selection import is_personal
    return is_personal(audience)


def clean_amount(value):
    """Keep source currency, numbers and open/closed bounds; never invent a bound."""
    source = text(value).replace('＜','<').replace('＞','>').replace('＝','=')
    source = source.replace('≥','>=').replace('≤','<=').replace('，',',')
    parts = [p.strip() for p in re.split(r'[；;\n]',source) if p.strip()]
    if not parts:
        return '-'
    currency = r'(?:USD|SGD|CNY|CNH|RMB|AUD|NZD|EUR|GBP|CAD|HKD|US\$|U\$|S\$|R\$|\$)'
    number = r'\d[\d,]*(?:\.\d+)?\s*(?:million|mil|[kKmM]|万)?'
    # A semicolon can separate the two halves of one published amount band,
    # e.g. "SGD >=10,000; <=1,000,000". Join only adjacent *pure* monetary
    # bounds in the same currency. Eligibility such as AUM, TRB or a personal
    # aggregate cap must stay a note and can never become the deposit upper cap.
    pure_bound = re.compile(r'^(?:且\s*|and\s+)?(?:(?P<currency>'+currency+r')\s*(?:等值\s*)?)?(?P<op>[<>]=?)\s*'+number+r'$',re.I)
    joined = []
    aliases = {'S$':'SGD','US$':'USD','U$':'USD','RMB':'CNY','R$':'CNY'}
    for part in parts:
        left = pure_bound.fullmatch(joined[-1]) if joined else None
        right = pure_bound.fullmatch(part)
        if left and right and left['op'].startswith('>') and right['op'].startswith('<'):
            lc,rc=left['currency'],right['currency']
            compatible=not rc or (lc and aliases.get(lc.upper(),lc.upper())==aliases.get(rc.upper(),rc.upper()))
            if compatible:
                joined[-1] += ' 且'+part[right.start('op'):]
                continue
        joined.append(part)
    parts = joined
    candidates = []
    for p in parts:
        if re.search(r'(?:采集|证据|截止|公告|核验|个人|personal|premier|preferred|产品)',p,re.I) and not re.search(currency+r'\s*(?:等值)?\s*[<>]?=?\s*\d',p,re.I):
            continue
        if not re.search(number,p):
            continue
        score = 4*bool(re.search(currency,p,re.I))+2*bool(re.search(r'[<>]|以上|以下|\d\s*[-–]',p))
        if any(word in p for word in ('起存','金额档位','柜台','电子银行')):
            score -= 2
        candidates.append((score,p))
    if not candidates:
        return '未列明' if any(w in source for w in ('未列','unspecified')) else '-'
    candidate = max(candidates,key=lambda item:item[0])[1]
    # Remove narrative prefixes/suffixes, preserving any bound punctuation.
    match = re.search(r'(?:'+currency+r'\s*(?:等值)?\s*)?(?:[<>]=?\s*)?'+number,candidate,re.I)
    if not match:
        return '-'
    candidate = candidate[match.start():]
    candidate = re.split(r'；|;|(?:电子银行|柜台|客户|渠道|个人|personal|online|counter|分行|须|需|必须)',candidate,flags=re.I)[0]
    candidate = re.sub(r'\s*(?:起存|起|存款|最低金额|最少)\s*$','',candidate).strip(' ，,：:/')
    candidate = re.sub(r'\s+',' ',candidate)
    return candidate.replace('>=','≥').replace('<=','≤') or '-'


def compact_notes(value):
    """Remove process narration, not financial eligibility clauses."""
    parts = re.split(r'[；;\n]',text(value))
    keep = []
    for part in parts:
        part = part.strip()
        if not part or (part.startswith(('http://','https://')) or re.match(r'^[A-Za-z]:[\\/]',part)):
            continue
        if part.lower() in ('personal','all','定期存款挂牌','外币挂牌','挂牌','渠道原文未列明','渠道未列明','未列明','not stated'):
            continue
        if re.search(r'^(?:本周已检查|新增独立产品行|证据\s*20|采集\s*20|银行标注\s*20|已核验|自动核验|核验通过|本期核验|明细有来源日期|详见明细|来源[:：]|数字未调整)',part):
            continue
        part = re.sub(r'（待复核）|\(待复核\)|沿用原调研分类|金额档位以本表上下界为准','',part)
        part = part.replace('须另询银行，不能直接套用本表','须另询银行').replace('渠道原文未列明','渠道未列明')
        replacements={'新资金要求：需要':'须新资金','新资金要求：见条款待核':'新资金要求待核',
                      '新资金要求：不限制资金':'','条款待复核':'条款待核',
                      'Fresh funds only':'须新资金','Fresh Funds':'须新资金',
                      'Existing or new Savings Account required':'须有储蓄账户',
                      'For New and Existing Customers':'新老客户均可',
                      '官网金额上限依序分档':'','月期/周期起存门槛按官网脚注分别处理':'月期/周期起存门槛分别适用'}
        for before,after in replacements.items():
            part=part.replace(before,after)
        part = part.strip(' ，,；;')
        if part and part not in keep:
            keep.append(part)
    return '；'.join(keep)


def eligibility_from_amount(value):
    cleaned=clean_amount(value)
    pure_bound=re.compile(r'^(?:USD|SGD|CNY|CNH|RMB|AUD|NZD|EUR|GBP|CAD|HKD|US\$|U\$|S\$|R\$|\$)?\s*(?:等值\s*)?(?:且\s*|and\s+)?[<>≥≤]=?\s*\d[\d,]*(?:\.\d+)?\s*(?:million|mil|[kKmM]|万)?$',re.I)
    def bare_bound(part):
        part=re.sub(r'^(?:USD|SGD|CNY|CNH|RMB|AUD|NZD|EUR|GBP|CAD|HKD|US\$|U\$|S\$|R\$|\$)\s*(?:等值\s*)?','',part,flags=re.I)
        return re.sub(r'\s+','',part.replace('>=','≥').replace('<=','≤'))
    amount_bounds={bare_bound(p) for p in cleaned.split(' 且')}
    result=[]
    for part in re.split(r'[；;\n]',text(value)):
        part=part.strip()
        if not part:
            continue
        if pure_bound.fullmatch(part) and bare_bound(re.sub(r'^(?:且\s*|and\s+)','',part,flags=re.I)) in amount_bounds:
            continue
        if clean_amount(part)==cleaned and not re.search(r'须|必须|换汇|账户|新资金|fresh|客户|premier|preferred',part,re.I):
            continue
        if re.fullmatch(r'.{0,35}\s*/\s*(?:personal|all)',part,re.I):
            continue
        result.append(part)
    return compact_notes('；'.join(result))


def channel_label(channel):
    source = text(channel).lower()
    online = any(x in source for x in ('online','app','mobile','在线','网银','电子','手机'))
    counter = any(x in source for x in ('counter','branch','分行','柜台','柜面','客户经理','指令','顾问'))
    return 'counter/online' if online and counter else 'online' if online else 'counter' if counter else '渠道未列明'


@dataclass
class Quote:
    bank: str
    tenor: str
    audience: str
    channel: str
    minimum: object
    maximum: object
    rate: Decimal
    notes: str
    actual: str
    source: str
    row: int
    product: str = ''
    min_inclusive: bool | None = None
    max_inclusive: bool | None = None

    @property
    def amount(self):
        if numeric(self.minimum) and numeric(self.maximum):
            if self.min_inclusive is not None or self.max_inclusive is not None:
                return f'S${"≥" if self.min_inclusive is not False else ">"}{money(self.minimum)}–{"≤" if self.max_inclusive is not False else "<"}{money(self.maximum)}'
            return f'S${money(self.minimum)}–{money(self.maximum)}'
        if numeric(self.minimum):
            return f'S${">" if self.min_inclusive is False else "≥"}{money(self.minimum)}'
        if numeric(self.maximum):
            return f'S${"<" if self.max_inclusive is False else "≤"}{money(self.maximum)}'
        return '金额未列明'

    @property
    def caption(self):
        return self.caption_for(show_audience=False)

    def caption_for(self, *, show_audience):
        return ' '.join(filter(None, (f'{self.rate*100:.2f}%',
            audience_label(self.audience) if show_audience else '',
            channel_label(self.channel), self.amount)))


def audience_label(audience):
    """Normalize equivalent Personal/all labels without changing eligibility."""
    value = text(audience)
    if customer_is_personal(value):
        return 'Personal'
    return {'premier':'Premier','preferred':'Preferred'}.get(value.casefold(),value)


def show_audience_labels(quotes):
    """Show labels only when this bank/tenor's quote list has multiple classes.

    Multiple amount tiers or channels do not count as multiple customer classes;
    Personal and all-customer quotes are the same class for this display rule.
    """
    return len({audience_label(q.audience).casefold() for q in quotes if audience_label(q.audience)}) >= 2


def public_quote_captions(quotes):
    show_audience = show_audience_labels(quotes)
    captions = [];seen=set()
    for q in sorted(quotes,key=lambda q:(not customer_is_personal(q.audience),-q.rate,q.audience,q.amount)):
        identity=(q.source,q.row,q.product,q.rate,q.minimum,q.maximum,audience_label(q.audience),q.channel,q.notes,q.actual)
        if identity in seen:continue
        seen.add(identity)
        caption = q.caption_for(show_audience=show_audience)
        if q.product:caption+=' '+q.product
        if q.notes:caption+='；'+q.notes
        captions.append(caption)
    return captions


def row_contexts(sheet, bankcol):
    """Identify banks from labels and section headers, never fixed row numbers."""
    section, bank, currency = None, None, sheet.name[:3]
    contexts, headers, section_rows = {}, [], []
    for r in range(1,sheet.maxrow+1):
        labels = [text(sheet.value(r,c)) for c in range(1,bankcol+2)]
        for column,label in enumerate(labels,1):
            if column<=bankcol and '促销利率' in label:
                section,bank=None,None
            for chinese,code in CURRENCIES.items():
                if chinese in label and ('利率' in label or '存款' in label):
                    currency = code
            # Legacy CNY sheets contain the literal typo "3 monthR"/"6 monthR".
            tenor = re.fullmatch(r'(?:Tenor:\s*)?(\d+)\s*(Months?|Days?|M|D)[Rr]?',label,re.I)
            if tenor:
                section = tenor[1]+('D' if tenor[2].lower().startswith('d') else 'M')
                bank = None
                section_rows.append(r)
            elif label.lower().startswith('tenor:'):
                raise ValueError(f'{sheet.name}!row {r}: unrecognized tenor header {label!r}')
        label = text(sheet.value(r,bankcol))
        if label in ('日期','银行','Bank'):
            headers.append(r)
            bank = None
            continue
        if section and label and re.fullmatch(r'[A-Za-z][A-Za-z /&.-]*',label) and not re.match(r'Tenor|Months?|Days?',label,re.I):
            bank = bank_name(label)
        if section and bank:
            contexts[r] = (currency,section,bank)
    return contexts, headers, section_rows


def sgd_quotes(book, as_of, issues=None):
    candidates = [s for s in book.sheets.values() if re.fullmatch(r'明细_\d{8}(?:_\d+)?',s.name)
                  and text(s.value(3,8))=='年利率']
    if not candidates:
        raise ValueError('SGD verified source sheet is required (明细_YYYYMMDD[_N]); refusing to infer Personal from a total MAX')
    if 'SGD促销' not in book.sheets:
        raise ValueError('Missing SGD促销 sheet')
    def source_rank(source):
        count=sum(1 for (r,c),node in book.sheets['SGD促销'].cells.items()
                  if c==3 and node.find(M+'f') is not None and source.name in (node.find(M+'f').text or ''))
        version=re.search(r'_(\d+)$',source.name)
        return count,int(version[1]) if version and len(version[1])!=8 else 0
    candidates=[s for s in candidates if s.name.startswith('明细_'+as_of.strftime('%Y%m%d'))]
    if not candidates:
        raise ValueError(f'No SGD source sheet matches the current collection date {as_of}')
    sheet=max(candidates,key=source_rank)
    style_root=book.xml('xl/styles.xml');formats={int(n.get('numFmtId')):n.get('formatCode','') for n in style_root.findall(M+'numFmts/'+M+'numFmt')}
    def inclusive(row,column):
        node=sheet.cells.get((row,column))
        if node is None:return None
        xf=style_root.find(M+'cellXfs')[int(node.get('s','0'))];code=formats.get(int(xf.get('numFmtId','0')),'')
        if any(symbol in code for symbol in ('≥','≤','>=','<=')):return True
        if '>' in code or '<' in code:return False
        return None
    condition_source = SGDConditions(sheet)
    display = {}
    view = book.sheets['SGD促销']
    contexts,_,_ = row_contexts(view,1)
    for (r,c),element in view.cells.items():
        formula = element.find(M+'f')
        if c != 3 or r not in contexts or formula is None:
            continue
        for match in re.finditer(re.escape(sheet.name)+r"'?!(?:\$)?H(?:\$)?(\d+)",formula.text or ''):
            display[int(match[1])] = contexts[r][1]
    result = defaultdict(list)
    for r in range(4,sheet.maxrow+1):
        rate = sheet.value(r,8)
        if not numeric(rate):
            continue
        bank = bank_name(text(sheet.value(r,1)))
        actual = text(sheet.value(r,3)).upper()
        tenor = actual
        raw_notes = condition_source.for_row(r)
        summary, unresolved = summarize_sgd_conditions(raw_notes, text(sheet.value(r,4)))
        notes = compact_notes(summary)
        # Some existing source workbooks contain a pasted clause from another
        # bank. Do not promote that known contradiction into customer guidance.
        mismatch = bank!='SCB' and (raw_notes.count('Standard Chartered')>=3 or raw_notes.count('www.sc.com/sg/td')>=3)
        if mismatch or unresolved:
            reason = '源备注混入其他银行条款，资格待核' if mismatch else '另有资格条款待核（详见原始明细）'
            if issues is not None:
                issues.append({'bank':bank,'tenor':actual,'sheet':sheet.name,'row':r,
                               'reason':reason,'original_notes':raw_notes})
            if mismatch:
                notes = reason
        fresh = text(sheet.value(r,9))
        if fresh not in ('','未列明','无','否','不要求','未明确要求'):
            notes = compact_notes('新资金要求：'+fresh+'；'+notes)
        end = parse_date(sheet.value(r,11))
        if end and end < as_of:
            notes = compact_notes(notes+f'；最新公开参考，公告已截止（{end.isoformat()}）')
        if actual != tenor:
            notes = compact_notes(f'实际{actual}，归入{tenor}组；'+notes)
        audience=text(sheet.value(r,4))
        if customer_is_personal(audience) and 'For Personal Banking customers' in notes:
            # This source enumerates alternative eligible customer classes.
            # Once Personal is explicit, other classes do not restrict it.
            notes='；'.join(p for p in notes.split('；') if p!='For Personal Banking customers'
                           and not re.fullmatch(r'Premier (?:Elite customers|customers.*)',p))
        q = Quote(bank,tenor,audience,text(sheet.value(r,5)),
                  sheet.value(r,6),sheet.value(r,7),rate,notes,actual,sheet.name,r,text(sheet.value(r,2)),inclusive(r,6),inclusive(r,7))
        result[bank,tenor].append(q)
    return result


def winner(quotes):
    return personal_quote(quotes)


def freeze_values(book, audit):
    from .premier_details import DETAIL_SHEET,validate_links
    linked=DETAIL_SHEET in book.sheets
    if linked:validate_links(book)
    for sheet in book.sheets.values():
        if linked and sheet.name in ('Premier Rates',DETAIL_SHEET):continue
        for (r,c),element in list(sheet.cells.items()):
            formula = element.find(M+'f')
            value = sheet.value(r,c)
            if formula is not None:
                if value is None:
                    raise ValueError(f'Missing formula cache at {sheet.name}!{address(r,c)}. Save/recalculate in Excel first.')
                audit['formulas_frozen'] += 1
                element.remove(formula)
            if element.get('t')=='e':
                audit['legacy_errors'].append({'sheet':sheet.name,'cell':address(r,c),'value':value,
                                               'formula':formula.text if formula is not None else None})
                sheet.put(r,c,'-')
            elif isinstance(value,str) and re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*%',value.strip()):
                sheet.put(r,c,Decimal(value.strip().rstrip('%').strip())/100)


def historical_rate(value):
    """Extract a rate from a legacy quote caption, keeping its source in audit."""
    rates=[]
    for part in re.split(r'[/\n；;]',text(value)):
        if re.search(r'premier|preferred|priority|elite|corporate|citigold',part,re.I) and not re.search('personal',part,re.I):
            continue
        rates.extend(Decimal(m)/100 for m in re.findall(r'(\d+(?:\.\d+)?)\s*%',part))
    return max(rates) if rates else '-'


def normalize_historical_rates(book,audit):
    for name,(bankcol,current) in GRID.items():
        if name not in book.sheets:
            continue
        sheet=book.sheets[name]
        contexts,headers,_=row_contexts(sheet,bankcol)
        delta=first_delta(sheet)
        for r in contexts:
            header=max(h for h in headers if h<r)
            for c in range(current,delta):
                value=sheet.value(r,c)
                if not parse_date(sheet.value(header,c)) or value in (None,'-','') or numeric(value):
                    continue
                rate=historical_rate(value)
                audit['historical_text_rates'].append({'sheet':name,'cell':address(r,c),'original':value,'selected':str(rate)})
                sheet.put(r,c,rate)


def first_delta(sheet):
    cols = {c for (r,c) in sheet.cells if text(sheet.value(r,c)) in DELTA_LABELS}
    if len(cols)!=1:
        raise ValueError(f'{sheet.name}: expected one current change column, found {sorted(cols)}')
    return next(iter(cols))


def existing_header(sheet, bankcol):
    return next((r for r in range(1,min(sheet.maxrow,15)+1) if text(sheet.value(r,bankcol)) in ('银行','日期','Bank')),None)


def comparison_columns(book):
    """Record the two existing leading date columns, without inserting data.

    Each section retains its own dates. New sections with no adjacent history
    stay blank; unlabelled numeric history is a layout error, not permission
    to look up another workbook or skip to older data.
    """
    result={}
    for name,(bankcol,current) in GRID.items():
        if name not in book.sheets:
            continue
        sheet=book.sheets[name]
        contexts,headers,_=row_contexts(sheet,bankcol)
        if not headers or first_delta(sheet)<=current+1:
            raise ValueError(f'{name}: missing the latest two date columns')
        sections=[]
        for index,r in enumerate(headers):
            now,prior=(parse_date(sheet.value(r,c)) for c in (current,current+1))
            end=headers[index+1] if index+1<len(headers) else sheet.maxrow+1
            empty_history=(sheet.value(r,current+1) in (None,'','-') and
                           not any(numeric(sheet.value(rr,current+1)) or historical_rate(sheet.value(rr,current+1))!='-'
                                   for rr in contexts if r<rr<end))
            if now is None or (prior is None and not empty_history) or (prior is not None and prior>now):
                raise ValueError(f'{name}!{address(r,current)}:{address(r,current+1)}: invalid latest two date columns')
            sections.append({'input_header_row':r,'current_column':col(current),'previous_column':col(current+1),
                             'current_date':now.isoformat(),'previous_date':prior.isoformat() if prior else None})
        result[name]=sections
    return result


def source_style(styles, ref, row, column, fallback=0):
    cell = ref.cells.get((row,column))
    return styles.get(cell.get('s','0') if cell is not None else fallback)


def width_map(sheet):
    result = {}
    cols = sheet.root.find(M+'cols')
    if cols is not None:
        for node in cols:
            for c in range(int(node.get('min')),int(node.get('max'))+1):
                result[c] = dict(node.attrib)
    return result


def set_columns(sheet, widths):
    old = sheet.root.find(M+'cols')
    if old is not None:
        sheet.root.remove(old)
    node = ET.Element(M+'cols')
    for c,values in sorted(widths.items()):
        values = dict(values,min=str(c),max=str(c))
        ET.SubElement(node,M+'col',values)
    sheet.insert_element(node)


def copy_layout(sheet, ref, styles, current=None, delta=None):
    # Page settings are properties of the report, not its changing row indices.
    for tag in ('sheetFormatPr','printOptions','pageMargins','pageSetup','headerFooter'):
        node = ref.root.find(M+tag)
        old = sheet.root.find(M+tag)
        if node is not None:
            if old is not None:
                sheet.root.remove(old)
            sheet.insert_element(deepcopy(node))
    widths = width_map(sheet)
    reference = width_map(ref)
    ref_delta = first_delta(ref) if current else None
    for c in range(1,sheet.maxcol+1):
        source_col = c
        if current:
            if c >= delta:
                source_col = ref_delta+c-delta
            elif c > current+1:
                source_col = min(c,ref_delta-1)
        if source_col in reference:
            widths[c] = dict(reference[source_col])
            if 'style' in widths[c]:
                # Column defaults index the workbook's cellXfs table just like
                # cell styles do. Copying the raw number can turn an unstyled
                # reference column into a bordered column in the target book.
                widths[c]['style']=str(styles.get(widths[c]['style']))
        if current and current<=c<delta:
            widths[c] = dict(widths.get(c,{}),width=str(reference.get(current if c==current else current+1,{}).get('width','12')),customWidth='1',hidden='0' if c<=current+1 else '1')
        if delta and c in (delta,delta+1,delta+2):
            widths[c] = dict(widths.get(c,{}),hidden='0',width='21' if c==delta else '42',customWidth='1')
    set_columns(sheet,widths)
    # Use a small useful frozen region; the reference sometimes freezes every
    # data row (e.g. A58). Do not reproduce an unusable full-screen frozen pane.
    views = sheet.root.find(M+'sheetViews')
    if views is not None and len(views):
        view = views[0]
        view.set('topLeftCell','A1')
        for child in list(view):
            if local(child.tag) in ('pane','selection'):
                view.remove(child)
        if current:
            header = existing_header(sheet,GRID[sheet.name][0]) or 2
            ET.SubElement(view,M+'pane',xSplit=str(current-1),ySplit=str(header),
                          topLeftCell=address(header+1,current),activePane='bottomRight',state='frozen')
            ET.SubElement(view,M+'selection',pane='bottomRight',activeCell=address(header+1,current),sqref=address(header+1,current))
        else:
            refviews = ref.root.find(M+'sheetViews')
            if refviews is not None and len(refviews):
                for child in refviews[0]:
                    if local(child.tag) in ('pane','selection'):
                        view.append(deepcopy(child))


def fitted_height(texts, widths, base=15):
    lines = 1
    for value,width in zip(texts,widths):
        count = 0
        for line in text(value).split('\n'):
            units = sum(2 if ord(ch)>255 else 1 for ch in line)
            # Excel widths use the normal font's digits, whereas report cells
            # often use 12pt type. Leave room for wider glyphs and word wraps.
            capacity=max(5,int((float(width)-2)*.86))
            count += max(1,(units+capacity-1)//capacity)
        lines = max(lines,count)
    height = max(float(base),lines*16.5+4)
    if height>409:
        raise ValueError('A cell exceeds Excel row height; move excessive qualification text to a separate business note before formatting')
    return height


def unmerge_written_cells(sheet, rows, columns, *, inherit=False):
    """Do not leave new per-row values hidden under old merged displays."""
    rows,columns=set(rows),set(columns)
    merges=sheet.root.find(M+'mergeCells')
    if merges is None:
        return
    for node in list(merges):
        ends=node.get('ref').split(':')
        (r1,c1),(r2,c2)=pos(ends[0]),pos(ends[-1])
        if not (any(r1<=r<=r2 for r in rows) and any(c1<=c<=c2 for c in columns)):
            continue
        value=sheet.value(r1,c1)
        if inherit and value is not None:
            style=sheet.cell(r1,c1).get('s','0')
            for r in range(r1,r2+1):
                for c in range(c1,c2+1):
                    if sheet.value(r,c) is None:
                        sheet.put(r,c,value,style)
        merges.remove(node)
    merges.set('count',str(len(merges)))


def quote_notes(quotes):
    """State shared restrictions once, and qualify only differing clauses."""
    if not quotes:
        return ''
    clauses=[compact_notes(q.notes).split('；') for q in quotes]
    common=[x for x in clauses[0] if x and all(x in parts for parts in clauses)]
    show_audience = show_audience_labels(quotes)
    if not show_audience:
        # A lone restricted customer class need not be repeated beside every
        # quote, but its eligibility restriction still belongs in the notes.
        audience = audience_label(quotes[0].audience)
        if re.search(r'premier|preferred|priority|elite|private|corporate|citigold',audience,re.I):
            restriction=f'仅限 {audience} 客户'
            if restriction not in common:
                common.insert(0,restriction)
    specific=defaultdict(list)
    for q,parts in zip(quotes,clauses):
        note='；'.join(x for x in parts if x and x not in common)
        if note:
            audience=audience_label(q.audience) if show_audience else ''
            label=' '.join(filter(None,(f'{q.rate*100:.2f}%',audience)))
            if label not in specific[note]:
                specific[note].append(label)
    return '\n'.join(['；'.join(common)]*(bool(common))+[' / '.join(labels)+'：'+note for note,labels in specific.items()])


def finish_history_headers(sheet, ref, styles, bankcol, current, delta):
    contexts,headers,sections=row_contexts(sheet,bankcol)
    last=delta+(2 if sheet.name=='SGD促销' else 1)
    # A generated provenance sentence may overlap the reference title cell.
    titles=[(c,ref.value(1,c)) for (r,c) in ref.cells if r==1 and isinstance(ref.value(r,c),str) and '利率' in ref.value(r,c)]
    if titles:
        c,title=titles[0]
        for (r,cc) in list(sheet.cells):
            if r==1:
                sheet.put(r,cc,None)
        sheet.put(1,bankcol,title,source_style(styles,ref,1,c))
        sections=[1]+sections
    sections+= [r for r in range(2,sheet.maxrow+1) if r not in contexts and r not in headers and
                 any('促销利率' in text(sheet.value(r,c)) for c in range(1,bankcol+1))]
    for r in sorted(set(sections)):
        label=sheet.value(r,bankcol) or next((sheet.value(r,c) for c in range(1,bankcol+1) if sheet.value(r,c)),None)
        if not label:
            continue
        unmerge_written_cells(sheet,[r],range(bankcol,last+1))
        for c in range(bankcol+1,last+1):
            sheet.put(r,c,None)
        sheet.put(r,bankcol,label)
        merges=sheet.root.find(M+'mergeCells')
        if merges is None:
            merges=ET.Element(M+'mergeCells');sheet.insert_element(merges)
        ET.SubElement(merges,M+'mergeCell',ref=f'{address(r,bankcol)}:{address(r,last)}')
        merges.set('count',str(len(merges)))
        sheet.height(r,25 if r==1 else 22)
    for r in headers:
        sheet.height(r,21)
        if sheet.name=='SGD促销':
            headerrow=r+1
            unmerge_written_cells(sheet,[headerrow],[current,current+1,delta,delta+1,delta+2])
            headstyle=styles.customize(source_style(styles,ref,4,2),wrap=True,alignment='center')
            for c,label in [(current,'Personal 最高值'),(current+1,'原表最高值'),
                            (delta,'当日最高报价变动'),(delta+1,'金额门槛'),(delta+2,'备注')]:
                sheet.put(headerrow,c,label,headstyle)
            sheet.height(headerrow,38)


def row_groups(contexts):
    result = defaultdict(list)
    for row,key in contexts.items():
        result[key].append(row)
    return result


def collapse_rows(sheet, groups, audit):
    """Caller has already resolved data conflicts into each group's first row."""
    anchors = {r:rows[0] for rows in groups for r in rows}
    deleted = {r for rows in groups for r in rows[1:]}
    rowmap, number = {}, 0
    for r in range(1,sheet.maxrow+1):
        if r not in deleted:
            number += 1
            rowmap[r] = number
    for r in deleted:
        rowmap[r] = rowmap[anchors[r]]
    # Drop removed-row cell records rather than leaving obsolete lower borders.
    sheet.cells = {(r,c):e for (r,c),e in sheet.cells.items() if r not in deleted}
    sheet.remap(rowmap=rowmap)
    audit['rows_removed'][sheet.name] = len(deleted)
    return rowmap


def apply_delta(sheet, rows, current, delta, styles, base_style):
    sheet.remove_icons()
    refs=[]
    style = styles.customize(base_style,format_code='0.00%;-0.00%;0.00%',wrap=False,alignment='right')
    for r in rows:
        now,prior = sheet.value(r,current),sheet.value(r,current+1)
        value = (now-prior).quantize(Decimal('0.0000000001')) if numeric(now) and numeric(prior) else '-'
        a,b=address(r,current),address(r,current+1)
        formula=f'IF(COUNT({a},{b})=2,ROUND({a}-{b},10),"-")'
        sheet.put(r,delta,value,style,formula)
        refs.append(address(r,delta))
    sheet.delta_rule(refs)


def append_business_notes(book, bank, lines, style):
    """Keep long quote groups readable within Excel's row-height limit."""
    name='业务说明'
    sheet=book.sheets[name] if name in book.sheets else book.add_sheet(name)
    sheet.put(1,1,'银行及期限',style)
    sheet.put(1,2,'完整报价及资格说明',style)
    for line in lines:
        for start in range(0,len(line),400):
            r=sheet.maxrow+1
            chunk=line[start:start+400]
            sheet.put(r,1,bank,style)
            sheet.put(r,2,chunk,style)
            sheet.height(r,fitted_height([chunk],[110],20))
    set_columns(sheet,{1:dict(width='22',customWidth='1'),2:dict(width='110',customWidth='1')})


def sgd_history_is_personal(label):
    value=text(label)
    if not caption_is_personal(value):
        return False
    # An unlabelled or unknown historic tier cannot be assumed to be retail.
    return bool(re.search(r'\b(?:personal|all)\b|普通个人|所有客户|所有客群',value,re.I))


def format_sgd(sheet, ref, styles, quotes, audit):
    contexts, headers, _ = row_contexts(sheet,1)
    existing={(bank,tenor) for _,tenor,bank in contexts.values()}
    missing=set(quotes)-existing
    if missing:
        from .quote_layout import tenor_order
        for tenor in sorted({t for _,t in missing},key=tenor_order):
            start=sheet.maxrow+3
            sheet.put(start,1,tenor[:-1]+(' Months' if tenor.endswith('M') else ' Days'))
            sheet.put(start+1,1,'日期');sheet.put(start+1,3,serial(parse_date(sheet.value(headers[0],3))))
            audit['comparison_columns'][sheet.name].append(dict(input_header_row=start+1,current_column='C',previous_column='D',current_date=parse_date(sheet.value(headers[0],3)).isoformat(),previous_date=None))
            sheet.put(start+2,2,'全部公开报价')
            for row,bank in enumerate(sorted(b for b,t in missing if t==tenor),start+3):sheet.put(row,1,bank)
        contexts,headers,_=row_contexts(sheet,1)
    complete=bool(workbook_policy().get('complete_public_quotes'))
    if complete:
        audit['full_sgd_public_rows']=[]
    groups = row_groups(contexts)
    delta = first_delta(sheet)
    refcontexts,refheaders,_ = row_contexts(ref,1)
    refgroups = row_groups(refcontexts)
    rate_style = styles.customize(source_style(styles,ref,refheaders[0]+2,3),format_code='0.00%',wrap=False,alignment='right')
    note_style = styles.customize(source_style(styles,ref,refheaders[0]+2,2),format_code='General',wrap=True,alignment='left')
    winners = {}
    for (currency,tenor,bank),rows in groups.items():
        first=rows[0]
        available=quotes.get((bank,tenor),[])
        chosen=winner(available)
        winners[bank,tenor]=chosen
        # Retain only comparable retail values in the consolidated history.
        # Record excluded or conflicting old observations with their source rows.
        for c in range(4,delta):
            observed=[(r,sheet.value(r,c)) for r in rows if numeric(sheet.value(r,c))]
            if not observed:
                continue
            eligible=[v for r,v in observed if not complete or sgd_history_is_personal(sheet.value(r,2))]
            selected=max(eligible) if eligible else '-'
            if len(observed)>1 or len(eligible)!=len(observed):
                audit['historical_consolidations'].append(dict(sheet=sheet.name,bank=bank,tenor=tenor,
                    column=col(c),observations=[dict(row=r,rate=str(v),label=text(sheet.value(r,2))) for r,v in observed],
                    selected=str(selected),basis='eligible Personal history only' if complete else 'legacy maximum'))
            sheet.put(first,c,selected)
        sheet.put(first,1,text(sheet.value(first,1)),source_style(styles,ref,refgroups.get((currency,tenor,bank),[refheaders[0]+2])[0],1))
        captions=public_quote_captions(available)
        quote_text='\n'.join(captions) if captions else '-'
        note=chosen.notes if complete and chosen else '' if complete else quote_notes(available)
        refrow=refgroups.get((currency,tenor,bank),[refheaders[0]+2])[0]
        base=float(ref.rows.get(refrow,{}).get('ht','15'))
        detail_key=bank+' / '+tenor
        overflow=False
        try:
            height=fitted_height([quote_text,note],[50,70],base)
        except ValueError:
            append_business_notes(sheet.book,detail_key,captions+[note],note_style)
            quote_text='完整报价见“业务说明”（'+detail_key+'）'
            note='资格说明见“业务说明”' if note else ''
            height=fitted_height([quote_text,note],[50,70],base)
            overflow=True
        sheet.put(first,2,quote_text,note_style)
        sheet.put(first,3,chosen.rate if chosen else '-',rate_style)
        previous=sheet.value(first,4)
        sheet.put(first,4,previous,rate_style)
        if chosen and not numeric(previous):
            audit['missing_previous'].append({'sheet':sheet.name,'row':first,'bank':bank,'tenor':tenor})
        sheet.put(first,delta+1,chosen.amount if chosen else '-',note_style)
        sheet.put(first,delta+2,note,note_style)
        for r in rows[1:]:
            for (rr,c) in list(sheet.cells):
                if rr==r:
                    sheet.put(r,c,None)
        sheet.height(first,height)
        if complete:
            audit['full_sgd_public_rows'].extend(dict(bank=bank,tenor=tenor,row=first,
                caption=line,display=quote_text,overflow=overflow) for line in captions)
        if chosen:
            audit['sgd_winners'].append({'bank':bank,'tenor':tenor,'actual_tenor':chosen.actual,'rate':str(chosen.rate),
                'audience':chosen.audience,'source_sheet':chosen.source,'source_row':chosen.row})
    for r in headers:
        sheet.put(r,3,sheet.value(r,3),source_style(styles,ref,refheaders[0],3))
        sheet.put(r,4,sheet.value(r,4),source_style(styles,ref,refheaders[0],3))
        sheet.put(r+1,delta,'当日最高报价变动')
        sheet.put(r+1,delta+1,'金额门槛')
        sheet.put(r+1,delta+2,'备注')
    for r in range(1,sheet.maxrow+1):
        if r not in contexts:
            sheet.height(r,21 if re.fullmatch(r'\d+ Months?',text(sheet.value(r,1))) or r==1 else 16.5)
    sheet.put(1,1,ref.value(1,1),source_style(styles,ref,1,1))
    mapping=collapse_rows(sheet,list(groups.values()),audit)
    current_rows=[mapping[v[0]] for v in groups.values()]
    for item in audit.get('full_sgd_public_rows',[]):
        item['row']=mapping[item['row']]
    unmerge_written_cells(sheet,current_rows,[2,3,4,delta,delta+1,delta+2])
    apply_delta(sheet,current_rows,3,delta,styles,rate_style)
    copy_layout(sheet,ref,styles,3,delta)
    widths=width_map(sheet)
    for column,width in ((2,50),(delta+1,28),(delta+2,70)):
        widths[column]=dict(widths.get(column,{}),width=str(width),hidden='0',customWidth='1')
    set_columns(sheet,widths)
    finish_history_headers(sheet,ref,styles,1,3,delta)
    return winners


def amount_key(label,bank):
    cleaned=clean_amount(label)
    unit=re.search(r'USD|SGD|CNY|CNH|RMB|AUD|NZD|EUR|GBP|CAD|HKD',cleaned)
    audience='premier' if re.search('premier',text(label),re.I) else 'preferred' if re.search('preferred',text(label),re.I) else 'personal'
    currency=unit[0].replace('CNH','CNY').replace('RMB','CNY') if unit else 'USD' if 'US$' in cleaned else 'SGD' if 'S$' in cleaned else 'CNY' if 'R$' in cleaned else ''
    return (currency,amount_identity(cleaned),audience)


def amount_interval(label):
    value=clean_amount(label)
    hits=list(re.finditer(r'(\d[\d,]*(?:\.\d+)?)\s*(mil|million|[kKmM]|万)?',value))
    if not hits or len(hits)>2:
        return None
    def number(hit):
        scale={'k':1000,'m':1000000,'mil':1000000,'million':1000000,'万':10000}.get((hit[2] or '').lower(),1)
        return Decimal(hit[1].replace(',',''))*scale
    nums=[number(h) for h in hits]
    before=value[:hits[0].start()]
    lower_inclusive=not bool(re.search(r'(?<![<>=])>(?!=)',before))
    if len(hits)==2:
        between=value[hits[0].end():hits[1].start()]
        upper_inclusive='≤' in between or '<=' in between or str(nums[1]).endswith('999')
        return nums[0],nums[1],lower_inclusive,upper_inclusive
    if re.search(r'<|≤|以下|below|up to',value,re.I):
        return None,nums[0],False,bool(re.search(r'≤|<=|以下|up to',value,re.I))
    return nums[0],None,lower_inclusive,False


def product_signature(label):
    """Full source captions carry product/customer/qualification identity.

    Legacy amount-only labels have no such identity. Do not erase product
    distinctions merely because two quoted rates and deposit amounts agree.
    """
    value=text(label)
    if '\n' in value and '/' in value:
        return caption_identity(eligibility_from_amount(value))
    return ''


def interval_contains(parent,child):
    if parent is None or child is None:
        return False
    lo,hi,li,ui=parent
    clo,chi,cli,cui=child
    lower=lo is None or (clo is not None and (lo<clo or (lo==clo and (li or not cli))))
    upper=hi is None or (chi is not None and (hi>chi or (hi==chi and (ui or not cui))))
    return lower and upper


def previous_rows(sheet,bankcol,current):
    contexts,_,_=row_contexts(sheet,bankcol)
    result=defaultdict(list)
    for r,context in contexts.items():
        value=sheet.value(r,current)
        if numeric(value):
            label=sheet.value(r,current-1)
            result[context].append((label,value))
    return result


def match_previous(label,bank,rows,currency=None):
    target=amount_key(label,bank)
    def same_unit(candidate):
        a,b=target[0] or currency,amount_key(candidate,bank)[0] or currency
        return not a or not b or a==b
    def compatible_product(candidate):
        a,b=product_signature(label),product_signature(candidate)
        return not a or not b or a==b
    # Prefer an exact complete caption when same-amount products coexist.
    found=[value for candidate,value in rows if caption_identity(candidate)==caption_identity(label)]
    if not found:
        found=[value for candidate,value in rows if amount_key(candidate,bank)[2]==target[2]
               and same_unit(candidate) and compatible_product(candidate)
               and amount_interval(candidate)==amount_interval(label) and amount_interval(label) is not None]
    if not found:
        # A newly split band may inherit a previous broader band only if that
        # older quote covers its entire interval, with compatible customer/unit.
        found=[value for candidate,value in rows if amount_key(candidate,bank)[2]==target[2]
               and same_unit(candidate) and compatible_product(candidate)
               and interval_contains(amount_interval(candidate),amount_interval(label))]
    if not found:
        # Conversely, a newly combined amount interval may be compared with
        # several prior intervals only when the entire range had ONE rate.
        # Require explicit full captions here: legacy amount-only rows do not
        # establish that separate tiers had the same product and eligibility.
        def full_identity(value):
            lines=text(value).splitlines()
            signature=product_signature(value)
            if len(lines)<2 or '/' not in lines[0] or not signature:return None
            head=caption_identity(lines[0])
            equivalent=bool(re.search(r'等值|equivalent',clean_amount(value),re.I))
            return head,signature,equivalent
        identity=full_identity(label)
        if identity is not None:
            tiers=[(amount_interval(candidate),value) for candidate,value in rows
                   if numeric(value) and same_unit(candidate) and full_identity(candidate)==identity]
            prior=equal_rate_coverage(amount_interval(label),tiers)
            if prior is not None:return prior
    return found[0] if found and len(set(found))==1 else '-'


def restore_fx_history_labels(book, input_path, audit):
    """Recover legacy placeholders only from the run's hash-verified FX input."""
    from .common import load
    from .table_validation import sha
    sidecar=input_path.parent/'table-validation-plan.json'
    if not sidecar.is_file():
        return
    plan=load(sidecar)
    if plan.get('kind')!='fx-promo' or Path(plan['report_output']).resolve()!=input_path:
        return
    source=Path(plan['report_template'])
    if not source.is_file() or sha(source)!=plan['report_sha256']:
        audit['fx_history_label_recovery']='predecessor missing or hash differs'
        return
    original=Book(source)
    for history in plan['histories']:
        name=history['sheet']
        if name not in ('CNY促销','USD促销'):
            continue
        sheet,old=book.sheets[name],original.sheets[name]
        bankcol,current=GRID[name]
        ctx,headers,_=row_contexts(sheet,bankcol)
        old_ctx,old_headers,_=row_contexts(old,bankcol)
        for item in history['existing']:
            row,colno=pos(item['address'])
            prev=item['old_row']
            label=old.value(prev,current-1)
            if (colno!=current or row not in ctx or ctx[row]!=old_ctx.get(prev)
                    or not text(sheet.value(row,current-1)).startswith('本期未列此旧产品或档位')
                    or numeric(sheet.value(row,current)) or not label):
                continue
            h=max((h for h in headers if h<row),default=0)
            oh=max((h for h in old_headers if h<prev),default=0)
            source_col=current if history['insert_date'] else current+1
            date=parse_date(sheet.value(h,current+1)) if h else None
            if date is None or not oh or date!=parse_date(old.value(oh,source_col)):
                continue
            before,after=old.value(prev,source_col),sheet.value(row,current+1)
            if numeric(before) and (not numeric(after) or abs(before-after)>Decimal('1e-12')):
                continue
            sheet.put(row,current-1,label)
            audit.setdefault('restored_fx_history_labels',[]).append(dict(sheet=name,row=row,
                source=str(source),source_row=prev,label=text(label)))


def history_records(sheet, rows, current, delta):
    return [dict(row=r,label=text(sheet.value(r,current-1)),rates={
        c:sheet.value(r,c) for c in range(current,delta) if numeric(sheet.value(r,c))}) for r in rows]


def history_snapshot(records):
    return [dict(row=r['row'],label=r['label'],rates={col(c):str(v) for c,v in r['rates'].items()}) for r in records]


def usd_board_groups(records, current, bank, audit):
    from .report_bands import explicit_band,legacy_usd_range,whole_dollar_key,combine_rates
    full=defaultdict(list)
    legacy=defaultdict(list)
    for record in records:
        label=record['label']
        cleaned=clean_amount(label)
        band=explicit_band(cleaned,'USD')
        qualification=eligibility_from_amount(label)
        if band and band[0]=='USD':
            if cleaned==label and not qualification and current not in record['rates']:
                legacy[band].append(record)
            else:
                full[band,caption_identity(qualification)].append(record)
        elif current not in record['rates']:
            shorthand=legacy_usd_range(label)
            if shorthand:
                legacy['USD',False,shorthand].append(record)
    replaced={}
    removed=set()
    for (band,qualifier),items in full.items():
        if not any(current in r['rates'] for r in items):
            continue
        # Legacy labels do not establish customer/offer identity. Adopt only
        # an unqualified ordinary tier, and only when its successor is unique.
        eligible=[]
        sample=next(r for r in items if current in r['rates'])
        ordinary=(not qualifier and caption_is_personal(sample['label'])
                  and (sample['label']==clean_amount(sample['label'])
                       or sample['label'].startswith('定期存款挂牌；')))
        if ordinary:
            for oldband,oldrows in legacy.items():
                # Whole-dollar shorthand is an established UOB grid convention.
                same=oldband==band or (bank=='UOB' and oldband[:2]==band[:2]
                    and whole_dollar_key(oldband[2])==whole_dollar_key(band[2])
                    and all(legacy_usd_range(r['label']) for r in oldrows))
                competing=[key for key in full if key[0]==band or
                    (bank=='UOB' and key[0][:2]==oldband[:2]
                     and whole_dollar_key(key[0][2])==whole_dollar_key(oldband[2]))]
                if same and len(competing)==1:
                    eligible.extend(oldrows)
        merged=items+eligible
        if len(merged)<2:
            continue
        rates=combine_rates(merged)
        if rates is None:
            audit['retained_amount_conflicts'].append(dict(sheet='USD挂牌',bank=bank,observations=history_snapshot(merged)))
            continue
        anchor=min(r['row'] for r in merged)
        replaced[anchor]=dict(sample,rates=rates)
        removed.update(r['row'] for r in merged)
        audit.setdefault('aligned_history',[]).append(dict(sheet='USD挂牌',bank=bank,
            observations=history_snapshot(merged),bands=[sample['label']]))
    return [replaced.get(r['row'],r) for r in records if r['row'] not in removed or r['row'] in replaced]


def cny_promo_groups(records, current, bank, audit):
    from .report_bands import explicit_band,partition,band_text,combine_rates
    groups=defaultdict(list)
    for record in records:
        label=record['label']
        lines=label.splitlines()
        cleaned=clean_amount(label)
        interval=explicit_band(cleaned,'CNY')
        if not interval or len(lines)<3 or '/' not in lines[0] or not record['rates']:
            continue
        # The same product, audience, channel, currency, equivalence and
        # eligibility are required; an equal rate does not establish identity.
        key=(interval[:2],caption_identity(lines[0]),product_signature(label))
        groups[key].append(dict(record,band=interval[2]))
    replaced={}
    removed=set()
    for (unit,_,_),items in groups.items():
        if len(items)<2 or not any(current in r['rates'] for r in items) or not any(current+1 in r['rates'] for r in items):
            continue
        rebuilt=[]
        for band,sources in partition(items):
            rates=combine_rates(sources)
            if rates is None:
                rebuilt=[]
                audit['retained_amount_conflicts'].append(dict(sheet='CNY促销',bank=bank,observations=history_snapshot(items)))
                break
            preferred=next((r for r in sources if current in r['rates']),sources[0])
            label=band_text(*unit,band)
            original=clean_amount(preferred['label'])
            # Keep the current offer's original extent in notes when split.
            note=('本期官网金额：'+original) if current in rates and original!=label else ''
            rebuilt.append(dict(preferred,label=label,rates=rates,extra_note=note,retail=caption_is_personal(preferred['label']),
                eligibility=eligibility_from_amount(preferred['label'])))
        if not rebuilt:
            continue
        anchor=min(r['row'] for r in items)
        replaced[anchor]=rebuilt
        removed.update(r['row'] for r in items)
        audit.setdefault('aligned_history',[]).append(dict(sheet='CNY促销',bank=bank,
            observations=history_snapshot(items),bands=[r['label'] for r in rebuilt]))
    result=[]
    for record in records:
        if record['row'] in replaced:
            result.extend(replaced[record['row']])
        elif record['row'] not in removed and (record['rates'] or record['label'] not in ('','-','本期未列此旧产品或档位')):
            result.append(record)
    return result or records[:1]


def hsbc_personal_groups(records, current, audit):
    personal=[r for r in records if caption_is_personal(r['label']) and r['rates']]
    archived=[r for r in records if r not in personal]
    if archived:
        audit.setdefault('hsbc_usd_archived_rows',[]).extend(history_snapshot(archived))
    if not personal:
        return [dict(records[0],label='-',rates={})]
    from .report_bands import combine_rates
    grouped=defaultdict(list)
    for r in personal:
        grouped[caption_identity(r['label']) or str(r['row'])].append(r)
    result=[]
    for rows in grouped.values():
        rates=combine_rates(rows)
        if rates is None:
            result.extend(rows)
        else:
            result.append(dict(rows[0],rates=rates))
    return result


def reconcile_history(sheet, bankcol, current, delta, contexts, audit):
    if sheet.name not in ('USD挂牌','USD促销','CNY促销'):
        return
    from itertools import groupby
    changes={}
    for (_,context),chunk in groupby(enumerate(contexts),key=lambda pair:(pair[1]-pair[0],contexts[pair[1]])):
        rows=[r for _,r in chunk]
        bank=context[2]
        records=history_records(sheet,rows,current,delta)
        if sheet.name=='USD挂牌':
            result=usd_board_groups(records,current,bank,audit)
        elif sheet.name=='CNY促销':
            result=cny_promo_groups(records,current,bank,audit)
        elif bank=='HSBC':
            result=hsbc_personal_groups(records,current,audit)
        else:
            continue
        if result!=records:
            changes[rows[0]]=(rows,result)
    if not changes:
        return {}
    retail={}
    affected={r for rows,_ in changes.values() for r in rows}
    unmerge_written_cells(sheet,affected,[delta+1],inherit=True)
    unmerge_written_cells(sheet,affected,range(bankcol,sheet.maxcol+1))
    cells=defaultdict(dict)
    for (r,c),cell in sheet.cells.items():
        if r in affected:
            cells[r][c]=cell
    sheet.cells={key:cell for key,cell in sheet.cells.items() if key[0] not in affected}
    old_max=sheet.maxrow
    mapping={}
    next_row=1
    targets={}
    for r in range(1,old_max+1):
        if r in changes:
            rows,result=changes[r]
            targets[r]=next_row
            for i,old in enumerate(rows):
                mapping[old]=next_row+min(i,len(result)-1)
            next_row+=len(result)
        elif r not in affected:
            mapping[r]=next_row
            next_row+=1
    sheet.remap(rowmap=mapping)
    for start,(rows,result) in changes.items():
        target=targets[start]
        bank=contexts[start][2]
        for i,record in enumerate(result):
            r=target+i
            for c,cell in cells[record['row']].items():
                clone=deepcopy(cell)
                clone.set('r',address(r,c))
                sheet.cells[r,c]=clone
            sheet.put(r,bankcol,bank if not i else None)
            sheet.put(r,current-1,record['label'])
            for c in range(current,delta):
                sheet.put(r,c,record['rates'].get(c,'-' if c in (current,current+1) else None))
            if 'retail' in record:
                retail[r]=record['retail']
            if 'eligibility' in record:
                sheet.put(r,delta+1,compact_notes('；'.join(
                    [text(sheet.value(r,delta+1)),record['eligibility'],record['extra_note']])))
        if len(result)>1:
            merges=sheet.root.find(M+'mergeCells')
            if merges is None:
                merges=ET.Element(M+'mergeCells')
                sheet.insert_element(merges)
            ET.SubElement(merges,M+'mergeCell',ref=address(target,bankcol)+':'+address(target+len(result)-1,bankcol))
            merges.set('count',str(len(merges)))
    audit.setdefault('history_row_changes',{})[sheet.name]=dict(
        before=len(affected),after=sum(len(result) for _,result in changes.values()))
    return retail


def format_history(sheet,ref,styles,audit):
    bankcol,current=GRID[sheet.name]
    contexts,headers,_=row_contexts(sheet,bankcol)
    delta=first_delta(sheet)
    retail=reconcile_history(sheet,bankcol,current,delta,contexts,audit) or {}
    contexts,headers,_=row_contexts(sheet,bankcol)
    eligible={r:retail.get(r,caption_is_personal(sheet.value(r,current-1))) for r in contexts}
    note_col=delta+1
    unmerge_written_cells(sheet,contexts,[note_col],inherit=True)
    refctx,refheaders,_=row_contexts(ref,bankcol)
    refgroups=row_groups(refctx)
    base_row=next(iter(refctx))
    amount_style=styles.customize(source_style(styles,ref,base_row,current-1),format_code='General',wrap=True,alignment='left')
    notes_style=styles.customize(amount_style,wrap=True)
    collapsed=[]
    by_identity=defaultdict(list)
    for r,context in contexts.items():
        bank=context[2]
        original=sheet.value(r,current-1)
        cleaned=clean_amount(original)
        priorvalue=sheet.value(r,current+1)
        rowref=refgroups.get(context,[base_row])[0]
        rate_style=styles.customize(source_style(styles,ref,rowref,current),wrap=False,alignment='right')
        sheet.put(r,current,sheet.value(r,current),rate_style)
        sheet.put(r,current+1,priorvalue,rate_style)
        sheet.put(r,current-1,cleaned,amount_style)
        note=eligibility_from_amount(original) if text(original)!=cleaned else ''
        existing=compact_notes(sheet.value(r,note_col))
        note=compact_notes(note+'；'+existing)
        # Remove the repeated pure amount segment; eligibility stays in notes.
        note='；'.join(p for p in note.split('；') if p and p!=cleaned)
        sheet.put(r,note_col,note,notes_style)
        if sheet.value(r,bankcol) is not None:
            sheet.put(r,bankcol,sheet.value(r,bankcol),source_style(styles,ref,rowref,bankcol))
        base=float(ref.rows.get(rowref,{}).get('ht','15'))
        amount_width=float(width_map(ref).get(current-1,{}).get('width','30'))
        sheet.height(r,fitted_height([cleaned,note],[amount_width,70],base))
        identity=(context,amount_key(original,bank),product_signature(original))
        if identity[1][1] is not None:
            by_identity[identity].append(r)
        if numeric(sheet.value(r,current)) and not numeric(priorvalue):
            audit['missing_previous'].append({'sheet':sheet.name,'row':r,'bank':bank,'amount':cleaned,'tenor':context[1]})
    for identity,rows in ([] if workbook_policy().get('complete_public_quotes') else by_identity.items()):
        if len(rows)<2:
            continue
        first=rows[0]
        # Never discard differing data from two amount/product histories.
        conflict=any(len({sheet.value(r,c) for r in rows if numeric(sheet.value(r,c))})>1 for c in range(current,delta))
        if conflict:
            audit['retained_amount_conflicts'].append({'sheet':sheet.name,'rows':rows,'amount':sheet.value(first,current-1)})
            continue
        for c in range(current,delta):
            values=[sheet.value(r,c) for r in rows if numeric(sheet.value(r,c))]
            if values:
                sheet.put(first,c,values[0])
        notes=list(dict.fromkeys(text(sheet.value(r,note_col)) for r in rows if sheet.value(r,note_col)))
        sheet.put(first,note_col,'；'.join(notes),notes_style)
        sheet.height(first,fitted_height([sheet.value(first,current-1),sheet.value(first,note_col)],
                                        [amount_width,70],float(sheet.rows[first].get('ht','15'))))
        for r in rows[1:]:
            for (rr,c) in list(sheet.cells):
                if rr==r:
                    sheet.put(r,c,None)
        collapsed.append(rows)
    for r in headers:
        sheet.put(r,current+1,sheet.value(r,current+1),source_style(styles,ref,refheaders[0],current))
        sheet.put(r,note_col,'备注',source_style(styles,ref,refheaders[0],current-1))
    # Empty title/spacer rows must not acquire a dummy current rate.
    for r in range(1,sheet.maxrow+1):
        if r not in contexts and r not in headers and sheet.value(r,current)=='-':
            sheet.put(r,current,None)
        if r not in contexts:
            sheet.height(r,24.75 if r==1 else 15)
    mapping=collapse_rows(sheet,collapsed,audit)
    current_rows=sorted({mapping[r] for r in contexts})
    audit.setdefault('retail_history_rows',{})[sheet.name]={
        mapping[r]:eligible[r] for r in reversed(contexts)}
    unmerge_written_cells(sheet,current_rows,[current-1,current,current+1,delta,note_col])
    apply_delta(sheet,current_rows,current,delta,styles,source_style(styles,ref,base_row,current))
    copy_layout(sheet,ref,styles,current,delta)
    if ref.value(1,bankcol):
        sheet.put(1,bankcol,ref.value(1,bankcol),source_style(styles,ref,1,bankcol))
    widths=width_map(sheet)
    widths[note_col]=dict(widths.get(note_col,{}),width='70',hidden='0',customWidth='1')
    set_columns(sheet,widths)
    finish_history_headers(sheet,ref,styles,bankcol,current,delta)


def format_matrix(sheet,ref,styles,audit):
    bankcol=1 if sheet.name=='SGD挂牌' else 2
    amountcol=bankcol+1
    note_cols=[c for (r,c) in sheet.cells if r==2 and text(sheet.value(r,c)) in ('金额要求','备注','其他','其他要求','Others')]
    if len(note_cols)!=1:
        raise ValueError(f'{sheet.name}: expected one qualification column, found {note_cols}')
    note_col=note_cols[0]
    unmerge_written_cells(sheet,range(3,sheet.maxrow+1),[note_col],inherit=True)
    base=3
    amountstyle=styles.customize(source_style(styles,ref,base,amountcol),format_code='General',wrap=True,alignment='left')
    notestyle=styles.customize(amountstyle,wrap=True)
    bank=None
    for r in range(3,sheet.maxrow+1):
        if sheet.value(r,bankcol):
            bank=text(sheet.value(r,bankcol))
        original=sheet.value(r,amountcol)
        if original is None:
            continue
        amount=clean_amount(original)
        note=compact_notes(sheet.value(r,note_col))
        qualifications=eligibility_from_amount(original)
        if qualifications != amount:
            note=compact_notes(qualifications+'；'+note)
        sheet.put(r,amountcol,amount,amountstyle)
        sheet.put(r,note_col,note,notestyle)
        if sheet.value(r,bankcol):
            sheet.put(r,bankcol,sheet.value(r,bankcol),source_style(styles,ref,base,bankcol))
        for c in range(amountcol+1,note_col):
            if sheet.value(r,c) is not None:
                sheet.put(r,c,sheet.value(r,c),source_style(styles,ref,base,c))
        height=27 if sheet.name=='SGD挂牌' else float(ref.rows.get(base,{}).get('ht','15.75'))
        amountwidth=float(width_map(ref).get(amountcol,{}).get('width','22'))
        sheet.height(r,fitted_height([amount,note],[amountwidth,70],height))
    sheet.put(1,bankcol,ref.value(1,bankcol),source_style(styles,ref,1,bankcol))
    sheet.put(2,note_col,'备注',source_style(styles,ref,2,amountcol))
    copy_layout(sheet,ref,styles)
    widths=width_map(sheet)
    widths[note_col]=dict(widths.get(note_col,{}),width='70',hidden='0',customWidth='1')
    for c in range(amountcol+1,note_col):
        widths[c]=dict(widths.get(c,{}),width=widths.get(c,{}).get('width','12'),hidden='0',customWidth='1')
        if float(widths[c].get('width','0'))<4:
            widths[c]['width']='12'
    for c in range(note_col+1,sheet.maxcol+1):
        if re.fullmatch(r'\d+[MD]',text(sheet.value(2,c))):
            widths[c]=dict(width='14',hidden='0',customWidth='1')
            for r in range(3,sheet.maxrow+1):
                if sheet.value(r,c) is not None:sheet.put(r,c,sheet.value(r,c),styles.customize(source_style(styles,ref,base,amountcol+1),format_code='0.00%'))
    set_columns(sheet,widths)
    # Ignore purely styled cells beyond the last actual business column.
    sheet.cells={key:element for key,element in sheet.cells.items() if key[1]<=note_col or sheet.value(*key) is not None}


def format_summary(sheet,ref,styles,winners,as_of,audit=None):
    for (r,c),cell in list(sheet.cells.items()):
        if (r,c) in ref.cells:
            cell.set('s',str(source_style(styles,ref,r,c)))
    for r,row in ref.rows.items():
        if 'ht' in row.attrib:
            sheet.height(r,float(row.get('ht')))
    # Locate the SGD summary table rather than assuming that it fills the page.
    start=next(r for (r,c) in sheet.cells if c==2 and sheet.value(r,c)=='Bank')
    sheet.put(start-1,2,ref.value(start-1,2),source_style(styles,ref,start-1,2))
    tenorcols={c:text(sheet.value(start,c)) for c in range(3,sheet.maxcol+1) if re.fullmatch(r'\d+[MD]',text(sheet.value(start,c)))}
    from .quote_layout import tenor_order
    for tenor in sorted({t for _,t in winners}-set(tenorcols.values()),key=tenor_order):
        c=max([13]+list(tenorcols))+1;tenorcols[c]=tenor
        sheet.put(start,c,tenor,source_style(styles,ref,start,3))
    overflow_notes=[]
    for r in range(start+1,sheet.maxrow+1):
        label=text(sheet.value(r,2))
        if label=='Bank' or ('利率' in label and 'SGD' not in label):
            break
        if not label:
            continue
        bank=bank_name(label)
        notes=[]
        for c,tenor in tenorcols.items():
            q=winners.get((bank,tenor))
            sheet.put(r,c,q.rate if q else '-')
            if q and q.actual!=tenor:
                notes.append(f'{tenor}列实际{q.actual}')
        # Rebuild source-dependent customer descriptions; a Preferred spread
        # from the old summary would contradict its new Personal-only values.
        chosen=[q for (b,t),q in winners.items() if b==bank and q]
        amounts=list(dict.fromkeys(q.amount for q in chosen))
        amount_style=styles.customize(int(sheet.cell(r,10).get('s','0')),format_code='General',wrap=True,alignment='left')
        amount_text='\n'.join(amounts) if amounts else '-'
        sheet.put(r,10,amount_text,amount_style)
        note=compact_notes('；'.join(q.notes for q in chosen if q.notes))
        sheet.put(r,11,note,styles.customize(int(sheet.cell(r,11).get('s','0')),format_code='General',wrap=True,alignment='left'))
        widths=width_map(ref)
        base_height=float(ref.rows.get(r,{}).get('ht','23.25'))
        height_widths=[float(widths.get(c,{}).get('width','30')) for c in (10,11)]
        try:
            height=fitted_height([amount_text,note],height_widths,base_height)
        except ValueError:
            overflow_notes.append({'bank':bank,'note':note})
            note=f'部分资格及业务说明见“业务说明”工作表（{bank}）'
            sheet.put(r,11,note,styles.customize(int(sheet.cell(r,11).get('s','0')),format_code='General',wrap=True,alignment='left'))
            height=fitted_height([amount_text,note],height_widths,base_height)
        sheet.height(r,height)
    sheet.put(2,6,serial(as_of))
    copy_layout(sheet,ref,styles)
    widths=width_map(sheet)
    for c in tenorcols:
        if c>=14:widths[c]=dict(width='14',hidden='0',customWidth='1')
    set_columns(sheet,widths)
    if overflow_notes:
        style=styles.customize(int(sheet.cell(start+1,11).get('s','0')),wrap=True,alignment='left')
        for item in overflow_notes:
            append_business_notes(sheet.book,item['bank'],item['note'].splitlines(),style)
        if audit is not None:
            audit['business_notes_moved']=[{'bank':x['bank'],'characters':len(x['note'])} for x in overflow_notes]


def format_premier(sheet,ref,styles):
    """Apply reference styles by column role, even after new tenors are added."""
    headers=sorted(r for r,c in sheet.cells if c==3 and text(sheet.value(r,c))=='Minimum Amount')
    ref_header=next(r for r,c in ref.cells if c==3 and text(ref.value(r,c))=='Minimum Amount')
    ref_note=next(c for r,c in ref.cells if r==ref_header and text(ref.value(r,c))=='其他')
    ref_columns={text(ref.value(ref_header,c)):c for r,c in ref.cells if r==ref_header}
    reference=width_map(ref)
    widths=width_map(sheet)
    live_rate_columns=set()
    for index,header in enumerate(headers):
        note_col=next((c for r,c in sheet.cells if r==header and text(sheet.value(r,c))=='其他'),None)
        if note_col is None:raise ValueError(f'Premier Rates header {header}: missing notes column')
        end=headers[index+1]-1 if index+1<len(headers) else sheet.maxrow+1
        rows=[r for r in range(header+1,end) if sheet.value(r,3) is not None]
        unmerge_written_cells(sheet,rows,[note_col],inherit=True)
        for c in range(1,note_col+1):
            source_col=c if c<=3 else ref_note if c==note_col else ref_columns.get(text(sheet.value(header,c)),5)
            widths[c]=dict(reference.get(source_col,{}),customWidth='1')
            if 4<=c<note_col and any(numeric(sheet.value(r,c)) for r in rows):
                live_rate_columns.add(c)
            sheet.cell(header,c).set('s',str(source_style(styles,ref,ref_header,source_col)))
            for r in rows:
                base=source_style(styles,ref,ref_header+1,source_col)
                style=styles.customize(base,format_code='0.00%' if 4<=c<note_col else 'General',wrap=True,
                                       alignment='center' if c==1 or 4<=c<note_col else 'left')
                sheet.cell(r,c).set('s',str(style))
        sheet.height(header,float(ref.rows[ref_header].get('ht','28')))
        sheet.cell(header-1,1).set('s',str(source_style(styles,ref,ref_header-1,1)))
        for r in rows:
            original=text(sheet.value(r,3))
            if re.search(r'AUM|eligible|elite customers',original,re.I):
                amount,eligibility='-',original
            else:
                amount,eligibility=clean_amount(original),eligibility_from_amount(original)
            notes=compact_notes(eligibility+'；'+text(sheet.value(r,note_col)))
            for c,value in ((3,amount),(note_col,notes)):
                sheet.put(r,c,value,int(sheet.cell(r,c).get('s','0')))
            sheet.height(r,fitted_height([sheet.value(r,2),amount,notes],
                                        [float(widths.get(c,{}).get('width','30')) for c in (2,3,note_col)],15))
    # Every live table cell above has its own imported style, including blank
    # rate cells. The reference's column-default styles contain bottom borders
    # and would otherwise draw horizontal rules across every empty spacer/tail
    # row. Keep widths and hidden flags, but do not style an entire empty column.
    for attributes in widths.values():
        attributes.pop('style',None)
    # Visibility is sheet-wide. A later currency section with no 1M quote
    # must not re-hide a column containing a current quote in another section.
    # Empty tenure columns still follow the reference's hidden preference.
    for c in live_rate_columns:
        widths[c]['hidden']='0'
        if float(widths[c].get('width','0'))<4:
            widths[c]['width']='12'
    set_columns(sheet,widths)


def format_summary_details(book,ref,styles,audit):
    """Finish every summary block using its own amount/notes headers."""
    sheet=next(s for s in book.sheets.values() if s.name.strip()=='最高报价汇总')
    headers=sorted(r for (r,c) in sheet.cells if c==2 and sheet.value(r,c)=='Bank')
    widths=width_map(sheet)
    audit['summary_rate_corrections']=[]
    for index,header in enumerate(headers[1:],1):
        multi=sheet.value(header+1,15)=='促销金额'
        labels=header+1 if multi else header
        amount=next((c for (r,c) in sheet.cells if r==labels and
                     ('amount' in text(sheet.value(r,c)).lower() or text(sheet.value(r,c))=='促销金额')),None)
        note=next((c for (r,c) in sheet.cells if r==labels and text(sheet.value(r,c)).lower() in ('others','要求','备注','其他')),None)
        if amount is None or note is None:
            raise ValueError(f'Summary header {header}: amount/qualification columns are missing')
        if multi:
            source_name='其他外币促销利率'
            widths[amount]=dict(widths.get(amount,{}),width='27',customWidth='1')
            widths[note]=dict(widths.get(note,{}),width='55',customWidth='1')
        else:
            title=' '.join(text(sheet.value(header-1,c)) for c in range(2,amount))
            source_name='USD促销' if '美元' in title and '促销' in title else 'USD挂牌' if '美元' in title else 'CNY挂牌' if '人民币' in title else None
            if source_name is None:
                raise ValueError(f'Unknown summary section: {title}')
            if ref.value(header-1,2):
                sheet.put(header-1,2,ref.value(header-1,2))
        source=book.sheets[source_name]
        bc,current=GRID[source_name]
        ctx,_,_=row_contexts(source,bc)
        groups=row_groups(ctx)
        ratecols={}
        currency=source_name[:3]
        for c in range(3,amount if multi else sheet.maxcol+1):
            if c in (amount,note):continue
            if multi:
                label=text(sheet.value(header,c))
                for chinese,code in CURRENCIES.items():
                    if chinese in label:currency=code
            tenor=text(sheet.value(labels,c))
            if re.fullmatch(r'\d+[MD]',tenor):ratecols[c]=(currency,tenor)
        end=headers[index+1] if index+1<len(headers) else sheet.maxrow+1
        body=[]
        for r in range(labels+1,end):
            label=text(sheet.value(r,2))
            if not label or not re.fullmatch(r'[A-Za-z][A-Za-z /&.-]*',label):
                continue
            body.append(r)
            bank=bank_name(label)
            amounts=[]
            misplaced_notes=[]
            for c,(currency,tenor) in ratecols.items():
                candidates=[rr for rr in groups.get((currency,tenor,bank),[]) if numeric(source.value(rr,current))]
                eligible=audit.get('retail_history_rows',{}).get(source_name,{})
                matches=[rr for rr in candidates if eligible.get(rr,True)]
                highest=max((source.value(rr,current) for rr in matches),default='-')
                old=sheet.value(r,c)
                # Some summary tenors have no displayed detail section (e.g.
                # CNY 18M/24M). Their valid cached rates remain authoritative.
                replacement=highest if matches else '-' if candidates else old if numeric(old) else '-'
                if not numeric(old) and old not in (None,'','-'):
                    misplaced_notes.append(text(old))
                if replacement!=old:
                    audit['summary_rate_corrections'].append({'cell':address(r,c),'bank':bank,'currency':currency,'tenor':tenor,'old':str(old),'new':str(highest)})
                    sheet.put(r,c,replacement)
                for rr in matches:
                    if source.value(rr,current)==highest:
                        value=clean_amount(source.value(rr,current-1))
                        if value not in amounts and value not in ('-','未列明'):
                            amounts.append(value)
            original=text(sheet.value(r,amount))
            extra=eligibility_from_amount(original)
            if re.search('见.*(?:挂牌|促销)|取本期|产品门槛见',original):
                extra=''
            value='\n'.join(amounts) if amounts else clean_amount(original)
            notes=compact_notes('；'.join([text(sheet.value(r,note)),extra]+misplaced_notes))
            for c,v in ((amount,value),(note,notes)):
                style=styles.customize(int(sheet.cell(r,c).get('s','0')),format_code='General',wrap=True,alignment='left')
                sheet.put(r,c,v,style)
        # USD promotional notes can use the otherwise empty J/K cells, while
        # preserving the shared widths of the SGD tenor columns above them.
        note_end=max(note,11) if source_name=='USD促销' else note
        for r in [labels]+body:
            if note_end>note and all(sheet.value(r,c) in (None,'') for c in range(note+1,note_end+1)):
                unmerge_written_cells(sheet,[r],range(note,note_end+1))
                merges=sheet.root.find(M+'mergeCells')
                if merges is None:
                    merges=ET.Element(M+'mergeCells');sheet.insert_element(merges)
                ET.SubElement(merges,M+'mergeCell',ref=f'{address(r,note)}:{address(r,note_end)}')
                merges.set('count',str(len(merges)))
                note_width=sum(float(widths.get(c,{}).get('width','12')) for c in range(note,note_end+1))
            else:
                note_width=float(widths.get(note,{}).get('width','30'))
            if r in body:
                sheet.height(r,fitted_height([sheet.value(r,amount),sheet.value(r,note)],
                    [float(widths.get(amount,{}).get('width','22')),note_width],
                    float(ref.rows.get(r,{}).get('ht','15'))))
    set_columns(sheet,widths)


def find_previous(project, input_path, as_of, *, before=None):
    """Legacy archive lookup; never used by formatting or four-file delivery.

    Select the previous collection, then its verified correction-chain tip.

    A correction retains its originating live collection's time and all three
    source dates; it is never a new observation. Only completed, hash-verified
    correction workbooks may replace that observation. File mtimes, formatting
    outputs, and unrelated correction branches cannot determine the baseline.
    """
    # Keep caller-visible paths stable (notably /var versus /private/var),
    # while using resolved paths for identity checks below.
    project=Path(project).expanduser().absolute()
    input_path=Path(input_path).expanduser().absolute()
    candidates=[]
    current_stamp=before
    adjacent=input_path.parent/'workflow.json'
    if adjacent.exists() and current_stamp is None:
        seen=set()
        while adjacent.is_file() and adjacent.resolve() not in seen:
            seen.add(adjacent.resolve())
            data=json.loads(adjacent.read_text(encoding='utf-8-sig'))
            if data.get('mode')!='correction' or not data.get('source_workflow'):
                current_stamp=data.get('started_at')
                break
            source=Path(data['source_workflow']).resolve()
            adjacent=source if source.name=='workflow.json' else source/'workflow.json'

    def checked_outputs(data,required=False):
        hashes=data.get('output_hashes',{})
        for kind in ('report','rainbow'):
            expected=hashes.get(kind) or hashes.get('raw_'+kind)
            value=data.get(kind)
            if required and (not expected or not value):return False
            if not expected:continue # Older completed live manifests omit hashes.
            path=Path(value) if value else None
            if path is None or not path.is_file():return False
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected.lower():return False
        return True

    def correction_report(path,data):
        if checked_outputs(data,required=True):
            return Path(data['report']).expanduser().absolute()
        # A user may subsequently edit the published copy. Never trust those
        # new bytes or rewrite its audit hash. A same-date SGD correction has
        # an explicit packaging plan pointing to its original generated pair;
        # accept that pair only when BOTH files still have the original hashes.
        try:
            plan=json.loads((path.parent/'table-validation-plan.json').read_text(encoding='utf-8-sig'))
            if plan.get('correction_kind')!='sgd_same_date_correction' or not plan.get('sgd_correction_plan'):
                return None
            if any(Path(plan[kind+'_output']).resolve()!=Path(data[kind]).resolve() for kind in ('report','rainbow')):
                return None
            source=json.loads(Path(plan['sgd_correction_plan']).read_text(encoding='utf-8-sig'))
            if source.get('as_of')!=data['source_dates']['sgd'] or source.get('date_column_shift')!=0:
                return None
            original=dict(data,**{kind:source[kind+'_output'] for kind in ('report','rainbow')})
            if checked_outputs(original,required=True):
                return Path(original['report']).expanduser().absolute()
        except (OSError,KeyError,TypeError,json.JSONDecodeError):
            pass
        return None

    for path in (project/'runs').glob('all-*/workflow.json'):
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('status')!='complete' or data.get('mode')!='live' or not data.get('report'):
            continue
        source_dates=data.get('source_dates',{})
        if not {'sgd','board','fx'} <= source_dates.keys():
            continue
        dates={source_dates[k] for k in ('sgd','board','fx')}
        if len(dates)!=1:
            continue
        day=next(iter(dates))
        stamp=data.get('started_at',day)
        eligible=(stamp<current_stamp if current_stamp else day<as_of.isoformat()) and day<=as_of.isoformat()
        report=Path(data['report']).expanduser().absolute()
        if eligible and report.is_file() and report.resolve()!=input_path.resolve() and checked_outputs(data):
            candidates.append((stamp,report,path.resolve(),data))
    if not candidates:
        raise ValueError('No previous complete collection found. Supply --previous with its report workbook.')
    _,report,origin,collection=max(candidates,key=lambda x:x[0])
    corrections={}
    for path in (project/'outputs').glob('*/workflow.json'):
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('status')!='complete' or data.get('mode')!='correction' or not data.get('source_workflow'):
            continue
        if data.get('project') and Path(data['project']).resolve()!=project.resolve():
            continue
        if any(data.get('source_dates',{}).get(key)!=collection['source_dates'][key] for key in ('sgd','board','fx')):
            continue
        corrected=correction_report(path,data)
        if corrected is None or corrected.resolve()==input_path.resolve():continue
        parent=Path(data['source_workflow']).resolve()
        parent=parent if parent.name=='workflow.json' else parent/'workflow.json'
        corrections[path.resolve()]=(parent,corrected)

    # Walk only explicit parent edges from the chosen live collection. This
    # rejects orphaned corrections, cycles, hash-invalid ancestors and newer
    # files that were merely reformatted or copied into the outputs directory.
    reachable={origin:report}
    pending=dict(corrections)
    while pending:
        added=[path for path,(parent,_) in pending.items() if parent in reachable]
        if not added:break
        for path in added:
            _,corrected=pending.pop(path)
            reachable[path]=corrected
    parents={parent for path,(parent,_) in corrections.items() if path in reachable}
    tips=[path for path in reachable if path not in parents]
    if len(tips)!=1:
        raise ValueError('Multiple verified correction branches for the previous collection; supply --previous explicitly: '+', '.join(str(reachable[path]) for path in tips))
    return reachable[tips[0]]


def verify_output(path,expected_sheets,audit):
    verify_book(Book(path),expected_sheets,audit)
    audit['verification']['saved_file_readback']='passed'


def verify_book(book,expected_sheets,audit):
    from .premier_details import DETAIL_SHEET,validate_links
    premier_links=validate_links(book)
    permitted={c['address']:c['formula'] for c in premier_links}
    if list(book.sheets)!=expected_sheets:
        raise AssertionError('Output business sheet order differs from reference')
    formulas=[]
    for sheet in book.sheets.values():
        for (r,c),element in sheet.cells.items():
            f=element.find(M+'f')
            if f is not None:
                if sheet.name=='Premier Rates' and permitted.get(address(r,c))==f.text:continue
                if sheet.name not in GRID or not (f.text or '').startswith('IF(COUNT('):
                    raise AssertionError(f'Unexpected remaining formula {sheet.name}!{address(r,c)}')
                formulas.append((sheet.name,address(r,c)))
            if element.get('t')=='e':
                raise AssertionError(f'Error cell remains: {sheet.name}!{address(r,c)}')
    if any(name.startswith('xl/externalLinks/') for name in book.parts):
        raise AssertionError('External links remain')
    for name,(bc,current) in GRID.items():
        if name not in book.sheets:
            continue
        sheet=book.sheets[name]
        contexts,headers,_=row_contexts(sheet,bc)
        delta=first_delta(sheet)
        original=audit['comparison_columns'][name]
        if len(headers)!=len(original):
            raise AssertionError(f'Comparison section count changed: {name}')
        for header,pair in zip(headers,original):
            if parse_date(sheet.value(header,current))!=date.fromisoformat(pair['current_date']):
                raise AssertionError(f'Current date mismatch: {name}!{address(header,current)}')
            previous_date=date.fromisoformat(pair['previous_date']) if pair['previous_date'] else None
            if parse_date(sheet.value(header,current+1))!=previous_date:
                raise AssertionError(f'Previous date mismatch: {name}!{address(header,current+1)}')
        for r in contexts:
            now,old=sheet.value(r,current),sheet.value(r,current+1)
            expected=(now-old).quantize(Decimal('0.0000000001')) if numeric(now) and numeric(old) else '-'
            if sheet.value(r,delta)!=expected:
                raise AssertionError(f'Delta mismatch at {name}!{address(r,delta)}')
        for merge in sheet.root.findall(M+'mergeCells/'+M+'mergeCell'):
            ends=merge.get('ref').split(':')
            (r1,c1),(r2,c2)=pos(ends[0]),pos(ends[-1])
            if c1<=delta<=c2 and r1!=r2 and any(r1<=r<=r2 for r in contexts):
                raise AssertionError(f'Delta cells are hidden by a merged range: {name}!{merge.get("ref")}')
        if name=='SGD促销':
            keys=list(contexts.values())
            if len(keys)!=len(set(keys)):
                raise AssertionError('SGD has multiple main-rate rows per bank/tenor')
            rows={}
            for r,v in contexts.items():rows.setdefault((v[2],v[1]),r)
            for item in audit['sgd_winners']:
                row=rows[item['bank'],item['tenor']]
                if sheet.value(row,current)!=Decimal(item['rate']):
                    raise AssertionError(f'Personal maximum mismatch: {item}')
            for item in audit.get('full_sgd_public_rows',[]):
                if sheet.value(item['row'],2)!=item['display']:
                    raise AssertionError('Public SGD quote display lost: '+str(item))
                if item['overflow'] and '业务说明' not in book.sheets:
                    raise AssertionError('Missing overflow quote details')
    summary=next((s for s in book.sheets.values() if s.name.strip()=='最高报价汇总'),None)
    if summary:
        header=next(r for (r,c) in summary.cells if c==2 and summary.value(r,c)=='Bank')
        expected={(x['bank'],x['tenor']):Decimal(x['rate']) for x in audit['sgd_winners']}
        for r in range(header+1,summary.maxrow+1):
            bank=text(summary.value(r,2))
            if bank=='Bank' or '利率' in bank:
                break
            if not bank:
                continue
            for c in range(3,summary.maxcol+1):
                if not re.fullmatch(r'\d+[MD]',text(summary.value(header,c))):continue
                rate=expected.get((bank_name(bank),text(summary.value(header,c))),'-')
                if summary.value(r,c)!=rate:
                    raise AssertionError(f'SGD summary maximum mismatch at {address(r,c)}')
    audit['verification']={'in_memory_checks':'passed','saved_file_readback':'not_run','delta_formula_count':len(formulas),
        'remaining_source_sheets':int(DETAIL_SHEET in book.sheets),'premier_formula_count':len(premier_links),
        'external_links':0,'excel_native_render':'not_run'}


def format_report(input_path,template_path,output_path=None,*,previous_path=None,project_root=None,dry_run=False):
    """Return a JSON-serializable audit. Never overwrite source/template/output.

    With dry_run=True the complete transformation runs in memory, then returns
    its plan and findings without creating an XLSX. The same inputs and rules
    are used by CLI and future automation integration.
    """
    if previous_path is not None:
        raise ValueError('--previous / previous_path 已取消：只比较输入工作簿现有最新两列，不接受外部比较文件。')
    input_path,template_path=Path(input_path).resolve(),Path(template_path).resolve()
    if not input_path.is_file() or not template_path.is_file():
        raise FileNotFoundError('Input or reference workbook does not exist')
    output=Path(output_path).resolve() if output_path else None
    if not dry_run and (output is None or output.exists()):
        raise ValueError('Choose a new --output path; existing files are never overwritten')
    if output in (input_path,template_path):
        raise ValueError('Output must differ from input/reference')
    original_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (input_path,template_path)}
    book,template=Book(input_path),Book(template_path)
    for name in ('全部公开报价','Personal最高值'):
        if name in book.sheets:book.drop_sheet(name)
    as_of=infer_date(book)
    comparisons=comparison_columns(book)
    previous_dates={pair['previous_date'] for sections in comparisons.values() for pair in sections if pair['previous_date']}
    source_note_issues=[]
    quotes=sgd_quotes(book,as_of,source_note_issues)
    expected=[name for name in template.sheets if not is_source(name) and name not in ('全部公开报价','Personal最高值')]
    from .premier_details import DETAIL_SHEET
    if DETAIL_SHEET in book.sheets and DETAIL_SHEET not in expected:expected.append(DETAIL_SHEET)
    missing=set(expected)-set(book.sheets)
    unknown=set(book.sheets)-set(expected)-{n for n in book.sheets if is_source(n)}
    if missing or unknown:
        raise ValueError(f'Unmapped sheet layout. Missing={sorted(missing)}, extra business sheets={sorted(unknown)}')
    # Row counts and bank/product membership may change. A new currency/tenor
    # scope, however, needs an explicit reference layout and summary mapping.
    # Refuse it before editing rather than silently reuse a different section.
    for name,(bankcol,_) in GRID.items():
        if name not in book.sheets:
            continue
        current_scopes={(currency,tenor) for currency,tenor,_ in row_contexts(book.sheets[name],bankcol)[0].values()}
        reference_scopes={(currency,tenor) for currency,tenor,_ in row_contexts(template.sheets[name],bankcol)[0].values()}
        unmapped=current_scopes-reference_scopes
        if unmapped and not workbook_policy().get('complete_public_quotes'):
            raise ValueError(f'Unmapped currency/tenor in {name}: {sorted(unmapped)}; add reference and summary mappings first')
    audit={'version':2,'input':str(input_path),'template':str(template_path),'previous':None,
        'comparison_mode':'latest_two_columns','comparison_source':str(input_path),'comparison_columns':comparisons,
        'as_of':as_of.isoformat(),'previous_date':next(iter(previous_dates)) if len(previous_dates)==1 else None,
        'output':str(output) if output else None,
        'dry_run':dry_run,'formulas_frozen':0,'legacy_errors':[],'rows_removed':{},'sgd_winners':[],
        'historical_consolidations':[],'historical_text_rates':[],'missing_previous':[],'retained_amount_conflicts':[],
        'source_note_issues':source_note_issues,
        'policy':{'sgd_main_and_summary':'Personal maximum only; no Premier/Preferred fallback',
                  'hlf_8m':'display actual 8M separately from 9M','delta':'existing latest column minus adjacent original date column; no external history lookup',
                  'legacy_errors':'replace with dash; preserve original error/formula in this report'},
        'input_sha256':original_hashes}
    freeze_values(book,audit)
    normalize_historical_rates(book,audit)
    restore_fx_history_labels(book,input_path,audit)
    styles=StyleMap(book,template)
    winners=format_sgd(book.sheets['SGD促销'],template.sheets['SGD促销'],styles,quotes,audit)
    for name in expected:
        if name==DETAIL_SHEET:continue
        sheet,ref=book.sheets[name],template.sheets[name]
        if name in GRID and name!='SGD促销':
            format_history(sheet,ref,styles,audit)
        elif name.endswith('挂牌') and name not in GRID:
            format_matrix(sheet,ref,styles,audit)
        elif name.strip()=='最高报价汇总':
            format_summary(sheet,ref,styles,winners,as_of,audit)
        elif name in ('Premier Rates','Bank List'):
            for (r,c),element in sheet.cells.items():
                if (r,c) in ref.cells:
                    element.set('s',str(source_style(styles,ref,r,c)))
            for r,row in ref.rows.items():
                if 'ht' in row.attrib and (name!='Premier Rates' or r in sheet.rows):
                    sheet.height(r,float(row.get('ht')))
            copy_layout(sheet,ref,styles)
            if name=='Premier Rates':
                format_premier(sheet,ref,styles)
    summary_name=next(name for name in expected if name.strip()=='最高报价汇总')
    format_summary_details(book,template.sheets[summary_name],styles,audit)
    if '业务说明' in book.sheets and '业务说明' not in expected:
        expected.append('业务说明')
    removed=[n for n in book.sheets if n not in expected]
    for name in removed:
        book.drop_sheet(name)
    sheet_nodes=book.workbook.find(M+'sheets')
    sheet_nodes[:]=[book.sheets[name].node for name in expected]
    book.sheets={name:book.sheets[name] for name in expected}
    book.detach_external(removed)
    audit['removed_source_sheets']=removed
    verify_book(book,expected,audit)
    if not dry_run:
        output.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='report-format-',dir=output.parent) as folder:
            temporary=Path(folder)/'validated.xlsx'
            book.save(temporary)
            verify_output(temporary,expected,audit)
            # Exclusive output creation also protects against a concurrent run.
            with output.open('xb') as target:
                target.write(temporary.read_bytes())
        audit['output_sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
    for p,digest in original_hashes.items():
        if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=digest:
            raise AssertionError(f'Source changed during formatting: {p}')
    audit['sources_unchanged']=True
    return audit


def main(argv=None):
    parser=argparse.ArgumentParser(description='离线整理市场利率调研：Personal最高值、原表最新两列增幅、金额与版式对齐')
    parser.add_argument('--input',required=True,type=Path,help='待整理的完整调研工作簿（含采集明细）')
    parser.add_argument('--template',required=True,type=Path,help='格式参考工作簿')
    parser.add_argument('--previous',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--output',type=Path,help='新的输出xlsx路径，禁止覆盖已有文件')
    parser.add_argument('--project-root',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--audit',type=Path,help='处理报告JSON路径，禁止覆盖已有文件')
    parser.add_argument('--dry-run',action='store_true',help='完整预检查，仅输出报告，不生成xlsx')
    args=parser.parse_args(argv)
    if not args.dry_run and not args.output:
        parser.error('正式执行需要 --output；只检查请使用 --dry-run')
    audit_path=args.audit or (args.output.with_suffix('.format-report.json') if args.output else None)
    if audit_path and args.output and audit_path.resolve()==args.output.resolve():
        parser.error('--audit 与 --output 必须是不同路径')
    if audit_path and audit_path.exists():
        parser.error(f'报告文件已存在，请换新路径：{audit_path}')
    try:
        result=format_report(args.input,args.template,args.output,previous_path=args.previous,
                             project_root=args.project_root,dry_run=args.dry_run)
        if audit_path:
            audit_path.parent.mkdir(parents=True,exist_ok=True)
            with audit_path.open('x',encoding='utf-8') as stream:
                json.dump(result,stream,ensure_ascii=False,indent=2,default=str)
        print(json.dumps({k:result[k] for k in ('dry_run','as_of','previous_date','comparison_mode','comparison_columns','output','formulas_frozen','rows_removed','sources_unchanged')},ensure_ascii=False,indent=2))
        print(f'历史错误 {len(result["legacy_errors"])}；历史归并 {len(result["historical_consolidations"])}；相邻列缺失报价 {len(result["missing_previous"])}')
        if audit_path:
            print(f'处理报告：{audit_path}')
        return 0
    except (ValueError,FileNotFoundError,AssertionError) as exc:
        parser.exit(2,f'整理失败：{exc}\n')


if __name__=='__main__':
    raise SystemExit(main())
