import hashlib
import tempfile
import unittest
from pathlib import Path
from market_rates.common import save,load,digest
from market_rates.board_wave import validate_wave,wave_source_hash
from scripts.merge_board_waves import merge,ensure_merged

class MergeTextWaveTests(unittest.TestCase):
    def source(self,root,key):
        root.mkdir();ev=root/'evidence';ev.mkdir()
        text='SGD 6 Months 1.75% p.a.'
        file=ev/(key+'.html');file.write_text('<table>'+text+'</table>',encoding='utf8')
        page=dict(id=key,bank='SCB',url='https://www.sc.com/sg/',captured_at='2026-10-13T00:00:00+00:00',text=text,images=[],image_hashes={},ok=True,complete=True,sha256=hashlib.sha256(text.encode()).hexdigest())
        task=dict(id=key+'-task',bank='SCB',page_id=key,kind='text',expected=text,images=[],image_hashes={})
        section=dict(id=key+'-section',bank='SCB',currency='SGD',rows=[[text]],page_id=key,task_ids=[task['id']])
        run=dict(id=key,as_of='2026-10-13',bank_dates={'SCB':'2026-10-13'},pages=[page],tasks=[task],sections=[section],errors=[],task_hash=digest([task]),source_file_hashes={file.name:hashlib.sha256(file.read_bytes()).hexdigest()})
        save(root/'run.json',run)
        save(root/'wave-checks.json',dict(passed=True,extraction_mode='llm_only',source_hash=wave_source_hash(run),task_hash=run['task_hash'],checks=[dict(task=task['id'],lane='llm',passed=True,task_hash=digest(task))],errors=[]))
        return root

    def test_merged_proof_preserves_all_source_files_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);sources=[self.source(root/k,k) for k in ['a','b']]
            out=root/'merged';result=merge(sources,out)
            self.assertEqual(validate_wave(out),result)
            self.assertEqual(set(result['source_file_hashes']),{'a.html','b.html'})
            self.assertEqual(len(load(out/'wave-checks.json')['checks']),2)
            (out/'evidence/a.html').write_text('changed',encoding='utf8')
            with self.assertRaises(ValueError):validate_wave(out)

    def test_legacy_source_proof_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=self.source(root/'a','a')
            proof=load(source/'wave-checks.json');proof.pop('extraction_mode');save(source/'wave-checks.json',proof)
            with self.assertRaises(ValueError):merge([source],root/'merged')
            self.assertFalse((root/'merged').exists())

    def test_resume_archives_incomplete_merge_and_rebuilds_from_verified_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=self.source(root/'a','a');out=root/'merged'
            save(out/'run.json',{'partial':True})
            ensure_merged([source],out,root)
            self.assertEqual(len(validate_wave(out)['tasks']),1)
            archives=list(root.glob('merged-failed-*'));self.assertEqual(len(archives),1)
            self.assertEqual(load(archives[0]/'run.json'),{'partial':True})
            ensure_merged([source],out,root)
            self.assertEqual(len(list(root.glob('merged-failed-*'))),1)
            with self.assertRaises(ValueError):ensure_merged([source],out,root/'other')
