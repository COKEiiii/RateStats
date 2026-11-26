"""Resolve the SGD detail's independent product catalog and concise conditions.

A:M contains individual quotes; O:Q is a separately ordered product catalog.
Never associate these two tables by their physical Excel row number.
"""
from __future__ import annotations

import re
import unicodedata


def _text(value):
    return '' if value is None else str(value).strip()


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', _text(value)).split()).casefold()


class SGDConditions:
    def __init__(self, sheet):
        self.sheet = sheet
        self.catalog = None
        headers = [_text(sheet.value(3, c)) for c in (15, 16, 17)]
        catalog_hint = headers[0] == '产品' or headers[1] == '官方来源' or headers[2].startswith('条件原文')
        if not catalog_hint:
            return  # Historical row-level fixtures/workbooks have no catalog.
        if not (headers[0] == '产品' and headers[1] == '官方来源' and headers[2].startswith('条件原文')):
            raise ValueError(f'{sheet.name}: incomplete SGD product conditions catalog O3:Q3: {headers}')
        self.catalog = {}
        banks = {}
        for r in range(4, sheet.maxrow + 1):
            product = _key(sheet.value(r, 2))
            bank = _key(sheet.value(r, 1))
            if product and bank:
                banks.setdefault(product, set()).add(bank)
            name = _key(sheet.value(r, 15))
            source, notes = _text(sheet.value(r, 16)), _text(sheet.value(r, 17))
            if not name:
                if source or notes:
                    raise ValueError(f'{sheet.name}!O{r}: SGD catalog conditions/source without product')
                continue
            record = (source, notes)
            if name in self.catalog and self.catalog[name] != record:
                raise ValueError(f'{sheet.name}!O{r}: conflicting SGD conditions catalog product {sheet.value(r, 15)!r}')
            self.catalog[name] = record
        self.ambiguous = {product for product, names in banks.items() if len(names) > 1}

    def for_row(self, row):
        if self.catalog is None:
            return _text(self.sheet.value(row, 17))
        name = _text(self.sheet.value(row, 2))
        key = _key(name)
        if key in self.ambiguous:
            raise ValueError(f'{self.sheet.name}!B{row}: ambiguous SGD product name shared by multiple banks: {name!r}')
        if not key or key not in self.catalog:
            raise ValueError(f'{self.sheet.name}!B{row}: missing SGD product conditions catalog entry: {name!r}')
        return self.catalog[key][1]


def summarize_sgd_conditions(raw, audience=''):
    """Summarize supported eligibility; flag unknown long clauses explicitly.

This is deliberately not a free-form legal summarizer. Short, unrecognized
conditions survive verbatim; a long unrecognized paragraph stays visible as a
review marker instead of being silently dropped. Raw evidence stays in detail.
"""
    raw = _text(raw)
    keep = []
    def add(value):
        if value and value not in keep:
            keep.append(value)

    personal = _key(audience) in ('personal', 'all', '个人', '个人客户', '普通个人', '所有个人客户', '所有客群', '所有客户', 'personal banking')
    # These are procedural/legal boilerplate, not eligibility conditions.
    legal_starts = (
        'A person who is not a party to these terms and conditions has no right under',
        'All information is accurate as at the date of publication.',
        'For the avoidance of doubt, the Eligible Tenor, Promotional Rate and Preferential Rate applicable to you',
        'For the purposes of this Promotion, the Bank may refresh or change the Eligible tenor',
        'In the event of any inconsistency between these terms and conditions and any brochures',
        'The Bank may vary, modify, add, delete or otherwise revise any of the terms and conditions governing the Promotion',
        'The Bank reserves the right to determine at its sole and absolute discretion all matters arising out of or in connection with the Promotion',
        'These Standard Chartered Time Deposit ',
        'These terms and conditions are governed by and shall be construed in accordance with the laws',
    )
    unresolved = False
    for paragraph in raw.splitlines():
        part = paragraph.strip()
        if not part:
            continue
        # Only the exact known financial clause shapes get shortened. Do not
        # classify arbitrary long paragraphs just because they mention funds.
        if part.startswith('“Fresh funds” refer to funds') and re.search(r'not originate from any existing account with the Bank', part, re.I) and re.search(r'not withdrawn and re-deposited within the last 30 days', part, re.I):
            add('须新资金（非本行现有账户资金；开户前30天内转出再存回不计）')
        elif part.startswith('To be eligible for this Promotion:') and re.search(r'minimum sum of S\$[\d,]+ in Fresh funds', part, re.I) and 'accounts with the Bank must be validly existing' in part and 'in good standing' in part:
            add('须新资金；账户须有效且状态良好')
        elif part.startswith('Subject to these terms, if you satisfy the eligibility criteria stipulated in Clause 2,') and 'only applicable if the Time Deposit is held until maturity' in part:
            add('须存至到期；提前支取可失去促销利息')
        elif part.startswith('Subject to these terms, if you are a Priority Banking or Priority Private Banking client') and 'cease to maintain your Priority Banking or Priority Private Banking status' in part:
            if not personal:
                add('优惠客群须保持相应资格至到期；提前支取可失去优惠利息')
        elif part.startswith('Any renewal upon maturity of the Time Deposit') and 'prevailing Singapore Dollar Time Deposit board rate' in part:
            add('到期续存按当时挂牌利率')
        elif part.startswith('Unless otherwise stated, this Promotion is not valid with other offers'):
            add('不可与其他优惠同享')
        elif part.startswith('This Standard Chartered Singapore Dollar Time Deposit ') and 'to all new and existing customers' in part and 'who are individuals' in part:
            add('个人新老客户均可')
        elif re.fullmatch(r'(?:To qualify for this promotion, simply )?transfer Fresh Funds\d* to your Standard Chartered deposit account(?: and log in to Online Banking or SC Mobile to place your time deposit\.)?', part, re.I):
            add('须新资金')
        elif part.startswith(legal_starts) or part.startswith(('官网本档报价期 ', '条款总活动期 ', '条款明确官网期限及利率')):
            continue
        else:
            for clause in re.split(r'[；;]', part):
                clause = clause.strip()
                if not clause or clause in ('not stated', '条款待复核', '条款待核', 'AND ABOVE'):
                    continue
                if personal and 'For Personal Banking customers' in raw and ('Premier' in clause or clause == 'For Personal Banking customers'):
                    continue
                replacements = {
                    'Fresh funds only': '须新资金', 'Fresh Funds': '须新资金',
                    'Fresh funds': '须新资金', 'fresh funds': '须新资金',
                    'fresh and renewal of funds': '新资金及续存均可',
                    'Existing or new Savings Account required': '须有储蓄账户',
                    '须有Vivid Savings Account': '须有Vivid储蓄账户',
                    'Individual Accounts only': '仅限个人账户',
                    'For New and Existing Customers': '新老客户均可',
                    'new placement': '须新存入',
                }
                for before, after in replacements.items():
                    clause = clause.replace(before, after)
                # Channels and amount ranges have dedicated columns.
                if re.fullmatch(r'placed via (?:the Branch|HLB Connect online banking or mobile banking app)', clause, re.I):
                    continue
                if len(clause) > 260:
                    unresolved = True
                else:
                    add(clause)
    if unresolved:
        add('另有资格条款待核（详见原始明细）')
    return '；'.join(keep), unresolved
