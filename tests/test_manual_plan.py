"""Synthetic manual override reaches report, summary and rainbow plan together."""
from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from market_rates.common import load
from tests.planning_fixture import prepare
from scripts.inspect_weekly_templates import inspect
from market_rates.manual_review import items,record,effective_offers
from market_rates.multi_bank_validation import plan_multi
from market_rates.weekly_history import baseline
from market_rates.table_validation import sha

ROOT=Path(__file__).resolve().parents[1]


class ManualPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        prepare(self.folder/'combined','sgd')
        self.approved=dict(report=str(ROOT/'inputs/baseline-report.xlsx'),rainbow=str(ROOT/'inputs/baseline-rainbow.xlsx'))
        inspect(self.approved['report'],self.approved['rainbow'],self.folder/'template-inspection.json')
    def tearDown(self):self.tmp.cleanup()

    def test_override_reaches_all_three_views_without_touching_evidence_or_approval(self):
        source=self.folder/'combined';run=load(source/'run.json');before=deepcopy(run)
        approved=self.approved;pin=sha(approved['report'])
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp);item=next(i for i in items(run,temp) if i['bank']=='BEA' and i['original']['tenor_value']==6)
            record(run,dict(version=0,id=item['id'],fingerprint=item['fingerprint'],action='replace',fields={'rate_pct':'3.75'},
                author='synthetic test',reason='Not a real bank rate',persist=True),temp)
            with patch('market_rates.manual_review.effective_offers',side_effect=lambda r:effective_offers(r,temp)):
                plan=plan_multi(source,approved['report'],approved['rainbow'],self.folder/'template-inspection.json',temp/'output',approved)
            g=next(g for g in plan['groups'] if g['bank']=='BEA' and g['display_tenor']=='6M')
            self.assertEqual(g['main_pct'],'3.75')
            target=next(u for u in plan['report_updates'] if u['group'] and u['group']['id']==g['id'])
            self.assertEqual(target['group']['main_pct'],'3.75')
            summary=next(s for s in plan['summary'] if s['bank']=='BEA' and s['tenor']=='6M')
            self.assertIn(g['input_rows'][0],summary['input_rows'])
            rainbow=next(t for t in plan['rainbow_groups'] if t['tenor']=='6M')
            self.assertEqual(rainbow['blocks'][0]['bank'],'BEA')
            self.assertTrue(next(r for r in plan['details'] if r['input_row']==g['input_rows'][0])['manual_reviewed'])
        self.assertEqual(run,before);self.assertEqual(sha(approved['report']),pin)

    def test_unquoted_coverage_cannot_insert_numbers(self):
        p=plan_multi(self.folder/'combined',self.approved['report'],self.approved['rainbow'],self.folder/'template-inspection.json',self.folder/'output')
        missing={c['bank'] for c in p['coverage'] if c['numeric_insertion'] is False}
        self.assertEqual(missing,{'DBS','POSB'})
        self.assertFalse(any(r['bank'] in missing for r in p['details']))
        for tenor in p['rainbow_groups']:
            for b in tenor['blocks']:
                if b['bank'] in missing:self.assertTrue(b['missing']);self.assertEqual(b['rank_rate'],'-1')

    def test_explicit_withdrawal_is_labelled_and_excluded_from_all_rankings(self):
        source=self.folder/'combined';run=load(source/'run.json');approved=self.approved
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp);item=next(i for i in items(run,temp) if i['bank']=='BEA' and i['original']['tenor_value']==6)
            record(run,dict(version=0,id=item['id'],fingerprint=item['fingerprint'],action='replace',fields={'availability':'withdrawn'},
                author='synthetic test',reason='Not a real withdrawal',persist=False),temp)
            with patch('market_rates.manual_review.effective_offers',side_effect=lambda r:effective_offers(r,temp)):
                plan=plan_multi(source,approved['report'],approved['rainbow'],self.folder/'template-inspection.json',temp/'output',approved)
            u=next(u for u in plan['report_updates'] if u['bank']=='BEA' and u['tenor']=='6M')
            self.assertIsNone(u['group']);self.assertEqual(u['missing_status'],'已停止提供')
            self.assertFalse(any(g['bank']=='BEA' and g['display_tenor']=='6M' for g in plan['groups']))

if __name__=='__main__':unittest.main()
