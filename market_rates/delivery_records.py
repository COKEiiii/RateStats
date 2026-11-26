"""Package existing run records beside a delivery, without changing workbooks."""
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
from pathlib import Path
import shutil
import tempfile
from urllib.parse import quote

from .common import load, digest
from .delivery import verify_delivery
from .table_validation import sha

VERSION = 1
COPY_LIMIT = 8 * 1024 * 1024
SUMMARY = '运行记录.txt'
INDEX = '查看运行记录.html'
MANIFEST = 'records-manifest.json'


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def package_records(workflow, *, state=None):
    """Idempotent record export used by live runs, rebuilds and old deliveries.

    Small records are copied byte-for-byte with hashes. Large reports and
    linked evidence remain in their original project location. HTML links to
    their original location so relative image/evidence links keep working.
    """
    work = Path(workflow).expanduser().absolute()
    if work.name == 'workflow.json':
        work = work.parent
    state = state if state is not None else load(work / 'workflow.json')
    project = Path(state['project']).resolve()
    output = Path(state['output']).resolve()
    if not work.resolve().is_relative_to(project / 'runs') or not output.resolve().is_relative_to(project / 'outputs'):
        raise ValueError('运行记录只允许整理本项目 runs 和 outputs 内的任务。')
    delivery = verify_delivery(output)
    if state.get('status') != 'complete' or state.get('results', {}).get('delivery', {}).get('files') != delivery['files']:
        raise ValueError('工作流必须已完成，且与当前四份交付文件一致。')
    records, evidence, warnings = [], [], []
    seen = set()

    def add(path, label, group):
        path = Path(path).expanduser().absolute()
        real = path.resolve()
        if real in seen:
            return
        seen.add(real)
        if not real.is_relative_to(project):
            warnings.append('未读取项目外记录：' + str(path))
            return
        if not path.is_file():
            warnings.append('记录未找到：' + str(path))
            return
        size = path.stat().st_size
        records.append(dict(source=str(path), label=label, group=group, bytes=size,
                            sha256=sha(path), copied=size <= COPY_LIMIT))

    for path in sorted(work.glob('*.log')):
        add(path, path.name, '主流程')
    for name in ('preflight.json', 'premier-sync.json', 'sgd-comparison.json', 'sgd-result.json', '识别修复汇总.txt'):
        if (work / name).is_file():
            add(work / name, name, '主流程')
    for pattern in ('automatic-repair.json', '自动修复结果.txt'):
        for path in sorted(work.rglob(pattern)):
            add(path, str(path.relative_to(work)), '识别修复')
    for key, label in (('sgd', '新元促销'), ('board', '全部挂牌'), ('fx', '外币促销')):
        phase = state.get('results', {}).get(key, {})
        source = phase.get('source_run') or (state.get('results', {}).get('fx_source') if key == 'fx' else None)
        if source:
            source = Path(source).expanduser().absolute()
            add(source / 'run.json', label + '采集与核验记录', label)
            if source.resolve().is_relative_to(project):
                for name in ('evidence/index.html', 'review.html', 'evidence'):
                    path = source / name
                    if path.exists():
                        evidence.append(dict(label=label + ' / ' + name, source=str(path)))
        if phase.get('output'):
            folder = Path(phase['output']).expanduser().absolute()
            if not folder.resolve().is_relative_to(project):
                warnings.append('未读取项目外阶段目录：' + str(folder))
                continue
            for path in sorted(folder.iterdir()) if folder.is_dir() else []:
                if path.is_file() and path.suffix.lower() in ('.json', '.txt', '.log'):
                    add(path, path.name, label)
                elif path.is_file() and path.name in ('table-validation.html', 'board-coverage.html'):
                    evidence.append(dict(label=label + ' / ' + path.name, source=str(path)))
    # These checks describe this delivery, not an earlier formatting revision.
    for path in [output / 'delivery.json', *(Path(p) for p in delivery.get('audit_files', []))]:
        add(path, path.name, '本次交付检查')
    snapshot = _json_bytes(state)
    fingerprint = digest(dict(version=VERSION, workflow=state, records=records, evidence=evidence, warnings=warnings))
    receipt = output / MANIFEST
    if receipt.exists():
        previous = load(receipt)
        if previous.get('fingerprint') == fingerprint:
            for relative, expected in previous['packaged_hashes'].items():
                target = (output / relative).resolve()
                if not target.is_relative_to(output) or not target.is_file() or sha(target) != expected:
                    raise ValueError('运行记录副本缺失或被修改：' + relative)
            return previous

    created = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    archive = Path('运行记录') / fingerprint[:16]
    target = output / archive
    if target.exists():
        raise FileExistsError('运行记录快照目录已存在，不能覆盖：' + str(target))
    root = output / '运行记录'
    root.mkdir(exist_ok=True)
    copied = sum(item['copied'] for item in records)
    linked = len(records) - copied
    rebuilt = bool(state.get('delivery_revisions'))
    mode = '格式重建（沿用原采集数据）' if rebuilt else {'live': '实时采集', 'rebuild': '使用已有证据重建'}.get(state.get('mode'), state.get('mode', '未记录'))
    lines = ['市场利率调研运行记录', '', '记录整理时间：' + created, '本次交付方式：' + mode,
             '原任务开始：' + str(state.get('started_at', '未记录')),
             '原任务完成：' + str(state.get('finished_at', '未记录')),
             '任务目录：' + str(work), '交付目录：' + str(output), '', '各部分资料日期：']
    lines += ['  ' + key + '：' + str(value) for key, value in state.get('source_dates', {}).items()]
    lines += ['', '比较口径：' + ('输入调研表现有最新两列相减，不另找采集文件补列。'
              if delivery.get('comparison_mode') == 'latest_two_columns' else '见本次交付和格式检查记录。')]
    for name, sections in delivery.get('comparison_columns', {}).items():
        pairs = sorted({(p['current_date'], p.get('previous_date') or '无历史日期') for p in sections})
        lines.append('  ' + name + '：' + '；'.join(a + ' / ' + b for a, b in pairs))
    if delivery.get('rainbow_sgd_policy') in ('all_quotes_personal_rank_v1','all_public_quotes_personal_rank_v3'):
        lines += ['', 'SGD 促销彩虹：保留各客群、金额及渠道报价，银行分块，按 Personal 最高利率排序；无 Personal 报价的银行列在末尾。']
    elif delivery.get('rainbow_sgd_policy') == 'personal_max_with_exclusive_quotes_v2':
        lines += ['', 'SGD 促销彩虹：每银行、每期限只保留一条 Personal 最高利率及其对应 Condition，Premier、Preferred 等专属客群报价继续列出；银行块按 Personal 最高利率排序，无 Personal 的银行列在末尾。',
                  'Personal 并列最高时保留最低可比金额门槛及其自身条件；金额币种不同则沿用原顺序。']
    lines += ['', '已完成步骤：', *['  ' + step for step in state.get('completed', [])], '', '本次检查：', '  四份 Excel 文件完整性：通过']
    for name in ('native-open-check.json', 'report-format.audit.json'):
        path = output / name
        if path.is_file():
            check = load(path)
            if name == 'native-open-check.json':
                lines.append('  Excel 打开检查：' + ('通过' if check.get('passed') else '未通过或未记录'))
            else:
                verified = check.get('verification', {})
                lines += ['  调研保存后回读：' + str(verified.get('saved_file_readback', '未记录')),
                          '  已检查增幅公式数：' + str(verified.get('delta_formula_count', '未记录'))]
    lines += ['', f'已归档 {copied} 份日志与检查记录，另有 {linked} 份大记录从原位置查看。',
              '工作流快照也已归档；已有记录保持原始内容。',
              '网页、PDF 和截图证据保留原位，可从“查看运行记录.html”打开；移动交付目录不会带走这些证据。',
              '本摘要汇集已有记录，不补造过去的执行日志。', '', '需要留意：', *(('  ' + w) for w in warnings)]
    if not warnings:
        lines.append('  本次所选记录均已找到。源记录中列出的异常或待复核事项仍以原记录为准。')
    lines += ['', '四份文件：', *['  ' + Path(p).name for p in delivery['files'].values()]]

    def link(href, label):
        return '<a href="' + html.escape(href, quote=True) + '">' + html.escape(label) + '</a>'

    with tempfile.TemporaryDirectory(prefix='.pack-', dir=root) as temporary:
        stage = Path(temporary)
        payload = stage / 'files'
        payload.mkdir()
        (payload / 'workflow.json').write_bytes(snapshot)
        hashes = {str(archive / 'workflow.json'): hashlib.sha256(snapshot).hexdigest()}
        rows = []
        for number, item in enumerate(records, 1):
            source = Path(item['source'])
            if item['copied']:
                filename = f'{number:03d}-' + source.name[-90:]
                dest = payload / filename
                shutil.copy2(source, dest)
                if sha(dest) != item['sha256'] or sha(source) != item['sha256']:
                    raise ValueError('原记录在复制期间发生改变：' + str(source))
                item['archive'] = str(archive / filename)
                hashes[item['archive']] = item['sha256']
                href = quote((archive / filename).as_posix())
                label = '查看副本'
            else:
                href, label = source.as_uri(), '查看原件（大记录）'
            rows.append('<tr><td>' + html.escape(item['group']) + '</td><td>' + html.escape(item['label']) +
                        '</td><td>' + link(href, label) + '</td><td>' + link(source.as_uri(), '原位置') + '</td></tr>')
        body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>运行记录</title>'
        body += '<style>body{font:16px/1.65 "Microsoft YaHei",sans-serif;margin:32px;max-width:1280px;color:#18202b}pre{white-space:pre-wrap;background:#f3f6fa;padding:20px}table{border-collapse:collapse;width:100%}td,th{padding:8px 12px;border:1px solid #d2d9e1;text-align:left;overflow-wrap:anywhere}a{color:#075caf}h2{margin-top:32px}</style>'
        body += '<h1>运行记录</h1><pre>' + html.escape('\n'.join(lines)) + '</pre>'
        body += '<p>' + link(quote((archive / 'workflow.json').as_posix()), '完整工作流快照') + '</p>'
        body += '<h2>日志与检查报告</h2><table><tr><th>阶段</th><th>记录</th><th>查看</th><th>来源</th></tr>' + ''.join(rows) + '</table>'
        body += '<h2>网页、截图与证据</h2><ul>' + ''.join('<li>' + link(Path(x['source']).as_uri(), x['label']) + '</li>' for x in evidence) + '</ul></html>'
        authored = {SUMMARY: '\n'.join(lines).encode('utf-8-sig'), INDEX: body.encode('utf-8')}
        repair = work / '识别修复汇总.txt'
        if repair.is_file():
            authored['识别修复汇总.txt'] = repair.read_bytes()
        for name, content in authored.items():
            (stage / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        result = dict(version=VERSION, fingerprint=fingerprint, created_at=created, workflow=str(work), output=str(output),
                      summary=str(output / SUMMARY), index=str(output / INDEX), copied_records=copied,
                      linked_records=linked, records=records, evidence=evidence, warnings=warnings, packaged_hashes=hashes)
        (stage / MANIFEST).write_bytes(_json_bytes(result))
        verify_delivery(output)
        payload.rename(target)
        for name in [*authored, MANIFEST]:
            (stage / name).replace(output / name)
    return result
