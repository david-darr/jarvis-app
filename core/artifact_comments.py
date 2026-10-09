"""Validated artifact locators and bounded, server-read review excerpts."""
import re
from html.parser import HTMLParser
from urllib.parse import unquote

from core import attachments, chat_artifacts, office_preview
from core.untrusted import wrap_untrusted

MAX_COMMENTS = 10
MAX_EXCERPT = 4000
POS = r"[1-9][0-9]{0,8}"
CELL = r"[A-Z]{1,3}[1-9][0-9]{0,6}"
FRACTION = r"(?:0(?:\.[0-9]{1,4})?|1(?:\.0{1,4})?)"
RECT = rf"(?:;rect=({FRACTION}),({FRACTION}),({FRACTION}),({FRACTION}))?"
PATTERNS = [
    rf"pptx:slide=({POS});shape=({POS})", r"docx:block=(0|[1-9][0-9]{0,8})",
    rf"xlsx:sheet=((?:[A-Za-z0-9_.!~*'()-]|%[0-9A-Fa-f]{{2}})+);cell=({CELL})(?::({CELL}))?",
    rf"pdf:page=({POS}){RECT}", rf"image{RECT}",
    rf"html:path=([a-z][a-z0-9-]*(?::nth-of-type\({POS}\))?(?:>[a-z][a-z0-9-]*(?::nth-of-type\({POS}\))?)*)",
    rf"text:line=({POS})", rf"(?:text|md):lines=({POS})-({POS})",
]


def validate(reference):
    url, comment, picks = (reference.get(k) for k in ("url", "comment", "picks"))
    if (not isinstance(url, str) or not 1 <= len(url) <= 500 or
            not url.startswith(("/generated-files/", "/generated-images/", "/chat-files/")) or
            not isinstance(comment, str) or not 1 <= len(comment) <= 4000 or not comment.strip() or
            not isinstance(picks, list) or not 1 <= len(picks) <= 50):
        raise ValueError("Invalid artifact comment")
    for pick in picks:
        if not isinstance(pick, str) or not 1 <= len(pick) <= 300:
            raise ValueError("Invalid artifact locator")
        match = next((m for pattern in PATTERNS if (m := re.fullmatch(pattern, pick))), None)
        if match is None:
            raise ValueError("Invalid artifact locator")
        if ";rect=" in pick:
            x, y, w, h = map(float, pick.split(";rect=")[1].split(","))
            if w <= 0 or h <= 0 or x + w > 1.00001 or y + h > 1.00001:
                raise ValueError("Invalid artifact rectangle")
        if ":lines=" in pick:
            start, end = map(int, pick.split("=")[1].split("-"))
            if start > end:
                raise ValueError("Invalid artifact line range")
    return {"kind": "artifact_comment", "url": url, "comment": comment, "picks": list(picks),
            "label": (reference.get("label") if isinstance(reference.get("label"), str) else "Artifact comment")[:500]}


class _HTMLText(HTMLParser):
    """Mirror the static preview's forbidden tags; retain no executable markup."""
    VOID = {"area", "br", "col", "hr", "img", "input", "source", "track", "wbr"}
    DROP = {"script", "iframe", "embed", "base", "meta", "link", "template", "noscript", "noembed", "noframes", "plaintext", "xmp"}
    UNWRAP = {"a", "form", "object"}
    # Static tags retained by the preview's bundled DOMPurify. Unknown HTML
    # wrappers are unwrapped there, and must not shift a CSS path here.
    ALLOWED = set((
        "abbr acronym address area article aside audio b bdi bdo big blink blockquote body br button canvas caption center "
        "cite code col colgroup content data datalist dd decorator del details dfn dialog dir div dl dt element em fieldset "
        "figcaption figure font footer h1 h2 h3 h4 h5 h6 head header hgroup hr html i img input ins kbd label legend li main "
        "map mark marquee menu menuitem meter nav nobr ol optgroup option output p picture pre progress q rp rt ruby s samp "
        "search section select shadow slot small source spacer span strike strong style sub summary sup table tbody td textarea "
        "tfoot th thead time tr track tt u ul var video wbr "
        "svg altglyph altglyphdef altglyphitem animatecolor animatemotion animatetransform circle clippath defs desc ellipse "
        "filter g glyph glyphref hkern image line lineargradient marker mask metadata mpath path pattern polygon polyline "
        "radialgradient rect stop switch symbol text textpath title tref tspan view vkern "
        "math menclose merror mfenced mfrac mglyph mi mlabeledtr mmultiscripts mn mo mover mpadded mphantom mroot mrow ms "
        "mspace msqrt mstyle msub msubsup msup mtable mtd mtext mtr munder munderover mprescripts none"
    ).split())

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.body = {"tag": "body", "children": [], "text": []}
        self.stack = [self.body]
        self.dropped = []
        self.in_head = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if self.dropped:
            if tag not in self.VOID and tag not in {"base", "meta", "link", "embed"}: self.dropped.append(tag)
            return
        if tag in self.DROP:
            if tag not in {"base", "meta", "link", "embed"}: self.dropped.append(tag)
            return
        if tag == "head": self.in_head = True; return
        if tag in {"html", "body"} | self.UNWRAP or self.in_head: return
        if tag not in self.ALLOWED: return
        # Match the browser's common implied elements/end tags, so source
        # tables and lists use the same nth-of-type paths as the static DOM.
        if tag == "li" and self.stack[-1]["tag"] == "li": self.stack.pop()
        if tag in {"p", "div", "section", "article", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"} and self.stack[-1]["tag"] == "p": self.stack.pop()
        if tag == "tr":
            if self.stack[-1]["tag"] in {"td", "th"}: self.stack.pop()
            if self.stack[-1]["tag"] == "tr": self.stack.pop()
            if self.stack[-1]["tag"] == "table": self.handle_starttag("tbody", [])
        if tag in {"td", "th"} and self.stack[-1]["tag"] in {"td", "th"}: self.stack.pop()
        node = {"tag": tag, "children": [], "text": []}
        self.stack[-1]["children"].append(node)
        self.stack[-1]["text"].append(node)
        if tag not in self.VOID: self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.dropped:
            if tag in self.dropped:
                self.dropped = self.dropped[:len(self.dropped) - 1 - self.dropped[::-1].index(tag)]
            return
        if tag == "head": self.in_head = False
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag: self.stack = self.stack[:i]; break

    def handle_data(self, data):
        if not self.dropped and not self.in_head:
            self.stack[-1]["text"].append(data)

    def picked(self, path):
        parts = path.split(">")
        if parts.pop(0) != "body": raise ValueError("HTML locator must start at body")
        node = self.body
        for part in parts:
            match = re.fullmatch(r"([a-z][a-z0-9-]*)(?::nth-of-type\(([0-9]+)\))?", part)
            children = [child for child in node["children"] if child["tag"] == match[1]]
            index = int(match[2] or 1) - 1
            if index >= len(children): raise ValueError("HTML element is no longer available")
            node = children[index]
        result, remaining, stack = [], MAX_EXCERPT, [iter(node["text"])]
        while stack and remaining:
            part = next(stack[-1], None)
            if part is None: stack.pop()
            elif isinstance(part, str):
                result.append(part[:remaining]); remaining -= min(len(part), remaining)
            else: stack.append(iter(part["text"]))
        return "".join(result)


def _label(text):
    """A name read from the file, safe to sit outside the untrusted fence:
    one short line with no quotes or control characters, so it can't pose as
    an instruction or close the sentence it sits in."""
    text = re.sub(r'[\x00-\x1f\x7f"`]+', " ", str(text))
    text = " ".join(text.split())
    return text[:60] + ("…" if len(text) > 60 else "")


def _cell(cell):
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", cell)
    column = 0
    for char in match[1]: column = column * 26 + ord(char) - 64
    return int(match[2]), column


def _pdf_excerpt(path, page_number, rect):
    """Best effort text only; failed optional extraction keeps the locator."""
    try:
        pdf = attachments.pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        if page_number > len(pdf): raise ValueError("PDF page is no longer available")
        try:
            page = pdf[page_number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    if rect:
                        x, y, w, h = rect
                        pw, ph = page.get_size()
                        return textpage.get_text_bounded(x*pw, (1-y-h)*ph, (x+w)*pw, (1-y)*ph)
                    return textpage.get_text_range()
                finally: textpage.close()
            finally: page.close()
        except Exception:
            return None
    finally: pdf.close()


def resolve(session_id, reference):
    ref = validate(reference)
    path, meta = chat_artifacts.resolve(session_id, ref["url"])
    ext = path.suffix.lower()
    data = None
    source = None
    words, excerpts = [], []
    if ext in office_preview.PREVIEWABLE and meta["size"] <= chat_artifacts.MAX_OFFICE_BYTES:
        data = office_preview.extract(path, ext, images=False)
    if ext in chat_artifacts.TEXT_EXTENSIONS and meta["size"] <= chat_artifacts.MAX_PREVIEW_BYTES:
        source = path.read_text(encoding="utf-8", errors="replace")
    html = _HTMLText(source) if ext in {".html", ".htm"} and source is not None else None
    cells_left = 200
    for pick in ref["picks"]:
        prefix = pick.split(":")[0].split(";")[0]
        excerpt = ""
        if prefix == "pptx" and data and data["kind"] == "pptx":
            slide_n, shape_id = map(int, re.findall(r"=([0-9]+)", pick))
            if slide_n > len(data["slides"]): raise ValueError("Slide is no longer available")
            slide = data["slides"][slide_n - 1]
            shape = next((s for s in slide["shapes"] + slide.get("groups", []) if s["id"] == shape_id), None)
            if shape is None: raise ValueError("Shape is no longer available")
            words.append(f'slide {slide_n}, shape {shape_id} "{_label(shape["name"])}" ({shape["kind"]})')
            selected = [shape] if shape["kind"] != "group" else [s for s in slide["shapes"] if s["id"] in shape["children"]]
            excerpt = "\n".join("\n".join("".join(r["text"] for r in p["runs"]) for p in s["paragraphs"]) or
                                "\n".join("\t".join(row) for row in s.get("rows", [])) or s["kind"] for s in selected)
        elif prefix == "docx" and data and data["kind"] == "docx":
            index = int(pick.split("=")[1])
            if index >= len(data["blocks"]): raise ValueError("Block is no longer available")
            block = data["blocks"][index]
            words.append(f"document block {index}")
            excerpt = block.get("text") or "\n".join("\t".join(row) for row in block.get("rows", [])) or block["type"]
        elif prefix == "xlsx" and data and data["kind"] in {"xlsx", "csv"}:
            sheet, cell_range = pick[11:].split(";cell=")
            sheet = unquote(sheet)
            rows = data["rows"] if data["kind"] == "csv" and sheet == "csv" else next((s["rows"] for s in data.get("sheets", []) if s["name"] == sheet), None)
            if rows is None: raise ValueError("Sheet is no longer available")
            start, _, end = cell_range.partition(":")
            r1, c1 = _cell(start); r2, c2 = _cell(end or start)
            if r2 < r1 or c2 < c1 or r2 > len(rows) or c2 > max((len(row) for row in rows), default=0): raise ValueError("Invalid cell range")
            words.append(f'sheet "{_label(sheet)}" cells {cell_range}')
            available = cells_left
            values = []
            for row in range(r1 - 1, r2):
                row_values = []
                for col in range(c1 - 1, c2):
                    if cells_left <= 0: break
                    row_values.append(str(rows[row][col]) if col < len(rows[row]) else "")
                    cells_left -= 1
                values.append("\t".join(row_values))
                if cells_left <= 0: break
            excerpt = "\n".join(values)
            if available < (r2-r1+1)*(c2-c1+1):
                excerpt += "\n[Selected cells limited to 200 per comment.]"
        elif prefix in {"text", "md"} and source is not None:
            interval = pick.split("=")[1].split("-")
            start, end = int(interval[0]), int(interval[-1])
            lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if start > len(lines) or end > len(lines): raise ValueError("Lines are no longer available")
            words.append(f"line {start}" if start == end else f"lines {start} through {end}")
            excerpt = "\n".join(lines[start - 1:end])
        elif prefix == "html" and html:
            css_path = pick.split("=", 1)[1]
            words.append(f"HTML element {css_path}")
            excerpt = html.picked(css_path)
        elif (prefix == "pdf" and ext == ".pdf") or (prefix == "image" and meta["kind"] == "image"):
            page_n = int(re.search(r"page=([0-9]+)", pick)[1]) if prefix == "pdf" else None
            location = f"page {page_n}" if page_n else "the image"
            rect = tuple(map(float, pick.split(";rect=")[1].split(","))) if ";rect=" in pick else None
            if rect:
                x, y, w, h = rect
                location += f", the region at {x*100:g}%–{(x+w)*100:g}% across and {y*100:g}%–{(y+h)*100:g}% down"
            words.append(location)
            excerpt = location
            if page_n and attachments.pdfium is not None and meta["size"] <= chat_artifacts.MAX_OFFICE_BYTES:
                excerpt = _pdf_excerpt(path, page_n, rect) or location
        else:
            raise ValueError("The locator does not match this artifact's preview")
        excerpts.append(excerpt[:MAX_EXCERPT])
    # Only the comment is user-authored. Shape and sheet names come from the
    # authorized file, so they are reduced to short single-line labels
    # (_label); everything else selected from the file lives inside the fence.
    sentence = f"The user commented on {' and '.join(words)} of {meta['filename']} (file: {path.resolve()}): {ref['comment']}"
    return sentence + "\n\n" + wrap_untrusted("selected artifact content", "\n\n".join(excerpts)[:MAX_EXCERPT])


PUBLISH_INSTRUCTION = (
    "To change the file, edit a copy in this chat's workspace and publish it as a new version "
    "with the same filename using save_generated_file (path and description); include the returned "
    "download link in your reply. Never overwrite the original, which the user may still want."
)
