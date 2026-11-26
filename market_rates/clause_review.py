"""Compare independent clause transcripts without correcting either source."""
from collections import defaultdict
import unicodedata


def normalized_text(value):
    # Typography/spacing only: preserve case, numbers, negation and conjunctions.
    value = unicodedata.normalize("NFKC", value).translate(str.maketrans("‘’“”", "''\"\""))
    return " ".join(value.split())


def clause_differences(metadata):
    clauses=defaultdict(list)
    for record in metadata:
        if record.get('lane')=='llm':
            for clause in record.get('clause_ledger',[]):clauses[(clause['page_id'],clause['number'])].append(clause)
    differences=[]
    by_page=defaultdict(set)
    for (page,number),items in clauses.items():
        by_page[page].add(number)
        if len(items)!=1 or not items[0].get('quote'):
            differences.append(dict(page_id=page,number=number,reason='条款重复或缺少原文',llm=items))
    for page,numbers in by_page.items():
        for number in sorted(set(range(min(numbers),max(numbers)+1))-numbers):
            differences.append(dict(page_id=page,number=number,reason='条款编号缺失，需人工检查文字来源',llm=[]))
    return differences
