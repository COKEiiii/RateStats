"""Select, preview and send a completed batch in the desktop mail window."""

from datetime import date
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from market_rates.common import load
from market_rates.report_email import FIELDS, ROOT, preview_config, read_config, send_mail, verified_batch
from scripts.batch_picker import filter_batches
from scripts.email_settings import tk_root
from scripts.send_report_email import STATUS, error_text, gui_confirm, latest_workflow, notify_gui, show_result


def batch_choices(project_root):
    """List delivery candidates without hashing every historical workbook."""
    root = Path(project_root).resolve()
    choices = {}
    for manifest in (root / 'runs').glob('*/workflow.json'):
        try:
            work = manifest.parent.resolve()
            if work.parent != root / 'runs':
                continue
            state = load(manifest)
            if state['status'] != 'complete' or Path(state['project']).resolve() != root:
                continue
            output = Path(state['output']).resolve()
            if not output.is_relative_to(root / 'outputs') or not (output / 'delivery.json').is_file():
                continue
            delivery = state['results']['delivery']
            files = delivery['files']
            if set(files) != set(FIELDS):
                continue
            paths = {Path(path).resolve() for path in files.values()}
            if len(paths) != len(FIELDS) or any(
                path.parent != output or path.suffix.lower() != '.xlsx' or not path.is_file()
                for path in paths
            ):
                continue
            as_of = date.fromisoformat(delivery['as_of']).isoformat()
            mode = '重建' if state.get('mode') == 'rebuild' or state.get('delivery_revisions') else '实时'
            choices[f'{as_of}  ·  {work.name}  ·  {mode}'] = work
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
    return dict(sorted(choices.items(), reverse=True))


def choose_and_send():
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk_root()
    root.title('发送结果邮件')
    root.minsize(680, 440)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(3, weight=1)

    search = tk.StringVar(root)
    selected = tk.StringVar(root)
    status = tk.StringVar(root)
    ttk.Label(frame, text='搜索批次').grid(row=0, column=0, sticky='w', padx=(0, 12), pady=4)
    search_box = ttk.Entry(frame, textvariable=search)
    search_box.grid(row=0, column=1, sticky='ew', pady=4)
    ttk.Label(frame, text='批次（日期或编号）').grid(row=1, column=0, sticky='w', padx=(0, 12), pady=4)
    picker = ttk.Combobox(frame, textvariable=selected, state='readonly', width=56)
    picker.grid(row=1, column=1, sticky='ew', pady=4)
    ttk.Label(frame, text='邮件预览').grid(row=2, columnspan=3, sticky='w', pady=(10, 4))
    preview = ScrolledText(frame, width=76, height=16, wrap='word', state='disabled')
    preview.grid(row=3, columnspan=3, sticky='nsew')
    ttk.Label(frame, textvariable=status, wraplength=510).grid(row=4, columnspan=2, sticky='w', pady=(10, 0))
    choices = {}
    busy = False
    exit_code = 0

    def show_preview():
        work = choices.get(selected.get())
        enabled = False
        if not work:
            text = '没有符合搜索条件的已完成批次。' if choices else '没有找到包含四份工作簿的已完成批次。'
            status.set('')
        else:
            try:
                config = read_config()
                text = preview_config(config, verified_batch(work))
                enabled = config['enabled']
                status.set('' if enabled else STATUS['disabled'])
            except (KeyError, OSError, TypeError, ValueError) as exc:
                text = error_text(exc)
                status.set('此批次暂不可发送。')
        preview.configure(state='normal')
        preview.delete('1.0', 'end')
        preview.insert('1.0', text)
        preview.configure(state='disabled')
        send_button.configure(state='normal' if enabled and not busy else 'disabled')

    def match_batches(*_, preferred=None, refresh_preview=False):
        matches = filter_batches(choices, search.get())
        picker.configure(values=matches, state='readonly' if matches else 'disabled')
        current = selected.get()
        selected.set(next((item for item in (preferred, current) if item in matches), matches[0] if matches else ''))
        if selected.get() != current or refresh_preview:
            show_preview()

    def refresh(*, initial=False):
        choices.clear()
        choices.update(batch_choices(ROOT))
        preferred = None
        if initial:
            try:
                latest = latest_workflow()
                if latest:
                    target = Path(latest).resolve()
                    preferred = next((label for label, path in choices.items() if path == target), None)
            except (KeyError, OSError, TypeError, ValueError):
                pass
        match_batches(preferred=preferred, refresh_preview=True)

    def send_selected():
        nonlocal busy, exit_code
        work = choices.get(selected.get())
        if busy or work is None:
            return
        busy = True
        for widget in (search_box, picker, refresh_button, send_button):
            widget.configure(state='disabled')
        status.set('正在检查附件，请稍候。')
        events = Queue()

        def confirm(text, duplicate):
            reply = Queue(maxsize=1)
            events.put(('confirm', (text, duplicate, reply)))
            return reply.get()

        def submit():
            try:
                result = send_mail(read_config(), verified_batch(work), confirm)
                events.put(('result', result))
            except Exception as exc:
                events.put(('error', error_text(exc)))

        def poll():
            nonlocal busy, exit_code
            try:
                kind, value = events.get_nowait()
            except Empty:
                root.after(100, poll)
                return
            if kind == 'confirm':
                text, duplicate, reply = value
                confirmed = False
                try:
                    confirmed = gui_confirm(root, text, duplicate)
                    status.set('正在提交邮件，请等待 SMTP 回执。' if confirmed else '正在取消发送。')
                finally:
                    reply.put(confirmed)
                    root.after(100, poll)
                return
            busy = False
            search_box.configure(state='normal')
            refresh_button.configure(state='normal')
            match_batches(refresh_preview=True)
            if kind == 'error':
                exit_code = 1
                messagebox.showerror('发送结果邮件', value, parent=root)
            else:
                exit_code = int(value['status'] in {'failed', 'partial', 'unknown', 'busy'})
                notify_gui(root, value)
                print(show_result(value), flush=True)

        Thread(target=submit, daemon=True).start()
        root.after(100, poll)

    def on_close():
        if busy:
            status.set('当前邮件正在处理，完成后可关闭窗口。')
        else:
            root.quit()

    refresh_button = ttk.Button(frame, text='刷新', command=refresh)
    refresh_button.grid(row=1, column=2, padx=(8, 0), sticky='e')
    send_button = ttk.Button(frame, text='确认发送…', command=send_selected)
    send_button.grid(row=4, column=2, padx=(8, 0), pady=(10, 0), sticky='e')
    picker.bind('<<ComboboxSelected>>', lambda _: show_preview())
    search.trace_add('write', match_batches)
    root.protocol('WM_DELETE_WINDOW', on_close)
    refresh(initial=True)
    root.deiconify()
    search_box.focus_set()
    try:
        root.mainloop()
        return exit_code
    finally:
        root.destroy()
