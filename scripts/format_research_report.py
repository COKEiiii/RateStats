"""File-picker launcher; the implementation/API lives in market_rates.report_format."""
from datetime import datetime
from pathlib import Path
import sys

from market_rates.report_format import main


def wizard():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError:
        print('当前 Python 未安装 tkinter。请使用命令行参数运行：')
        return main(['--help'])
    root=tk.Tk()
    root.withdraw()
    project=Path(__file__).resolve().parents[1]
    options={'parent':root,'filetypes':[('Excel 工作簿','*.xlsx')],'initialdir':str(project.parent)}
    try:
        source=filedialog.askopenfilename(title='选择待整理的调研表（保留采集明细的版本）',**options)
        if not source:
            return 0
        reference=filedialog.askopenfilename(title='选择格式参考表',**options)
        if not reference:
            return 0
        folder=project/'outputs'
        output=filedialog.asksaveasfilename(parent=root,title='选择新文件的保存位置',initialdir=str(folder),
            initialfile=f'{Path(source).stem}_格式对齐_{datetime.now():%Y%m%d_%H%M%S}.xlsx',
            defaultextension='.xlsx',filetypes=[('Excel 工作簿','*.xlsx')],confirmoverwrite=False)
        if not output:
            return 0
        result=main(['--input',source,'--template',reference,'--output',output])
        if result==0:
            messagebox.showinfo('整理完成',f'已保存：\n{output}\n\n增幅按原表最新两列计算。同目录的 .format-report.json 记录比较日期、历史归并、相邻列缺失报价和异常备注。',parent=root)
        return result
    except (Exception,SystemExit) as exc:
        if isinstance(exc,SystemExit) and exc.code==0:
            return 0
        messagebox.showerror('整理失败',str(exc),parent=root)
        return 2
    finally:
        root.destroy()


if __name__=='__main__':
    raise SystemExit(wizard() if sys.argv[1:]==['--wizard'] else main())
