"""Build the website's live demo (docs/demo) from the app's own front end.

    python scripts/build_site_demo.py           # rebuild docs/demo
    python scripts/build_site_demo.py --check   # fail if docs/demo is stale

The demo is the real interface (static/) with demo/shim.js answering its
requests in the page from the sample data in demo/fixtures.js (shared with
scripts/ui-smoke.cjs). It has to run anywhere the site is opened: on GitHub
Pages, in a sandboxed preview, and straight from disk. So it uses no service
worker, and the app's modules are bundled by esbuild into one plain script
(app.js), since browsers won't load module scripts from file://. The app's
absolute /static/ paths are made relative for the same reason. PDF previews
load the same local viewer and worker as the app, only when a PDF is opened.

docs/demo/manifest.json records the hash of every source and every built
file, so --check (run by scripts/test_site_demo.py) catches a demo built from
older app files, or edited by hand.
"""
import hashlib
import os
import platform
import uuid
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC, DEMO, OUT = REPO / "static", REPO / "demo", REPO / "docs" / "demo"
LEFT_OUT = ("usage-overlay.html", "css/usage-overlay.css")
# The hero and the lazy PDF viewer/worker keep their own module files.
KEPT_JS = ("js/dither.js", "js/vendor/pdf.mjs", "js/vendor/pdf.worker.mjs")
DEMO_STYLE = """<style>
  .demo-badge { position: fixed; z-index: 10200; right: 14px; bottom: 14px; padding: 6px 12px; border-radius: 999px;
    background: #2A1F18; color: #F3EADB; font: 500 11px/1.4 Jost, "Segoe UI", sans-serif; pointer-events: none; opacity: .92; }
</style>"""
# app.js loads a tab's module from a path held in a variable, which no bundler
# can follow. Literal imports bundle built-in views and each prebuilt view.
TAB_IMPORT = "modules[tabId] = await import(path);"


def tab_import_demo():
    prebuilts = ",".join(json.dumps(p.parent.name) + ": () => import(" +
        json.dumps("../../tabs/" + p.parent.name + "/view.js") + ")"
        for p in sorted((REPO / "tabs").glob("*/view.js")))
    return "const prebuiltViews = {" + prebuilts + "}; modules[tabId] = await (prebuiltViews[tabId] ? prebuiltViews[tabId]() : import(`./views/${STUB_TABS.has(tabId) ? 'stub' : tabId}.js`));"



def sources():
    files = {}
    for path in sorted(STATIC.rglob("*")):
        rel = path.relative_to(STATIC).as_posix()
        if path.is_file() and not rel.startswith(LEFT_OUT) and "__pycache__" not in rel:
            files["static/" + rel] = path
    for path in sorted(DEMO.glob("*.js")):
        files["demo/" + path.name] = path
    for path in sorted((REPO / "tabs").rglob("*")):
        if path.is_file() and (path.suffix == ".js" or path.name in ("view.css", "tab.json")):
            files[path.relative_to(REPO).as_posix()] = path
    files["scripts/build_site_demo.py"] = Path(__file__).resolve()
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
    page = page.replace(module, '<script src="app.js"></script>')
    charset = '<meta charset="utf-8">'
    assert charset in page
    page = page.replace(charset, charset + '\n<script src="fixtures.js"></script>\n<script src="shim.js"></script>\n' + DEMO_STYLE, 1)
    page = page.replace('"/static/', '"static/')
    assert '"/' not in page.replace('"//', ""), "static/index.html has an absolute path the demo can't serve"
    return page.replace("<title>Kairos</title>", "<title>Kairos demo</title>")


def esbuild():
    # A git worktree has no node_modules of its own; use the main checkout's.
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    for root in (REPO, Path(common).parent if common else REPO):
        candidate = root / "node_modules" / "esbuild"
        if candidate.exists():
            return candidate
    sys.exit("esbuild is missing: run npm install")


def bundle(target):
    # Stage source rewrites in this worktree, then call the native CLI. This
    # avoids Node's service subprocess (blocked by some Windows runners) and
    # keeps the exact same module identities for relative/absolute imports.
    stage = REPO / "data" / ("demo-build-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        shutil.copytree(STATIC / "js", stage / "static" / "js")
        for original in (REPO / "tabs").rglob("*.js"):
            copied = stage / original.relative_to(REPO)
            copied.parent.mkdir(parents=True, exist_ok=True)
            source = original.read_text(encoding="utf-8")
            def resolve(match):
                absolute = stage / "static" / match.group(2)
                relative = os.path.relpath(absolute, copied.parent).replace(os.sep, "/")
                return match.group(1) + relative + match.group(1)
            source = re.sub(r"([\"'])/static/([^\"']+)\1", resolve, source)
            copied.write_text(source, encoding="utf-8")
        entry = stage / "static" / "js" / "app.js"
        source = entry.read_text(encoding="utf-8")
        assert TAB_IMPORT in source, "app.js no longer loads tabs the way the demo expects"
        entry.write_text(source.replace(TAB_IMPORT, tab_import_demo()), encoding="utf-8")
        system = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux")
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        package = esbuild().parent / "@esbuild" / (system + "-" + arch)
        binary = package / ("esbuild.exe" if system == "win32" else "bin/esbuild")
        found = subprocess.run([str(binary), str(entry), "--bundle", "--format=iife", "--minify",
                                "--legal-comments=none", "--target=es2022", "--log-level=error",
                                "--external:./vendor/pdf.mjs", "--outfile=" + str(target)], cwd=REPO)
        if found.returncode:
            sys.exit("bundling the app for the demo failed")
    finally:
        shutil.rmtree(stage)
    code = target.read_text(encoding="utf-8")
    code = code.replace('./vendor/pdf.mjs', './static/js/vendor/pdf.mjs')
    # Stylesheet CSS variables resolve against static/css/, not the page.
    code = code.replace("url('/static/", "url('${new URL(\"static/\",document.baseURI).href}")
    target.write_text(re.sub(r"""(["'`(])/static/""", r"\1static/", code), encoding="utf-8")


def build():
    files = sources()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name, path in files.items():
        if (name.startswith("static/js/") and path.suffix in (".js", ".mjs") and name[len("static/"):] not in KEPT_JS
                or name.startswith("scripts/") or name == "static/index.html"
                or name.startswith("tabs/") and path.name != "view.css"):
            continue  # bundled into app.js, rewritten as the demo's index.html, or not part of the site
        target = OUT / (name[len("demo/"):] if name.startswith("demo/") else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".css":  # url('/static/img/x') from static/css/ is ../img/x
            relative_static = os.path.relpath(OUT / "static", target.parent).replace(os.sep, "/") + "/"
            target.write_text(path.read_text(encoding="utf-8").replace("/static/", relative_static), encoding="utf-8")
        else:
            shutil.copy2(path, target)
    bundle(OUT / "app.js")
    (OUT / "index.html").write_text(index_html(), encoding="utf-8")
    built = digest({p.relative_to(OUT).as_posix(): p for p in sorted(OUT.rglob("*")) if p.is_file()})
    manifest = {"sources": digest(files), "built": built}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"docs/demo: {len(built)} files, {size / 1048576:.1f} MB")


def check():
    try:
        manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["docs/demo has not been built"]
    want, have = digest(sources()), manifest.get("sources", {})
    stale = sorted(n for n in set(want) | set(have) if want.get(n) != have.get(n))
    built = manifest.get("built", {})
    present = {p.relative_to(OUT).as_posix() for p in OUT.rglob("*") if p.is_file()} - {"manifest.json"}
    for name in sorted(present | set(built)):
        path = OUT / name
        if name not in built or not path.exists() or file_hash(path) != built[name]:
            stale.append("docs/demo/" + name)
    return stale


if __name__ == "__main__":
    if "--check" in sys.argv:
        stale = check()
        print("docs/demo is up to date" if not stale else "docs/demo is stale; run scripts/build_site_demo.py:\n  " + "\n  ".join(stale[:20]))
        sys.exit(1 if stale else 0)
    build()
