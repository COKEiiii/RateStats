from unittest import TestCase

from market_rates.multi_bank_validation import deferred_actual_tenors


class UnmappedTenorTest(TestCase):
    def test_new_actual_tenor_is_audited_without_assigning_another_section(self):
        def offer(bank, product, months):
            return dict(bank=bank, product_id=product, tenor_value=months,
                        tenor_unit='M', currency='SGD', rate_type='promo')

        offers = [offer('BOC', 'boc-sgd-mobile', n) for n in (3, 4, 5, 6)]
        configured = {'complete_public_quotes': False,'deferred_actual_tenors': ['BOC/boc-sgd-mobile/5M']}
        cfg={'workbook_policy':{'complete_public_quotes':False}}
        all_deferred, new = deferred_actual_tenors(offers, cfg, configured)
        self.assertEqual(new, {'BOC/boc-sgd-mobile/4M'})
        self.assertEqual(all_deferred, {'BOC/boc-sgd-mobile/4M',
                                        'BOC/boc-sgd-mobile/5M'})
        self.assertNotIn('BOC/boc-sgd-mobile/3M', all_deferred)
        self.assertNotIn('BOC/boc-sgd-mobile/6M', all_deferred)
