from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.common import save
from market_rates.coverage_status import public_inventory
from scripts.collect_public_status import public_sources


class PublicStatusTest(TestCase):
    def test_missing_posb_link_does_not_get_replaced_by_another_bank(self):
        def missing(bank, currency, kind):
            self.assertEqual((bank, currency, kind), ('POSB', 'SGD', 'board'))
            raise ValueError('POSB SGD board 链接缺失或不唯一')

        with patch('scripts.collect_public_status.rate_url', side_effect=missing), \
             patch('scripts.collect_public_status.named_url', side_effect=lambda key: 'https://official.example/' + key), \
             patch('scripts.collect_public_status.bank_in_scope', return_value=True):
            sources, unavailable = public_sources()
        self.assertNotIn('POSB', sources)
        self.assertEqual(unavailable, [{'bank': 'POSB', 'reason': 'POSB SGD board 链接缺失或不唯一'}])
        self.assertIn('DBS', sources)

    def test_inventory_marks_missing_and_failed_sources_as_unverified(self):
        with TemporaryDirectory() as folder:
            target = Path(folder)
            save(target / 'status.json', dict(captured_at='2026-10-14T00:00:00Z',
                pages=[dict(bank='DBS', url='https://www.dbs.com.sg/public')],
                errors=['MARI https://www.maribank.sg/: timeout'],
                sources={'DBS': ['https://www.dbs.com.sg/public'],
                         'MARI': ['https://www.maribank.sg/']},
                unavailable_sources=[dict(bank='POSB', reason='POSB source link missing')]))
            with patch('market_rates.coverage_status.bank_in_scope', return_value=True):
                inventory = {row['bank']: row for row in public_inventory(target)}
        self.assertEqual(inventory['DBS']['status'], 'no_verified_public_quote')
        self.assertEqual(inventory['POSB']['status'], 'source_link_missing')
        self.assertEqual(inventory['POSB']['urls'], [])
        self.assertEqual(inventory['MARI']['status'], 'source_capture_failed')
        self.assertTrue(all(row['numeric_insertion'] is False for row in inventory.values()))
