"""Historical comparison follows audited corrections of one live collection."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from market_rates.report_format import find_previous


class PreviousCorrectionChainTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.current=self.root/'current.xlsx'
        self.current.write_bytes(b'current')

    def workflow(self,name,day='2026-10-07',stamp='2026-10-07T10:00:00',*,parent=None,mode=None,new_keys=False):
        folder=self.root/('outputs' if parent else 'runs')/name
        folder.mkdir(parents=True)
        data=dict(project=str(self.root),mode=mode or ('correction' if parent else 'live'),status='complete',
                  source_dates={key:day for key in ('sgd','board','fx')},output_hashes={})
        if parent:data['source_workflow']=str(parent)
        else:data['started_at']=stamp
        for kind in ('report','rainbow'):
            path=folder/(kind+'.xlsx');path.write_bytes((name+'/'+kind).encode())
            data[kind]=str(path)
            data['output_hashes'][('raw_' if new_keys else '')+kind]=hashlib.sha256(path.read_bytes()).hexdigest()
        (folder/'workflow.json').write_text(json.dumps(data),encoding='utf8')
        return folder,data

    def replace_state(self,folder,data):
        (folder/'workflow.json').write_text(json.dumps(data),encoding='utf8')

    def selected(self,**kwargs):
        return find_previous(self.root,self.current,date(2026,10,8),**kwargs)

    def test_same_collection_correction_replaces_uncorrected_report(self):
        run,_=self.workflow('all-old')
        _,fixed=self.workflow('fixed',parent=run)
        self.assertEqual(self.selected(),Path(fixed['report']))

    def test_multi_hop_chain_uses_unique_tip(self):
        run,_=self.workflow('all-old')
        first,_=self.workflow('first',parent=run)
        _,second=self.workflow('second',parent=first)
        self.assertEqual(self.selected(),Path(second['report']))

    def test_new_four_workbook_hash_keys_supported(self):
        run,base=self.workflow('all-old',new_keys=True)
        self.assertEqual(self.selected(),Path(base['report']))
        _,fixed=self.workflow('fixed',parent=run,new_keys=True)
        self.assertEqual(self.selected(),Path(fixed['report']))

    def test_bad_report_or_rainbow_hash_correction_is_not_accepted(self):
        for kind in ('report','rainbow'):
            with self.subTest(kind=kind):
                run,base=self.workflow('all-'+kind)
                _,fixed=self.workflow('fixed-'+kind,parent=run)
                Path(fixed[kind]).write_bytes(b'tampered')
                self.assertNotEqual(self.selected(),Path(fixed['report']))

    def test_missing_hash_and_failed_and_mixed_date_corrections_rejected(self):
        run,base=self.workflow('all-old')
        for name in ('missing-hash','failed','mixed-date'):
            folder,data=self.workflow(name,parent=run)
            if name=='missing-hash':data['output_hashes'].pop('rainbow')
            elif name=='failed':data['status']='failed'
            else:data['source_dates']['fx']='2026-10-06'
            self.replace_state(folder,data)
        self.assertEqual(self.selected(),Path(base['report']))

    def test_orphan_and_wrong_project_corrections_rejected(self):
        run,base=self.workflow('all-old')
        self.workflow('orphan',parent=self.root/'missing-parent')
        folder,data=self.workflow('foreign-project',parent=run)
        data['project']=str(self.root/'different-project');self.replace_state(folder,data)
        self.assertEqual(self.selected(),Path(base['report']))

    def test_invalid_ancestor_does_not_admit_valid_looking_child(self):
        run,base=self.workflow('all-old')
        first,data=self.workflow('first',parent=run)
        Path(data['rainbow']).write_bytes(b'tampered ancestor')
        self.workflow('second',parent=first)
        self.assertEqual(self.selected(),Path(base['report']))

    def test_unrelated_and_formatted_outputs_not_new_observations(self):
        old,_=self.workflow('all-old',day='2026-10-06',stamp='2026-10-06T10:00:00')
        self.workflow('old-correction',day='2026-10-06',parent=old)
        current,data=self.workflow('all-new')
        formatted,_=self.workflow('formatted-new',parent=current,mode='format')
        os.utime(formatted/'workflow.json',(2100000000,2100000000))
        self.assertEqual(self.selected(),Path(data['report']))

    def test_before_selects_collection_time_not_correction_stamp_or_mtime(self):
        old,_=self.workflow('all-early',stamp='2026-10-07T09:00:00')
        folder,fixed=self.workflow('early-fixed',parent=old)
        fixed['started_at']='2099-01-01T00:00:00';self.replace_state(folder,fixed)
        os.utime(folder/'workflow.json',(2100000000,2100000000))
        _,later=self.workflow('all-later',stamp='2026-10-07T11:00:00')
        self.assertEqual(self.selected(before='2026-10-07T10:00:00'),Path(fixed['report']))
        self.assertEqual(self.selected(before='2026-10-07T12:00:00'),Path(later['report']))

    def test_independent_verified_correction_branches_require_explicit_baseline(self):
        run,_=self.workflow('all-old')
        self.workflow('branch-a',parent=run)
        self.workflow('branch-b',parent=run)
        with self.assertRaisesRegex(ValueError,'Multiple verified correction branches'):
            self.selected()

    def test_current_correction_chain_infers_current_collection_time(self):
        _,earlier=self.workflow('all-early',stamp='2026-10-07T09:00:00')
        run,_=self.workflow('all-current',stamp='2026-10-07T11:00:00')
        first,_=self.workflow('first-current',parent=run)
        _,second=self.workflow('second-current',parent=first)
        self.assertEqual(find_previous(self.root,second['report'],date(2026,10,7)),Path(earlier['report']))

    def test_hash_modified_live_output_not_a_complete_baseline(self):
        _,earlier=self.workflow('all-earlier',day='2026-10-06',stamp='2026-10-06T09:00:00')
        _,broken=self.workflow('all-modified',new_keys=True)
        Path(broken['report']).write_bytes(b'changed')
        self.assertEqual(self.selected(),Path(earlier['report']))

    def correction_original(self,folder,data):
        source=self.root/'original-generated';source.mkdir()
        source_plan=dict(as_of='2026-10-07',date_column_shift=0)
        for kind in ('report','rainbow'):
            dest=source/(kind+'.xlsx');dest.write_bytes(Path(data[kind]).read_bytes())
            source_plan[kind+'_output']=str(dest)
        plan_file=source/'table-validation-plan.json';plan_file.write_text(json.dumps(source_plan),encoding='utf8')
        packaging=dict(correction_kind='sgd_same_date_correction',sgd_correction_plan=str(plan_file),
                       **{kind+'_output':data[kind] for kind in ('report','rainbow')})
        (folder/'table-validation-plan.json').write_text(json.dumps(packaging),encoding='utf8')
        return source_plan

    def test_changed_published_copy_can_use_explicit_original_pair_with_same_hashes(self):
        run,_=self.workflow('all-old')
        folder,data=self.workflow('fixed',parent=run)
        original=self.correction_original(folder,data)
        Path(data['report']).write_bytes(b'user edited this copy')
        self.assertEqual(self.selected(),Path(original['report_output']))
        self.assertEqual(Path(data['report']).read_bytes(),b'user edited this copy')
        manifest=json.loads((folder/'workflow.json').read_text())
        self.assertEqual(manifest['output_hashes'],data['output_hashes'])

    def test_fallback_pair_requires_both_original_hashes(self):
        run,base=self.workflow('all-old')
        folder,data=self.workflow('fixed',parent=run)
        original=self.correction_original(folder,data)
        Path(data['report']).write_bytes(b'changed published')
        Path(original['rainbow_output']).write_bytes(b'changed original rainbow')
        self.assertEqual(self.selected(),Path(base['report']))

    def test_same_hash_copy_outside_explicit_packaging_chain_not_discovered(self):
        run,base=self.workflow('all-old')
        folder,data=self.workflow('fixed',parent=run)
        self.correction_original(folder,data)
        (folder/'table-validation-plan.json').unlink()
        Path(data['report']).write_bytes(b'changed published')
        self.assertEqual(self.selected(),Path(base['report']))
