"""Choose a saved all-workflow batch for the macOS resume launcher."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_rates.common import load
from scripts.batch_picker import filter_batches
from scripts.run_all import resume_strategy

SGT = timezone(timedelta(hours=8))
STATUSES = {'running': '运行中或被中断', 'failed': '失败', 'paused': '已暂停', 'complete': '已完成'}
MODES = {'live': '实时采集', 'rebuild': '历史重建'}


def display_start(state):
    try:
        started = datetime.fromisoformat(state.get('started_at', ''))
    except (TypeError, ValueError):
        return '未记录'
    if started.tzinfo:
        started = started.astimezone(SGT)
    return started.strftime('%Y-%m-%d %H:%M:%S')


@dataclass(frozen=True)
class SavedBatch:
    work: Path
    state: dict
    modified: datetime

    @property
    def label(self):
        status = STATUSES[self.state['status']]
        return f'{display_start(self.state)}  ·  {status}  ·  {self.work.name}'


def read_batch(work, root):
    runs = root / 'runs'
    work = Path(work).resolve()
    manifest = work / 'workflow.json'
    if work.parent != runs or not manifest.resolve().is_relative_to(work):
        raise ValueError('请选择本项目的全量批次。')
    state = load(manifest)
    if state['status'] not in STATUSES or state['mode'] not in MODES:
        raise ValueError('无法识别该批次的状态或运行类型。')
    if not isinstance(state.get('results', {}), dict) or not isinstance(state.get('completed', []), list):
        raise ValueError('批次的阶段记录无效。')
    if Path(state['project']).resolve() != root:
        raise ValueError('批次不属于当前项目。')
    return SavedBatch(work, state, datetime.fromtimestamp(manifest.stat().st_mtime, SGT))


def saved_batches(root):
    root = Path(root).resolve()
    batches = []
    for manifest in (root / 'runs').glob('all-*/workflow.json'):
        try:
            batches.append(read_batch(manifest.parent, root))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
    batches.sort(key=lambda batch: (batch.modified, batch.work.name), reverse=True)
    return {batch.label: batch for batch in batches}


def picker_action(batch, root, *, today=None):
    state = batch.state
    if state['project'] != str(root):
        return None, '此批次记录的是迁移前的项目路径。'
    action, _ = resume_strategy(state, today)
    if action == 'resume':
        return '继续此批次', '将从保存的断点继续，运行进度显示在终端。'
    if action == 'complete':
        return '查看交付', '任务已完成。点击后校验交付文件并打开结果。'
    if state['status'] == 'complete':
        return None, '此旧版批次没有四份交付文件。可用「一键运行」生成新的批次。'
    return None, '实时采集只支持在启动当天续跑。需要采集当天数据时，请使用「一键运行」。'


def choose_workflow(project_root):
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    from scripts.email_settings import tk_root

    project = Path(project_root).resolve()
    root = tk_root()
    root.title('继续上次全量任务')
    root.minsize(720, 360)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(3, weight=1)
    query, selected, hint = (tk.StringVar(root) for _ in range(3))

    ttk.Label(frame, text='搜索批次').grid(row=0, column=0, sticky='w', padx=(0, 12), pady=4)
    search = ttk.Entry(frame, textvariable=query)
    search.grid(row=0, column=1, sticky='ew', pady=4)
    ttk.Label(frame, text='批次（启动时间或编号）').grid(row=1, column=0, sticky='w', padx=(0, 12), pady=4)
    picker = ttk.Combobox(frame, textvariable=selected, state='readonly', width=56)
    picker.grid(row=1, column=1, sticky='ew', pady=4)
    ttk.Label(frame, text='批次详情（新加坡时间）').grid(row=2, columnspan=3, sticky='w', pady=(10, 4))
    detail = ScrolledText(frame, width=78, height=10, wrap='word', state='disabled')
    detail.grid(row=3, columnspan=3, sticky='nsew')
    ttk.Label(frame, textvariable=hint, wraplength=520).grid(row=4, columnspan=2, sticky='w', pady=(10, 0))
    choices = {}
    chosen = None

    def show_details():
        batch = choices.get(selected.get())
        button = None
        if batch is None:
            message = '没有符合搜索条件的批次。' if choices else '没有找到本项目的全量批次。'
            contents = ''
        else:
            button, message = picker_action(batch, project)
            state = batch.state
            contents = '\n'.join([
                f'批次编号：{batch.work.name}',
                f'启动时间：{display_start(state)}',
                f'最近保存：{batch.modified:%Y-%m-%d %H:%M:%S}',
                f'运行类型：{MODES[state["mode"]]}',
                f'状态：{STATUSES[state["status"]]}',
                f'当前阶段：{state.get("stage") or "未记录"}',
                f'已完成阶段：{"、".join(map(str, state.get("completed", []))) or "无"}',
            ])
            if state.get('error'):
                contents += '\n上次错误：' + str(state['error'])
        detail.configure(state='normal')
        detail.delete('1.0', 'end')
        detail.insert('1.0', contents)
        detail.configure(state='disabled')
        hint.set(message)
        run_button.configure(text=button or '继续此批次', state='normal' if button else 'disabled')

    def match(*_, preferred=None):
        matches = filter_batches(choices, query.get())
        current = selected.get()
        picker.configure(values=matches, state='readonly' if matches else 'disabled')
        selected.set(next((label for label in (preferred, current) if label in matches),
                          matches[0] if matches else ''))
        show_details()

    def refresh():
        choices.clear()
        choices.update(saved_batches(project))
        match()

    def select():
        nonlocal chosen
        try:
            batch = read_batch(choices[selected.get()].work, project)
            button, message = picker_action(batch, project)
            if not button:
                raise ValueError(message)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            messagebox.showerror('无法打开批次', str(exc), parent=root)
            refresh()
            return
        chosen = batch.work
        root.quit()

    ttk.Button(frame, text='刷新', command=refresh).grid(row=1, column=2, padx=(8, 0))
    run_button = ttk.Button(frame, text='继续此批次', command=select)
    run_button.grid(row=4, column=2, padx=(8, 0), pady=(10, 0), sticky='e')
    picker.bind('<<ComboboxSelected>>', lambda _: show_details())
    query.trace_add('write', match)
    root.protocol('WM_DELETE_WINDOW', root.quit)

    choices.update(saved_batches(project))
    preferred = next((label for label, batch in choices.items()
                      if batch.state['status'] != 'complete' and picker_action(batch, project)[0]), None)
    match(preferred=preferred)
    root.deiconify()
    search.focus_set()
    try:
        root.mainloop()
        return chosen
    finally:
        root.destroy()


if __name__ == '__main__':
    work = choose_workflow(Path.cwd())
    print(json.dumps(str(work) if work is not None else None))
