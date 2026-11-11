#!/usr/bin/env python3
"""Fail fast on architectural regressions in the POPO core.

Rules enforced:

* no function longer than 300 lines;
* no module longer than 700 lines in ``popo/``;
* no module-level environment read (settings are built, never snapshotted);
* no import cycle among core packages.

This is a guardrail, not a proof. It catches the two failure modes that made
V1 unmaintainable: a function that grows into a program, and a module that
becomes the program.
"""
from __future__ import annotations
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "popo"
MAX_FUNCTION_LINES = 300
MAX_MODULE_LINES = 700
FAILURES: list[str] = []


def _display(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def inspect_file(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    rel = _display(path)
    line_count = len(text.splitlines())
    if line_count > MAX_MODULE_LINES:
        FAILURES.append(f"{rel}: module is {line_count} lines (limit {MAX_MODULE_LINES})")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        FAILURES.append(f"{rel}: syntax error: {exc}")
        return
    function_ranges = [(node.lineno, getattr(node, "end_lineno", node.lineno))
                       for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            size = end - node.lineno + 1
            if size > MAX_FUNCTION_LINES:
                FAILURES.append(f"{rel}:{node.lineno}: function {node.name} is {size} "
                                f"lines (limit {MAX_FUNCTION_LINES})")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            is_environ = (node.func.attr in {"get", "getenv"}
                          and isinstance(node.func.value, ast.Attribute)
                          and isinstance(node.func.value.value, ast.Name)
                          and node.func.value.value.id == "os"
                          and node.func.value.attr == "environ")
            if is_environ and not any(start <= node.lineno <= end
                                      for start, end in function_ranges):
                FAILURES.append(f"{rel}:{node.lineno}: module-level env read")


def _imports_of(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def module_name(path: pathlib.Path) -> str:
    parts = list(path.with_suffix("").relative_to(ROOT).parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def check_import_cycles(files: list[pathlib.Path]) -> None:
    parsed = {module_name(path): _imports_of(ast.parse(path.read_text(encoding="utf-8")))
              for path in files}
    known = set(parsed)
    # Only edges between modules that are actually part of this tree can form a
    # cycle; stdlib and third-party imports are leaves.
    graph = {name: {target for target in imports if target in known}
             for name, imports in parsed.items()}

    def reaches(start: str, node: str, seen: frozenset[str]) -> bool:
        for target in graph.get(node, ()):
            if target == start:
                return True
            if target in seen or target not in graph:
                continue
            if reaches(start, target, seen | {target}):
                return True
        return False

    for name in graph:
        if reaches(name, name, frozenset({name})):
            FAILURES.append(f"import cycle starting at {name}")


def main() -> int:
    files = [path for path in sorted(CORE.rglob("*.py")) if "__pycache__" not in path.parts]
    for path in files:
        inspect_file(path)
    check_import_cycles(files)
    if FAILURES:
        print("ARCHITECTURE CHECK FAILED")
        print("\n".join(f" - {item}" for item in sorted(set(FAILURES))))
        return 1
    print(f"architecture checks passed: {MAX_FUNCTION_LINES}-line function cap, "
          f"{MAX_MODULE_LINES}-line module cap, no module-level env reads, no import cycles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
