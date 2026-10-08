"""A resource-free HTML subset for read-only Google Docs previews."""
import html
import re
from html.parser import HTMLParser


class _Reader(HTMLParser):
    tags = frozenset("p div span h1 h2 h3 h4 h5 h6 strong b em i u s del sub sup br hr ul ol li blockquote pre code table thead tbody tfoot tr th td caption a".split())
    blocked = frozenset("script style iframe object embed svg math template noscript head form textarea".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in self.blocked:
                self.skip.append(tag)
            return
        if tag in self.blocked:
            self.skip.append(tag)
            return
        if tag not in self.tags:
            return
        safe = []
        for key, value in attrs:
            if value is None:
                continue
            if key in ("title", "lang") or (key == "dir" and value in ("ltr", "rtl", "auto")):
                safe.append((key, value[:500]))
            elif tag in ("td", "th") and key in ("colspan", "rowspan") and re.fullmatch(r"[1-9][0-9]?", value):
                safe.append((key, value))
            elif tag == "a" and key == "href" and re.fullmatch(r"#[A-Za-z0-9_-]+", value):
                safe.append((key, value))
        self.output.append("<" + tag + "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in safe) + ">")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
        elif tag in self.tags and tag not in ("br", "hr"):
            self.output.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.skip:
            self.output.append(html.escape(data))


def sanitize_html(content: str) -> str:
    reader = _Reader()
    reader.feed(content)
    reader.close()
    return "<!doctype html><meta charset=utf-8>" + "".join(reader.output)
