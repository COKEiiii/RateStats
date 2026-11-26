"""Customer segregation and repeatable Premier worksheet updates."""
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from market_rates.premier_sync import select_offers, group_rows, amount_text, rebuild_premier, sync_premier, note_text
from market_rates.report_xml import Book, M, P, R
from tests.test_report_format import fixture


def offer(**kw):
    result = dict(bank='HSBC', product_id='hsbc-usd', product_name='USD promo', currency='USD',
                  rate_type='promo', tenor_value=3, tenor_unit='M', audience='premier',
                  channel='branch', amount_min='30000', amount_max=None, min_inclusive=True,
                  max_inclusive=False, amount_currency='USD', amount_is_equivalent=False,
                  fresh_funds='yes', conditions='special customer requirement', rate_pct='3.5',
                  rate_basis='annual_nominal', valid_from='2026-10-01', valid_to='2026-10-31',
                  availability='available', evidence=[])
    return dict(result, **kw)


RUN = dict(id='current', as_of='2026-10-08', bank_dates={'HSBC': '2026-10-08'})
POLICY = dict(excluded_banks=[], excluded_currencies=[], presentation_currency_map={'CNH': 'CNY'},
              deferred_product_ids=[], promo_quote_date_policy='latest_published_reference')


def selected(*items):
    return select_offers(RUN, items, POLICY)[0]


class PremierScopeTests(unittest.TestCase):
    def test_personal_and_all_never_promote_to_premier(self):
        rows = selected(offer(audience='personal', rate_pct='10'), offer(audience='all', rate_pct='10'),
                        offer(audience='premier', rate_pct='3.5'))
        self.assertEqual([(r['audience'], r['rate']) for r in rows], [('premier', '0.035')])

    def test_premier_usd_ignores_main_table_personal_scope(self):
        rows, _ = select_offers(RUN, [offer(audience='premier_elite')],
                                dict(POLICY, hsbc_usd_promo_audiences=['personal']))
        self.assertEqual(len(rows), 1)

    def test_all_premier_tiers_are_kept_separately(self):
        rows = group_rows(selected(*(offer(audience=a) for a in ('premier_elite', 'premier_wealth', 'premier_standard'))))
        self.assertEqual(len(rows), 3)

    def test_same_product_merges_different_tenors(self):
        rows = group_rows(selected(offer(), offer(tenor_value=12, rate_pct='3.1')))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['rates'], {'3M': D('.035'), '12M': D('.031')})

    def test_different_amounts_channels_and_eligibility_stay_separate(self):
        rows = group_rows(selected(offer(), offer(amount_min='50000'), offer(channel='online'),
                                   offer(conditions='another qualification')))
        self.assertEqual(len(rows), 4)

    def test_conflicting_same_eligibility_does_not_silently_pick_max(self):
        with self.assertRaisesRegex(ValueError, '冲突报价'):
            group_rows(selected(offer(), offer(rate_pct='3.7')))

    def test_expired_reference_keeps_original_end_date(self):
        rows = selected(offer(valid_to='2026-09-30'))
        self.assertTrue(rows[0]['reference_quote'])
        self.assertEqual(rows[0]['valid_to'], '2026-09-30')
        self.assertEqual(selected(offer(valid_from='2026-11-01')), [])
        self.assertEqual(selected(offer(availability='withdrawn')), [])

    def test_zero_is_valid_and_deferred_product_stays_out(self):
        self.assertEqual(selected(offer(rate_pct='0'))[0]['rate'], '0')
        rows, omitted = select_offers(RUN, [offer(product_id='deferred')],
                                     dict(POLICY, deferred_product_ids=['deferred']))
        self.assertEqual(rows, [])
        self.assertEqual(len(omitted), 1)

    def test_amount_boundaries_and_currency_not_aum(self):
        self.assertEqual(amount_text(offer(amount_min='30000.00', amount_max='100000', min_inclusive=False,
                                           amount_currency='SGD', amount_is_equivalent=True)), 'SGD 等值 >30,000；<100,000')

    def test_scb_note_keeps_eligibility_instead_of_legal_boilerplate(self):
        row = offer(bank='SCB', audience='preferred', conditions='Contracts (Rights of Third Parties) Act. '
                    'Priority Banking client up till maturity. Funds not withdrawn and re-deposited within the last 30 days.')
        text = note_text(group_rows(selected(row))[0])
        self.assertIn('全程保持对应 Priority', text)
        self.assertIn('30天内提取再存', text)
        self.assertNotIn('Contracts', text)

    def test_atomic_sync_removes_stale_formula_chain_and_keeps_other_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.xlsx'
            fixture(path, date(2026, 10, 7))
            book = Book(path)
            sheet = book.sheets.pop('最高报价汇总 ')
            sheet.name = 'Premier Rates'
            sheet.node.set('name', sheet.name)
            book.sheets[sheet.name] = sheet
            sheet.put(1, 1, '新元')
            sheet.put(2, 10, '其他')
            part = 'xl/calcChain.xml'
            book.parts[part] = b'<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><c r="J1" i="1"/></calcChain>'
            ET.SubElement(book.rels, P+'Relationship', Id='rCalcChain', Type=R[1:-1]+'/calcChain', Target='calcChain.xml')
            ET.SubElement(book.xml('[Content_Types].xml'), '{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                          PartName='/'+part, ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml')
            book.save(path)
            sources = []
            for currency in ('SGD', 'USD'):
                source = Path(tmp) / currency
                source.mkdir()
                (source/'run.json').write_text(json.dumps(dict(RUN, id=currency)), encoding='utf-8')
                sources.append(source)
            records = [([offer(currency=c, amount_currency=c)], [], []) for c in ('SGD', 'USD')]
            with patch('market_rates.premier_sync.effective_offers', side_effect=records):
                result = sync_premier(path, *sources)
            self.assertTrue(result['passed'])
            after = Book(path)
            self.assertNotIn(part, after.parts)
            self.assertFalse(any(x.get('Type', '').endswith('/calcChain') for x in after.rels))
            self.assertEqual(after.sheets['SGD促销'].value(5, 3), D('.018'))
            self.assertEqual(after.sheets['SGD促销'].cell(5, 3).findtext(M+'f'), '明细_20261007!H4')

    def test_page_rebuild_clears_stale_rates_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'fixture.xlsx'
            fixture(source, date(2026, 10, 7))
            book = Book(source)
            sheet = book.sheets.pop('最高报价汇总 ')
            sheet.name = 'Premier Rates'
            sheet.node.set('name', sheet.name)
            book.sheets[sheet.name] = sheet
            sheet.put(1, 1, '美元')
            sheet.put(2, 10, '其他')
            sheet.put(3, 1, 'CITI')
            sheet.put(3, 6, D('.99'))
            rows = selected(offer(), offer(audience='premier_elite', tenor_value=15))
            audit = {}
            rebuild_premier(book, rows, {'SGD': '2026-10-08', 'USD': '2026-10-08'}, audit)
            self.assertEqual(len(audit['rate_cells']), 2)
            self.assertNotIn(D('.99'), [sheet.value(r, c) for r, c in sheet.cells])
            self.assertIn('15M', audit['sections'][0]['tenors'])
            self.assertIn({'currency': 'USD', 'bank': 'CITI'}, audit['removed_stale_banks'])
            self.assertEqual(sum(x.find(M + 'f') is not None for x in sheet.cells.values()),2)
            from market_rates.premier_details import validate_links,DETAIL_SHEET
            self.assertEqual(len(validate_links(book)),2)
            self.assertIn(DETAIL_SHEET,book.sheets)
            before = {(r, c): sheet.value(r, c) for r, c in sheet.cells}
            rebuild_premier(book, rows, {'SGD': '2026-10-08', 'USD': '2026-10-08'}, {})
            self.assertEqual(before, {(r, c): sheet.value(r, c) for r, c in sheet.cells})
            book.save(source)
            reread = Book(source).sheets['Premier Rates']
            self.assertEqual(before, {(r, c): reread.value(r, c) for r, c in reread.cells})


if __name__ == '__main__':
    unittest.main()
