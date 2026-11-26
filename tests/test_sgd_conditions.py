"""Independent product-catalog matching and shared SGD eligibility regressions."""
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from market_rates.sgd_conditions import SGDConditions, summarize_sgd_conditions
from market_rates.report_format import sgd_quotes
from market_rates.report_xml import Book
from market_rates.rainbow_format import Reader, Quote
from tests.test_rainbow_format import FakeSheet, audit
from tests.test_report_format import fixture


def catalog_sheet():
    return FakeSheet('明细_20261007', {
        (3, 8): '年利率', (3, 15): '产品', (3, 16): '官方来源', (3, 17): '条件原文/待复核',
        (4, 1): 'Maybank', (4, 2): 'Maybank Deposit', (4, 3): '6M', (4, 4): 'Personal',
        (4, 5): 'online', (4, 6): D(10000), (4, 8): D('.017'),
        (5, 1): 'SBI', (5, 2): 'SBI Deposit', (5, 3): '6M', (5, 4): 'Personal',
        (5, 5): 'counter', (5, 6): D(50000), (5, 8): D('.018'),
        # Catalog rows are deliberately reversed relative to quotes.
        (4, 15): 'SBI Deposit', (4, 16): 'https://sbi.example/',
        (4, 17): '条款待复核；fresh and renewal of funds；个人多笔合计 < SGD 1000000',
        (5, 15): 'Maybank Deposit', (5, 16): 'https://maybank.example/', (5, 17): '须有Vivid Savings Account',
    })


class CatalogTests(unittest.TestCase):
    def test_reordered_catalog_is_joined_by_product_not_excel_row(self):
        resolver = SGDConditions(catalog_sheet())
        self.assertEqual(resolver.for_row(4), '须有Vivid Savings Account')
        self.assertIn('个人多笔合计', resolver.for_row(5))

    def test_legacy_row_notes_remain_supported_without_catalog(self):
        sheet = FakeSheet('明细_20261007', {(4, 17): '须有账户'})
        self.assertEqual(SGDConditions(sheet).for_row(4), '须有账户')

    def test_older_catalog_header_is_supported(self):
        sheet = catalog_sheet(); sheet.values[3, 17] = '条件原文'
        self.assertIn('Vivid', SGDConditions(sheet).for_row(4))

    def test_catalog_missing_product_does_not_fall_back_to_physical_row(self):
        sheet = catalog_sheet(); sheet.values[4, 2] = 'New unnamed-in-catalog product'
        with self.assertRaisesRegex(ValueError, 'missing SGD product conditions'):
            SGDConditions(sheet).for_row(4)

    def test_duplicate_conflicting_catalog_entry_fails_explicitly(self):
        sheet = catalog_sheet(); sheet.maxrow = 6
        sheet.values.update({(6, 15): 'SBI Deposit', (6, 16): 'https://sbi.example/', (6, 17): 'different restriction'})
        with self.assertRaisesRegex(ValueError, 'conflicting SGD conditions'):
            SGDConditions(sheet)

    def test_same_product_name_at_different_banks_is_not_arbitrarily_assigned(self):
        sheet = catalog_sheet(); sheet.values[4, 2] = 'SBI Deposit'
        with self.assertRaisesRegex(ValueError, 'multiple banks'):
            SGDConditions(sheet).for_row(4)

    def test_partial_catalog_does_not_activate_legacy_fallback(self):
        sheet = catalog_sheet(); sheet.values[3, 16] = 'unknown source'
        with self.assertRaisesRegex(ValueError, 'incomplete SGD product'):
            SGDConditions(sheet)

    def test_report_and_rainbow_use_the_same_product_qualification(self):
        detail = catalog_sheet()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.xlsx'; fixture(path, date(2026, 10, 7))
            book = Book(path); book.sheets[detail.name] = detail
            quotes = sgd_quotes(book, date(2026, 10, 7))
        maybank = next(q for group in quotes.values() for q in group if q.bank == 'Maybank')
        sbi = next(q for group in quotes.values() for q in group if q.bank == 'SBI')
        self.assertIn('Vivid', maybank.notes); self.assertNotIn('合计', maybank.notes)
        self.assertIn('个人多笔合计 < SGD 1000000', sbi.notes); self.assertNotIn('Vivid', sbi.notes)
        reader = Reader.__new__(Reader); reader.audit = audit(); reader.as_of = date(2026, 10, 7)
        for row, bank in ((4, 'Maybank'), (5, 'SBI')):
            q = Quote(bank, '6M', 'SGD', '促销', D('.018'))
            q = reader.from_detail(q, detail, row, '')
            self.assertEqual(q.notes, maybank.notes if bank == 'Maybank' else sbi.notes)


class SummaryTests(unittest.TestCase):
    def test_scb_fresh_funds_30_day_redeposit_and_maturity_stay_visible(self):
        raw = ('“Fresh funds” refer to funds that do not originate from any existing account with the Bank '
               'and funds that are not withdrawn and re-deposited within the last 30 days of opening your Time Deposit.\n'
               'Subject to these terms, if you satisfy the eligibility criteria stipulated in Clause 2, '
               'you earn promotional interest. The Promotional Rate is only applicable if the Time Deposit is held until maturity of the tenor.')
        summary, unresolved = summarize_sgd_conditions(raw, 'Personal')
        self.assertFalse(unresolved)
        for token in ('新资金', '非本行现有账户', '30天', '存至到期'):
            self.assertIn(token, summary)

    def test_unknown_long_clause_is_marked_without_erasing_known_qualifications(self):
        summary, unresolved = summarize_sgd_conditions('须有Vivid Savings Account\nYou must ' + 'new eligibility ' * 40)
        self.assertTrue(unresolved); self.assertIn('Vivid', summary); self.assertIn('资格条款待核', summary)

    def test_hsbc_personal_does_not_inherit_other_customer_classes(self):
        summary, _ = summarize_sgd_conditions('For Personal Banking customers\nPremier Elite customers\nPremier customers with wealth holdings', 'Personal')
        self.assertNotIn('Premier', summary)


if __name__ == '__main__':
    unittest.main()
