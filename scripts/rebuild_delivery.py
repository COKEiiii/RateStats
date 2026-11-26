"""Rebuild four delivered workbooks from verified raw files, without collection."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from scripts.platform_runtime import open_path

from market_rates.common import load, save
from market_rates.delivery import prepare_templates, publish_four, verify_delivery
from market_rates.delivery_records import package_records
from market_rates.table_validation import sha
from scripts.run_all import ROOT, project_lock


def _restore(path, content):
    """Restore metadata atomically if a later pointer update fails."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(prefix=path.name+'.restore-',dir=path.parent,delete=False) as handle:
        temporary=Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rebuild_delivery(workflow, *, no_open=True, project_root=ROOT):
    """Return the updated workflow after publishing and checking four new files.

    The original acquisition timestamps and raw evidence do not change. Output
    metadata is committed only after both formatters and native Excel checks
    succeed; existing deliveries and an exact prior workflow copy remain.
    """
    project=Path(project_root).expanduser().absolute()
    work=Path(workflow).resolve()
    if work.name=='workflow.json':work=work.parent
    if not work.is_relative_to((project/'runs').resolve()):
        raise ValueError('请选择本项目 runs 目录内的完整采集任务。')
    manifest=work/'workflow.json'
    with project_lock():
        original=manifest.read_bytes()
        state=json.loads(original.decode('utf-8-sig'))
        if Path(state.get('project','')).resolve()!=project.resolve() or state.get('status')!='complete':
            raise ValueError('只能重新整理本项目已完成的完整采集任务。')
        if not state.get('started_at') or not state.get('finished_at') or not {'sgd','board','fx'}<=set(state.get('source_dates',{})):
            raise ValueError('任务缺少完整采集时间和来源日期，不能只重建交付文件。')
        old=verify_delivery(state['output'])
        if not isinstance(state.get('results',{}).get('delivery'),dict):
            raise ValueError('任务没有新版四份文件交付记录。')
        fields={'report':'raw_report','rainbow':'raw_rainbow',
                'formatted_report':'formatted_report','formatted_rainbow':'formatted_rainbow'}
        for field,kind in fields.items():
            if Path(state.get(field,'')).resolve()!=Path(old['files'][kind]).resolve():
                raise ValueError('工作流与四份交付文件记录不一致：'+field)
        if state.get('output_hashes')!=old['hashes'] or state['results']['delivery'].get('files')!=old['files']:
            raise ValueError('工作流与四份交付校验记录不一致。')
        if not (work/'format-templates/templates.json').is_file():
            raise ValueError('任务缺少已冻结的格式样板，不能改用当前外部样板。')
        templates=prepare_templates(work)
        saved_templates=state.get('format_templates',{})
        if any(saved_templates.get(kind)!=templates[kind] or saved_templates.get('hashes',{}).get(kind)!=templates['hashes'][kind]
               for kind in ('report','rainbow')):
            raise ValueError('工作流与已冻结的格式样板记录不一致。')

        timestamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        output=project/'outputs'/(work.name+'-delivery-'+timestamp)
        if output.exists():raise FileExistsError('新的交付目录已存在：'+str(output))
        # Keep the manifest's original display path when passing files to the
        # formatter; comparisons above use resolve() so symlink aliases remain
        # equivalent without rewriting /var as /private/var in user records.
        raw={kind:Path(old['files']['raw_'+kind]).expanduser().absolute() for kind in ('report','rainbow')}
        # The formatter compares the raw report's own latest two columns.
        # Rebuilding never selects another collection as a comparison source.
        publish_four(raw['report'],raw['rainbow'],templates,output,
                     project_root=project,started_at=state['started_at'])
        delivered=verify_delivery(output)
        for kind,path in raw.items():
            if sha(path)!=old['hashes']['raw_'+kind] or delivered['hashes']['raw_'+kind]!=old['hashes']['raw_'+kind]:
                raise ValueError('重建过程中原始采集工作簿发生变化：'+str(path))
        prepare_templates(work) # Recheck the frozen snapshots before commit.
        if manifest.read_bytes()!=original:
            raise ValueError('工作流记录在重建期间被修改，保留新文件但不更新任务指针。')

        backup=work/'delivery-revisions'/(timestamp+'.json')
        backup.parent.mkdir(parents=True,exist_ok=True)
        with backup.open('xb') as handle:handle.write(original)
        updated=deepcopy(state)
        updated['results']['delivery']=delivered
        for field,kind in fields.items():updated[field]=delivered['files'][kind]
        updated['output']=str(output)
        updated['output_hashes']=delivered['hashes']
        updated.setdefault('delivery_revisions',[]).append(dict(
            rebuilt_at=datetime.now().isoformat(),previous_output=state['output'],output=str(output),
            previous_baseline=old.get('previous'),comparison_mode=delivered.get('comparison_mode'),
            comparison_columns=delivered.get('comparison_columns',{}),workflow_backup=str(backup)))
        package_records(work,state=updated)
        pointers={manifest:updated,
                  project/'outputs/latest-all.json':dict(workflow=str(work),output=str(output),mode=state['mode'],source_dates=state['source_dates']),
                  project/'outputs/latest-all-run.json':dict(workflow=str(work),status='complete')}
        prior={path:path.read_bytes() if path.exists() else None for path in pointers}
        try:
            for path,value in pointers.items():save(path,value)
        except Exception:
            for path,content in prior.items():_restore(path,content)
            raise
    if not no_open:
        try:open_path(updated['formatted_report'])
        except OSError as exc:print('四份文件已完成，自动打开失败：'+str(exc),file=sys.stderr)
    return updated


def main(argv=None):
    parser=argparse.ArgumentParser(description='只重建已完成任务的四份交付文件，不重新采集或调用模型。')
    parser.add_argument('--workflow',required=True,help='runs/all-... 工作流目录')
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args(argv)
    result=rebuild_delivery(args.workflow,no_open=args.no_open)
    print('四份交付文件已重新生成，原始采集日期保持不变：'+result['output'])
    print('增幅口径：原始调研工作簿现有最新两列相减')
    print('运行记录：'+str(Path(result['output'])/'查看运行记录.html'))
    for path in result['results']['delivery']['files'].values():print(path)
    return result


if __name__=='__main__':main()
