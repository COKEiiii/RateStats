from copy import deepcopy
from unittest import TestCase

from market_rates.board_source_inventory import direct_board_rows
from market_rates.llm_validation import validated_offers


class BoardSourceInventoryTests(TestCase):
    def test_qualified_partial_wave_does_not_become_complete_delivery(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from market_rates.common import save
        from market_rates.llm_validation import require_complete_sources
        with TemporaryDirectory() as folder:
            save(Path(folder)/'run.json',dict(errors=[],coverage=[dict(bank='UOB',currency='SGD',numeric_insertion=False,reason='model transcription incomplete')]))
            with self.assertRaisesRegex(ValueError,'部分报价'):require_complete_sources(dict(wide_sources=[folder]))

    def pages(self):
        return [dict(id='hlb-sgd-rates',bank='HLB',currency='SGD',rate_type='board',ok=True,complete=True,
                     text='Effective Date: 20-Oct-2026\nTENOR BOARD RATES % (p.a.)\n1 MONTH 0.1\n3 MONTHS 0.2',
                     grid=[['TENOR','BOARD RATES % (p.a.)'],['1 MONTH','0.1'],['3 MONTHS','0.2']]),
                dict(id='hlb-sgd-terms',bank='HLB',currency='SGD',rate_type='board',ok=True,complete=True,
                     text='Minimum S$10,000 (online) or S$100,000 (branch); individual and non-individual customers',grid=[])]

    def test_grid_and_channel_minima_define_every_expected_row(self):
        pages=self.pages();rows=direct_board_rows(pages)
        self.assertEqual(len(rows),4)
        self.assertEqual({(r['channel'],r['amount_min']) for r in rows},{('online','10000'),('branch','100000')})
        run=dict(pages=pages,llm=dict(offers=rows,coverage_complete=True,unreadable=[]))
        accepted,blocked=validated_offers(run);self.assertEqual(len(accepted),4);self.assertFalse(blocked)
        for changed in [rows[:-1],deepcopy(rows)]:
            if len(changed)==4:changed[0]['amount_min']='500'
            with self.assertRaisesRegex(ValueError,'覆盖不足'):validated_offers(dict(run,llm=dict(run['llm'],offers=changed)))

    def test_extra_model_tier_is_not_an_extra_source_row(self):
        pages=self.pages();rows=direct_board_rows(pages);extra=deepcopy(rows[0]);extra['amount_min']='500'
        with self.assertRaisesRegex(ValueError,'没有对应'):
            validated_offers(dict(pages=pages,llm=dict(offers=rows+[extra],coverage_complete=True,unreadable=[])))
