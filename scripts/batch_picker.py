"""Search labels by date, time or batch identifier."""

import re


def filter_batches(choices, query):
    terms = [re.sub(r'[-_/.:]', '', term) for term in query.casefold().split()]
    return tuple(label for label in choices
                 if all(term in re.sub(r'[-_/.:]', '', label.casefold()) for term in terms))
