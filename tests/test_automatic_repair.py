import hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from market_rates.common import load,save,digest
from market_rates.evidence_repair import source_cell_cuts,repair
from scripts.verify_with_repair import check
from scripts.verify_board_wave import VerificationMismatch


class AutomaticRepairTests(unittest.TestCase):
    def make(self,root,borders=True):
        ev=root/'evidence';ev.mkdir()
        doc=pymupdf.open();page=doc.new_page(width=320,height=40)
        if borders:
            for x in [80,160,240]:page.draw_line((x,0),(x,40),color=(.7,.7,.7),width=1)
        page.insert_text((4,22),'5,000',fontsize=9)
        for x in [85,165,245]:page.insert_text((x,22),'-',fontsize=9)
        page.get_pixmap(matrix=pymupdf.Matrix(2,2)).save(ev/'row.png');doc.close()
        sha=hashlib.sha256((ev/'row.png').read_bytes()).hexdigest()
        task=dict(id='uob-row',bank='UOB',kind='grid',expected=[['5,000','-','-','-']],images=['row.png'],image_hashes={'row.png':sha},page_id='uob')
        run=dict(id='test',tasks=[task],task_hash=digest([task]),pages=[dict(id='uob',images=['row.png'],image_hashes={'row.png':sha})],sections=[dict(id='s',task_ids=['uob-row'])])
        run['evidence_hash']=digest(run['pages']);save(root/'run.json',run)
        return run

    def fail(self,root,error='Literal source/transcription mismatch'):
        run=load(root/'run.json')
        errors=[dict(task=t['id'],lane='llm',task_hash=digest(t),error=error,passed=False) for t in run['tasks']]
        save(root/'wave-checks.json',dict(task_hash=run['task_hash'],passed=False,errors=errors,checks=errors))

    def test_source_geometry_unequal_columns_no_guessing(self):
        boxes=[dict(x=10,y=5,width=50,height=30),dict(x=60,y=5,width=100,height=30),dict(x=160,y=5,width=40,height=30)]
        self.assertEqual(source_cell_cuts(boxes,380),[100,300])
        boxes[1]['x']=62;self.assertIsNone(source_cell_cuts(boxes,380))
        boxes[1]['x']=60;boxes[1]['height']=60;self.assertIsNone(source_cell_cuts(boxes,380))

    def test_text_retry_keeps_original_cells_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=self.make(root);self.fail(root)
            image=(root/'evidence/row.png').read_bytes()
            event=repair(root,'source');self.assertEqual(len(event['actions']),1)
            task=load(root/'run.json')['tasks'][0]
            self.assertEqual(task['expected'],before['tasks'][0]['expected'])
            self.assertEqual(task['images'],before['tasks'][0]['images'])
            self.assertEqual(task['text_rows_per_request'],1)
            self.assertEqual((root/'evidence/row.png').read_bytes(),image)
            self.assertTrue((Path(event['archive'])/'run.json').exists())

    def test_repeat_loop_skips_same_image_prompt_and_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.make(root);calls=[]
            def verify(r,c):
                t=load(r/'run.json')['tasks'][0];calls.append(t)
                if not t.get('strict_row_layout'):
                    self.fail(r,'prediction aborted, token repeat limit reached');raise VerificationMismatch('failed')
                save(r/'wave-checks.json',dict(passed=True,task_hash=load(r/'run.json')['task_hash'],checks=[],errors=[]))
            with patch('scripts.verify_with_repair.verify',side_effect=verify):check(root,{})
            self.assertEqual(len(calls),2);self.assertEqual(load(root/'automatic-repair.json')['status'],'complete')

    def test_numeric_mismatch_remains_failed_and_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=self.make(root);count=[]
            def verify(r,c):
                count.append(1);self.fail(r);raise VerificationMismatch('rate does not match')
            with patch('scripts.verify_with_repair.verify',side_effect=verify):
                with self.assertRaises(VerificationMismatch):check(root,{})
                self.assertEqual(len(count),3)
                with self.assertRaises(VerificationMismatch):check(root,{})
                self.assertEqual(len(count),4) # Resume does not repeat exhausted repairs.
            self.assertEqual(load(root/'run.json')['tasks'][0]['expected'],before['tasks'][0]['expected'])
            self.assertEqual(load(root/'automatic-repair.json')['status'],'needs_review')
            self.assertIn('未把识别错误', (root/'自动修复结果.txt').read_text(encoding='utf-8-sig'))

    def test_repeated_source_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.make(root,False);self.fail(root)
            self.assertTrue(repair(root,'source')['actions']);self.fail(root)
            event=repair(root,'source');self.assertFalse(event['actions']);self.assertTrue(event['unresolved'])
            self.assertEqual(load(root/'run.json')['tasks'][0]['images'],['row.png'])

    def test_integrity_error_is_never_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.make(root);self.fail(root)
            (root/'evidence/row.png').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'证据已变化'):repair(root,'source')
            with patch('scripts.verify_with_repair.verify',side_effect=ValueError('Evidence modified')):
                with self.assertRaises(ValueError):check(root,{})
            self.assertEqual(load(root/'automatic-repair.json')['status'],'error')

    def test_network_failure_does_not_crop_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.make(root);self.fail(root,'Connection refused')
            self.assertFalse(repair(root,'source')['actions'])
            self.assertFalse(load(root/'run.json')['tasks'][0].get('images_are_cells'))

    def test_mixed_grid_and_pdf_failures_are_repaired_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=self.make(root)
            doc=pymupdf.open();page=doc.new_page()
            literal='Minimum amount USD 5000 with new funds only.'
            page.insert_text((40,40),literal)
            doc.save(root/'evidence/terms.pdf');page.get_pixmap().save(root/'evidence/terms.png');doc.close()
            sha=hashlib.sha256((root/'evidence/terms.png').read_bytes()).hexdigest()
            task=dict(id='terms-text',bank='UOB',kind='text',expected=literal,images=['terms.png'],image_hashes={'terms.png':sha},page_id='terms')
            run['tasks'].append(task);run['pages'].append(dict(id='terms',images=['terms.png'],image_hashes={'terms.png':sha}))
            run['sections'][0]['task_ids'].append(task['id']);run.update(task_hash=digest(run['tasks']),evidence_hash=digest(run['pages']))
            save(root/'run.json',run);self.fail(root)
            result=repair(root,'source');self.assertEqual(len(result['actions']),2)
            after=load(root/'run.json');self.assertTrue(after['tasks'][0]['text_rows_per_request']==1)
            parts=[t for t in after['tasks'] if t['kind']=='text']
            self.assertEqual(' '.join(t['expected'] for t in parts),literal)
            self.assertTrue(all(t['text_chunk_size']==600 for t in parts))
            self.assertEqual(set(after['sections'][0]['task_ids']),{t['id'] for t in after['tasks']})
            self.fail(root);self.assertFalse(repair(root,'source')['actions'])

    def test_changed_task_hash_is_never_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=self.make(root);self.fail(root)
            run['tasks'][0]['expected']=[['invented']];save(root/'run.json',run)
            with self.assertRaisesRegex(ValueError,'不匹配'):repair(root,'source')
