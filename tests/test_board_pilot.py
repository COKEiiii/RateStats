from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from market_rates.board_wide_plan import plan
from market_rates.manual_review import items,record,effective_offers
from tests.planning_fixture import prepare
ROOT=Path(__file__).resolve().parents[1]

class BoardMappingTests(unittest.TestCase):
    def test_manual_usd_change_preserves_sgd_and_other_currencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';run=prepare(source,'board')
            base=dict(report_output=str(ROOT/'inputs/baseline-report.xlsx'),rainbow_output=str(ROOT/'inputs/baseline-rainbow.xlsx'))
            with patch('market_rates.board_wide_plan.effective_offers',side_effect=lambda r:effective_offers(r,root)):
                before=plan(source,root/'before',base)
                item=next(i for i in items(run,root) if i['bank']=='HLB' and i['original']['currency']=='USD' and i['original']['tenor_value']==1)
                record(run,dict(version=0,id=item['id'],fingerprint=item['fingerprint'],action='replace',fields={'rate_pct':'3.3'},author='test',reason='synthetic USD-only revision',persist=True),root)
                after=plan(source,root/'after',base)
                self.assertEqual([r for r in before['details'] if r['currency']!='USD'],[r for r in after['details'] if r['currency']!='USD'])
                changed=[r for r in after['details'] if r.get('manual_reviewed')]
                self.assertEqual(len(changed),1);self.assertEqual(changed[0]['rate_pct'],'3.3')
    def test_source_failure_blocks_numeric_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);prepare(root/'source','board')
            with patch('market_rates.board_wide_plan.effective_offers',return_value=([],['failed-source'],[])):
                with self.assertRaisesRegex(ValueError,'Unresolved'):plan(root/'source',root/'out',{})
            self.assertFalse((root/'out').exists())
