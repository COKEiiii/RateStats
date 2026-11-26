"""Open the latest run's recognition summary without starting collection."""
from pathlib import Path
from market_rates.common import load
from market_rates.repair_feedback import write_summary
from scripts.platform_runtime import open_path

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    latest=load(ROOT/'outputs/latest-all-run.json');work=Path(latest['workflow']).resolve()
    work.relative_to(ROOT/'runs')
    target=write_summary(work);print(target);open_path(target)
