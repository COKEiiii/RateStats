from pathlib import Path
import unittest
from market_rates.common import load
from tests.fixtures import historical,text_run
from market_rates.multi_bank_validation import raw_matched_offers
from market_rates.workbook_policy import workbook_policy

ROOT=Path(__file__).resolve().parents[1]

class RemainingBankTests(unittest.TestCase):
    def test_citi_investment_2m_is_detail_only(self):
        p=workbook_policy()
        for segment in ['gold','private']:
            self.assertNotIn('CITI/citi-'+segment+'-investment-bundle/2M',p['deferred_actual_tenors'])

    def test_frozen_new_banks_have_no_missing_or_differing_tiers(self):
        samples={'BEA':('',2),'HSBC':('',16),'SingFinance':('',12),'Singapura Finance':('',7),'CITI':('',7)}
        for b,(v,count) in samples.items():
            run=text_run([r for r in historical('sgd-planning')['offers'] if r['bank']==b]);rows,bad=raw_matched_offers(run)
            self.assertEqual(bad,[]);self.assertEqual(len(rows),count)
            if b=='HSBC':self.assertEqual({r['tenor_value']:r['rate_pct'] for r in rows if r['audience']=='personal'},{3:'1.4',6:'1.8',9:'1.2',12:'1.3'})
            if b=='SingFinance':
                self.assertEqual(sorted((r['amount_min'],r['rate_pct']) for r in rows if r['tenor_value']==6),[('1000','1.5'),('10000','1.8')])

if __name__=='__main__':unittest.main()
