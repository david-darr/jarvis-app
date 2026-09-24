"""A read-only browser for models (Hermes phase 7 step 3, 2026-09-24).

Hermes Agent's browser tools drive Chromium sessions; this is the narrow
JARVIS version: fetch one public page, rendered, and hand back its title,
readable text and links. Each call is a fresh headless Chromium inside a
core/sandbox.py container on core/sandbox_egress.py's filtered network, so
it reaches public addresses on 80/443 only, and it keeps nothing: no
logins, cookies or history survive the call.

Chromium's own sandbox is off (`--no-sandbox`): it needs kernel features the
container deliberately withholds, so the container is the containment, the
usual arrangement for Chromium in Docker. `--proxy-bypass-list=<-loopback>`
cancels Chromium's habit of reaching localhost directly, so even that goes
to the filter and is refused.

The image is Microsoft's Playwright image, pinned by digest; only its
Chromium headless shell is used, and the page is read with Python's
standard library, so nothing is installed at run time.
"""
import json
from typing import Optional
from urllib.parse import urlsplit

from core import sandbox

BROWSER_IMAGE = ("mcr.microsoft.com/playwright/python:v1.63.0-noble"
                 "@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0")
CHROME = "/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell"
PAGE_SECONDS = 45
MAX_TEXT_CHARS = 12_000
MAX_LINKS = 60

_EXTRACT = r'''
import json, sys
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer", "pre"}


class Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.skip, self.in_title, self.in_a = base, 0, False, None
        self.title, self.parts, self.links = "", [], []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "mailto:", "#", "data:")):
                self.in_a = [urljoin(self.base, href), ""]

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.in_a:
            self.links.append({"url": self.in_a[0], "text": " ".join(self.in_a[1].split())[:120]})
            self.in_a = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)
            if self.in_a:
                self.in_a[1] += data


page = Page(sys.argv[2])
page.feed(open(sys.argv[1], encoding="utf-8", errors="replace").read())
lines = [" ".join(line.split()) for line in "".join(page.parts).splitlines()]
seen, links = set(), []
for link in page.links:
    if link["url"] not in seen:
        seen.add(link["url"])
        links.append(link)
# Trimmed here: the sandbox caps what a run prints, and a cut JSON is unreadable.
text = "\n".join(l for l in lines if l)
limit, max_links = int(sys.argv[3]), int(sys.argv[4])
print(json.dumps({"title": " ".join(page.title.split()), "text": text[:limit], "cut": len(text) > limit,
                  "links": links[:max_links]}))
'''


def _check_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("give a full http:// or https:// address")
    return url


async def browse(url: str) -> dict:
    """{"url", "title", "text", "links": [{"url", "text"}], "error"}. Raises
    sandbox.SandboxUnavailable when Docker or the filter is not there."""
    url = _check_url(url)
    command = (
        f"timeout -s KILL {PAGE_SECONDS}s {CHROME} --no-sandbox --disable-gpu --disable-dev-shm-usage "
        "--no-first-run --disable-background-networking --disable-extensions --user-data-dir=/tmp/profile "
        f"--proxy-server={_proxy()} --proxy-bypass-list='<-loopback>' --virtual-time-budget=8000 "
        "--dump-dom \"$PAGE_URL\" > /tmp/page.html 2>/dev/null; "
        f"python3 extract.py /tmp/page.html \"$PAGE_URL\" {MAX_TEXT_CHARS} {MAX_LINKS}"
    )
    result = await sandbox.run(f"PAGE_URL={_shell_quote(url)}; export PAGE_URL; {command}",
                               files={"extract.py": _EXTRACT}, network=True, image=BROWSER_IMAGE,
                               memory="1g", pids=512, timeout=PAGE_SECONDS + 30)
    try:
        page = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"url": url, "title": "", "text": "", "links": [],
                "error": f"the browser did not finish (exit {result.exit_code})"}
    error: Optional[str] = None
    if "Blocked by the JARVIS sandbox egress filter" in page["text"]:
        error = "refused: " + page["text"].split("egress filter:", 1)[-1].strip()[:200]
        page.update(text="", links=[])
    elif not page["text"] and not page["title"]:
        error = "nothing loaded: the address was refused by the filter or did not answer"
    text = page["text"] + ("\n... page text cut here" if page.get("cut") else "")
    return {"url": url, "title": page["title"], "text": text, "links": page["links"], "error": error}


def _proxy() -> str:
    from core import sandbox_egress
    return sandbox_egress.PROXY_URL


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
