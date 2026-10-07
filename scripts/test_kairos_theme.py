"""The Kairos theme holds (brand handoff 2026-10-06; spec: the vault note
"Kairos Rebrand (Build Spec)").

What these prove: every color in the app's stylesheets and scripts comes from
the theme's tokens, so Settings > Appearance can re-derive all of them and a
dark-ground literal can't creep back onto parchment; and every text color the
theme defines reads at WCAG AA (4.5:1) on every ground it is used on.

No app or browser is started.
Run: python scripts/test_kairos_theme.py
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d")

# Files whose literal colors are the point, each with why.
ALLOWED_FILES = {
    "css/kairos-theme.css": "the brand palette itself",
    "js/appearance.js": "derives the tokens for a person's own base color",
    "js/views/agents.js": "the agent colors the backend accepts (services/agent_service.py COLORS)",
    "js/views/appearancePanel.js": "the base colors a person can pick",
}
# Single literals allowed elsewhere, each with why.
ALLOWED_SNIPPETS = (
    "linear-gradient(#fff 0 0)",          # a mask shape, not a painted color
    "background: #fff; }",                # web pages and documents keep their own white page
    "color:#202124;background:white",     # an HTML artifact's own sandboxed page
)


def root_block(text: str) -> tuple[int, int]:
    start = text.find(":root {")
    return (start, text.find("\n}", start)) if start >= 0 else (-1, -1)


def raw_colors(path: Path) -> list[str]:
    rel = path.relative_to(STATIC).as_posix()
    if rel in ALLOWED_FILES:
        return []
    text = path.read_text(encoding="utf-8")
    skip = []
    if rel == "css/style.css":
        skip.append(root_block(text))  # the token map
    if rel == "css/usage-overlay.css":
        start = text.find(":root{--n-body")
        skip.append((start, text.find("}", start)))  # the notch's own reverse palette
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        offset = sum(len(l) + 1 for l in text.splitlines()[:number - 1])
        if any(a <= offset <= b for a, b in skip):
            continue
        if line.lstrip().startswith(("//", "/*", "*")):
            continue
        if COLOR.search(line) and not any(s in line for s in ALLOWED_SNIPPETS):
            found.append(f"{rel}:{number}: {line.strip()[:120]}")
    return found


def files():
    for pattern in ("css/*.css", "js/*.js", "js/views/*.js"):
        yield from sorted(STATIC.glob(pattern))


# -- contrast --------------------------------------------------------------

def tokens() -> dict:
    text = (STATIC / "css" / "kairos-theme.css").read_text(encoding="utf-8")
    style = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    a, b = root_block(style)
    found = {}
    for source in (text, style[a:b]):
        for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", source):
            found[name] = value.strip()
    return found


def resolve(name: str, table: dict) -> tuple:
    value = table[name]
    while value.startswith("var("):
        value = table[value[4:-1].strip()]
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    raise ValueError(f"{name} is {value}, not a solid color")


def luminance(rgb) -> float:
    c = [v / 255 for v in rgb]
    c = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in c]
    return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]


def ratio(a, b) -> float:
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + .05) / (y + .05)


class ThemeTests(unittest.TestCase):
    def test_no_raw_colors_outside_the_theme(self):
        found = [hit for path in files() for hit in raw_colors(path)]
        self.assertEqual(found, [], "use a theme token (static/css/style.css :root):\n" + "\n".join(found))

    def test_the_check_would_catch_a_raw_color(self):
        probe = STATIC / "css" / "zz-probe.css"
        probe.write_text(".x { color: #101113; background: rgba(255, 255, 255, .04); }\n", encoding="utf-8")
        try:
            self.assertEqual(len(raw_colors(probe)), 1)
        finally:
            probe.unlink()

    def test_text_reads_at_aa_on_every_ground(self):
        table = tokens()
        grounds = ["--bg", "--bg-panel-solid", "--surface-2", "--sidebar-bg"]
        texts = ["--text", "--text-dim", "--text-faint", "--accent", "--danger", "--success", "--warn"]
        low = []
        for ground in grounds:
            for text in texts:
                value = ratio(resolve(text, table), resolve(ground, table))
                if value < 4.5:
                    low.append(f"{text} on {ground}: {value:.2f}")
        self.assertEqual(low, [], "below WCAG AA (4.5:1)")

    def test_the_ink_pill_reads(self):
        table = tokens()
        self.assertGreaterEqual(ratio(resolve("--text", table), resolve("--bg-panel-solid", table)), 4.5)


if __name__ == "__main__":
    unittest.main()
