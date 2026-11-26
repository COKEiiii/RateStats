"""Remove only explicit screen-reader footnote hints from literal DOM text.

Visible footnote markers remain. This does not normalize model answers:
the complete literal text stays bound to its archived HTML source.
"""
from html.parser import HTMLParser
import re


class SourceTree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = dict(tag='root', attrs={}, children=[])
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), children=[])
        self.stack[-1]['children'].append(node)
        self.nodes.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1]['children'].append(data)


def text_content(node):
    if isinstance(node, str):
        return node
    return ''.join(text_content(c) for c in node['children'])


def visible_source_text(html, expected):
    """Return an unambiguous original-DOM match with hidden footnote hints removed.

    No rate, eligibility sentence, visible footnote number or punctuation is
    removed. If the exact source passage cannot be matched, return None.
    """
    tree = SourceTree()
    tree.feed(html)
    compact = lambda value: re.sub(r'\s+', '', value)
    original = compact(expected)
    candidates = []
    for node in tree.nodes:
        if node['tag'] not in {'strong', 'b', 'span', 'div', 'p', 'h1', 'h2', 'h3', 'h4', 'caption'}:
            continue
        if compact(text_content(node)) != original:
            continue
        removed = []
        def read(part):
            if isinstance(part, str):
                return part
            classes = set((part['attrs'].get('class') or '').split())
            text = text_content(part).strip()
            if classes & {'visuallyhidden', 'sr-only', 'screen-reader-only'} and re.fullmatch(r'Footnote link\s+\d+', text):
                removed.append(text)
                return ''
            return ''.join(read(child) for child in part['children'])
        visible = read(node).strip()
        if removed:
            candidates.append(dict(text=visible, removed=removed))
    if not candidates or len({compact(c['text']) for c in candidates}) != 1:
        return None
    return candidates[-1]
