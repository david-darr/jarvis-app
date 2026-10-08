"""The website's live demo (docs/demo) is built from the current app.

What this proves: every file the demo serves matches the app's front end
(static/) and the demo's own files (demo/) as they are now, so the site never
shows an older interface than the app ships, and the sample data the demo
answers with is the same module ui-smoke tests against.

Run: python scripts/test_site_demo.py (rebuild with scripts/build_site_demo.py)
"""
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_site_demo", REPO / "scripts" / "build_site_demo.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class SiteDemoTests(unittest.TestCase):
    def test_the_demo_is_built_from_the_current_app(self):
        self.assertEqual(build.check(), [], "docs/demo is stale; run python scripts/build_site_demo.py")

    def test_the_check_notices_a_changed_app_file(self):
        target = REPO / "static" / "css" / "kairos-theme.css"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n/* probe */\n")
            self.assertIn("static/css/kairos-theme.css", build.check())
        finally:
            target.write_bytes(original)

    def test_the_demo_shares_ui_smokes_sample_data(self):
        smoke = (REPO / "scripts" / "ui-smoke.cjs").read_text(encoding="utf-8")
        self.assertIn('require("../demo/fixtures.js")', smoke)
        self.assertTrue((REPO / "docs" / "demo" / "fixtures.js").exists())

    def test_the_demo_runs_without_a_server(self):
        # Opened from disk there is no service worker and no module loading,
        # so the page loads plain scripts by relative paths only.
        page = (REPO / "docs" / "demo" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('type="module"', page)
        self.assertNotIn('"/static/', page)
        self.assertFalse((REPO / "docs" / "demo" / "sw.js").exists())
        self.assertIn('src="demo/index.html"', (REPO / "docs" / "index.html").read_text(encoding="utf-8"))

    def test_the_demo_ships_the_same_local_pdf_viewer_and_worker(self):
        for name in ("pdf.mjs", "pdf.worker.mjs"):
            self.assertTrue((REPO / "docs" / "demo" / "static" / "js" / "vendor" / name).exists())
        self.assertIn('./static/js/vendor/pdf.mjs', (REPO / "docs" / "demo" / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
