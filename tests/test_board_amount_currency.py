import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_rates.board_extractor import extract, number


class BoardAmountCurrencyTests(unittest.TestCase):
    def test_prefix_suffix_and_scaled_bounds(self):
        for text, currency, expected in [
            ('10000.00 SGD', 'SGD', '10000'),
            ('100000.00 SGD', 'SGD', '100000'),
            ('S$10,000', 'SGD', '10000'),
            ('< USD 1 mio', 'USD', '1000000'),
            ('1.5 million USD', 'USD', '1500000'),
            ('10k', 'SGD', '10000'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(number(text, currency), expected)
        self.assertIsNone(number(None, 'SGD'))

    def test_conflicting_currencies_and_unclear_amounts_are_blocked(self):
        for text in ['10000 USD', 'US$10,000', 'SGD 10000 USD', '10000 SGD extra']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                number(text, 'SGD')
        with self.assertRaises(ValueError):
            number('SGD 10000 USD')

    def test_extraction_preserves_channel_minimums_and_rejects_wrong_currency(self):
        page = dict(id='HLB-SGD-rates', bank='HLB', currency='SGD',
                    url='https://www.hlbank.com.sg/rates',
                    text='Fixed Deposit % (p.a.) S$10,000 (online); S$100,000 (branch)',
                    grid=[['Tenor', '% (p.a.)'], ['1 MONTH', '0.1']])
        parsed = dict(currency='SGD', annual_basis_text='% (p.a.)',
                      effective_date_text='02-Nov-2026 9:23AM', audience='all',
                      amount_tiers=[dict(minimum=amount, maximum=None,
                                         min_inclusive=True, max_inclusive=False,
                                         amount_currency='SGD', amount_is_equivalent=False,
                                         channel=channel)
                                    for amount, channel in [('10000.00 SGD', 'online'),
                                                            ('100000.00 SGD', 'branch')]],
                      rows=[dict(tenor_text='1 MONTH', rate_text='0.1')], unreadable=[])
        with tempfile.TemporaryDirectory() as temp, \
             patch('market_rates.board_extractor.settings', return_value={'text_model': 'test'}), \
             patch('market_rates.board_extractor.check_model'), \
             patch('market_rates.board_extractor.response_cache.model_revision', return_value=None), \
             patch('market_rates.board_extractor.runtime_status', return_value={}), \
             patch('market_rates.board_extractor.request_json') as request:
            def reply():
                return dict(done=True, done_reason='stop', message={'content': json.dumps(parsed)})
            request.return_value = reply()
            result = extract({'pages': [page]}, Path(temp), {})
            self.assertEqual([(o['channel'], o['amount_min']) for o in result['llm']['offers']],
                             [('online', '10000'), ('branch', '100000')])
            parsed['amount_tiers'][0]['minimum'] = '10000.00 USD'
            request.return_value = reply()
            with self.assertRaisesRegex(ValueError, 'Amount currency mismatch'):
                extract({'pages': [page]}, Path(temp), {})


if __name__ == '__main__':
    unittest.main()
