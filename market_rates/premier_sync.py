"""Rebuild Premier Rates from this run's verified customer-specific offers.

The historic worksheet is a style source only. Personal/all quotations never
become Premier quotations, and old account/AUM thresholds are not carried over.
This stage runs on the generated report, before the presentation formatter.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .common import load, save
from .manual_review import effective_offers
from .report_xml import Book, M, StyleMap, address, local, numeric
from .rules import active
from .workbook_policy import (bank_in_scope, currency_in_scope,
                              presentation_currency, promo_reference_allowed, workbook_policy)

SPECIAL_AUDIENCES = {
    'premier': 'Premier', 'preferred': 'Preferred', 'private': 'Private',
    'premier_elite': 'Premier Elite', 'premier_wealth': 'Premier（财富／FX资格）',
    'premier_standard': 'Premier（无财富持仓）',
}
# Reference columns; additional actual tenors are added from verified offers.
DEFAULT_TENORS = ('1M', '3M', '6M', '9M', '12M', '18M')
CURRENCY_TITLES = {'SGD': '新元', 'USD': '美元', 'CNY': '人民币', 'AUD': '澳元',
                   'EUR': '欧元', 'GBP': '英镑', 'NZD': '新西兰元'}


def tenor(offer):
    value = Decimal(str(offer['tenor_value']))
    if value <= 0 or value != int(value) or offer['tenor_unit'] not in ('M', 'D'):
        raise ValueError('Premier 期限必须为已核验的整月或天数')
    return str(int(value)) + offer['tenor_unit']


def money(value):
    return format(Decimal(str(value)), ',f').rstrip('0').rstrip('.') if '.' in str(value) else format(Decimal(str(value)), ',f')


def amount_text(offer):
    prefix = offer['amount_currency'] + (' 等值 ' if offer.get('amount_is_equivalent') else ' ')
    pieces = []
    if offer.get('amount_min') is not None:
        pieces.append(('≥' if offer.get('min_inclusive', True) else '>') + money(offer['amount_min']))
    if offer.get('amount_max') is not None:
        pieces.append(('≤' if offer.get('max_inclusive', False) else '<') + money(offer['amount_max']))
    return prefix + '；'.join(pieces) if pieces else '官网未列起存金额'


def audience_text(offer):
    if offer['bank'] == 'SCB':
        return 'Priority Private' if offer['audience'] == 'private' else 'Priority Banking'
    return SPECIAL_AUDIENCES[offer['audience']]


def select_offers(run, offers, policy=None):
    """Keep separate customer segments, expiry provenance and genuine zeroes."""
    policy = policy or workbook_policy()
    selected, excluded = [], []
    pages = {p['id']: p for p in run.get('pages', [])}
    for item in offers:
        if item.get('rate_type') != 'promo' or item.get('audience') not in SPECIAL_AUDIENCES:
            continue
        reason = None
        if not bank_in_scope(item['bank'], policy) or not currency_in_scope(item['currency'], policy):
            reason = '银行或币种不在已确认范围'
        elif item['product_id'] in policy.get('deferred_product_ids', []):
            reason = '用户已排除的独立产品'
        elif item['bank'] + '/' + item['product_id'] + '/' + tenor(item) in policy.get('deferred_actual_tenors', []):
            reason = '用户已排除的实际期限'
        reference = promo_reference_allowed(item, run['as_of'], policy)
        if reason is None and not (active(item, run['as_of']) or reference):
            reason = '本次未提供、尚未生效或过期且不允许作为参考'
        if reason:
            excluded.append(dict(bank=item['bank'], product_id=item['product_id'], audience=item['audience'],
                                 currency=item['currency'], tenor=tenor(item), reason=reason))
            continue
        rate = Decimal(str(item['rate_pct']))
        if not rate.is_finite() or rate < 0 or rate > 25:
            raise ValueError('Premier 无效百分数报价：' + str(item))
        urls = list(dict.fromkeys(pages.get(e['page_id'], {}).get('url', '') for e in item.get('evidence', [])))
        selected.append(dict(item, display_currency=presentation_currency(item['currency'], policy),
                             tenor=tenor(item), rate=str(rate / 100), reference_quote=bool(reference),
                             collected_on=run.get('bank_dates', {}).get(item['bank'], run['as_of']),
                             source_run=run.get('id', ''), urls=[u for u in urls if u]))
    return selected, excluded


def group_rows(offers):
    """Merge tenors only when actual product/customer/amount/channel agree."""
    groups = {}
    keys = ('display_currency', 'bank', 'product_id', 'audience', 'channel', 'amount_currency',
            'amount_is_equivalent', 'amount_min', 'amount_max', 'min_inclusive', 'max_inclusive',
            'fresh_funds', 'valid_from', 'valid_to', 'reference_quote', 'conditions')
    for offer in offers:
        key = tuple(str(offer.get(k, '')) for k in keys)
        group = groups.setdefault(key, dict(sample=offer, offers=[], rates={}))
        prior = group['rates'].get(offer['tenor'])
        value = Decimal(offer['rate'])
        if prior is not None and prior != value:
            # Identical eligibility cannot silently choose one contradictory
            # quote; source verification must resolve it.
            raise ValueError('Premier 同一产品同一条件存在冲突报价：' + offer['bank'] + '/' + offer['tenor'])
        group['rates'][offer['tenor']] = value
        if offer not in group['offers']:
            group['offers'].append(offer)
    return sorted(groups.values(), key=lambda g: (g['sample']['display_currency'] != 'SGD',
                  g['sample']['display_currency'] != 'USD', g['sample']['display_currency'],
                  g['sample']['bank'], g['sample']['audience'], g['sample']['product_id'],
                  Decimal(str(g['sample'].get('amount_min') or 0)), g['sample']['channel']))


def note_text(group):
    item = group['sample']
    channel = {'online': 'online', 'branch': 'counter', 'staff_instruction': '分行／客户经理指令',
               'branch_or_mobile': 'counter／手机银行', 'online_banking|sc_mobile': '网银／SC Mobile',
               'unknown': '官网未列明'}.get(item.get('channel'), item.get('channel') or '未注明')
    bits = [item['product_name'], '渠道：' + channel]
    if item.get('fresh_funds') == 'yes':
        bits.append('新资金')
    original = str(item.get('conditions') or '')
    conditions = re.sub(r'\s+', ' ', original).strip('；; ')
    if item['bank'] == 'SCB':
        qualified = []
        if re.search(r'Priority.*?(?:up till maturity|held until maturity)', original, re.I | re.S):
            qualified.append('须持有至到期，并全程保持对应 Priority 客户资格')
        if re.search(r'(?:withdrawn.*?re-deposited.*?30 days|30天内提取再存)', original, re.I | re.S):
            qualified.append('新资金不含本行转账及30天内提取再存')
        if re.search(r'validly existing.*?good standing', original, re.I | re.S):
            qualified.append('本行账户须有效且状态良好')
        if qualified:
            conditions = '；'.join(qualified)
    if conditions and conditions not in ('not stated', '条款待复核；not stated'):
        # Preserve qualification wording, while keeping long extraction text in
        # the machine-readable audit instead of producing unusable cell heights.
        bits.append(conditions if len(conditions) <= 500 else conditions[:497] + '…（完整原文见审计）')
    if item.get('valid_from') or item.get('valid_to'):
        bits.append('公告期：' + str(item.get('valid_from') or '未列起日') + '–' + str(item.get('valid_to') or '未列截止日'))
    if item.get('reference_quote'):
        bits.append('公告已截止；最新公开参考')
    bits.append('采集：' + item['collected_on'])
    return '；'.join(dict.fromkeys(bits))


def previous_banks(sheet):
    result = defaultdict(set)
    currency = None
    for row in range(1, sheet.maxrow + 1):
        value = str(sheet.value(row, 1) or '').strip()
        title = next((k for k, v in CURRENCY_TITLES.items() if value == v or value.startswith(v + ' ')), None)
        if title:
            currency = title
        elif currency and '本期未列入：' in value:
            # Keep the same source-gap notice if a completed stage is safely
            # retried against its own generated workbook.
            listed = value.split('本期未列入：', 1)[1].split('；', 1)[0]
            result[currency].update(listed.split('、'))
        elif currency and value and value != 'Bank' and re.fullmatch(r'[A-Za-z][A-Za-z /&.-]*', value):
            result[currency].add(value)
    return result


def rebuild_premier(book, offers, dates, audit):
    sheet = book.sheets.get('Premier Rates')
    if sheet is None:
        raise ValueError('调研底稿缺少 Premier Rates 工作表')
    old_banks = previous_banks(sheet)
    old_note_col = next((c for (r, c) in sheet.cells if r == 2 and sheet.value(r, c) == '其他'), 10)
    samples = {key: int(sheet.cell(*loc).get('s', '0')) for key, loc in {
        'title': (1, 1), 'header': (2, 2), 'rate_header': (2, 5), 'bank': (3, 1),
        'text': (3, 2), 'amount': (3, 3), 'rate': (3, 5), 'note': (3, old_note_col),
    }.items()}
    styles = StyleMap(book, book)
    style = {k: styles.customize(v, format_code='0.00%' if k == 'rate' else 'General',
                                wrap=True, alignment='center' if k in ('rate', 'bank', 'rate_header') else 'left')
             for k, v in samples.items()}
    from .premier_details import build_details,record_key,reference_formula,DETAIL_SHEET
    detail_last=build_details(book,offers,style)
    audit['detail_sheet']=DETAIL_SHEET
    for node in list(sheet.root):
        if local(node.tag) in ('mergeCells', 'conditionalFormatting', 'autoFilter', 'cols', 'rowBreaks', 'colBreaks'):
            sheet.root.remove(node)
        elif local(node.tag) in ('drawing', 'legacyDrawing', 'tableParts'):
            raise ValueError('Premier Rates 含图形、批注或表格，须先明确迁移规则')
    sheet.rows, sheet.cells = {}, {}
    groups = group_rows(offers)
    tenors = sorted(set(DEFAULT_TENORS) | {q['tenor'] for q in offers},
                    key=lambda t: (int(t[:-1]) * (30 if t[-1] == 'M' else 1), t[-1]))
    note_col = 4 + len(tenors)
    columns = ET.Element(M + 'cols')
    for c in range(1, note_col + 1):
        width = 16 if c == 1 else 32 if c == 2 else 28 if c == 3 else 78 if c == note_col else 12
        ET.SubElement(columns, M + 'col', min=str(c), max=str(c), width=str(width), customWidth='1')
    sheet.insert_element(columns)
    merges = ET.Element(M + 'mergeCells', count='0')
    currencies = sorted({'SGD', 'USD'} | {q['display_currency'] for q in offers},
                        key=lambda c: (c != 'SGD', c != 'USD', c))
    row = 1
    audit['sections'], audit['rate_cells'], audit['removed_stale_banks'] = [], [], []
    for currency in currencies:
        these = [g for g in groups if g['sample']['display_currency'] == currency]
        selected_banks = {g['sample']['bank'] for g in these}
        omitted = sorted(old_banks[currency] - selected_banks)
        title_row = row
        sheet.put(row, 1, CURRENCY_TITLES.get(currency, currency) + ' ' + dates.get(currency, max(dates.values())), style['title'])
        ET.SubElement(merges, M + 'mergeCell', ref=f'A{row}:{address(row, note_col)}')
        sheet.height(row, 26)
        row += 1
        for c, label in enumerate(['Bank', '准入门槛', 'Minimum Amount', *tenors, '其他'], 1):
            sheet.put(row, c, label, style['rate_header'] if 4 <= c < note_col else style['header'])
        sheet.height(row, 28)
        header_row = row
        bank_start, last_bank = None, None
        for group in these:
            row += 1
            item = group['sample']
            if item['bank'] != last_bank:
                if bank_start and row - 1 > bank_start:
                    ET.SubElement(merges, M + 'mergeCell', ref=f'A{bank_start}:A{row-1}')
                last_bank, bank_start = item['bank'], row
                sheet.put(row, 1, item['bank'], style['bank'])
            else:
                sheet.put(row, 1, None, style['bank'])
            sheet.put(row, 2, audience_text(item), style['text'])
            sheet.put(row, 3, amount_text(item), style['amount'])
            for c, t in enumerate(tenors, 4):
                value = group['rates'].get(t, '-')
                matches=[q for q in group['offers'] if q['tenor']==t]
                formula=reference_formula(record_key(matches[0]),detail_last) if matches else None
                sheet.put(row, c, value, style['rate'],formula=formula)
                if numeric(value):
                    audit['rate_cells'].append(dict(address=address(row, c), bank=item['bank'], currency=currency,
                                                    audience=item['audience'], tenor=t, expected=str(value),formula=formula,
                                                    detail_key=record_key(matches[0])))
            notes = note_text(group)
            sheet.put(row, note_col, notes, style['note'])
            # Font remains the template's 11 pt. English characters use about
            # half a Chinese character's width; reserve a little extra wrap space.
            weighted = sum(2 if ord(ch) > 255 else 1 for ch in notes)
            sheet.height(row, max(42, min(240, 15 * (weighted // 70 + 2))))
        if bank_start and row > bank_start:
            ET.SubElement(merges, M + 'mergeCell', ref=f'A{bank_start}:A{row}')
        if not these or omitted:
            row += 1
            notice = ('本期未取得特殊客群专属已核验报价。' if not these else '')
            if omitted:
                notice += '本期未列入：' + '、'.join(omitted) + '；无本期对应客群的已核验报价，旧报价已移除。'
            sheet.put(row, 1, notice, style['note'])
            ET.SubElement(merges, M + 'mergeCell', ref=f'A{row}:{address(row, note_col)}')
            sheet.height(row, 30)
        audit['sections'].append(dict(currency=currency, title_row=title_row, header_row=header_row,
                                      last_row=row, note_col=note_col, groups=len(these), tenors=tenors))
        audit['removed_stale_banks'] += [dict(currency=currency, bank=bank) for bank in omitted]
        row += 3
    merges.set('count', str(len(merges)))
    sheet.insert_element(merges)
    for view in sheet.root.findall(M + 'sheetViews/' + M + 'sheetView'):
        view.set('topLeftCell', 'A1')
        for child in list(view):
            if local(child.tag) in ('pane', 'selection'):
                view.remove(child)
        ET.SubElement(view, M + 'pane', ySplit='2', topLeftCell='A3', activePane='bottomLeft', state='frozen')
        ET.SubElement(view, M + 'selection', pane='bottomLeft', activeCell='A3', sqref='A3')
    sheet.sync()
    return sheet


def sync_premier(report_path, sgd_run, fx_run, *, audit_path=None):
    """Atomically rebuild Premier and its current-run detail in a report workbook."""
    path = Path(report_path).resolve()
    book = Book(path)
    offers, dates, sources, excluded = [], {}, [], []
    for source in (sgd_run, fx_run):
        source = Path(source).resolve()
        run_path = source if source.name.endswith('.json') else source / 'run.json'
        run = load(run_path)
        accepted, blocked, receipts = effective_offers(run)
        if blocked:
            raise ValueError('Premier 同步前须解决本批核验分歧：' + str(blocked))
        picked, rejected = select_offers(run, accepted)
        offers.extend(picked)
        excluded.extend(rejected)
        dates['SGD' if source == Path(sgd_run).resolve() else 'USD'] = run['as_of']
        dates.update({p['display_currency']: run['as_of'] for p in picked})
        sources.append(dict(path=str(run_path), as_of=run['as_of'],
                            sha256=hashlib.sha256(run_path.read_bytes()).hexdigest(), selected=len(picked)))
    audit = dict(version=2, sheet='Premier Rates', sources=sources, offers=offers, excluded=excluded,
                 policy='本期已核验的特殊客群报价；不沿用底稿旧数值，不以Personal或all补Premier；实际期限自动扩展')
    rebuild_premier(book, offers, dates, audit)
    calc=book.workbook.find(M+'calcPr')
    if calc is None:calc=ET.SubElement(book.workbook,M+'calcPr')
    calc.set('calcMode','auto');calc.set('fullCalcOnLoad','1')
    # The template dates used formulas at J1/F23. After replacing those cells,
    # Excel rejects a calcChain still referring to their removed formulas.
    # Discard only its rebuildable dependency cache; keep every other sheet's
    # formula and saved value, external links and workbook metadata intact.
    for relation in list(book.rels):
        if relation.get('Type', '').endswith('/calcChain'):
            part = book.resolve('xl/workbook.xml', relation.get('Target'))
            book.parts.pop(part, None)
            book.roots.pop(part, None)
            book.rels.remove(relation)
            types = book.xml('[Content_Types].xml')
            for entry in list(types):
                if entry.get('PartName', '').lstrip('/') == part:
                    types.remove(entry)
    with tempfile.NamedTemporaryFile(prefix='.premier-', suffix='.xlsx', dir=path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        book.save(temp_path)
        readback = Book(temp_path).sheets['Premier Rates']
        for cell in audit['rate_cells']:
            from .report_xml import pos
            actual = readback.value(*pos(cell['address']))
            if actual != Decimal(cell['expected']):
                raise ValueError('Premier 保存回读数值不一致：' + cell['address'])
        from .premier_details import validate_links
        audit['formula_links']=validate_links(Book(temp_path))
        if len(audit['formula_links'])!=len(audit['rate_cells']):raise ValueError('Premier 明细公式未完整生成')
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    audit.update(report=str(path), rate_count=len(audit['rate_cells']), group_count=sum(s['groups'] for s in audit['sections']),
                 output_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), passed=True)
    save(Path(audit_path) if audit_path else path.with_suffix('.premier-audit.json'), audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description='从本期已核验源同步 Premier Rates')
    parser.add_argument('--report', required=True)
    parser.add_argument('--sgd-run', required=True)
    parser.add_argument('--fx-run', required=True)
    parser.add_argument('--audit')
    args = parser.parse_args()
    result = sync_premier(args.report, args.sgd_run, args.fx_run, audit_path=args.audit)
    print(json.dumps({k: result[k] for k in ('passed', 'report', 'rate_count', 'group_count')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
