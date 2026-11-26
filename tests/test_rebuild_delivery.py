"""A format-only rebuild preserves evidence, acquisition dates and old state."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.common import load, save
from market_rates.table_validation import sha
from scripts import rebuild_delivery as rebuild


class RebuildDeliveryTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work=self.root/'runs/all-unit';self.work.mkdir(parents=True)
        self.output=self.root/'outputs/original';self.output.mkdir(parents=True)
        self.files={}
        for kind in ('raw_report','raw_rainbow','formatted_report','formatted_rainbow'):
            path=self.output/(kind+'.xlsx');path.write_bytes(('old '+kind).encode());self.files[kind]=str(path)
        self.delivery=dict(status='complete',previous='old-baseline.xlsx',files=self.files,
                           hashes={kind:sha(path) for kind,path in self.files.items()})
        save(self.output/'delivery.json',self.delivery)
        templates={'hashes':{}}
        for kind in ('report','rainbow'):
            path=self.work/'format-templates'/(kind+'.xlsx');path.parent.mkdir(exist_ok=True)
            path.write_bytes(('template '+kind).encode());templates[kind]=str(path);templates['hashes'][kind]=sha(path)
        save(self.work/'format-templates/templates.json',templates)
        self.state=dict(project=str(self.root),mode='live',status='complete',output=str(self.output),
                        started_at='2026-10-08T09:00:00',finished_at='2026-10-08T10:00:00',
                        source_dates={key:'2026-10-08' for key in ('sgd','board','fx')},
                        format_templates=templates,results={'delivery':deepcopy(self.delivery),'sgd':{'source_run':'unchanged'}},
                        report=self.files['raw_report'],rainbow=self.files['raw_rainbow'],
                        formatted_report=self.files['formatted_report'],formatted_rainbow=self.files['formatted_rainbow'],
                        output_hashes=deepcopy(self.delivery['hashes']))
        self.manifest=self.work/'workflow.json';save(self.manifest,self.state)
        save(self.root/'outputs/latest-all.json',{'workflow':str(self.work),'output':str(self.output)})
        save(self.root/'outputs/latest-all-run.json',{'workflow':str(self.work),'status':'complete'})
        self.metadata={path:path.read_bytes() for path in (self.manifest,self.root/'outputs/latest-all.json',self.root/'outputs/latest-all-run.json')}
        self.original_files={Path(path):Path(path).read_bytes() for path in self.files.values()}

    def fake_publish(self,report,rainbow,templates,output,**kwargs):
        self.assertEqual(Path(report),Path(self.files['raw_report']))
        self.assertEqual(Path(rainbow),Path(self.files['raw_rainbow']))
        self.assertEqual(kwargs['started_at'],self.state['started_at'])
        self.assertNotIn('previous_path',kwargs)
        output=Path(output);output.mkdir()
        files={}
        for kind in self.files:
            path=output/(kind+'.xlsx')
            if kind=='raw_report':path.write_bytes(Path(report).read_bytes())
            elif kind=='raw_rainbow':path.write_bytes(Path(rainbow).read_bytes())
            else:path.write_bytes(('new '+kind).encode())
            files[kind]=str(path)
        result=dict(status='complete',previous=None,comparison_mode='latest_two_columns',files=files,
                    hashes={kind:sha(path) for kind,path in files.items()})
        save(output/'delivery.json',result)
        return result

    def call(self):
        return rebuild.rebuild_delivery(self.work,project_root=self.root,no_open=True)

    def test_success_publishes_four_and_keeps_raw_sources_dates_and_backup(self):
        with patch.object(rebuild,'project_lock',return_value=nullcontext()) as lock, \
             patch.object(rebuild,'publish_four',side_effect=self.fake_publish):
            result=self.call()
        lock.assert_called_once()
        for path,contents in self.original_files.items():self.assertEqual(path.read_bytes(),contents)
        for field in ('started_at','finished_at','source_dates'):
            self.assertEqual(result[field],self.state[field])
        self.assertEqual(result['results']['sgd'],self.state['results']['sgd'])
        self.assertEqual(result['delivery_revisions'][-1]['comparison_mode'],'latest_two_columns')
        self.assertEqual(len(list(Path(result['output']).glob('*.xlsx'))),4)
        self.assertNotEqual(result['output'],self.state['output'])
        self.assertEqual(load(self.root/'outputs/latest-all.json')['output'],result['output'])
        backup=Path(result['delivery_revisions'][-1]['workflow_backup'])
        self.assertEqual(backup.read_bytes(),self.metadata[self.manifest])
        self.assertEqual(load(self.manifest),result)
        self.assertTrue((Path(result['output'])/'查看运行记录.html').is_file())
        self.assertTrue((Path(result['output'])/'运行记录.txt').is_file())

    def test_record_packaging_failure_does_not_commit_new_delivery_pointers(self):
        with patch.object(rebuild,'project_lock',return_value=nullcontext()), \
             patch.object(rebuild,'publish_four',side_effect=self.fake_publish), \
             patch.object(rebuild,'package_records',side_effect=OSError('record export failed')):
            with self.assertRaisesRegex(OSError,'record export failed'):self.call()
        for path,contents in self.metadata.items():self.assertEqual(path.read_bytes(),contents)

    def test_formatter_failure_does_not_change_workflow_or_pointers(self):
        with patch.object(rebuild,'project_lock',return_value=nullcontext()), \
             patch.object(rebuild,'publish_four',side_effect=ValueError('format failed')):
            with self.assertRaisesRegex(ValueError,'format failed'):self.call()
        for path,contents in self.metadata.items():self.assertEqual(path.read_bytes(),contents)
        for path,contents in self.original_files.items():self.assertEqual(path.read_bytes(),contents)
        self.assertFalse((self.work/'delivery-revisions').exists())

    def test_damaged_old_delivery_is_rejected_before_rebuild(self):
        Path(self.files['raw_report']).write_bytes(b'modified')
        with patch.object(rebuild,'project_lock',return_value=nullcontext()),patch.object(rebuild,'publish_four') as publish:
            with self.assertRaisesRegex(ValueError,'缺失或被修改'):self.call()
            publish.assert_not_called()
        for path,contents in self.metadata.items():self.assertEqual(path.read_bytes(),contents)

    def test_missing_frozen_snapshot_never_loads_current_external_template(self):
        (self.work/'format-templates/templates.json').unlink()
        with patch.object(rebuild,'project_lock',return_value=nullcontext()),patch.object(rebuild,'publish_four') as publish:
            with self.assertRaisesRegex(ValueError,'已冻结'):self.call()
            publish.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(),self.metadata[self.manifest])

    def test_non_complete_workflow_rejected(self):
        state=dict(self.state,status='failed');save(self.manifest,state)
        before=self.manifest.read_bytes()
        with patch.object(rebuild,'project_lock',return_value=nullcontext()),patch.object(rebuild,'publish_four') as publish:
            with self.assertRaisesRegex(ValueError,'已完成'):self.call()
            publish.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(),before)

    def test_metadata_commit_failure_rolls_back_original_state_and_pointers(self):
        real_save=rebuild.save
        def fail_latest(path,value):
            if Path(path)==self.root/'outputs/latest-all.json':raise OSError('metadata write failed')
            return real_save(path,value)
        with patch.object(rebuild,'project_lock',return_value=nullcontext()), \
             patch.object(rebuild,'publish_four',side_effect=self.fake_publish), \
             patch.object(rebuild,'save',side_effect=fail_latest):
            with self.assertRaisesRegex(OSError,'metadata write failed'):self.call()
        for path,contents in self.metadata.items():self.assertEqual(path.read_bytes(),contents)
        for path,contents in self.original_files.items():self.assertEqual(path.read_bytes(),contents)

    def test_inconsistent_state_delivery_cannot_publish(self):
        state=deepcopy(self.state);state['output_hashes']['raw_report']='wrong';save(self.manifest,state)
        before=self.manifest.read_bytes()
        with patch.object(rebuild,'project_lock',return_value=nullcontext()),patch.object(rebuild,'publish_four') as publish:
            with self.assertRaisesRegex(ValueError,'校验记录不一致'):self.call()
            publish.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(),before)
