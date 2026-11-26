import unittest
from market_rates.board_wide_plan import canonical_bank
from market_rates.board_wave import tenor_values

class BoardAliasWeekTests(unittest.TestCase):
    def test_aliases(self):
        for label,expected in [('Citibank','CITI'),('CITI','CITI'),('HL Bank','HLB'),('HLB','HLB'),('HLF','HLF'),('DBS/POSB','DBS')]:
            self.assertEqual(canonical_bank(label),expected)
    def test_week_interval(self):
        self.assertEqual(tenor_values('1 to 2 Weeks'),[(7,'D'),(14,'D')])
        self.assertEqual(tenor_values('1-2 weeks'),[(7,'D'),(14,'D')])
        self.assertEqual(tenor_values('1 Month'),[(1,'M')])
        self.assertEqual(tenor_values('2 to 1 Weeks'),[])
