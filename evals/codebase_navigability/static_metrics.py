"""How easy is this codebase for an agent to find its way around?

Modelled on Hermes Agent's evals/codebase_navigability/static_metrics.py
(https://github.com/NousResearch/hermes-agent, MIT), rewritten on the standard
library only: Hermes shells out to radon and tiktoken, which JARVIS does not
ship, so complexity comes from Python's own parser here. Offline, deterministic,
no model calls.

It reports the shape of the Python tree (lines split into code, comments,
docstrings and blanks; file and function sizes; cyclomatic complexity; nesting;
if/elif ladders; the first-party import graph and its cycles) and flags the
roadmap's split thresholds: a file over 2,000 lines, a function over 300 lines
or complexity 30, and an if/elif ladder of four or more.

Usage (from the repo root):
    python evals/codebase_navigability/static_metrics.py . head --out out/
    python evals/codebase_navigability/static_metrics.py . head --baseline out/base.json
"""
import argparse
import ast
import io
import json
import os
import sys
import tokenize
from collections import defaultdict

SKIP_DIRS = {".venv", "data", "dist", "runtime", "node_modules", "__pycache__", ".git", "evals"}
FILE_LINES_LIMIT = 2000
FUNC_LINES_LIMIT = 300
FUNC_CC_LIMIT = 30
LADDER_LIMIT = 4


def python_files(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        found += [os.path.join(dirpath, f) for f in sorted(filenames) if f.endswith(".py")]
    return found


def is_test(rel: str) -> bool:
    name = os.path.basename(rel)
    return name.startswith("test_") or rel.replace("\\", "/").startswith("tests/")


def line_kinds(source: str, tree: ast.AST) -> dict:
    """Counts of code, comment-only, docstring and blank lines."""
    lines = source.splitlines()
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    code_lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                tokenize.ENDMARKER):
                code_lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    code_lines -= doc_lines
    blank = sum(1 for line in lines if not line.strip())
    comment = len(lines) - blank - len(code_lines) - len(doc_lines)
    return {"lines": len(lines), "code": len(code_lines), "docstring": len(doc_lines), "comment": max(comment, 0),
            "blank": blank}


_BRANCHES = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.Assert)
_BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(func: ast.AST):
    """Nodes of a function, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def complexity(func: ast.AST) -> int:
    """McCabe cyclomatic complexity, counted the way radon does for the common
    constructs: 1 plus one per branch point, per extra boolean operand, and
    per comprehension loop and condition."""
    cc = 1
    for node in _own_nodes(func):
        if isinstance(node, _BRANCHES):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            cc += 1 + len(node.ifs)
        elif hasattr(ast, "match_case") and isinstance(node, ast.match_case):
            cc += 1
    return cc


def nesting(func: ast.AST) -> int:
    def depth(node, level):
        deepest = level
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_FUNCS, ast.ClassDef)):
                continue
            deepest = max(deepest, depth(child, level + 1 if isinstance(child, _BLOCKS) else level))
        return deepest
    return depth(func, 0)


def ladders(tree: ast.AST) -> list[int]:
    """Length of every if/elif chain (an `if` whose else is a single `if`),
    counted once from its head."""
    tails, lengths = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and id(node) not in tails:
            length, cur = 1, node
            while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
                tails.add(id(cur))
                length += 1
            if length > 1:
                lengths.append(length)
    return lengths


def module_name(rel: str) -> str:
    parts = rel.replace("\\", "/")[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(tree: ast.AST, module: str, known: set[str]) -> set[str]:
    targets = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for name in names:
            while name and name not in known:
                name = name.rpartition(".")[0]
            if name and name != module:
                targets.add(name)
    return targets


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of size > 1 (Tarjan, iterative)."""
    index, low, on_stack, stack, result, counter = {}, {}, set(), [], [], [0]
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter[0]; counter[0] += 1
        stack.append(start); on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]; counter[0] += 1
                    stack.append(child); on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop(); on_stack.discard(member); component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    return result


def measure(root: str) -> dict:
    files = python_files(root)
    rels = [os.path.relpath(f, root) for f in files]
    known = {module_name(r) for r in rels}
    totals = {"source": defaultdict(int), "tests": defaultdict(int)}
    functions, flagged, graph, ladder_lengths, file_sizes = [], [], {}, [], []
    for path, rel in zip(files, rels):
        with open(path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            flagged.append({"kind": "unparseable", "file": rel})
            continue
        group = "tests" if is_test(rel) else "source"
        kinds = line_kinds(source, tree)
        totals[group]["files"] += 1
        for key, value in kinds.items():
            totals[group][key] += value
        if group == "tests":
            continue
        file_sizes.append(kinds["lines"])
        if kinds["lines"] > FILE_LINES_LIMIT:
            flagged.append({"kind": "file_lines", "file": rel, "value": kinds["lines"]})
        module = module_name(rel)
        graph[module] = imports(tree, module, known)
        for length in ladders(tree):
            ladder_lengths.append(length)
            if length >= LADDER_LIMIT:
                flagged.append({"kind": "if_elif_ladder", "file": rel, "value": length})
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = node.end_lineno - node.lineno + 1
                cc = complexity(node)
                functions.append({"file": rel, "name": node.name, "line": node.lineno, "lines": size, "cc": cc,
                                  "nesting": nesting(node)})
                if size > FUNC_LINES_LIMIT:
                    flagged.append({"kind": "function_lines", "file": rel, "name": node.name, "value": size})
                if cc > FUNC_CC_LIMIT:
                    flagged.append({"kind": "function_cc", "file": rel, "name": node.name, "value": cc})
    found_cycles = cycles(graph)

    def pct(values, q):
        values = sorted(values)
        return values[min(len(values) - 1, int(q * len(values)))] if values else 0

    sizes = [f["lines"] for f in functions]
    ccs = [f["cc"] for f in functions]
    return {
        "source": dict(totals["source"]),
        "tests": dict(totals["tests"]),
        "files": {"count": len(file_sizes), "p50_lines": pct(file_sizes, .5), "p90_lines": pct(file_sizes, .9),
                  "max_lines": max(file_sizes, default=0)},
        "functions": {"count": len(functions), "p50_lines": pct(sizes, .5), "p90_lines": pct(sizes, .9),
                      "max_lines": max(sizes, default=0), "cc_avg": round(sum(ccs) / len(ccs), 2) if ccs else 0,
                      "cc_p90": pct(ccs, .9), "cc_max": max(ccs, default=0),
                      "nesting_max": max((f["nesting"] for f in functions), default=0)},
        "ladders": {"count": len(ladder_lengths), "max": max(ladder_lengths, default=0)},
        "imports": {"modules": len(graph), "edges": sum(len(v) for v in graph.values()),
                    "max_fan_out": max((len(v) for v in graph.values()), default=0),
                    "cycles": len(found_cycles), "largest_cycle": max((len(c) for c in found_cycles), default=0),
                    "cycle_members": found_cycles},
        "most_complex": sorted(functions, key=lambda f: -f["cc"])[:10],
        "longest": sorted(functions, key=lambda f: -f["lines"])[:10],
        "flagged": flagged,
    }


def summary_line(label: str, r: dict) -> str:
    s, fn, im = r["source"], r["functions"], r["imports"]
    return (f"{label}: source files={s.get('files', 0)} lines={s.get('lines', 0)} code={s.get('code', 0)} "
            f"functions={fn['count']} fn_p90={fn['p90_lines']} fn_max={fn['max_lines']} cc_avg={fn['cc_avg']} "
            f"cc_max={fn['cc_max']} ladders>={LADDER_LIMIT}={sum(1 for f in r['flagged'] if f['kind'] == 'if_elif_ladder')} "
            f"import_cycles={im['cycles']} (largest {im['largest_cycle']}) flagged={len(r['flagged'])} | "
            f"tests files={r['tests'].get('files', 0)} lines={r['tests'].get('lines', 0)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root")
    parser.add_argument("label")
    parser.add_argument("--out", help="directory to write <label>.json into")
    parser.add_argument("--baseline", help="a previous <label>.json to print deltas against")
    args = parser.parse_args(argv)
    result = measure(args.root)
    print(summary_line(args.label, result))
    for item in result["flagged"]:
        where = f"{item['file']}" + (f" {item['name']}()" if "name" in item else "")
        print(f"  flagged {item['kind']}: {where} = {item.get('value', '')}")
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            base = json.load(f)
        for section in ("files", "functions", "ladders"):
            for key, value in result[section].items():
                before = base.get(section, {}).get(key)
                if isinstance(value, (int, float)) and before is not None and value != before:
                    print(f"  {section}.{key}: {before} -> {value}")
        for key in ("cycles", "largest_cycle", "edges"):
            before, after = base["imports"][key], result["imports"][key]
            if before != after:
                print(f"  imports.{key}: {before} -> {after}")
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, f"{args.label}.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
