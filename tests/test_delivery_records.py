"""Records must be traceable, repeatable and never modify the Excel delivery."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.common import load, save
from market_rates.delivery_records import package_records
from market_rates.table_validation import sha


class DeliveryRecordsTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.work=self.root/'runs/all-test';self.work.mkdir(parents=True)
        self.output=self.root/'outputs/final';self.output.mkdir(parents=True)
        files={}
        for kind in ('raw_report','raw_rainbow','formatted_report','formatted_rainbow'):
            p=self.output/(kind+'.xlsx');p.write_bytes(kind.encode());files[kind]=str(p)
        self.hashes={key:sha(value) for key,value in files.items()}
        receipt=dict(status='complete',files=files,hashes=self.hashes,comparison_mode='latest_two_columns',audit_files=[])
        save(self.output/'delivery.json',receipt)
        self.state=dict(project=str(self.root),output=str(self.output),status='complete',mode='live',
                        started_at='2026-10-08T09:00:00+08:00',finished_at='2026-10-08T10:00:00+08:00',
                        completed=['新元促销','格式对齐与四份输出'],source_dates={'sgd':'2026-10-08'},results={'delivery':receipt})
        save(self.work/'workflow.json',self.state)
        self.log=self.work/'01采集.log';self.log.write_text('原始采集\n正常结束\n',encoding='utf-8')
        (self.work/'识别修复汇总.txt').write_text('原始修复记录',encoding='utf-8-sig')

    def test_packages_real_logs_workflow_and_chinese_entry_without_touching_excel(self):
        before=self.log.read_bytes()
        result=package_records(self.work)
        self.assertTrue(Path(result['summary']).read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn('原任务开始：2026-10-08',Path(result['summary']).read_text(encoding='utf-8-sig'))
        log=next(x for x in result['records'] if x['source']==str(self.log))
        self.assertEqual((self.output/log['archive']).read_bytes(),before)
        self.assertEqual(self.log.read_bytes(),before)
        self.assertEqual(self.hashes,{key:sha(p) for key,p in self.state['results']['delivery']['files'].items()})
        self.assertEqual(len(list(self.output.glob('*.xlsx'))),4)
        self.assertEqual((self.output/'识别修复汇总.txt').read_bytes(),(self.work/'识别修复汇总.txt').read_bytes())

    def test_unchanged_inputs_reuse_snapshot_and_modified_copies_are_reported(self):
        first=package_records(self.work)
        self.assertEqual(package_records(self.work),first)
        self.assertEqual(len([p for p in (self.output/'运行记录').iterdir() if p.is_dir()]),1)
        log=next(x for x in first['records'] if x['source']==str(self.log))
        (self.output/log['archive']).write_text('edited',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'副本缺失或被修改'):
            package_records(self.work)

    def test_changed_source_creates_new_snapshot_preserving_old_record(self):
        first=package_records(self.work)
        log=next(x for x in first['records'] if x['source']==str(self.log))
        original=(self.output/log['archive']).read_bytes()
        self.log.write_text('new run record',encoding='utf-8')
        second=package_records(self.work)
        self.assertNotEqual(first['fingerprint'],second['fingerprint'])
        self.assertEqual((self.output/log['archive']).read_bytes(),original)

    def test_large_records_are_indexed_and_html_escapes_labels(self):
        phase=self.root/'outputs/phase';phase.mkdir()
        large=phase/'a&b.json';large.write_bytes(b'X'*100)
        state=deepcopy(self.state);state['results']['sgd']={'output':str(phase)}
        with patch('market_rates.delivery_records.COPY_LIMIT',10):
            result=package_records(self.work,state=state)
        entry=next(x for x in result['records'] if x['source']==str(large))
        self.assertFalse(entry['copied']);self.assertNotIn('archive',entry)
        page=Path(result['index']).read_text(encoding='utf-8')
        self.assertIn('a&amp;b.json',page)
        self.assertIn('查看原件（大记录）',page)

    def test_rebuild_snapshot_describes_reused_capture_and_new_output_state(self):
        state=deepcopy(self.state)
        state['delivery_revisions']=[{'rebuilt_at':'2026-10-08T11:00:00'}]
        result=package_records(self.work,state=state)
        self.assertIn('格式重建（沿用原采集数据）',Path(result['summary']).read_text(encoding='utf-8-sig'))
        snapshot=next(p for p in result['packaged_hashes'] if p.endswith('workflow.json'))
        self.assertEqual(load(self.output/snapshot),state)
        self.assertEqual(load(self.work/'workflow.json'),self.state)

    def test_invalid_output_and_modified_workbook_fail_before_writing_records(self):
        state=deepcopy(self.state);state['output']=str(self.root/'outside')
        with self.assertRaisesRegex(ValueError,'本项目'):
            package_records(self.work,state=state)
        Path(self.state['results']['delivery']['files']['raw_report']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'缺失或被修改'):
            package_records(self.work)
        self.assertFalse((self.output/'查看运行记录.html').exists())

    def test_missing_source_is_disclosed_without_fabricating_a_record(self):
        state=deepcopy(self.state);state['results']['sgd']={'source_run':str(self.work/'missing')}
        result=package_records(self.work,state=state)
        self.assertTrue(any('记录未找到' in warning for warning in result['warnings']))
        self.assertFalse(any('/missing/' in x.get('archive','') for x in result['records']))
