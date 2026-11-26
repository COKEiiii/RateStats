from tests.fixtures import historical
import unittest
from pathlib import Path
from market_rates.common import load
from market_rates.board_wide_plan import cny_board_cell,cny_board_candidates

class CnyBoardScopeTests(unittest.TestCase):
    def quote(self,**changes):
        return dict(dict(bank='BEA',currency='CNY',rate_type='board',product_id='bea-fx-board',insertable=True,audience='personal',rate_pct='0.8',input_row=10),**changes)
    def test_promo_and_legacy_tier_cannot_win(self):
        rows=[self.quote(),self.quote(rate_type='promo',rate_pct='9',input_row=20),self.quote(product_id='bea-fx-tier',rate_pct='8',input_row=30)]
        cell=cny_board_cell('B91',rows,'Board')
        self.assertEqual(cell['formula'],"='Board'!I10")
        self.assertEqual(cell['expected'],0.008)
        self.assertNotIn('MAX',cell['formula'])
    def test_no_promo_fallback(self):
        self.assertEqual(cny_board_cell('B91',[self.quote(rate_type='promo')],'Board')['formula'],'-')
    def test_cnh_retained_unavailable_and_other_currency_excluded(self):
        q=self.quote(currency='CNH')
        self.assertEqual(cny_board_candidates([q,self.quote(currency='USD'),self.quote(insertable=False)]),[q])
    def test_existing_source_bea_12m_is_board_08_not_tier_11(self):
        root=Path(__file__).resolve().parents[1]
        p=historical('bea-cny')
        rows=[r for r in p['details'] if r['bank']=='BEA' and r['display_currency']=='CNY' and r['tenor_value']==12]
        cell=cny_board_cell('F95',rows,p['input_sheet'])
        self.assertEqual(cell['expected'],0.008)
        self.assertEqual(len(cny_board_candidates(rows)),2)

if __name__=='__main__':unittest.main()
