from tests.fixtures import historical
import unittest,copy
from pathlib import Path
from decimal import Decimal
from market_rates.common import load
from market_rates.fx_promo import normalize_fx,promotion_dates
from market_rates.rules import main_offer
from scripts.verify_board_wave import canonical_transcription

ROOT=Path(__file__).resolve().parents[1]
class FxPromotionTests(unittest.TestCase):
    def test_invalid_dbs_source_is_not_a_zero_or_board_promotion(self):
        s=dict(bank='DBS',currency='USD',layout='fx-promo-source-unavailable',status='source_unavailable',rows=[],task_ids=['verified-marker'],page_id='dbs-invalid',requested_url='https://www.dbs.com.sg/personal/promotion/usdfd-a',resolved_url='https://www.dbs.com.sg/personal/default.page',extra_evidence=[dict(quote='Page you are trying to access is invalid')])
        self.assertEqual(normalize_fx(s),[])
        for field,value in [('bank','BOC'),('task_ids',[]),('resolved_url','https://example.com'),('extra_evidence',[])]:
            bad=copy.deepcopy(s);bad[field]=value
            with self.assertRaises(ValueError):normalize_fx(bad)
        s.update(resolved_url='https://www.dbs.com.sg/personal/404.page',extra_evidence=[dict(quote='Page not found')])
        self.assertEqual(normalize_fx(s),[])
        for url,text in [('https://example.com/personal/404.page','Page not found'),
                         ('https://www.dbs.com.sg/personal/promotion/usdfd-a','Page not found'),
                         ('https://www.dbs.com.sg/personal/404.page','Please try later'),
                         ('https://www.dbs.com.sg/personal/404.page','Page not found in this table')]:
            bad=copy.deepcopy(s);bad.update(resolved_url=url,extra_evidence=[dict(quote=text)])
            with self.assertRaises(ValueError):normalize_fx(bad)
    def section(self,bank):
        folders={'ICBC':'i','SCB':'i','CITI':'i','DBS':'i','HSBC':'h','BEA':'d','CIMB':'a','RHB':'c','SBI':'c'}
        return historical('fx-'+bank)['sections']
    def test_dbs_derived_not_stale_headline(self):
        board=historical('board-dbs-hlf')['offers'];r=normalize_fx(self.section('DBS')[0],board)
        self.assertEqual(len(r),10);self.assertEqual(max(Decimal(x['rate_pct']) for x in r),Decimal('4.26'))
        self.assertTrue(all(Decimal(x['amount_min'])>=10000 and Decimal(x['amount_max'])<=500000 for x in r))
        self.assertEqual({x['rate_pct'] for x in r if x['tenor_value']==12},{'3.8','4.1'})
    def test_icbc_currency_and_channel_thresholds(self):
        sections=self.section('ICBC');cny=normalize_fx(sections[0]);usd=normalize_fx(sections[1])
        self.assertEqual((len(cny),len(usd)),(10,10));self.assertTrue(all('20,000' in r['conditions'] for r in cny+usd));self.assertTrue(all(r['channel']=='电子银行/柜台' for r in cny))
        low=[r for r in usd if r['channel']=='电子银行' and r['amount_max']=='5000'];self.assertEqual(len(low),5);self.assertTrue(all(not r['max_inclusive'] for r in low));self.assertEqual(low[2]['rate_pct'],'3.9')
    def test_hsbc_personal_ranking_separate_from_highest(self):
        r=[o for s in self.section('HSBC') for o in normalize_fx(s) if o['currency']=='USD' and o['tenor_value']==3]
        self.assertEqual(main_offer(r)['rate_pct'],'3.75');self.assertEqual(max(Decimal(o['rate_pct']) for o in r),Decimal('4.5'))
        self.assertTrue(all('非App' in o['conditions'] for o in r))
    def test_citi_conversion_equivalent_and_new_funds_distinct(self):
        a,b=self.section('CITI');r=normalize_fx(a);nf=normalize_fx(b)
        self.assertTrue(all(x['amount_min']=='50000' and x['amount_currency']=='USD' and x['amount_is_equivalent'] for x in r));self.assertIn('CNH',{x['currency'] for x in r});self.assertNotIn('JPY',{x['currency'] for x in r})
        self.assertEqual({x['rate_pct'] for x in nf},{'4','4.5'});self.assertTrue(all(x['fresh_funds']=='yes' and x['amount_min']=='5000' for x in nf))
    def test_citi_grouped_headers_keep_usd_and_skip_blank_tenor(self):
        s=copy.deepcopy(self.section('CITI')[1])
        s['rows']=[['Currency / New Funds Time Deposit Amount','For New and Existing Customers','For Existing Customers only'],['3 Months (p.a.)','6 Months (p.a.)','9 Months (p.a.)'],['SGD / S$5,000 – S$5,000,000','1.40%','2.00%','1.70%'],['USD / US$5,000 – US$5,000,000','4.00%','4.50%',' ']]
        r=normalize_fx(s)
        self.assertEqual([(x['currency'],x['tenor_value'],x['rate_pct']) for x in r],[('USD',3,'4'),('USD',6,'4.5')])
        self.assertIn('仅现有',r[1]['conditions'])
    def test_sbi_aggregate_limit_not_per_placement(self):
        r=normalize_fx(self.section('SBI')[0])[0];self.assertFalse(r['max_inclusive']);self.assertIn('合计',r['conditions']);self.assertEqual(r['amount_min'],'25000')
    def test_sbi_all_tenors_including_new_rows_and_line_breaks(self):
        s=copy.deepcopy(self.section('SBI')[0])
        s['rows']=[s['rows'][0],['USD','6-month','4.10%','USD 25,000'],['USD','12-\nmonth','4.40%','USD 25,000'],['USD','18-month','4.50%','USD 50,000']]
        r=normalize_fx(s)
        self.assertEqual([(x['tenor_value'],x['rate_pct'],x['amount_min']) for x in r],[(6,'4.1','25000'),(12,'4.4','25000'),(18,'4.5','50000')])
        s['rows'][2]=['USD','12-month','4.40%']
        with self.assertRaisesRegex(ValueError,'SBI'):normalize_fx(s)
    def test_scb_additional_tenor_keeps_all_three_audiences(self):
        s=copy.deepcopy(self.section('SCB')[0]);extra=list(s['rows'][1]);extra[0]='12 months';s['rows'].append(extra)
        r=normalize_fx(s)
        self.assertEqual(len(r),6)
        self.assertEqual(len([x for x in r if x['tenor_value']==12]),3)
    def test_rhb_new_tenor_column_not_silently_dropped(self):
        s=copy.deepcopy(self.section('RHB')[0]);s['rows'][0].insert(-1,'18-Month (p.a.)')
        for row in s['rows'][1:]:row.insert(-1,'4.80%')
        r=normalize_fx(s)
        self.assertEqual(len(r),4*(len(s['rows'])-1))
        self.assertEqual(len([x for x in r if x['tenor_value']==18]),len(s['rows'])-1)
    def test_column_mismatch_is_not_silently_truncated(self):
        for bank in ['BEA','CIMB','RHB']:
            s=copy.deepcopy(self.section(bank)[0]);s['rows'][-1].append('9.99%')
            with self.assertRaises(ValueError,msg=bank):normalize_fx(s)
        s=copy.deepcopy(self.section('ICBC')[0]);s['rows'].append(s['rows'][-1])
        with self.assertRaisesRegex(ValueError,'tiers'):normalize_fx(s)
    def test_icbc_new_third_usd_band_is_not_dropped(self):
        s=copy.deepcopy(self.section('ICBC')[1])
        s['rows']=[['USD','Promotion Rates'],['Tenor','USD500(inclusive)to USD5K','USD5K(inclusive)to USD100K','USD100K (inclusive) & Above'],['6 months','3.90%','4.10%','4.15%'],['1 year','3.90%','4.20%','4.30%']]
        r=normalize_fx(s)
        self.assertEqual(len(r),6)
        self.assertEqual([(x['amount_min'],x['amount_max'],x['rate_pct']) for x in r if x['tenor_value']==12],[('500','5000','3.9'),('5000','100000','4.2'),('100000',None,'4.3')])
        s['rows'][-1].pop()
        with self.assertRaisesRegex(ValueError,'column count'):normalize_fx(s)

    def test_icbc_cny_band_growth_and_reduction(self):
        s=copy.deepcopy(self.section('ICBC')[0])
        s['rows']=[['Amount(RMB)','1 month'],['Below50k (exclusive)','1.15%'],['50k (inclusive) to 100k','1.20%'],['100k (inclusive)& above','1.30%']]
        self.assertEqual([(r['amount_min'],r['amount_max']) for r in normalize_fx(s)],[('500','50000'),('50000','100000'),('100000',None)])
        s['rows']=[s['rows'][0],['500 (inclusive)& above','1.20%']]
        self.assertEqual(len(normalize_fx(s)),1)
        s['rows'].append(['100k (inclusive)& above','1.30%'])
        with self.assertRaisesRegex(ValueError,'overlap'):normalize_fx(s)

    def test_icbc_minimum_follows_source(self):
        s=copy.deepcopy(self.section('ICBC')[0])
        for e in s['extra_evidence']:e['quote']=e['quote'].replace('RMB500','RMB1000')
        r=normalize_fx(s)
        self.assertEqual(r[0]['amount_min'],'1000')
        self.assertIn('1,000',r[0]['conditions'])
        s['extra_evidence']=[]
        with self.assertRaisesRegex(ValueError,'minimum'):normalize_fx(s)

    def test_icbc_usd_four_bands_then_one(self):
        s=copy.deepcopy(self.section('ICBC')[1])
        s['rows']=[['USD','Promotion Rates'],['Tenor','Below USD5K(exclusive)','USD5K(inclusive)to USD50K','USD50K(inclusive)to USD100K','USD100K(inclusive)& Above'],['12 months','3.90%','4.10%','4.20%','4.30%']]
        self.assertEqual(len(normalize_fx(s)),4)
        s['rows']=[s['rows'][0],['Tenor','USD500(inclusive)& Above'],['12 months','4.00%']]
        self.assertEqual(len(normalize_fx(s)),1)

    def test_scb_audience_reduction_and_unknown_label(self):
        s=copy.deepcopy(self.section('SCB')[0]);s['rows'][1][2]='Personal Banking: 4.30% p.a.'
        self.assertEqual([r['audience'] for r in normalize_fx(s)],['personal'])
        s['rows'][1][2]+='\nNew Customer: 4.90%'
        with self.assertRaisesRegex(ValueError,'unrecognized'):normalize_fx(s)

    def test_bea_added_and_removed_tenor_columns(self):
        s=copy.deepcopy(self.section('BEA')[0]);s['rows'][1].append('18 months')
        for r in s['rows'][2:]:r.append('4.20%')
        result=normalize_fx(s)
        self.assertTrue(any(r['tenor_value']==18 for r in result))
        s['rows'][1].pop()
        for r in s['rows'][2:]:r.pop()
        self.assertFalse(any(r['tenor_value']==18 for r in normalize_fx(s)))
    def test_validity_moves_with_source_not_collection_date(self):
        s=copy.deepcopy(self.section('CIMB')[0]);self.assertEqual(promotion_dates(s),('2026-09-01','2026-09-30'))
        for e in s['extra_evidence']:e['quote']=e['quote'].replace('September','November')
        self.assertEqual(promotion_dates(s),('2026-11-01','2026-11-30'))
        r=normalize_fx(s);self.assertTrue(all(x['valid_to']=='2026-11-30' for x in r))
    def test_term_typography_does_not_hide_financial_errors(self):
        task={'kind':'text','comparison_profile':'promo-terms'};c=lambda x:canonical_transcription(x,task)
        self.assertEqual(c('3. “Fresh Funds” required.'),c('"Fresh Funds" required'))
        for a,b in [('1.25%','1.35%'),('USD50,000','USD5,000'),('<=5000','<5000'),('not required','required'),('30 days','3 days')]:self.assertNotEqual(c(a),c(b))
    def test_all_active_source_waves_are_checked(self):
        # Scope regression uses frozen source sections, never follows old local paths.
        for bank in ['ICBC','SCB','CITI','DBS','HSBC','BEA','CIMB','RHB','SBI']:
            sections=self.section(bank)
            self.assertTrue(sections)
            self.assertTrue(all(s['bank']==bank and s['task_ids'] for s in sections))
    def test_expired_boc_and_two_month_bundles_remain_outside_active(self):
        run=historical('fx-deferred');self.assertFalse(any(x['bank']=='BOC' for x in run['offers']));self.assertEqual(len(run['deferred']),32)
        self.assertTrue(all(x['offer']['valid_to']=='2026-09-27' for x in run['deferred'] if x['offer']['bank']=='BOC'))
    def test_all_116_quotes_reach_both_outputs(self):
        p=historical('fx-layout')
        self.assertEqual(sum(r['insertable'] for r in p['details']),116)
        self.assertEqual(sum(len(i['rows']) for h in p['histories'] for i in h['inserts']),116)
        self.assertEqual(sum(len(g['rows']) for b in p['rainbow_blocks'] for g in b['groups']),116)
        self.assertTrue(any(i.get('kind')=='tenor' and i['currency']=='EUR' for h in p['histories'] for i in h['inserts']))

if __name__=='__main__':unittest.main()
