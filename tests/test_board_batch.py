import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from copy import deepcopy
from decimal import Decimal
from market_rates.board_batch import bands,minimums,reference,validate,normalize_table
from market_rates.board_batch_plan import present_band,label_tenor,interval_rows,winner_minimums

class BoardBatchTests(unittest.TestCase):
    def test_failed_source_read_is_retried_but_never_accepted_without_validation(self):
        from market_rates.board_batch import read_validated_parts
        p=dict(bank='HLB',currency='AUD');ref={'expected':'source'}
        with tempfile.TemporaryDirectory() as tmp,patch('market_rates.board_batch.read_parts',side_effect=[({'bad':True},[]),({'good':True},[])]) as read,patch('market_rates.board_batch.validate',side_effect=[ValueError('rows'),None]) as gate:
            parsed,_=read_validated_parts(p,{},Path(tmp),'llm','local',{},ref)
            self.assertEqual(parsed,{'good':True});self.assertEqual(gate.call_count,2)
            self.assertEqual([c.kwargs['repair_attempt'] for c in read.call_args_list],[0,1])
            self.assertTrue((Path(tmp)/'literal-HLB-AUD-llm.json').exists())
            self.assertTrue((Path(tmp)/'literal-HLB-AUD-llm-repair1.json').exists())
        with tempfile.TemporaryDirectory() as tmp,patch('market_rates.board_batch.read_parts',return_value=({'bad':True},[])) as read,patch('market_rates.board_batch.validate',side_effect=ValueError('rows')):
            with self.assertRaises(ValueError):read_validated_parts(p,{},Path(tmp),'llm','local',{},ref)
            self.assertEqual(read.call_count,3)

    def test_amount_bounds_and_equivalent_currency(self):
        self.assertEqual(bands('5,000至 50,000以下'),(Decimal(5000),Decimal(50000)))
        self.assertEqual(bands('500,000及以上'),(Decimal(500000),None))
        self.assertEqual(minimums('HLB','CNH','S$50,000 equivalent'),[(Decimal(50000),'unknown','SGD',True)])
        self.assertEqual(minimums('HLB','HKD',None),[(None,'unknown','HKD',False)])
        with self.assertRaises(ValueError):minimums('HLB','GBP','US$50,000')

    def test_each_wrong_cell_and_missing_unit_stops(self):
        a=dict(currency_label='CAD',annual_header='年利率（%）',effective_date='2026-06-22',eligibility_text='仅限个人存款客户',minimum_text='5,000',tenors=['1个月','3个月'],rows=[['5,000以上','1.20','/']])
        validate(a,a)
        for field,value in [('annual_header','Fixed *'),('currency_label','USD'),('minimum_text','50,000'),('rows',[['5,000以上','1.21','/']]),('rows',[['5,000以上','1.20','/','/']])]:
            b=deepcopy(a);b[field]=value
            with self.assertRaises(ValueError,msg=field):validate(b,a)

    def test_absent_last_boc_cell_is_unquoted_without_changing_source(self):
        packet = dict(bank='BOC', currency='CNY', layout='horizontal', pages=['rates', 'terms'])
        grid = [
            ['货币', '存款金额', '年利率（%）', '年利率（%）'],
            ['货币', '存款金额', '24个月', '36个月'],
            ['人民币 (RMB)', '200,000及以上', '1.45'],
        ]
        pages = {
            'rates': dict(id='rates', grid=grid, text='2026-06-22'),
            'terms': dict(grid=[['币种', '起存金额'], ['RMB', '3,000']]),
        }
        ref = reference(packet, pages)
        self.assertEqual(ref['rows'], [['200,000及以上', '1.45', '']])
        self.assertEqual(len(grid[2]), 3)
        validate(ref, ref)
        for bad_cell in ['1.45', '/']:
            wrong = deepcopy(ref)
            wrong['rows'][0][-1] = bad_cell
            with self.assertRaisesRegex(ValueError, 'rows'):
                validate(wrong, ref)
        offers, raw = normalize_table(packet, ref, pages, 'llm')
        self.assertEqual([(r['tenor_value'], r['rate_pct']) for r in offers], [(24, '1.45')])
        self.assertEqual(raw[-1]['rate_literal'], '')

    def test_intersect_minimums_and_preserve_tiny_rates(self):
        p=dict(bank='BOC',currency='EUR',layout='horizontal',pages=['rates','terms'])
        parsed=dict(minimum_text='5,000',tenors=['1个月','3个月'],effective_date='2026-06-22',rows=[['5,000以下','0.0001','/'],['5,000至 50,000以下','0.0001','/']])
        pages={k:dict(images=[k+'.png']) for k in p['pages']}
        offers,raw=normalize_table(p,parsed,pages,'llm')
        self.assertEqual(len(raw),4);self.assertEqual(len(offers),1)
        self.assertEqual(offers[0]['rate_pct'],'0.0001');self.assertEqual(offers[0]['amount_min'],'5000');self.assertFalse(offers[0]['max_inclusive'])
        self.assertEqual(Decimal(offers[0]['rate_pct'])/100,Decimal('.000001'))

    def test_no_hidden_maximum_inside_historical_band(self):
        rs=[dict(rate_pct='3.2',input_row=4),dict(rate_pct='3.3',input_row=5)]
        with self.assertRaises(ValueError):present_band(rs,'input','USD <50000')
        self.assertEqual(present_band([],'input','empty')['expected'],'-')

    def test_exact_legacy_heading_map_no_tenor_guess(self):
        self.assertEqual(label_tenor('Tenor: 3 monthR'),'3M')
        self.assertEqual(label_tenor('32M'),'32M')
        self.assertIsNone(label_tenor('Tenor: 2 monthR'))

    def test_summary_minimum_belongs_to_winning_rate(self):
        rows=[dict(tenor_value=1,tenor_unit='M',rate_pct='3.2',amount_min='2000'),dict(tenor_value=1,tenor_unit='M',rate_pct='3.3',amount_min='500000')]
        self.assertEqual(winner_minimums(rows),'≥500,000')

if __name__=='__main__':unittest.main()
