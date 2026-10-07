"""Build the website's live demo (docs/demo) from the app's own front end.

    python scripts/build_site_demo.py           # rebuild docs/demo
    python scripts/build_site_demo.py --check   # fail if docs/demo is stale

The demo is the real interface (static/) plus demo/: a service worker that
serves it on a static site and answers its requests from the sample data in
demo/fixtures.js (shared with scripts/ui-smoke.cjs). The PDF viewer is left
out (it loads only when a PDF is opened). docs/demo/manifest.json records
the hash of every source file, so --check (run by scripts/test_site_demo.py)
catches a demo built from older app files.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC, DEMO, OUT = REPO / "static", REPO / "demo", REPO / "docs" / "demo"
LEFT_OUT = ("js/vendor/pdf.mjs", "js/vendor/pdf.worker.mjs", "js/vendor/pdf-assets/")
DEMO_STYLE = """<style>
  .demo-badge { position: fixed; z-index: 10200; right: 14px; bottom: 14px; padding: 6px 12px; border-radius: 999px;
    background: #2A1F18; color: #F3EADB; font: 500 11px/1.4 Jost, "Segoe UI", sans-serif; pointer-events: none; opacity: .92; }
  .demo-unsupported { font: 15px/1.6 Jost, "Segoe UI", sans-serif; color: #3A2A20; text-align: center; margin: 20vh auto; max-width: 420px; }
</style>"""


def sources():
    files = {}
    for path in sorted(STATIC.rglob("*")):
        rel = path.relative_to(STATIC).as_posix()
        if path.is_file() and not rel.startswith(LEFT_OUT) and "__pycache__" not in rel:
            files["static/" + rel] = path
    for path in sorted(DEMO.glob("*.js")):
        files[path.name] = path
    return files


def file_hash(path):
    # Line endings are normalised: Git may check text files out as CRLF.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def digest(files):
    return {name: file_hash(path) for name, path in files.items()}


def index_html():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    module = '<script type="module" src="/static/js/app.js"></script>'
    assert module in page, "static/index.html no longer loads app.js the way the demo expects"
    page = page.replace(module, "")
    charset = '<meta charset="utf-8">'
    assert charset in page
    page = page.replace(charset, charset + '\n<script src="boot.js"></script>\n' + DEMO_STYLE, 1)
    return page.replace("<title>Kairos</title>", "<title>Kairos demo</title>")


def build():
    files = sources()
    if OUT.exists():
        shutil.rmtree(OUT)
    for name, path in files.items():
        target = OUT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    (OUT / "index.html").write_text(index_html(), encoding="utf-8")
    manifest = {"sources": digest(files), "index": hashlib.sha256(index_html().encode()).hexdigest()}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"docs/demo: {len(files)} files, {size / 1048576:.1f} MB")


def check():
    try:
        manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["docs/demo has not been built"]
    want, have = digest(sources()), manifest.get("sources", {})
    stale = sorted(n for n in set(want) | set(have) if want.get(n) != have.get(n))
    if manifest.get("index") != hashlib.sha256(index_html().encode()).hexdigest():
        stale.append("index.html")
    for name, sha in have.items():
        built = OUT / name
        if not built.exists() or file_hash(built) != sha:
            stale.append("docs/demo/" + name)
    return stale


if __name__ == "__main__":
    if "--check" in sys.argv:
        stale = check()
        print("docs/demo is up to date" if not stale else "docs/demo is stale; run scripts/build_site_demo.py:\n  " + "\n  ".join(stale[:20]))
        sys.exit(1 if stale else 0)
    build()
