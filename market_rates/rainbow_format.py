"""Offline rainbow workbook finishing stage, using only the Python standard library.

Public API: format_rainbow(input_path, template_path, output_path, dry_run=False).
The input is the collector's full workbook, including its verified detail sheets.
The template supplies styles, never quotes or dates. No Excel process is required.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET

from .report_xml import Book, StyleMap, M, R, P, NS, XML, address, col, numeric, pos, local
from .report_format import text, parse_date, serial, customer_is_personal, channel_label
from .sgd_conditions import SGDConditions, summarize_sgd_conditions
from .quote_selection import personal_quote
from .quote_layout import tenor_order

BUSINESS = ('SGD Promotional Rate', 'SGD Board Rate', 'USD Rate + Other Currency Rates')
SGD_PROMO_POLICY = 'all_public_quotes_personal_rank_v3'
TENOR = re.compile(r'\d+[MD]$')
REF = re.compile(r"(?:'([^']+)'|([\w\u4e00-\u9fff]+))!\$?([A-Z]+)\$?(\d+)")
CT = '{http://schemas.openxmlformats.org/package/2006/content-types}'


def bank_label(value):
    s = text(value)
    return {'citibank':'CITI', 'citi':'CITI', 'hl bank':'HLB', 'maribank':'MARI',
            'mari':'MARI', 'trustbank':'TRUST', 'trust bank':'TRUST', 'maybank':'Maybank',
            'singfinance':'SingFinance', 'singapura finance':'Singapura Finance'}.get(s.lower(), s)


def rate_value(value):
    if numeric(value):
        # Excel caches contain binary floating-point tails. This does not round
        # to the displayed percentage precision (which would collapse tiers).
        return value.quantize(Decimal('0.000000000001')).normalize()
    if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?%', value.strip()):
        return rate_value(Decimal(value.strip()[:-1]) / 100)
    return None


def short_money(value):
    value = Decimal(value)
    for amount, suffix in [(Decimal(1000000), 'M'), (Decimal(1000), 'K')]:
        scaled = value / amount
        if value >= amount and scaled == scaled.quantize(Decimal('.001')):
            return f'{scaled.normalize():f}{suffix}'
    return f'{value:,.0f}' if value == int(value) else f'{value.normalize():,f}'


def amount_text(currency, minimum, maximum, lower='≥', upper='≤'):
    currency = text(currency).replace(' ', '') or '金额'
    prefix = 'S$' if currency == 'SGD' else currency + ' '
    if numeric(minimum) and numeric(maximum) and minimum == maximum:
        return prefix + '=' + short_money(minimum)
    lo = lower + short_money(minimum) if numeric(minimum) else ''
    hi = upper + short_money(maximum) if numeric(maximum) else ''
    return prefix + ' / '.join(filter(None, (lo, hi))) if lo or hi else '未列明'


def brief_notes(raw, bank, actual, tenor, as_of, end=None):
    """Retain financial restrictions, while omitting collection/process narration."""
    raw = text(raw)
    if bank != 'SCB' and (raw.count('Standard Chartered') >= 3 or len(raw) > 5000):
        return '源备注混入其他银行条款，资格待核'
    keep = []
    if actual and actual != tenor:
        keep.append(f'实际{actual}，归入{tenor}组')
    end_date = parse_date(end)
    if end_date and end_date < as_of:
        keep.append(f'公告已截止（{end_date.isoformat()}），最新公开参考')
    # Visible captions already summarize most relevant restrictions. The raw
    # detail is also searched so a caption omission does not remove eligibility.
    mappings = [
        (r'仅同期限旧存款续期|不接受新存入', '仅同期限续存，不接受新存入'),
        (r'Vivid', '须有Vivid储蓄账户'),
        (r'现有客户带入新资金；新户先建立财富关系', '现有客户须新资金；新户须先建立财富关系'),
        (r'须已有活期账户|existing.*current account', '须已有活期账户'),
        (r'非新客户产品', '非新客户产品'),
        (r'须先换汇|换汇资金|currency conversion|convert.*currenc', '须先换汇'),
        (r'新开S\$200', '须有储蓄账户（新开S$200）'),
        (r'按账户总定存余额分档', '按账户总定存余额分档'),
        (r'新旧资金|existing or fresh|new or existing funds', '新旧资金均可'),
        (r'不限新资金|不要求新资金|无须新资金', '不限新资金'),
        (r'fresh and renewal of funds', '新资金及续存均可'),
        (r'合资格续存', '含合资格续存'),
        (r'新资金要求待核|见条款待核', '新资金要求待核'),
        (r'计息口径待核', '计息口径待核'),
        (r'客户资格见明细', '客户资格待核'),
    ]
    for pattern, label in mappings:
        if re.search(pattern, raw, re.I) and label not in keep:
            keep.append(label)
    if re.search(r'新资金|fresh funds', raw, re.I) and not any('资金' in x for x in keep):
        keep.append('须新资金')
    if re.search(r'Fresh funds only', raw,re.I) and '须新资金' not in keep:
        keep.append('须新资金（源结构字段待核）' if '新资金要求待核' in keep else '须新资金')
    for part in re.split(r'[；;\n]', raw):
        part = part.strip()
        if not part or part.startswith(('http', '证据', '来源', '自动', '采集')):
            continue
        if re.search(r'须另询银行|另须达到|(?:须|仅限|必须|合计|不接受|限额|新开储蓄).{1,100}|only|require', part, re.I):
            part = re.sub(r'，不能直接套用本表.*', '', part)
            part = part.replace('Fresh funds only','须新资金').replace('Existing or new Savings Account required','须有储蓄账户')
            part = part.replace('须有Vivid Savings Account','须有Vivid储蓄账户').replace('(新开S$200)','（新开S$200）')
            if len(part) <= 140 and not any(part in x or x in part for x in keep):
                keep.append(part)
    return '；'.join(dict.fromkeys(keep))


@dataclass
class Quote:
    bank: str
    tenor: str
    currency: str
    category: str
    rate: Decimal
    minimum: object = None
    maximum: object = None
    amount_currency: str = ''
    lower: str = '≥'
    upper: str = '≤'
    audience: str = ''
    channel: str = ''
    actual: str = ''
    notes: str = ''
    source: str = ''
    source_row: int = 0
    published_cell: str = ''
    raw: str = ''
    order: int = 0
    product: str = ''

    @property
    def amount(self):
        return amount_text(self.amount_currency or self.currency, self.minimum, self.maximum, self.lower, self.upper)

    @property
    def condition(self):
        audience = 'Personal' if customer_is_personal(self.audience) else self.audience
        channel = channel_label(self.channel)
        return '\n'.join(x for x in (self.product,self.amount, ' '.join(x for x in (audience,channel if channel != '渠道未列明' else '') if x),self.notes) if x)

    @property
    def minimum_key(self):
        # Unknown minima sort last; an exclusive lower bound is stricter than
        # an inclusive lower bound with the same number.
        return (self.minimum if numeric(self.minimum) else Decimal('Infinity'), self.lower == '>', self.order)


class Reader:
    def __init__(self, book, audit):
        self.book, self.audit, self.order = book, audit, 0
        dates = []
        for name in BUSINESS:
            if name not in book.sheets:
                raise ValueError(f'Missing business sheet: {name}')
            match = re.search(r'20\d{2}-\d{2}-\d{2}', text(book.sheets[name].value(1, 1)))
            if match:
                dates.append(parse_date(match[0]))
        if len(set(dates)) != 1 or len(dates) != 3:
            raise ValueError('Business sheets must have one consistent collection date in A1')
        self.as_of = dates[0]
        audit['as_of'] = self.as_of.isoformat()
        audit['source_formula_count'] = 0
        for name in BUSINESS:
            for rc, cell in book.sheets[name].cells.items():
                if cell.find(M+'f') is not None:
                    audit['source_formula_count'] += 1
                    if book.sheets[name].value(*rc) is None or cell.get('t') == 'e':
                        raise ValueError(f'Missing/error formula cache: {name}!{address(*rc)}')

    def quote(self, view, r, c, bank, currency, category, tenor, caption='', expand_ties=False):
        value = rate_value(view.value(r, c))
        if value is None:
            return None
        self.order += 1
        q = Quote(bank_label(bank), tenor, currency, category, value,
                  actual=tenor, published_cell=f'{view.name}!{address(r,c)}', raw=text(caption), order=self.order)
        formula = view.cells[r, c].findtext(M+'f', '')
        candidates = []
        for match in REF.finditer(formula):
            sn = match[1] or match[2]
            if sn not in self.book.sheets:
                raise ValueError(f'Unresolved source {sn}: {q.published_cell}')
            detail = self.book.sheets[sn]
            sr, sc = int(match[4]), pos(match[3]+'1')[1]
            v = rate_value(detail.value(sr, sc))
            if v == value:
                candidates.append((detail, sr, sc))
        if not formula:
            # Some long-tenor quotes are already constants in the collector's
            # view. Reconcile them to this batch's detail rather than drop them.
            datepart = self.as_of.strftime('%Y%m%d')
            prefix = '明细_' if currency=='SGD' and category=='促销' else '外币促销_' if category=='促销' else '挂牌全批_'
            sources = [s for s in self.book.sheets.values() if s.name.startswith(prefix+datepart)]
            for detail in sources:
                for sr in range(4,detail.maxrow+1):
                    sc = 8 if prefix=='明细_' else 9
                    actual = text(detail.value(sr,3 if prefix=='明细_' else 5))
                    cu = 'SGD' if prefix=='明细_' else text(detail.value(sr,3 if prefix=='外币促销_' else 2))
                    if bank_label(detail.value(sr,1))==q.bank and actual==tenor and cu==currency and rate_value(detail.value(sr,sc))==value:
                        candidates.append((detail,sr,sc))
        if not candidates:
            raise ValueError(f'No matching verified detail for numeric quote: {q.published_cell}')
        detail, sr, sc = candidates[0]
        # Matrix MAX caches often reference just the winning source row. Search
        # same product/audience/currency/tenor for lower equal-rate minima.
        if expand_ties and detail.name.startswith(('挂牌全批_', '外币促销_')):
            keycols = (1,2,3,4,5,6)
            key = tuple(text(detail.value(sr,x)) for x in keycols)
            candidates = [(detail,k,9) for k in range(4,detail.maxrow+1)
                          if tuple(text(detail.value(k,x)) for x in keycols) == key
                          and rate_value(detail.value(k,9)) == value]
        parsed = [self.from_detail(deepcopy(q), d, k, caption) for d,k,_ in candidates]
        q = min(parsed, key=lambda x:x.minimum_key)
        if q.bank != bank_label(bank):
            raise ValueError(f'Bank mismatch at {q.published_cell}')
        self.audit['candidates'].append(asdict(q))
        return q

    def from_detail(self, q, d, r, caption):
        q.source, q.source_row = d.name, r
        q.product=text(d.value(r,2 if d.name.startswith('明细_') else 4))
        if bank_label(d.value(r,1)) != q.bank:
            raise ValueError(f'Quote/source bank mismatch: {q.published_cell} -> {d.name}!{r}')
        if d.name.startswith('明细_'):
            q.actual, q.audience, q.channel = map(text, (d.value(r,3), d.value(r,4), d.value(r,5)))
            q.minimum, q.maximum, q.amount_currency = d.value(r,6), d.value(r,7), 'SGD'
            if not hasattr(self, '_sgd_conditions'):
                self._sgd_conditions = {}
            if d.name not in self._sgd_conditions:
                self._sgd_conditions[d.name] = SGDConditions(d)
            conditions = self._sgd_conditions[d.name].for_row(r)
            summary, _ = summarize_sgd_conditions(conditions, q.audience)
            if q.bank != 'SCB' and (conditions.count('Standard Chartered') >= 3 or conditions.count('www.sc.com/sg/td') >= 3):
                summary = '源备注混入其他银行条款，资格待核'
            raw = text(caption) + '；' + conditions + '；' + text(d.value(r,9))
            end = d.value(r,11)
        elif d.name.startswith(('挂牌全批_', '外币促销_')):
            q.actual = text(d.value(r,5))
            cust_channel = text(d.value(r,6)).split('/',1)
            q.audience = cust_channel[0].strip()
            q.channel = cust_channel[1].strip() if len(cust_channel)>1 else ''
            q.minimum, q.maximum, q.amount_currency = d.value(r,7), d.value(r,8), text(d.value(r,11))
            raw = text(caption) + '；' + text(d.value(r,15))
            end = d.value(r,13) if d.name.startswith('外币促销_') else None
            bounds = text(d.value(r,17)) if d.name.startswith('挂牌全批_') else ''
            q.lower = '>' if '不含下限' in bounds else '≥'
            q.upper = '<' if '不含上限' in bounds else '≤'
        else:
            raise ValueError(f'Unsupported detail schema: {d.name}')
        # Captions have explicit inequalities for promotional tiers whose
        # detail minimum/maximum cells do not encode inclusivity.
        monetary = next((p for p in re.split(r'[；;\n]',text(caption)) if re.search(r'(?:S\$|SGD|USD|CNY|CNH|AUD|NZD|EUR|GBP|CAD|HKD|CHF).*\d',p)), '')
        if re.search(r'(?:且|[-–])\s*<(?!=)', monetary):
            q.upper = '<'
        if re.search(r'(?:SGD|USD|CNY|CNH|AUD|NZD|EUR|GBP|CAD|HKD|CHF|S\$)\s*>(?!=)',monetary):
            q.lower = '>'
        q.raw = raw
        if d.name.startswith('明细_'):
            # Keep the original in the audit, but summarize the correctly
            # linked product conditions instead of discarding long clauses.
            q.notes = brief_notes(text(caption), q.bank, q.actual, q.tenor, self.as_of, end)
            q.notes = '；'.join(dict.fromkeys(p for p in (q.notes + '；' + summary).split('；') if p))
            fresh = text(d.value(r,9))
            if fresh in ('需要', '是') and '资金' not in q.notes:
                q.notes += ('；' if q.notes else '') + '须新资金'
            elif '待核' in fresh and '资金' not in q.notes:
                q.notes += ('；' if q.notes else '') + '新资金要求待核'
        else:
            q.notes = brief_notes(raw, q.bank, q.actual, q.tenor, self.as_of, end)
        if '资格待核' in q.notes or '资格条款待核' in q.notes:
            self.audit['issues'].append({'bank':q.bank, 'tenor':q.tenor, 'reason':q.notes,
                                          'source':d.name, 'row':r})
        return q

    def blocks(self, sheet, header, end, currency, category):
        blocks = {}
        for c in range(1,sheet.maxcol+1):
            tenor = text(sheet.value(header,c))
            if not TENOR.fullmatch(tenor):continue
            if text(sheet.value(header,c+1)).lower() not in ('promo rate','highest rate','最高报价') or not text(sheet.value(header,c+2)):
                raise ValueError(f'Incomplete tenor header triple: {sheet.name}!{address(header,c)} ({tenor})')
            bank, records = '', []
            for r in range(header+1,end):
                label = text(sheet.value(r,c))
                if label:
                    bank = label
                v = rate_value(sheet.value(r,c+1))
                if v is None:
                    if label:
                        self.audit['missing_removed'].append({'sheet':sheet.name,'cell':address(r,c),'bank':label,'tenor':tenor})
                    continue
                if not bank:
                    raise ValueError(f'Numeric rate without bank: {sheet.name}!{address(r,c+1)}')
                records.append(self.quote(sheet,r,c+1,bank,currency,category,tenor,text(sheet.value(r,c+2))))
            blocks[tenor] = records
        if not blocks:
            raise ValueError(f'Unrecognized block header: {sheet.name}!{header}')
        return blocks


def select_quotes(records, mode, audit):
    groups = defaultdict(list)
    for q in records:
        if mode == 'personal' and not customer_is_personal(q.audience):
            audit['excluded_audience'].append(asdict(q))
            continue
        groups[(q.bank, q.rate if mode == 'dedup_rate' else None)].append(q)
    winners = []
    for candidates in groups.values():
        high = max(q.rate for q in candidates)
        tied = [q for q in candidates if q.rate == high]
        # Different amount currencies cannot be compared without an FX rate.
        currencies = {q.amount_currency for q in tied}
        pick = min(tied,key=lambda q:q.minimum_key) if len(currencies)==1 else min(tied,key=lambda q:q.order)
        if len(tied)>1:
            audit['ties'].append({'bank':pick.bank,'tenor':pick.tenor,'rate':high,'picked':pick.source_row,
                                  'rows':[q.source_row for q in tied], 'comparable_amounts':len(currencies)==1})
        if len(currencies)>1:
            pick.notes = '同利率金额币种不同，保留原先顺序；' + pick.notes
        winners.append(pick)
    if mode == 'dedup_rate':
        # Rank banks by their best quote; keep all retained tiers of a bank
        # adjacent, in amount order, as in the reference's merged bank cells.
        banks = defaultdict(list)
        for q in winners:
            banks[q.bank].append(q)
        order = sorted(banks,key=lambda bank:(-max(q.rate for q in banks[bank]),bank.casefold()))
        return [q for bank in order for q in sorted(banks[bank],key=lambda q:(q.minimum_key,-q.rate))]
    return sorted(winners,key=lambda q:(-q.rate,q.bank.casefold(),q.minimum_key))


def select_sgd_promotions(records, audit=None):
    """Retain every public quote, ranking bank blocks by the shared retail winner."""
    banks=defaultdict(list)
    for q in records:banks[q.bank].append(q)
    order={bank:i for i,bank in enumerate(banks)}
    winners={bank:personal_quote(qs) for bank,qs in banks.items()}
    ranked=sorted(banks,key=lambda bank:(winners[bank] is None,
                  -winners[bank].rate if winners[bank] else Decimal(0),order[bank]))
    selected=[]
    for bank in ranked:
        pick=winners[bank]
        if pick:selected.append(pick)
        selected.extend(q for q in banks[bank] if q is not pick)
    return selected


def source_sections(reader):
    source = reader.book
    sgd = reader.blocks(source.sheets[BUSINESS[0]],2,source.sheets[BUSINESS[0]].maxrow+1,'SGD','促销')
    board = reader.blocks(source.sheets[BUSINESS[1]],2,source.sheets[BUSINESS[1]].maxrow+1,'SGD','挂牌')
    fx = source.sheets[BUSINESS[2]]
    titles = []
    for r in range(1,fx.maxrow+1):
        label = text(fx.value(r,1))
        match = re.match(r'([A-Z]{3}) (Promo Rate|Board Rates)', label)
        if match:
            if match[1] not in ('USD','CNY','AUD','EUR','GBP','NZD','CAD','HKD','CHF'):
                raise ValueError(f'Unsupported currency section: {label}; add a layout mapping first')
            titles.append((r,match[1], '促销' if match[2].startswith('Promo') else '挂牌'))
        elif label == 'Other Currency Rates':
            titles.append((r,'OTHER','混合'))
    sections, matrices = {}, {}
    for i,(start,currency,category) in enumerate(titles):
        end = titles[i+1][0] if i+1<len(titles) else fx.maxrow+1
        if currency == 'OTHER' or (currency=='CNY' and category=='挂牌'):
            rows = defaultdict(dict)
            first = start+3 if currency=='OTHER' else start+2
            columns = [(2+j,cur,ten) for j,(cur,ten) in enumerate(( (cu,t) for cu in ('AUD','NZD','EUR','GBP') for t in ('1M','3M','6M')))] if currency=='OTHER' else [(c,'CNY',text(fx.value(start+1,c))) for c in range(2,9)]
            for r in range(first,end):
                bank = text(fx.value(r,1))
                if not bank:
                    continue
                valid = False
                for c,cu,ten in columns:
                    if rate_value(fx.value(r,c)) is None:
                        continue
                    valid = True
                    formula = fx.cells[r,c].findtext(M+'f','')
                    cat = '促销' if '外币促销_' in formula else '挂牌'
                    caption = ''
                    if currency=='CNY':
                        caption = next((v for v in text(fx.value(r,9)).splitlines() if v.startswith(ten+'：')), '')
                    q = reader.quote(fx,r,c,bank,cu,cat,ten,caption,expand_ties=True)
                    old = rows[q.bank].get((cu,ten))
                    if old is None or q.rate>old.rate or (q.rate==old.rate and q.minimum_key<old.minimum_key):
                        rows[q.bank][cu,ten] = q
                if not valid and bank not in ('有促销取促销，无促销再取挂牌；CIMB 外币多为网上存储',):
                    if len(bank)<40:
                        reader.audit['missing_removed'].append({'sheet':fx.name,'cell':address(r,1),'bank':bank})
            matrices[currency] = rows
        else:
            sections[currency,category] = reader.blocks(fx,start+1,end,currency,category)
    supported = {('USD','促销'),('USD','挂牌'),('CNY','促销'),('AUD','促销'),('EUR','促销'),('GBP','促销'),('NZD','促销'),('CAD','促销'),('HKD','促销'),('CHF','促销')}
    if set(sections)-supported or ('USD','挂牌') not in sections or set(matrices) != {'CNY','OTHER'}:
        raise ValueError('Missing or unsupported currency sections; refusing partial output')
    # A promotion section may disappear when its last product is withdrawn.
    for key in supported:
        sections.setdefault(key,{})
    for label,blocks,allowed in [('SGD promo',sgd,{'1M','3M','6M','9M','12M','18M','24M'}),
                                 ('SGD board',board,{'7D','1M','3M','6M','9M','12M','18M','24M','36M'})] + [
                                  (f'{cu} {kind}',blocks,{'1M','3M','6M','9M','12M','18M','24M'} if cu=='CNY' else {'7D','1M','3M','6M','9M','12M'})
                                  for (cu,kind),blocks in sections.items()]:
        if any(not TENOR.fullmatch(t) for t in blocks):
            raise ValueError(f'Invalid tenor in {label}: {list(blocks)}')
    return sgd, board, sections, matrices


def complete_board_sections(reader):
    """Read each current verified board row once, including nonstandard tenors."""
    prefix='挂牌全批_'+reader.as_of.strftime('%Y%m%d')
    sources=[s for s in reader.book.sheets.values() if s.name.startswith(prefix)]
    if not sources:return None
    detail=sources[-1];blocks=defaultdict(lambda:defaultdict(list))
    for row in range(4,detail.maxrow+1):
        rate=rate_value(detail.value(row,9))
        currency=text(detail.value(row,2));rate_col=9
        if rate is None and currency=='CHF' and text(detail.value(row,14))=='现有模板无CHF区域，仅留明细':
            rate=rate_value(detail.value(row,10));rate_col=10
        if rate is None:continue
        currency='CNY' if currency=='CNH' else currency
        tenor=text(detail.value(row,5))
        if not TENOR.fullmatch(tenor):raise ValueError('Invalid verified board tenor: '+tenor)
        reader.order+=1
        q=Quote(bank_label(detail.value(row,1)),tenor,currency,'挂牌',rate,
                actual=tenor,published_cell=f'{detail.name}!{address(row,rate_col)}',order=reader.order)
        q=reader.from_detail(q,detail,row,'')
        blocks[currency][tenor].append(q)
    return blocks


def verify_source_quote_coverage(reader,selected):
    """Compare visible quote identities with current input sheets, not parsed views."""
    result=[];stamp=reader.as_of.strftime('%Y%m%d')
    for prefix,column in (('明细_',8),('挂牌全批_',9),('外币促销_',9)):
        sources=[sheet for sheet in reader.book.sheets.values() if sheet.name.startswith(prefix+stamp)]
        if not sources:raise ValueError('Missing current verified source sheet: '+prefix+stamp)
        source=sources[-1];expected=set()
        for row in range(4,source.maxrow+1):
            rate=rate_value(source.value(row,column))
            if rate is None and prefix=='挂牌全批_' and text(source.value(row,2))=='CHF' and text(source.value(row,14))=='现有模板无CHF区域，仅留明细':rate=rate_value(source.value(row,10))
            if rate is not None:expected.add((source.name,row))
        actual=Counter((q['source'],q['source_row']) for q in selected if q['source'].startswith(prefix))
        for q in selected:
            if q['source'].startswith(prefix) and q['actual']!=q['tenor']:
                raise ValueError('Visible public quotes use the wrong actual tenor: '+str((q['source'],q['source_row'],q['actual'],q['tenor'])))
        missing=expected-set(actual);extra=set(actual)-expected
        repeated=[key for key,count in actual.items() if prefix=='明细_' and count!=1]
        if missing or extra or repeated:raise ValueError('Visible public quotes differ from current source rows: '+str(dict(source=source.name,missing=sorted(missing),extra=sorted(extra),repeated=repeated)))
        result.append(dict(source=source.name,expected=len(expected),displayed_unique=len(actual),displayed_cells=sum(actual.values()),passed=True))
    return result


class Painter:
    def __init__(self, book, template):
        self.book, self.template = book, template
        self.styles = StyleMap(book,template)
        self.cache, self.comments, self.ranges, self.rate_cells = {}, defaultdict(dict), [], []
        self.bank_groups = []
        self.widths = {}

    def style(self, name, r, c, role='text', bank='', fill_from=None):
        source = self.template.sheets[name].cells.get((r,c))
        if source is None:
            raise ValueError(f'Reference style missing: {name}!{address(r,c)}')
        base = int(source.get('s',0))
        key = name,base,role,bank=='BOC',fill_from
        if key in self.cache:
            return self.cache[key]
        root = self.styles.root
        xf = deepcopy(root.find(M+'cellXfs')[base])
        font = deepcopy(root.find(M+'fonts')[int(xf.get('fontId','0'))])
        for n in list(font):
            if local(n.tag)=='color': font.remove(n)
        ET.SubElement(font,M+'color',rgb='FFFF0000' if bank=='BOC' else 'FF000000')
        fonts = root.find(M+'fonts'); xf.set('fontId',str(len(fonts))); fonts.append(font);fonts.set('count',str(len(fonts)))
        if fill_from is not None:
            xf.set('fillId',root.find(M+'cellXfs')[fill_from].get('fillId'))
        if role!='header':
            # A source row may be the top half of a vertically merged quote.
            # Every new row is complete, so each cell needs all four edges.
            borders=root.find(M+'borders'); border=ET.Element(M+'border')
            for edge in ('left','right','top','bottom'):
                ET.SubElement(ET.SubElement(border,M+edge,style='thin'),M+'color',rgb='FF000000')
            xf.set('borderId',str(len(borders)));borders.append(border);borders.set('count',str(len(borders)))
        xfs=root.find(M+'cellXfs'); idx=len(xfs);xfs.append(xf);xfs.set('count',str(len(xfs)))
        idx=self.styles.customize(idx,format_code='0.00%' if role=='rate' else 'General',wrap=True,alignment='center')
        self.cache[key]=idx
        return idx

    def reset(self, sheet, lastcol, freeze_row):
        # The supported templates have no drawings/tables; fail rather than
        # silently break linked objects if a future template adds them.
        if any(sheet.root.find(M+x) is not None for x in ('drawing','tableParts','pivotTableParts')):
            raise ValueError(f'Unsupported reference objects on {sheet.name}')
        sheet.rows, sheet.cells = {}, {}
        for element in list(sheet.root):
            if local(element.tag) not in ('sheetPr','dimension','sheetViews','sheetFormatPr','cols','sheetData','pageMargins','pageSetup'):
                sheet.root.remove(element)
        columns=sheet.root.find(M+'cols')
        self.widths[sheet.name]={}
        for n in list(columns):
            if int(n.get('min'))>lastcol:
                columns.remove(n);continue
            n.set('max',str(min(int(n.get('max')),lastcol)))
            n.attrib.pop('style',None)
            for c in range(int(n.get('min')),int(n.get('max'))+1):
                self.widths[sheet.name][c]=float(n.get('width','10'))
        views=sheet.root.find(M+'sheetViews');views[:]=[]
        view=ET.SubElement(views,M+'sheetView',workbookViewId='0',showGridLines='0',zoomScale='70',topLeftCell='A1')
        ET.SubElement(view,M+'pane',ySplit=str(freeze_row),topLeftCell=f'A{freeze_row+1}',activePane='bottomLeft',state='frozen')
        ET.SubElement(view,M+'selection',pane='bottomLeft',activeCell=f'A{freeze_row+1}',sqref=f'A{freeze_row+1}')
        setup=sheet.root.find(M+'pageSetup')
        if setup is not None:
            setup.attrib.clear();setup.attrib.update(orientation='landscape',paperSize='8',fitToWidth='1',fitToHeight='0')
        sheet.node.set('state','visible')

    def widen(self, sheet, c, width):
        self.widths[sheet.name][c]=width
        cols=sheet.root.find(M+'cols')
        # Rebuild explicit columns, avoiding overlapping ranges.
        cols[:]=[]
        for n,w in sorted(self.widths[sheet.name].items()):
            ET.SubElement(cols,M+'col',min=str(n),max=str(n),width=str(w),customWidth='1')

    def merge(self, sheet, r, c1, c2):
        if c1==c2:return
        merges=sheet.root.find(M+'mergeCells')
        if merges is None:
            merges=ET.Element(M+'mergeCells');sheet.insert_element(merges)
        ET.SubElement(merges,M+'mergeCell',ref=f'{address(r,c1)}:{address(r,c2)}')
        merges.set('count',str(len(merges)))

    def merge_bank(self, sheet, c, first, last, bank, tenor, quotes, personal_rank=False):
        if first < last:
            merges=sheet.root.find(M+'mergeCells')
            if merges is None:
                merges=ET.Element(M+'mergeCells');sheet.insert_element(merges)
            ET.SubElement(merges,M+'mergeCell',ref=f'{address(first,c)}:{address(last,c)}')
            merges.set('count',str(len(merges)))
            for r in range(first+1,last+1):
                sheet.put(r,c,None)
        ranking=max((q.rate for q in quotes if not personal_rank or customer_is_personal(q.audience)),default=None)
        self.bank_groups.append({'sheet':sheet.name,'tenor':tenor,'bank':bank,'column':c,
                                 'currency':quotes[0].currency,'category':quotes[0].category,
                                 'first':first,'last':last,'highest_rate':max(q.rate for q in quotes),
                                 'ranking_basis':'personal' if personal_rank else 'highest','ranking_rate':ranking})

    def fit(self, sheet, row, cells, base, span=None):
        required=base
        for c,value,style in cells:
            xf=self.styles.root.find(M+'cellXfs')[style]
            font=self.styles.root.find(M+'fonts')[int(xf.get('fontId'))]
            size=float(font.find(M+'sz').get('val'))
            width=sum(self.widths[sheet.name].get(x,10) for x in range(c,span.get(c,c)+1)) if span else self.widths[sheet.name].get(c,10)
            capacity=max(3,(width*7-7)/(size*.47*1.333))
            lines=sum(max(1,math.ceil(sum(2 if unicodedata.east_asian_width(ch) in ('W','F') else 1 for ch in part)/capacity)) for part in str(value or '').split('\n'))
            required=max(required,lines*(size*1.18)+4)
        sheet.height(row,round(required,2))

    def record(self,sheet,r,c,q):
        note=f'{q.category}；{q.currency} {q.tenor}；{q.amount}'
        if q.audience: note+='；'+('Personal' if customer_is_personal(q.audience) else q.audience)
        ch=channel_label(q.channel)
        if ch!='渠道未列明':note+='；'+ch
        if q.notes:note+='\n'+q.notes
        self.comments[sheet.name][address(r,c)]=note
        self.rate_cells.append({'sheet':sheet.name,'cell':address(r,c),**asdict(q)})

    def blocks(self,sheet,header,firstcol,blocks,reference_header,reference_data,reference_cols,condition=False,base=18,group_banks=False,personal_rank=False):
        length=max((len(v) for v in blocks.values()),default=0)
        for i,(tenor,quotes) in enumerate(blocks.items()):
            c=firstcol+i*3;rc=reference_cols.get(tenor,next(iter(reference_cols.values())))
            def has_reference(column):
                return all((row,column+off) in self.template.sheets[sheet.name].cells for row in (reference_header,reference_data) for off in range(3))
            if not has_reference(rc):
                rc=next((column for column in reference_cols.values() if has_reference(column)),None)
                if rc is None:raise ValueError('Reference lacks a complete quote style triple: '+sheet.name)
            for off,width in enumerate((12,12,44 if condition else 28)):self.widen(sheet,c+off,width)
            for off,value in enumerate((tenor,'Promo Rate' if condition else 'Highest Rate','Condition' if condition else 'Amount')):
                sid=self.style(sheet.name,reference_header,rc+off,'header')
                sheet.put(header,c+off,value,sid)
            for j,q in enumerate(quotes,header+1):
                cells=[]
                for off,value in enumerate((q.bank,q.rate,q.condition)):
                    sid=self.style(sheet.name,reference_data,rc+off,'rate' if off==1 else 'text',q.bank)
                    sheet.put(j,c+off,value,sid);cells.append((c+off,value,sid))
                current=float(sheet.rows[j].get('ht',base))
                self.fit(sheet,j,cells,max(base,current))
                self.record(sheet,j,c+1,q)
            if group_banks:
                start=0
                while start<len(quotes):
                    end=start+1
                    while end<len(quotes) and quotes[end].bank==quotes[start].bank:
                        end+=1
                    self.merge_bank(sheet,c,header+1+start,header+end,quotes[start].bank,tenor,quotes[start:end],personal_rank)
                    start=end
        header_cells=[(c,sheet.value(header,c),int(n.get('s'))) for (r,c),n in sheet.cells.items() if r==header]
        self.fit(sheet,header,header_cells,base)
        self.ranges.append({'sheet':sheet.name,'start':header,'end':header+length,'last':firstcol+3*len(blocks)-1})
        return header+length+3

    def title(self,sheet,row,label):
        sid=self.style(BUSINESS[2],1,6,'header')
        sheet.put(row,2,label,sid);self.merge(sheet,row,2,8);sheet.height(row,22)

    def matrix(self,sheet,start,title,rows,other=False):
        self.title(sheet,start,title)
        header=start+2;first=header+(2 if other else 1)
        dimensions=[(cu,t) for cu in ('AUD','NZD','EUR','GBP') for t in ('1M','3M','6M')] if other else [('CNY',t) for t in ('1M','3M','6M','9M','12M','18M','24M')]
        amount_col,amount_end,notes_col,notes_end=(15,17,18,19) if other else (10,13,14,19)
        hsid=self.style(sheet.name,25 if other else 37,2,'header')
        for c in range(2,notes_end+1):sheet.put(header,c,None,hsid)
        sheet.put(header,2,'Bank',hsid)
        for i,(cu,tenor) in enumerate(dimensions,3):
            if other:
                sheet.put(header+1,i,tenor,hsid)
                if tenor=='1M':sheet.put(header,i,cu,hsid);self.merge(sheet,header,i,i+2)
            else:sheet.put(header,i,tenor,hsid)
        for c,end,label in [(amount_col,amount_end,'Minimum Amount'),(notes_col,notes_end,'Remarks')]:
            sheet.put(header,c,label,hsid);self.merge(sheet,header,c,end)
        sheet.height(header,22)
        if other:sheet.height(header+1,18)
        # Matrices have one bank row spanning different tenors. Keep reference
        # bank order, append newly collected banks deterministically.
        ref=self.template.sheets[sheet.name]
        reforder=[bank_label(ref.value(r,2)) for r in (range(27,35) if other else range(38,45))]
        banks=sorted(rows,key=lambda b:(reforder.index(b) if b in reforder else 999,b))
        for r,bank in enumerate(banks,first):
            quotes=rows[bank]
            if not quotes:continue
            sid=self.style(sheet.name,27,2,'text',bank)
            rateid=self.style(sheet.name,27,3,'rate',bank)
            cells=[(2,bank,sid)];sheet.put(r,2,bank,sid)
            amounts=[];note_groups=defaultdict(list)
            for c,key in enumerate(dimensions,3):
                q=quotes.get(key)
                sheet.put(r,c,q.rate if q else None,rateid)
                if q:
                    self.record(sheet,r,c,q)
                    if q.amount not in amounts:amounts.append(q.amount)
                    note_groups[q.category].append(key)
            amount='\n'.join(amounts)
            if len(note_groups)==1:
                remarks=next(iter(note_groups))+'；门槛/资格见利率批注'
            else:remarks='促销优先；挂牌补充\n门槛/资格见利率批注'
            if any('公告已截止' in q.notes for q in quotes.values()):remarks+='\n含已截止公开报价'
            for c,end,value in [(amount_col,amount_end,amount),(notes_col,notes_end,remarks)]:
                for cc in range(c,end+1):sheet.put(r,cc,value if cc==c else None,sid)
                self.merge(sheet,r,c,end);cells.append((c,value,sid))
            self.fit(sheet,r,cells,18,{amount_col:amount_end,notes_col:notes_end})
        end=first+len(banks)-1
        self.ranges.append({'sheet':sheet.name,'start':start,'end':end,'last':notes_end})
        return end+3


def add_comments(book, comments):
    v='{urn:schemas-microsoft-com:vml}';o='{urn:schemas-microsoft-com:office:office}';x='{urn:schemas-microsoft-com:office:excel}'
    types=book.xml('[Content_Types].xml')
    if not any(n.get('Extension')=='vml' for n in types):
        ET.SubElement(types,CT+'Default',Extension='vml',ContentType='application/vnd.openxmlformats-officedocument.vmlDrawing')
    for index,(name,notes) in enumerate(comments.items(),1):
        sheet=book.sheets[name]
        cp=f'xl/comments/rainbow{index}.xml';vp=f'xl/drawings/rainbow{index}.vml'
        root=ET.Element(M+'comments');ET.SubElement(ET.SubElement(root,M+'authors'),M+'author').text='User'
        listing=ET.SubElement(root,M+'commentList')
        drawing=ET.Element('xml')
        shape_type=ET.SubElement(drawing,v+'shapetype',id='_x0000_t202',coordsize='21600,21600',attrib={o+'spt':'202', 'path':'m,l,21600r21600,l21600,xe'})
        ET.SubElement(shape_type,v+'stroke',joinstyle='miter');ET.SubElement(shape_type,v+'path',gradientshapeok='t',attrib={o+'connecttype':'rect'})
        for i,(ref,note) in enumerate(notes.items(),1025):
            item=ET.SubElement(listing,M+'comment',ref=ref,authorId='0')
            ET.SubElement(ET.SubElement(item,M+'text'),M+'t').text=note
            r,c=pos(ref)
            shape=ET.SubElement(drawing,v+'shape',id=f'_x0000_s{i}',type='#_x0000_t202',style='position:absolute;width:330pt;height:110pt;visibility:hidden',fillcolor='#ffffe1',attrib={o+'insetmode':'auto'})
            ET.SubElement(shape,v+'fill',color2='#ffffe1');ET.SubElement(shape,v+'shadow',on='t',color='black',obscured='t')
            ET.SubElement(ET.SubElement(shape,v+'textbox',style='mso-direction-alt:auto'),'div',style='text-align:left')
            client=ET.SubElement(shape,x+'ClientData',ObjectType='Note')
            ET.SubElement(client,x+'MoveWithCells');ET.SubElement(client,x+'SizeWithCells')
            ET.SubElement(client,x+'Anchor').text=f'{c}, 10, {r-1}, 2, {c+4}, 20, {r+6}, 4'
            ET.SubElement(client,x+'AutoFill').text='False';ET.SubElement(client,x+'Row').text=str(r-1);ET.SubElement(client,x+'Column').text=str(c-1)
        book.parts[cp]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        book.parts[vp]=ET.tostring(drawing,encoding='utf-8',xml_declaration=True)
        ET.SubElement(types,CT+'Override',PartName='/'+cp,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml')
        relpath=posixpath.join(posixpath.dirname(sheet.path),'_rels',posixpath.basename(sheet.path)+'.rels')
        rels=ET.Element(P+'Relationships')
        for rid,typ,target in [('rIdComments','comments',cp),('rIdNotes','vmlDrawing',vp)]:
            ET.SubElement(rels,P+'Relationship',Id=rid,Type=R[1:-1]+'/'+typ,Target=posixpath.relpath(target,posixpath.dirname(sheet.path)))
        book.parts[relpath]=ET.tostring(rels,encoding='utf-8',xml_declaration=True)
        book.roots.pop(relpath,None)
        sheet.insert_element(ET.Element(M+'legacyDrawing',{R+'id':'rIdNotes'}))


def validate_sgd_selection(audit):
    """All quote identities, including equal rates with different terms, survive."""
    def identity(q):
        return tuple(q.get(k) for k in ('bank','tenor','source','source_row','published_cell','rate',
                                       'audience','minimum','maximum','amount_currency','lower','upper','channel','actual','product'))
    original=[q for q in audit['candidates'] if q['currency']=='SGD' and q['category']=='促销']
    retained=[q for q in audit['selected'] if q['sheet']==BUSINESS[0]]
    if Counter(map(identity,original))!=Counter(map(identity,retained)):
        raise ValueError('Public SGD quotes or their conditions differ from verified source rows')


def validate_output(path, audit):
    book=Book(path)
    if tuple(book.sheets)!=BUSINESS:raise ValueError('Unexpected output sheets/order')
    if any(n.startswith('xl/externalLinks/') for n in book.parts):raise ValueError('External links remain')
    for sheet in book.sheets.values():
        if any(float(row.get('ht','15'))>409.5 for row in sheet.rows.values()):raise ValueError('Formatted row exceeds native Excel height limit: '+sheet.name)
        for node in sheet.cells.values():
            if node.find(M+'f') is not None or node.get('t')=='e':raise ValueError('Output formula/error remains')
    for q in audit['selected']:
        r,c=pos(q['cell']);v=book.sheets[q['sheet']].value(r,c)
        if not numeric(v) or v!=q['rate']:raise ValueError(f'Numeric rate mismatch: {q["sheet"]}!{q["cell"]}')
        if q['sheet']==BUSINESS[0]:
            source_quote=Quote(**{key:q[key] for key in Quote.__dataclass_fields__ if key in q})
            if book.sheets[q['sheet']].value(r,c+1)!=source_quote.condition:
                raise ValueError(f'Condition differs from selected SGD quote: {q["cell"]}')
    by_tenor=defaultdict(list)
    for g in audit.get('bank_groups',[]):
        sheet=book.sheets[g['sheet']];first,last,c=g['first'],g['last'],g['column']
        if sheet.value(first,c)!=g['bank'] or any(sheet.value(r,c) is not None for r in range(first+1,last+1)):
            raise ValueError(f'Bank must appear in one cell per block: {g}')
        if first<last and f'{address(first,c)}:{address(last,c)}' not in {m.get('ref') for m in sheet.root.findall(M+'mergeCells/'+M+'mergeCell')}:
            raise ValueError(f'Bank block merge is missing: {g}')
        rows=[q for q in audit['selected'] if q['sheet']==g['sheet'] and q['bank']==g['bank'] and q['tenor']==g['tenor'] and q['currency']==g['currency'] and q['category']==g['category']]
        eligible=[q['rate'] for q in rows if g['ranking_basis']!='personal' or customer_is_personal(q['audience'])]
        if g['ranking_rate']!=max(eligible,default=None):
            raise ValueError(f'Bank ranking disagrees with eligible quotes: {g}')
        by_tenor[g['sheet'],g['currency'],g['category'],g['tenor']].append(g)
    for scope,groups in by_tenor.items():
        rates=[g['ranking_rate'] for g in groups]
        expected=sorted(rates,key=lambda rate:(rate is None,-rate if rate is not None else Decimal(0)))
        if len({g['bank'] for g in groups})!=len(groups) or rates!=expected:
            raise ValueError(f'Bank blocks are duplicated or ranked against their stated basis: {scope}')
    validate_sgd_selection(audit)
    audit['validation']={'business_sheets':3,'numeric_rate_cells':len(audit['selected']),'formulas':0,'external_links':0,'errors':0,'passed':True}


def format_rainbow(input_path, template_path, output_path, *, audit_path=None, dry_run=False):
    input_path,template_path,output_path=map(Path,(input_path,template_path,output_path))
    audit_path=Path(audit_path) if audit_path else output_path.with_suffix('.audit.json')
    protected={input_path.resolve(),template_path.resolve()}
    if output_path.resolve() in protected or audit_path.resolve() in protected or audit_path.resolve()==output_path.resolve():
        raise ValueError('Output/audit paths must differ from each other and both inputs')
    if output_path.exists() or audit_path.exists():raise FileExistsError('Output or audit already exists; choose a new output path')
    audit={'input':str(input_path.resolve()),'template':str(template_path.resolve()),'output':str(output_path.resolve()),
           'sgd_promo_policy':SGD_PROMO_POLICY,
           'input_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest(),
           'template_sha256':hashlib.sha256(template_path.read_bytes()).hexdigest(),
           'candidates':[],'selected':[],'ties':[],'excluded_audience':[],'excluded_personal_quotes':[],'missing_removed':[],'issues':[]}
    reader=Reader(Book(input_path),audit)
    sgd,board,sections,matrices=source_sections(reader)
    complete=complete_board_sections(reader)
    if complete is not None:
        board=dict(complete.pop('SGD',{}))
        for currency,blocks in complete.items():sections[currency,'挂牌']=dict(blocks)
    sgd={t:select_sgd_promotions(q,audit) for t,q in sgd.items()}
    board={t:select_sgd_promotions(q,audit) for t,q in board.items()}
    sections={key:{t:select_sgd_promotions(q,audit) for t,q in blocks.items()} for key,blocks in sections.items()}
    sgd=dict(sorted(sgd.items(),key=lambda kv:tenor_order(kv[0])))
    board=dict(sorted(board.items(),key=lambda kv:tenor_order(kv[0])))
    sections={k:dict(sorted(v.items(),key=lambda kv:tenor_order(kv[0]))) for k,v in sections.items()}
    # Keep composite promo values consistent with each independently filtered
    # promotional block; use its board cache only when no promo is available.
    for cu in ('AUD','NZD','EUR','GBP'):
        for tenor,quotes in sections[cu,'促销'].items():
            if tenor in ('1M','3M','6M'):
                for bank in dict.fromkeys(q.bank for q in quotes):
                    q=personal_quote([q for q in quotes if q.bank==bank])
                    if q:matrices['OTHER'][q.bank][cu,tenor]=q
    cny_promo=defaultdict(dict)
    for ten,quotes in sections['CNY','促销'].items():
        for bank in dict.fromkeys(q.bank for q in quotes):
            q=personal_quote([q for q in quotes if q.bank==bank])
            if q:cny_promo[q.bank]['CNY',ten]=q
    template=Book(template_path);target=Book(template_path)
    for name in list(target.sheets):
        if name not in BUSINESS:target.drop_sheet(name)
    if set(target.sheets)!=set(BUSINESS):raise ValueError('Reference lacks required business sheets')
    # Reorder intentionally and remove obsolete print names/local sheet indices.
    nodes=target.workbook.find(M+'sheets');nodes[:]=[target.sheets[n].node for n in BUSINESS]
    target.sheets={n:target.sheets[n] for n in BUSINESS}
    names=target.workbook.find(M+'definedNames')
    if names is not None:target.workbook.remove(names)
    target.detach_external([n for n in template.sheets if n not in BUSINESS])
    for view in target.workbook.findall(M+'bookViews/'+M+'workbookView'):view.set('activeTab','0');view.set('firstSheet','0')
    p=Painter(target,template)
    sh=target.sheets[BUSINESS[0]];p.reset(sh,max(22,1+3*len(sgd)),3)
    p.widen(sh,1,12)
    date_style=p.styles.customize(int(template.sheets[BUSINESS[0]].cells[1,1].get('s',0)),format_code='yyyymmdd',wrap=False)
    sh.put(1,1,serial(reader.as_of),date_style);sh.height(1,22)
    p.blocks(sh,3,2,sgd,4,5,{t:2+i*3 for i,t in enumerate(('1M','3M','6M','9M','12M','18M','24M'))},condition=True,base=24,group_banks=True,personal_rank=True)
    sh=target.sheets[BUSINESS[1]];p.reset(sh,max(27,3*len(board)),3)
    title_style=int(template.sheets[BUSINESS[1]].cells[3,1].get('s',0))
    sh.put(1,1,f'SGD BOARD RATES  {reader.as_of.isoformat()}',title_style);p.merge(sh,1,1,6);sh.height(1,22)
    # The reference's 7D bank column is unusually narrow; make its longest bank
    # readable while keeping all other template column widths.
    p.widen(sh,1,max(p.widths[sh.name][1],11.57))
    p.blocks(sh,3,1,board,5,6,{t:1+i*3 for i,t in enumerate(('7D','1M','3M','6M','9M','12M','18M','24M','36M'))},base=14.45,group_banks=True,personal_rank=True)
    sh=target.sheets[BUSINESS[2]];p.reset(sh,max([19]+[4+3*len(v) for v in sections.values()]),3)
    p.widen(sh,2,12)
    fxcols={'7D':2,'1M':5,'3M':8,'6M':11,'9M':14,'12M':17}
    p.title(sh,1,'USD Promo Rates  '+reader.as_of.isoformat())
    nextrow=p.blocks(sh,3,5,sections['USD','促销'],3,14,fxcols,base=18,group_banks=True,personal_rank=True)
    p.title(sh,nextrow,'USD Board Rates')
    nextrow=p.blocks(sh,nextrow+2,2,sections['USD','挂牌'],13,14,fxcols,base=18,group_banks=True,personal_rank=True)
    nextrow=p.matrix(sh,nextrow,'Other Currency Rates',matrices['OTHER'],other=True)
    nextrow=p.matrix(sh,nextrow,'RMB Board Rates（含CNH）',matrices['CNY'])
    for currency in ('CNY','AUD','NZD','CAD','HKD','EUR','GBP','CHF'):
        if not any(sections.get((currency,'挂牌'),{}).values()):continue
        p.title(sh,nextrow,currency+' Board Rates — 全部公开报价')
        nextrow=p.blocks(sh,nextrow+2,2,sections[currency,'挂牌'],13,14,fxcols,condition=True,base=18,group_banks=True,personal_rank=True)
    p.title(sh,nextrow,'RMB Promo Rates（含CNH）')
    nextrow=p.blocks(sh,nextrow+2,2,sections['CNY','促销'],3,14,fxcols,condition=True,base=18,group_banks=True,personal_rank=True)
    for currency in ('AUD','EUR','GBP','NZD','CAD','HKD','CHF'):
        if not any(sections[currency,'促销'].values()):
            continue
        p.title(sh,nextrow,currency+' Promo Rates')
        # Use the same tenor colors as USD; compact contiguous blocks.
        nextrow=p.blocks(sh,nextrow+2,5,sections[currency,'促销'],3,14,fxcols,base=18,group_banks=True,personal_rank=True)
    audit['selected']=p.rate_cells;audit['preview_ranges']=p.ranges;audit['bank_groups']=p.bank_groups
    audit['source_quote_coverage']=verify_source_quote_coverage(reader,p.rate_cells)
    audit['counts']={'sgd_promo':sum(map(len,sgd.values())),'sgd_board':sum(map(len,board.values())),
                     'fx_rate_cells':sum(q['sheet']==BUSINESS[2] for q in p.rate_cells),
                     'removed_source_sheets':len(reader.book.sheets)-3}
    if dry_run:
        validate_sgd_selection(audit)
        audit['dry_run']=True
        return audit
    add_comments(target,p.comments)
    output_path.parent.mkdir(parents=True,exist_ok=True);audit_path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rainbow-format-',dir=output_path.parent) as tmp:
        candidate=Path(tmp)/'candidate.xlsx';target.save(candidate);validate_output(candidate,audit)
        if hashlib.sha256(input_path.read_bytes()).hexdigest()!=audit['input_sha256'] or hashlib.sha256(template_path.read_bytes()).hexdigest()!=audit['template_sha256']:
            raise ValueError('An input changed during formatting; refusing to publish')
        # Exclusive creation protects an existing result even in concurrent runs.
        with output_path.open('xb') as out:out.write(candidate.read_bytes())
    with audit_path.open('x',encoding='utf-8') as out:json.dump(audit,out,ensure_ascii=False,indent=2,default=str)
    return audit


def main(argv=None):
    parser=argparse.ArgumentParser(description='按参考彩虹表样式整理采集工作簿；原文件不覆盖')
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--template',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--audit',type=Path)
    parser.add_argument('--dry-run',action='store_true',help='只解析、筛选和检查，不生成文件')
    args=parser.parse_args(argv)
    try:
        audit=format_rainbow(args.input,args.template,args.output,audit_path=args.audit,dry_run=args.dry_run)
    except (ValueError,FileExistsError,OSError) as exc:
        parser.exit(2,f'整理失败：{exc}\n')
    print(json.dumps({k:audit[k] for k in ('as_of','counts','validation','issues') if k in audit},ensure_ascii=False,indent=2,default=str))
    if not args.dry_run:print(f'输出：{args.output}\n检查报告：{args.audit or args.output.with_suffix(".audit.json")}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
