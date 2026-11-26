import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_expansion_bank_pilot import boc_capture_units
from scripts.run_market_workflow import archive_failed_capture
from market_rates.capture import _html_evidence
from market_rates.source_inventory import BOC_SGD_BUNDLE, boc_bundle_inventory, boc_bundle_terms


class BocCaptureLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def capture(self, welcome):
        page = self.browser.new_page()
        try:
            special = '<table><tr><th>手机银行 仅限新客户</th></tr><tr><td>新元 4个月 2.18</td></tr></table>' if welcome else ''
            page.set_content('<div class="trs_editor_view">' + special + '''
                <table><tr><th>手机银行(*新存单)</th></tr>
                <tr><td>新元</td><td>3个月</td><td>1.60</td></tr>
                <tr><td>美元</td><td>3个月</td><td>9.99</td></tr></table>
                <ul><li>本次个人定期存款促销利率有效期为2026年11月2日至2026年11月9日。</li></ul></div>''')
            units = boc_capture_units(page)
            with TemporaryDirectory() as folder:
                _, _, texts, _, _ = _html_evidence(page, {'capture_units': units}, Path(folder), 'boc', 4)
            for unit, text in zip(units, texts):
                if unit['kind'] == 'rates' and unit['product_ids'] == ['boc-sgd-mobile']:
                    self.assertIn('1.60', text)
                    self.assertNotIn('9.99', text)
                    self.assertNotIn('2.18', text)
            self.assertEqual(any('boc-sgd-welcome' in u['product_ids'] for u in units), welcome)
        finally:
            page.close()

    def test_current_notice_without_welcome_table(self):
        self.capture(False)

    def test_prior_notice_keeps_separate_welcome_table(self):
        self.capture(True)

    def test_bundle_table_and_terms_stay_out_of_mobile_evidence(self):
        page = self.browser.new_page()
        try:
            page.set_content('''<div class="trs_editor_view">
                <table><tr><th>手机银行(*新存单)</th></tr>
                <tr><td>新元</td><td>3个月</td><td>1.60</td></tr>
                <tr><td>美元</td><td>3个月</td><td>3.80</td></tr></table>
                <table><tr><th>定期和储蓄通账户组合促销利率（新存单）</th></tr>
                <tr><th>年利率(%)</th><th>有效年利率(%)</th><th>起存金额（新元）</th></tr>
                <tr><td>3个月</td><td>2.20</td><td>2.01</td><td rowspan="2">S$20,000</td></tr>
                <tr><td>6个月</td><td>2.20</td><td>2.01</td></tr></table>
                <p>通过柜台办理时，须另存资金到 SmartSaver。</p>
                <ul><li><a href="/bundle.pdf">TD-MCS Bundle Promotion</a></li></ul>
                <ul><li>本次个人定期存款促销利率有效期为2026年11月16日至2026年11月22日。</li></ul>
                </div>''')
            units = boc_capture_units(page)
            with TemporaryDirectory() as folder:
                _, _, texts, _, _ = _html_evidence(page, {'capture_units': units}, Path(folder), 'boc', 4)
            rates, terms, bundle, bundle_note = texts
            self.assertIn('1.60', rates)
            self.assertNotIn('3.80', rates)
            self.assertNotIn('2.20', rates)
            self.assertIn('2026年11月22日', terms)
            self.assertNotIn('TD-MCS', terms)
            self.assertEqual(units[2]['product_ids'], [BOC_SGD_BUNDLE])
            self.assertIn('SmartSaver', bundle_note)
            self.assertEqual([(r['tenor_value'], r['amount_min'], r['rate_pct'], r['effective'])
                              for r in boc_bundle_inventory(bundle)],
                             [(3, '20000', '2.20', '2.01'), (6, '20000', '2.20', '2.01')])
        finally:
            page.close()

    def test_bundle_period_comes_from_its_own_terms(self):
        pdf = dict(id='pdf', bank='BOC', terms_role='primary', product_ids=[BOC_SGD_BUNDLE],
                   text='''The Promotion is valid from 16 November 2026 (“Promotion Period”) and remains
                   valid until such further date as may be determined.
                   Customers must place a minimum of S$20,000 in an SGD TD account. Both fresh
                   and existing funds are eligible, with no maximum placement limit.
                   Maintain a balance equivalent to 10% of the TD placement amount (minimum S$2,000).
                   The Promotion is available exclusively over the counter at all BOCSG branches.''')
        period, minimum, percentage, savings, evidence = boc_bundle_terms([pdf])
        self.assertEqual((period, minimum, percentage, savings), (('2026-11-16', None), '20000', '10', '2,000'))
        self.assertTrue(all(e['page_id'] == 'pdf' for e in evidence))

    def test_unknown_sgd_table_blocks_collection(self):
        page = self.browser.new_page()
        try:
            page.set_content('<div class="trs_editor_view"><table><tr><td>手机银行 新存单 新元</td></tr></table>'
                             '<table><tr><td>新元 未识别产品</td></tr></table></div>')
            with self.assertRaisesRegex(ValueError, 'Unrecognized'):
                boc_capture_units(page)
        finally:
            page.close()


class CaptureResumeTests(unittest.TestCase):
    def test_failed_capture_archived_and_successful_batch_preserved(self):
        with TemporaryDirectory() as folder:
            work = Path(folder)
            failed = work / 'UOB-ICBC-BOC'
            failed.mkdir()
            evidence = failed / 'run.json'
            evidence.write_text(json.dumps({'errors': ['BOC selector timeout']}))
            original = evidence.read_bytes()
            archived = archive_failed_capture(failed, work)
            self.assertFalse(failed.exists())
            self.assertEqual((archived / 'run.json').read_bytes(), original)
            failed.mkdir()
            evidence.write_text(json.dumps({'errors': ['TERMS_REVIEW: pending']}))
            self.assertIsNone(archive_failed_capture(failed, work))
            self.assertTrue(evidence.exists())

    def test_incomplete_capture_archived_and_outside_path_rejected(self):
        with TemporaryDirectory() as folder:
            work = Path(folder)
            failed = work / 'batch'
            failed.mkdir()
            (failed / 'partial.txt').write_text('retained')
            archived = archive_failed_capture(failed, work)
            self.assertEqual((archived / 'partial.txt').read_text(), 'retained')
            with self.assertRaises(ValueError):
                archive_failed_capture(work.parent / 'other', work)
