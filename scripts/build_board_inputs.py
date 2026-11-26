import argparse
from market_rates.input_sheets import create

def main():
    parser=argparse.ArgumentParser();parser.add_argument('output');args=parser.parse_args()
    print(create(args.output,'board'))

if __name__=='__main__':main()
