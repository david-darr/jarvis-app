"""Keep tab implementations behind the stable API. Run without an app."""
import ast
import re
import unittest
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.tab_checks import check_python
SLUG_CODE = re.compile(r'''tab-crm|tab-school|crm_|["'](?:crm|school)["']''')


def python_code(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if hasattr(node, "body") and isinstance(node.body, list):
            node.body = [n for n in node.body if not (isinstance(n, ast.Expr)
                         and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
    return ast.unparse(tree), tree


def javascript_code(source):
    # Preserve quoted/template strings while discarding comments.
    tokens = r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*[\s\S]*?\*/'''
    return re.sub(tokens, lambda m: m[1] or "", source)


class TabBoundaries(unittest.TestCase):
    def test_base_code_has_no_tab_implementation_dependencies(self):
        files = [REPO / "app.py"]
        for folder in ("core", "routes", "services"):
            files += list((REPO / folder).rglob("*.py"))
        for path in files:
            with self.subTest(file=str(path.relative_to(REPO))):
                code, tree = python_code(path.read_text(encoding="utf-8-sig"))
                self.assertIsNone(SLUG_CODE.search(code))
                for node in ast.walk(tree):
                    modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                               else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                    self.assertFalse(any(m.split(".")[0] in ("kairos_tabs", "tabs") for m in modules))
        for path in (REPO / "static/js").rglob("*.js"):
            with self.subTest(file=str(path.relative_to(REPO))):
                code = javascript_code(path.read_text(encoding="utf-8"))
                self.assertIsNone(SLUG_CODE.search(code))
                self.assertIsNone(re.search(r'''(?:from\s*|import\s*\()["'][^"']*(?:kairos_tabs|/tabs/)''', code))

    def test_tabs_import_kairos_only_through_tab_api(self):
        for path in (REPO / "tabs").rglob("*.py"):
            with self.subTest(file=str(path.relative_to(REPO))):
                check_python(path.read_text(encoding="utf-8"), str(path))


if __name__ == "__main__":
    unittest.main()
