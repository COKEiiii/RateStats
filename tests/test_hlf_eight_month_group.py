import unittest
from market_rates.rules import display_tenor
from market_rates.workbook_policy import workbook_policy, included_groups
class HlfEightMonthTests(unittest.TestCase):
    def test_authorized_group_preserves_actual_tenor(self):
        r=dict(bank='HLF',product_id='hlf-branch-promo',currency='SGD',rate_type='promo',tenor_value=8,tenor_unit='M')
        self.assertEqual(display_tenor(r,{}),'8M')
        self.assertEqual(r['tenor_value'],8)
        self.assertEqual(display_tenor(dict(r,bank='SBI'),{}),'8M')
        self.assertEqual(display_tenor(dict(r,product_id='hlf-digital-promo',tenor_value=9),{}),'9M')

    def test_published_promotion_not_deferred(self):
        groups=[dict(bank='HLF', product_id='hlf-digital-promo', display_tenor=t)
                for t in ['6M','9M','12M']]
        self.assertEqual(included_groups(groups,workbook_policy()),groups)
