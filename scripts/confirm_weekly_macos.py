"""Preserve the explicit human confirmation required before rolling history."""
import argparse
from pathlib import Path
from market_rates.common import load
from market_rates.weekly_history import approve

def main():
    p=argparse.ArgumentParser();p.add_argument("--output");args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    output=args.output or load(root/"outputs/latest-workflow.json")["output"]
    print("将登记经人工检查的两份工作簿："+output)
    print("请先在 Excel 中检查利率、条件和历史日期。")
    if input("完成检查后输入 YES 登记；其他输入退出：").strip()!="YES":return 0
    record=approve(root,output,"用户运行 Mac 历史确认入口并输入 YES：已人工检查本期两份工作簿")
    print("已登记："+record["as_of"]+"\n"+record["report"])
    return 0

if __name__=="__main__":raise SystemExit(main())
