import unittest
from unittest.mock import patch
from market_rates.capture import _section_text
from market_rates.link_registry import bank_link
from market_rates.remaining_banks import configuration


class LinkRegistryTests(unittest.TestCase):
    def setUp(self):
        self.cells={'A45':'maybank','B45':'others','C45':'促销','D45':None,
            'C46':'挂牌','D46':'https://sslsecure.maybank.com.sg/board',
            'B47':'SGD','C47':'促销','D47':'https://www.maybank2u.com.sg/new-promotion',
            'C48':'挂牌','D48':'https://sslsecure.maybank.com.sg/board'}

    def read(self):return bank_link('user-links.xlsx','Maybank','SGD','促销',['www.maybank2u.com.sg'])

    def test_latest_cell_used_and_board_rate_not_selected(self):
        with patch('market_rates.link_registry.read_xlsx',return_value={'Links':self.cells}):
            self.assertEqual(self.read()['cell'],'D47')
            self.cells['D47']='https://www.maybank2u.com.sg/revised-url'
            cfg=configuration('Maybank',links='user-links.xlsx')
            self.assertEqual(cfg['sources'][0]['urls'],[self.cells['D47']])
            self.assertIn('section_between',cfg['sources'][0]['capture_units'][0])
            self.assertFalse(cfg['pdf_rate_products'])

    def test_maybank_standalone_section_is_selected_by_product_title(self):
        with patch('market_rates.link_registry.read_xlsx', return_value={'Links': self.cells}), \
             patch('market_rates.remaining_banks.load', return_value={'models': {}}):
            config = configuration('Maybank', links='user-links.xlsx')
        section = config['sources'][0]['capture_units'][0]['section_between']
        context = 'Singapore Dollar Time Deposit / Term Deposit-i / iSAVvy Time Deposit Promotion'
        title = 'Standalone Time Deposit and Term Deposit-i (at branches and online)'
        rates = 'Tenure\tEligible Customers\tPromotional Rates\n6 month\tIndividual customers only\t1.23%'
        previous_campaigns = (
            'i. Year End Festive Fixed Deposit Promotion\n'
            'Standalone Time Deposit and Term Deposit-i\t6, 9, and 12 months\n'
            'ii. Deposits Bundle Promotion (at branches and online)\nTenure\tEffective Rate\n'
        )
        for prefix in ('ii. ', 'iii. ', ''):
            with self.subTest(prefix=prefix):
                heading = prefix + title
                page = f'{context}\n{previous_campaigns}{heading}\n{rates}\nHow to place deposit'
                text, start, end = _section_text(page, section)
                self.assertEqual(text, f'{context}\n{heading}\n{rates}')
                self.assertEqual((start, end), (heading, 'How to place deposit'))
        for headings in ('', f'ii. {title}\niii. {title}\n'):
            with self.subTest(headings=headings), self.assertRaisesRegex(ValueError, 'Section start'):
                _section_text(f'{context}\n{previous_campaigns}{headings}{rates}\nHow to place deposit', section)

    def test_missing_duplicate_and_unregistered_urls_stop(self):
        with patch('market_rates.link_registry.read_xlsx',return_value={'Links':self.cells}):
            self.cells['D47']=None
            with self.assertRaises(ValueError):self.read()
            self.cells['D47']='https://www.maybank2u.com.sg.evil.invalid/rates'
            with self.assertRaises(ValueError):self.read()
            self.cells['D47']='https://www.maybank2u.com.sg/rates'
            self.cells.update(C49='促销',D49='https://www.maybank2u.com.sg/another')
            with self.assertRaises(ValueError):self.read()

    def test_blank_url_reports_exact_source_cell_without_using_stale_hyperlink(self):
        cells={'A20':'DBS','B20':'others','C20':'促销','D20':None}
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as folder:
            source=Path(folder)/'links.xlsx';source.touch()
            with patch('market_rates.link_registry.read_xlsx',return_value={'工作表1':cells}), \
                 patch('market_rates.link_registry.read_hyperlinks',return_value={'工作表1':{'D20':'https://www.dbs.com.sg/old'}}):
                with self.assertRaisesRegex(ValueError,'DBS others promo 网址为空.*D20'):
                    bank_link(source,'DBS','others','promo')


if __name__=='__main__':unittest.main()
