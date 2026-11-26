"""Publish four workbooks together after both reusable formatters pass."""
from pathlib import Path
from decimal import Decimal
import re
import shutil
import subprocess
import tempfile

from .common import load, save
from .table_validation import sha
from .report_xml import Book, M, address
from .report_format import format_report, infer_date
from .rainbow_format import format_rainbow, SGD_PROMO_POLICY
from scripts.platform_runtime import excel_command,prepare_environment

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/format-templates.local.json'


def find_stale_sgd_quotes(path, as_of):
    """Identify prior-date SGD quote formulas without changing their source workbook."""
    book = Book(path)
    sheet = book.sheets['SGD Promotional Rate']
    current = as_of.strftime('%Y%m%d')
    removed = []
    for (row, column), cell in list(sheet.cells.items()):
        formula = cell.findtext(M+'f', '')
        dates = re.findall(r"(?:'?(?:明细_)(\d{8})'?)!", formula)
        if not dates or current in dates or not any(day != current for day in dates):
            continue
        removed.append({'cell': address(row, column), 'source_dates': sorted(set(dates))})
    return removed


def clear_stale_sgd_quotes(path, as_of):
    """Use native Excel to remove prior-date quotes from the delivery copy only."""
    removed = find_stale_sgd_quotes(path, as_of)
    if removed:
        manifest=path.parent/'stale-sgd-removal.json'
        save(manifest,{'workbook':str(path.resolve()),'as_of':as_of.isoformat(),
                       'cells':removed,'input_sha256':sha(path)})
        subprocess.run(excel_command('scrub-sgd','--Manifest',manifest),
                       check=True,env=prepare_environment())
        result=load(manifest)
        if result.get('status')!='complete' or sha(path)!=result.get('output_sha256'):
            raise ValueError('SGD old-date quote removal did not save cleanly')
        if find_stale_sgd_quotes(path,as_of):
            raise ValueError('SGD old-date quote remains after native Excel removal')
    return removed


def validate_native_books(files, staging):
    """Check in a separate hidden Excel instance, without saving or updating links."""
    manifest=Path(staging)/'native-open-input.json'
    result_path=Path(staging)/'native-open-check.json'
    save(manifest,{'files':{k:str(v) for k,v in files.items()}})
    subprocess.run(excel_command('check-delivery','--Manifest',manifest,'--Out',result_path),
                   check=True,env=prepare_environment())
    result=load(result_path)
    if not result.get('passed'):
        raise ValueError('交付文件未通过 Excel 原生打开检查')
    manifest.unlink()
    return result


def prepare_templates(work, config_path=CONFIG):
    """Freeze this run's layout references so resume cannot change its rules."""
    target = Path(work) / 'format-templates'
    receipt = target / 'templates.json'
    if receipt.exists():
        result = load(receipt)
        for kind in ('report', 'rainbow'):
            if sha(result[kind]) != result['hashes'][kind]:
                raise ValueError('格式样板快照已被修改：' + result[kind])
        return result
    config = load(config_path)
    target.mkdir(exist_ok=True)
    result = {'hashes': {}, 'originals': {}}
    for kind in ('report', 'rainbow'):
        from .runtime import project_path
        source = project_path(config[kind])
        if not source.is_file():
            raise FileNotFoundError('格式样板不存在，请检查 config/format-templates.local.json：' + str(source))
        output = target / (kind + '.xlsx')
        before = sha(source)
        shutil.copy2(source, output)
        if sha(output) != before or sha(source) != before:
            raise ValueError('格式样板在复制期间发生改变：' + str(source))
        result[kind] = str(output.resolve())
        result['hashes'][kind] = before
        result['originals'][kind] = str(source)
    save(receipt, result)
    return result


def _relocate(value, old, new):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return value.replace(str(old), str(new))
    if isinstance(value, list):
        return [_relocate(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _relocate(v, old, new) for k, v in value.items()}
    return value


def verify_delivery(output):
    """A completed workflow is valid only while its four delivered files agree."""
    result=load(Path(output)/'delivery.json')
    required={'raw_report','raw_rainbow','formatted_report','formatted_rainbow'}
    if result.get('status')!='complete' or set(result.get('files',{}))!=required:
        raise ValueError('四份交付记录不完整：'+str(output))
    for kind,path in result['files'].items():
        if not Path(path).is_file() or sha(path)!=result['hashes'][kind]:
            raise ValueError('已交付文件缺失或被修改：'+path)
    if result.get('quote_coverage'):
        coverage=load(Path(output)/'quote-coverage.audit.json')
        if not coverage.get('passed') or set(coverage.get('files',{}))!=required:
            raise ValueError('四份报价覆盖审计不完整')
        for field in ('catalog_sha256','selection_sha256'):
            if len({v.get(field) for v in coverage['files'].values()})!=1 or any(not v.get(field) for v in coverage['files'].values()):
                raise ValueError('四份报价／最高值审计不一致：'+field)
    return result


def publish_four(report, rainbow, templates, output, *, project_root=ROOT,
                 started_at=None, previous_path=None):
    """An unsuccessful formatter never exposes a partially complete delivery.

    Existing completed delivery is reusable only if its input/output hashes
    match. Raw files retain source sheets and remain available for future runs.
    """
    if previous_path is not None:
        raise ValueError('previous_path 已取消：只比较输入工作簿现有最新两列。')
    report, rainbow, output = map(lambda p: Path(p).resolve(), (report, rainbow, output))
    as_of = infer_date(Book(report))
    receipt = output / 'delivery.json'
    inputs = {'report': str(report), 'rainbow': str(rainbow),
              'report_sha256': sha(report), 'rainbow_sha256': sha(rainbow),
              'comparison_mode':'latest_two_columns',
              'rainbow_sgd_policy':SGD_PROMO_POLICY,
              'template_hashes': {k: sha(templates[k]) for k in ('report', 'rainbow')}}
    if output.exists():
        if not receipt.exists():
            raise FileExistsError('输出目录已存在但没有完整交付记录：' + str(output))
        result = verify_delivery(output)
        if result['inputs'] != inputs:
            raise ValueError('已交付文件的输入已改变或比较口径已更新，请启动新任务或重建交付。')
        return result
    prefix = as_of.strftime('%Y%m%d')
    names = {'raw_report': prefix + '_全量_调研.xlsx',
             'raw_rainbow': prefix + '_全量_彩虹表.xlsx',
             'formatted_report': prefix + '_格式对齐_调研.xlsx',
             'formatted_rainbow': prefix + '_格式对齐_彩虹表.xlsx'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.four-workbooks-', dir=output.parent) as tmp:
        staging = Path(tmp) / 'delivery'
        staging.mkdir()
        paths = {k: staging / name for k, name in names.items()}
        shutil.copy2(report, paths['raw_report'])
        shutil.copy2(rainbow, paths['raw_rainbow'])
        from .rainbow_borders import fix_sgd_bank_borders
        fix_sgd_bank_borders(paths['raw_rainbow'])
        stale_sgd_quotes = clear_stale_sgd_quotes(paths['raw_rainbow'], as_of)
        report_audit = format_report(report, templates['report'], paths['formatted_report'])
        rainbow_audit = format_rainbow(paths['raw_rainbow'], templates['rainbow'], paths['formatted_rainbow'])
        quote_coverage=None
        source_plan=report.parent/'table-validation-plan.json'
        if source_plan.exists():
            from .public_quotes import attach_four
            quote_coverage=attach_four(paths,source_plan,staging)
        elif {'SGD挂牌','USD促销','CNY促销'} <= set(Book(report).sheets):
            raise ValueError('完整交付缺少对应报价计划，无法核对四份报价集合')
        native=validate_native_books(paths,staging)
        save(staging/'native-open-check.json',_relocate(native,staging,output))
        save(staging / 'report-format.audit.json', _relocate(report_audit, staging, output))
        save(paths['formatted_rainbow'].with_suffix('.audit.json'), _relocate(rainbow_audit, staging, output))
        if sha(report) != inputs['report_sha256'] or sha(rainbow) != inputs['rainbow_sha256']:
            raise ValueError('采集工作簿在格式整理期间发生变化。')
        result = {'version': 2, 'status': 'complete', 'as_of': as_of.isoformat(),
                  'previous':None,'comparison_mode':'latest_two_columns',
                  'rainbow_sgd_policy':SGD_PROMO_POLICY,
                  'comparison_columns':report_audit.get('comparison_columns',{}),'inputs': inputs,
                  'removed_prior_date_sgd_quotes': stale_sgd_quotes,
                  'quote_coverage':quote_coverage,
                  'files': {k: str(output / name) for k, name in names.items()},
                  'hashes': {k: sha(p) for k, p in paths.items()},
                  'audit_files': [str(output / 'report-format.audit.json'),
                                  str((output / names['formatted_rainbow']).with_suffix('.audit.json')),
                                  str(output/'native-open-check.json')]+([str(output/'quote-coverage.audit.json')] if quote_coverage else [])}
        save(staging / 'delivery.json', result)
        staging.rename(output)
    return result
