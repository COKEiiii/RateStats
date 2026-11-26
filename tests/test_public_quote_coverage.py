from collections import Counter
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.public_quotes import attach_four


class PublicCoverageTests(TestCase):
    def test_missing_group_needs_explicit_manual_reason(self):
        key=('BANK','SGD','promo','product','3M','personal','online')
        cat=dict(records=[],winners=[],source_coverage=[],source_counts=Counter({key:1}),manual_adjustments={})
        files={key:key for key in ('raw_report','raw_rainbow','formatted_report','formatted_rainbow')}
        check=dict(passed=True,catalog_sha256='quotes',selection_sha256='selection')
        with TemporaryDirectory() as folder,patch('market_rates.public_quotes.catalog',return_value=cat),patch('market_rates.public_quotes.write_catalog',return_value=check),patch('market_rates.public_quotes.align_sgd_summary',return_value={'passed':True}):
            with self.assertRaisesRegex(ValueError,'未解释差异'):attach_four(files,'plan',folder)
            cat['manual_adjustments'][key]=[dict(action='exclude',author='reviewer',reason='Duplicate source entry')]
            audit=attach_four(files,'plan',folder)
            self.assertTrue(audit['passed']);self.assertEqual(audit['coverage'][0]['displayed'],0)
            self.assertEqual(audit['coverage'][0]['source_quotes'],1)
            self.assertEqual(audit['coverage'][0]['selection_adjustment'][0]['reason'],'Duplicate source entry')

    def test_summary_amount_comes_from_the_selected_higher_tier(self):
        from pathlib import Path
        from openpyxl import Workbook
        from market_rates.public_quotes import align_sgd_summary
        from market_rates.report_xml import Book
        with TemporaryDirectory() as folder:
            path=Path(folder)/'report.xlsx';b=Workbook();s=b.active;s.title='最高报价汇总 '
            for address,value in {'B3':'Bank','C3':'3M','B4':'BOC','C4':.0175,'J4':'SGD 500'}.items():s[address]=value
            main=b.create_sheet('SGD促销')
            for address,value in {'A2':'3 Months','A3':'日期','A5':'BOC','B5':'报价','C5':.0175,'H4':'当日最高报价变动','I5':'500','J5':'旧模板条件'}.items():main[address]=value
            b.save(path)
            quote=dict(quote_id='winner',bank='BOC',product_name='本期产品',currency='SGD',rate_type='promo',tenor_value=3,tenor_unit='M',audience='personal',rate_pct='1.75',amount_min='200000',amount_max=None,amount_currency='SGD',min_inclusive=True,max_inclusive=False,channel='online',fresh_funds='unknown',valid_from=None,valid_to=None)
            cat=dict(records=[quote],winners=[dict(bank='BOC',currency='SGD',rate_type='promo',tenor='3M',quote_id='winner')])
            self.assertTrue(align_sgd_summary(path,cat)['passed'])
            self.assertEqual(Book(path).sheets['最高报价汇总 '].value(4,10),'3M：SGD ≥200,000')
            self.assertEqual(Book(path).sheets['SGD促销'].value(5,9),'3M：SGD ≥200,000')
            self.assertNotIn('旧模板条件',Book(path).sheets['SGD促销'].value(5,10))


    def test_raw_summary_annotates_the_product_row_linked_to_winning_quote(self):
        from pathlib import Path
        from openpyxl import Workbook
        from market_rates.public_quotes import align_sgd_summary
        from market_rates.report_xml import Book, M

        with TemporaryDirectory() as folder:
            path = Path(folder) / 'report.xlsx'
            workbook = Workbook()
            summary = workbook.active
            summary.title = '最高报价汇总 '
            for address, value in {'B3': 'Bank', 'C3': '9M', 'B4': 'BOC', 'C4': .022}.items():
                summary[address] = value
            main = workbook.create_sheet('SGD促销')
            for address, value in {'A2': '9 Months', 'A3': '日期', 'A5': 'BOC',
                                   'B5': '手机银行', 'B6': '定存组合',
                                   'H4': '当日最高报价变动'}.items():
                main[address] = value
            main.merge_cells('A5:A6')
            workbook.save(path)
            book = Book(path)
            main = book.sheets['SGD促销']
            main.put(5, 3, .022, formula='MAX(明细_20261123!H12)')
            main.put(6, 3, .022, formula="MAX('明细_20261123'!$H$4)")
            book.save(path)
            quote = dict(quote_id='bundle', bank='BOC', product_name='定存组合',
                         input_row=4, currency='SGD', rate_type='promo', tenor_value=9,
                         tenor_unit='M', audience='personal', rate_pct='2.2', amount_min='20000',
                         amount_max=None, amount_currency='SGD', min_inclusive=True,
                         max_inclusive=False, channel='branch', fresh_funds='no',
                         valid_from=None, valid_to=None)
            cat = dict(records=[quote], winners=[dict(bank='BOC', currency='SGD',
                       rate_type='promo', tenor='9M', quote_id='bundle')])
            audit = align_sgd_summary(path, cat, source_sheet='明细_20261123')
            self.assertEqual(audit['main_selection'][0]['row'], 6)
            saved = Book(path).sheets['SGD促销']
            self.assertEqual(saved.value(5, 9), '-')
            self.assertEqual(saved.value(6, 9), '9M：SGD ≥20,000')
            self.assertEqual(saved.cells[6, 3].findtext(M+'f'), "MAX('明细_20261123'!$H$4)")

            book = Book(path)
            book.sheets['SGD促销'].put(5, 3, .023, formula='MAX(明细_20261123!H12)')
            book.save(path)
            with self.assertRaisesRegex(ValueError, 'SGD主表最高值'):
                align_sgd_summary(path, cat, source_sheet='明细_20261123')
