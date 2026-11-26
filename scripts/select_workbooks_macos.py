"""Native macOS file picker for selecting both workbook templates."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog
from market_rates.input_workbooks import save_configuration

ROOT=Path(__file__).resolve().parents[1]

def main():
    root=tk.Tk();root.withdraw()
    try:
        report=filedialog.askopenfilename(title="选择完整调研底稿",initialdir=ROOT/"inputs",filetypes=[("Excel 工作簿","*.xlsx")])
        if not report:return 0
        rainbow=filedialog.askopenfilename(title="选择完整彩虹底稿",initialdir=ROOT/"inputs",filetypes=[("Excel 工作簿","*.xlsx")])
        if not rainbow:return 0
        print("已保存："+str(save_configuration("selected",report,rainbow)))
        return 0
    finally:root.destroy()

if __name__=="__main__":raise SystemExit(main())
