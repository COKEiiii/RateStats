"""Dispatch the daily macOS launchers."""
import argparse
import subprocess
import sys
from pathlib import Path
from scripts.platform_runtime import prepare_environment

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument("action");args=p.parse_args()
    py=sys.executable
    actions={
        "email-settings":[py,"-m","scripts.email_settings"],
        "send-email":[py,"-m","scripts.send_report_email","--gui"],
        "run":[py,"-m","scripts.run_all","--mail-gui"],
        "rebuild":[py,"-m","scripts.run_all","--mode","rebuild","--mail-gui"],
        "check":[py,"-m","scripts.run_all","--check"],
        "sgd-rebuild":[py,"-m","scripts.run_market_workflow","--mode","rebuild"],
        "review":[py,"-m","scripts.review_server"],
        "board-review":[py,"-m","scripts.review_server","--dataset","board","--port","8766"],
        "fx-review":[py,"-m","scripts.review_server","--dataset","fx","--port","8767"],
        "show-repair":[py,"-m","scripts.show_repair_report"],
        "format":[py,"-m","scripts.format_research_report","--wizard"],
        "select":[py,"-m","scripts.select_workbooks_macos"],
        "confirm":[py,"-m","scripts.confirm_weekly_macos"],
        "fx-rebuild":[py,"-m","scripts.run_fx_promo"],
    }
    if args.action=="resume":
        from scripts.resume_gui import choose_workflow
        work=choose_workflow(ROOT)
        if work is None:return 0
        cmd=[py,"-m","scripts.run_all","--resume",str(work),"--resume-only","--mail-gui"]
    elif args.action in actions:cmd=actions[args.action]
    else:raise ValueError("Unknown Mac action: "+args.action)
    if args.action in {'run','resume','rebuild','check'}:
        return subprocess.run(cmd,cwd=ROOT,env=prepare_environment()).returncode
    subprocess.run(cmd,cwd=ROOT,check=True,env=prepare_environment())
    return 0

if __name__=="__main__":raise SystemExit(main())
