from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from market_rates.common import digest,save,load
from market_rates.board_wave import normalize_section,wave_source_hash,validate_wave
from market_rates.llm_validation import require_complete_sources, validated_offers
from market_rates.ollama_adapter import request_json


class TextOnlyContractTests(unittest.TestCase):
    def fixture(self,path):
        grid=[['GBP','1 MTH'],['5,000 TO 24,999','2.50']]
        page=dict(id='p',bank='SCB',ok=True,complete=True,text='GBP\t1 MTH\n5,000 TO 24,999\t2.50',images=[],image_hashes={})
        task=dict(id='t',bank='SCB',kind='grid',expected=grid,image_hashes={})
        section=dict(id='s',bank='SCB',currency='FX',table=1,product_id='scb-gbp-board',page_id='p',rows=grid,task_ids=['t'])
        source=dict(as_of='2026-10-08',pages=[page],tasks=[task],sections=[section],errors=[],task_hash=digest([task]))
        save(path/'run.json',source)
        save(path/'wave-checks.json',dict(passed=True,extraction_mode='llm_only',source_hash=wave_source_hash(source),task_hash=source['task_hash'],checks=[dict(task='t',lane='llm',task_hash=digest(task),passed=True)]))
        return dict(pages=[page],wide_sources=[str(path)],llm=dict(offers=normalize_section(section,'llm'),coverage_complete=True,unreadable=[]))

    def test_projected_quote_requires_verified_original_grid(self):
        with tempfile.TemporaryDirectory() as temp:
            run=self.fixture(Path(temp));self.assertEqual(validated_offers(run)[1],[])
            run['llm']['offers'][0]['amount_min']='6000'
            with self.assertRaisesRegex(ValueError,'来源表格覆盖不足'):validated_offers(run)

    def test_changed_section_cannot_reuse_an_unchanged_task_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=self.fixture(root);source=load(root/'run.json')
            source['sections'][0]['rows'][1][1]='9.99';save(root/'run.json',source)
            with self.assertRaises(ValueError):validated_offers(run)

    def test_legacy_proof_and_legacy_run_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=self.fixture(root);proof=load(root/'wave-checks.json')
            proof.pop('extraction_mode');save(root/'wave-checks.json',proof)
            with self.assertRaises(ValueError):validate_wave(root)
            run['vlm']=deepcopy(run['llm'])
            with self.assertRaises(ValueError):validated_offers(run)

    def test_transport_refuses_images_before_network(self):
        with patch('urllib.request.build_opener') as network:
            for message in [dict(role='user',content='text',images=['base64']),dict(role='user',content=[{'type':'image_url'}])]:
                with self.assertRaises(ValueError):request_json({},'/api/chat',dict(messages=[message]))
            network.assert_not_called()

    def test_source_wide_failure_reports_bank_without_requiring_a_currency(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            reason='Source changed since discovery; recapture inventory'
            save(root/'run.json',dict(
                qualification_capture_errors=[dict(bank='Maybank',error=reason)],
                coverage=[dict(bank='Maybank',reason=reason,numeric_insertion=False)]))
            with self.assertRaisesRegex(ValueError,'来源尚有采集或核验缺项.*Maybank.*Source changed'):
                require_complete_sources({'wide_sources':[str(root)]})

    def test_only_explicitly_excluded_currency_can_be_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            run={'wide_sources':[str(root)]}
            held=dict(bank='Maybank',numeric_insertion=False,reason='核验未通过',currency='JPY')
            save(root/'run.json',{'coverage':[held]})
            require_complete_sources(run)
            save(root/'run.json',{'coverage':[dict(held,currency='FX')]})
            with self.assertRaisesRegex(ValueError,'来源尚有采集或核验缺项.*Maybank'):
                require_complete_sources(run)
