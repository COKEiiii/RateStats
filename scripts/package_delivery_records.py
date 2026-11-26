"""Export missing user-readable records for an existing four-workbook delivery."""
import argparse
from market_rates.delivery_records import package_records
from scripts.run_all import project_lock


def main(argv=None):
    parser=argparse.ArgumentParser(description='补齐已完成任务的中文运行记录、日志副本和证据入口，不改写 Excel。')
    parser.add_argument('--workflow',required=True)
    args=parser.parse_args(argv)
    with project_lock():
        result=package_records(args.workflow)
    print(f'已整理 {result["copied_records"]} 份记录副本，{result["linked_records"]} 份大记录保留原位置。')
    print(result['summary'])
    print(result['index'])
    return result


if __name__=='__main__':
    main()
