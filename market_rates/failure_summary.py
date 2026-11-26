"""Pass failure details from the active subprocess to the full workflow."""
from collections import deque
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory

CHANNEL = 'MARKET_RATES_FAILURE_CHANNEL'
BUSINESS = 'MARKET_RATES_FAILURE_BUSINESS'


def one_line(value):
    lines = str(value).splitlines()
    return ' '.join(lines[0].split()) if lines else ''


def detail(reason, *, bank=None, business=None, step=None, substep=None, log=None):
    return dict(bank=bank, business=business or os.environ.get(BUSINESS),
                step=step, substep=substep, reason=one_line(reason) or '原因未记录',
                log=str(log) if log else None)


def publish_detail(item):
    path = os.environ.get(CHANNEL)
    if path:
        try:
            with open(path, 'a', encoding='utf8') as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + '\n')
        except OSError:
            # A missing diagnostic channel must not mask the actual failure.
            pass


def record_failure(reason, **context):
    publish_detail(detail(reason, **context))


@contextmanager
def bank_step(bank, step, **context):
    try:
        yield
    except Exception as exc:
        record_failure(str(exc) or type(exc).__name__, bank=bank, step=step, **context)
        raise


class StepFailure(RuntimeError):
    def __init__(self, name, code, details, log=None):
        super().__init__(name + '失败' + ('，详情：'+str(log) if log else ''))
        self.code = code
        self.details = details
        self.log = log


class WorkflowFailure(RuntimeError):
    def __init__(self, work, stage, details, log):
        super().__init__(stage)
        self.work = work
        self.stage = stage
        self.details = details
        self.log = log


def _records(path):
    try:
        lines = path.read_text(encoding='utf8', errors='replace').splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get('reason'), str):
            result.append(item)
    return result


def run_step(command, *, cwd, env, name, log=None, allowed=(0,),
             bank=None, business=None, step=None, stream=True, log_mode='a'):
    """Stream output and collect details in a fresh channel for this command only."""
    with TemporaryDirectory(prefix='rate-failure-') as directory:
        channel = Path(directory)/'details.jsonl'
        child_env = {**env, CHANNEL: str(channel)}
        if business:
            child_env[BUSINESS] = business
        recent = deque(maxlen=60)
        with (log.open(log_mode, encoding='utf8') if log else nullcontext()) as output:
            with subprocess.Popen(command, cwd=cwd, env=child_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding='utf8',
                                  errors='replace') as process:
                for line in process.stdout:
                    if output is not None:
                        output.write(line)
                        output.flush()
                    if stream:
                        print(line.rstrip(), flush=True)
                    recent.append(line.strip())
                code = process.wait()
        if allowed is None or code in allowed:
            return code
        items = _records(channel)
        if not items:
            reason = next((line for line in reversed(recent)
                           if re.match(r'^(?:[\w.]*Error|[\w.]*Exception):\s', line)),
                          f'子进程退出码 {code}')
            items = [detail(reason)]
        for item in items:
            item['substep'] = item.get('substep') or name
            item['step'] = item.get('step') or step
            item['bank'] = item.get('bank') or bank
            item['business'] = item.get('business') or business or child_env.get(BUSINESS)
            item['log'] = item.get('log') or (str(log) if log else None)
            publish_detail(item)
        raise StepFailure(name, code, items, log)


def unique_details(items):
    found = {}
    for item in items:
        key = tuple(item.get(k) for k in ('bank', 'business', 'reason'))
        found.setdefault(key, item)
    return list(found.values())


def format_summary(stage, items, log=None, work=None):
    substeps = list(dict.fromkeys(x['substep'] for x in items if x.get('substep') and x['substep'] != stage))
    heading = '全量任务失败｜' + ' → '.join([stage, '、'.join(substeps)] if substeps else [stage])
    lines = [heading]
    for item in items:
        bank = item.get('bank')
        if not bank and item.get('step') in ('采集', '核验', '采集与核验'):
            bank = '银行未确认'
        labels = [x for x in (bank, item.get('business'), item.get('step') or item.get('substep')) if x]
        lines.append('- ' + ('｜'.join(labels) + '：' if labels else '') + item['reason'])
    for path in dict.fromkeys([*(x.get('log') for x in items), str(log) if log else None]):
        if path:
            lines.append('详细日志：' + path)
    if work:
        lines += ['进度已保存。修复后在项目目录从本批次续跑：',
                  shlex.join(['.venv/bin/python', '-m', 'scripts.run_all', '--resume', str(work)])]
    return '\n'.join(lines)
