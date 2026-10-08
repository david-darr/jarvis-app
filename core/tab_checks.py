"""Static import boundary shared by tab installation and contract tests.

Approval permits arbitrary Python; this check enforces the supported API
import convention, not a sandbox or a guarantee that code is safe.
"""
import ast


def check_python(source, filename="<tab>"):
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            if module == "core" and all(a.name == "tab_api" for a in node.names):
                continue
            names = [module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        for name in names:
            if name.split(".")[0] in ("core", "services", "routes", "app", "tabs", "kairos_tabs") and name != "core.tab_api":
                raise ValueError(f"{filename}:{node.lineno}: import Kairos only through core.tab_api")
    return tree
