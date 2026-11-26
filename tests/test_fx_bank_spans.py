import unittest
from market_rates.fx_promo_plan import bank_span


class BankSpanTests(unittest.TestCase):
    def test_existing_two_bands(self):
        self.assertEqual(bank_span('ICBC',10,11,[]),dict(bank='ICBC',start=10,end=11))

    def test_new_tiers_and_next_bank_are_separate(self):
        inserts=[dict(bank='BOC',anchor=3,before=5,count=2,target_row=5,new_bank=False),
                 dict(bank='ICBC',anchor=10,before=12,count=2,target_row=14,new_bank=False),
                 dict(bank='SBI',anchor=10,before=12,count=1,target_row=16,new_bank=True)]
        self.assertEqual(bank_span('ICBC',10,11,inserts),dict(bank='ICBC',start=12,end=15))

    def test_three_existing_bands_and_later_tenor(self):
        self.assertEqual(bank_span('ICBC',20,22,[]),dict(bank='ICBC',start=20,end=22))
        self.assertEqual(bank_span('SCB',30,33,[]),dict(bank='SCB',start=30,end=33))
