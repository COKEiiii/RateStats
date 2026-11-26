"""Edit or import local SMTP settings without sending email."""
from pathlib import Path

from market_rates.report_email import (
    CONFIG, DEFAULTS, DELIVERY_MODES, FIELDS, ROOT, SOURCES, Delivery, import_legacy,
    preview_config, read_config, save_config, verified_batch,
)
from scripts.send_report_email import error_text, latest_workflow

LEGACY = ROOT.parent.parent / '新加坡同业银行利率抓取/RateStats2/assets/email_params.log'
LABELS = {
    'smtp_host': 'SMTP 服务器', 'smtp_port': 'SMTP 端口', 'smtp_user': '登录账号',
    'smtp_password': '密码 / 授权码', 'from_email': '发件人（可沿用登录账号）',
    'to_emails': '收件人', 'cc_emails': '抄送（可留空）', 'subject_template': '邮件标题模板',
}


def tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    return root


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk_root()
    root.title('邮件设置')
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(1, weight=1)
    enabled = tk.BooleanVar(root)
    tls = tk.StringVar(root)
    variables = {key: tk.StringVar(root) for key in LABELS}
    ttk.Checkbutton(frame, text='启用正式邮件发送', variable=enabled).grid(
        row=0, columnspan=2, sticky='w', pady=4)
    ttk.Label(frame, text='加密方式').grid(row=1, sticky='w', pady=4)
    ttk.Combobox(frame, textvariable=tls, values=('ssl', 'starttls'), state='readonly').grid(
        row=1, column=1, sticky='ew', pady=4)
    ttk.Label(frame, text='发送方式').grid(row=2, sticky='w', pady=4)
    modes = tuple(DELIVERY_MODES)
    delivery_mode = ttk.Combobox(frame, values=tuple(DELIVERY_MODES.values()), state='readonly')
    delivery_mode.grid(row=2, column=1, sticky='ew', pady=4)
    for row, (key, label) in enumerate(LABELS.items(), 3):
        ttk.Label(frame, text=label).grid(row=row, sticky='w', padx=(0, 12), pady=4)
        ttk.Entry(frame, textvariable=variables[key], width=50,
                  show='*' if key == 'smtp_password' else '').grid(row=row, column=1, sticky='ew', pady=4)
    body_row = len(LABELS) + 3
    ttk.Label(frame, text='邮件正文模板').grid(row=body_row, sticky='nw', pady=4)
    body = tk.Text(frame, width=50, height=6, wrap='word')
    body.grid(row=body_row, column=1, sticky='nsew', pady=4)
    frame.rowconfigure(body_row, weight=1)
    ttk.Label(frame, text='地址用逗号或分号分隔。模板支持 {date}、{batch}、{source_dates}、{attachments}。').grid(
        row=body_row + 1, columnspan=2, sticky='w', pady=4)

    def fill(config):
        mode = config['delivery_mode']
        if mode not in modes:
            raise ValueError('请选择一封或两封邮件的发送方式。')
        enabled.set(config['enabled'])
        tls.set(config['tls'])
        delivery_mode.current(modes.index(mode))
        for key, var in variables.items():
            var.set(config[key])
        body.delete('1.0', 'end')
        body.insert('1.0', config['body_template'])

    def values():
        return {**{key: var.get().strip() for key, var in variables.items()}, 'enabled': enabled.get(),
                'tls': tls.get(), 'delivery_mode': modes[delivery_mode.current()],
                'body_template': body.get('1.0', 'end-1c')}

    def load_old():
        path = filedialog.askopenfilename(parent=root, title='选择旧项目 email_params.log',
                                          initialdir=LEGACY.parent if LEGACY.exists() else ROOT,
                                          filetypes=[('旧邮件配置', '*.log'), ('所有文件', '*')])
        if path:
            fill(import_legacy(path))
            messagebox.showinfo('邮件设置', '已载入旧配置。请检查内容，按需启用并保存。', parent=root)

    def preview():
        config = values()
        workflow = latest_workflow()
        if workflow:
            batch = verified_batch(workflow)
        else:
            sample = ('全量_调研', '全量_彩虹表', '格式对齐_调研', '格式对齐_彩虹表')
            batch = Delivery(Path('示例批次'), ROOT, '2000-01-01',
                             dict.fromkeys(SOURCES, '2000-01-01'), False,
                             {key: Path(f'20000101_{name}.xlsx') for key, name in zip(FIELDS, sample)}, {})
        messagebox.showinfo('邮件预览', preview_config(config, batch, example=not workflow), parent=root)

    def persist():
        save_config(values())
        messagebox.showinfo('邮件设置', '已保存到本机：\n' + str(CONFIG), parent=root)

    def safe(action):
        def invoke():
            try:
                action()
            except (ValueError, OSError, TypeError, KeyError) as exc:
                messagebox.showerror('邮件设置', error_text(exc), parent=root)
        return invoke

    buttons = ttk.Frame(frame)
    buttons.grid(row=body_row + 2, columnspan=2, sticky='e', pady=(10, 0))
    for label, action in [('导入旧配置', load_old), ('预览邮件', preview), ('保存设置', persist)]:
        ttk.Button(buttons, text=label, command=safe(action)).pack(side='left', padx=4)
    fill(DEFAULTS)
    root.deiconify()
    safe(lambda: fill(read_config()))()
    root.mainloop()


if __name__ == '__main__':
    main()
