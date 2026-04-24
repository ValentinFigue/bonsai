#!/usr/bin/env python3
"""
pyfindunused — find unused Python symbols across the project.

Three detectors:
  --dead-code   Top-level functions/classes with no cross-module refs (default)
  --params      Function parameters never used in the body
  --imports     Imports never used within their file

Usage:
    pyfindunused.py [--dead-code] [--params] [--imports]
                    [--project-root PATH] [--json]
    pyfindunused.py --params backend/src/agent/graph.py

Examples:
    pyfindunused.py                     # run all three detectors
    pyfindunused.py --dead-code         # only cross-module dead functions
    pyfindunused.py --params            # only unused parameters
    pyfindunused.py --imports src/      # unused imports under src/
"""

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from ._common import (
        collect_python_files,
        find_project_root,
        get_lines,
        parse_file,
        path_to_module,
        resolve_relative_import,
    )
else:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from _common import (
        collect_python_files,
        find_project_root,
        get_lines,
        parse_file,
        path_to_module,
        resolve_relative_import,
    )

# Decorators implying framework registration — function is not dead.
_FRAMEWORK_DECORATORS = {
    "get", "post", "put", "delete", "patch",
    "head", "options", "websocket", "route",
    "task", "shared_task", "periodic_task",
    "fixture", "mark",
    "command", "group",
    "on_startup", "on_shutdown", "on_event",
    "receiver", "signal",
    "classmethod", "staticmethod",
    "overload",
}

# Names that are implicitly called by a framework or runtime.
_ENTRY_POINTS = {
    "main", "cli", "app", "create_app", "application",
    "handler", "lambda_handler",
    "upgrade", "downgrade",               # Alembic
    "run_migrations_online", "run_migrations_offline",
    "setUp", "tearDown",                  # unittest
    "setUpClass", "tearDownClass",
    "startup", "shutdown",
}

# Directory names to skip for dead-code detection.
_DEAD_SKIP = {"migrations", "tests", "test", "alembic"}


@dataclass
class UnusedResult:
    filepath: str
    line: int
    kind: str    # dead_code | unused_param | unused_import
    name: str
    detail: str


# ── helpers ───────────────────────────────────────────────────────────────────

def _python_roots(root: Path) -> list[Path]:
    roots = [root]
    try:
        for child in root.iterdir():
            markers = ["pyproject.toml", "setup.py"]
            if child.is_dir() and any((child / m).exists() for m in markers):
                roots.append(child)
    except OSError:
        pass
    return roots


def _file_modules(fpath: Path, py_roots: list[Path]) -> list[str]:
    names = []
    for r in py_roots:
        m = path_to_module(fpath, r)
        if m:
            names.append(m)
    return names


def _decorator_names(node) -> set[str]:
    names: set[str] = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _names_in_stmts(stmts: list) -> set[str]:
    names: set[str] = set()
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                names.add(node.id)
    return names


def _snippet(fpath: Path, lineno: int) -> str:
    lines = get_lines(fpath)
    if lines and 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _skip_for_dead(fpath: Path) -> bool:
    return any(part in _DEAD_SKIP for part in fpath.parts)


# ── dead code ─────────────────────────────────────────────────────────────────

def find_dead_code(
    all_files: list[Path], root: Path
) -> list[UnusedResult]:
    py_roots = _python_roots(root)
    files = [f for f in all_files if not _skip_for_dead(f)]

    # Pass 1: collect top-level public definitions and __all__ per file
    defs: dict[str, tuple[Path, int, str]] = {}  # "mod:name" → (fpath, lineno, name)
    all_exports: dict[Path, set[str]] = {}

    for fpath in files:
        tree = parse_file(fpath)
        if not tree:
            continue
        modules = _file_modules(fpath, py_roots)
        if not modules:
            continue

        exports: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        val = node.value
                        if isinstance(val, (ast.List, ast.Tuple)):
                            for elt in val.elts:
                                if (
                                    isinstance(elt, ast.Constant)
                                    and isinstance(elt.value, str)
                                ):
                                    exports.add(elt.value)
        all_exports[fpath] = exports

        for node in ast.iter_child_nodes(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            name = node.name
            if name.startswith("_"):
                continue
            if name in _ENTRY_POINTS:
                continue
            if name.startswith("test_") or name.endswith("_test"):
                continue
            if _decorator_names(node) & _FRAMEWORK_DECORATORS:
                continue
            for m in modules:
                defs[f"{m}:{name}"] = (fpath, node.lineno, name)

    # Pass 2: build referenced set
    referenced: set[str] = set()

    local_def_names: dict[Path, set[str]] = {}
    for fpath, _, name in defs.values():
        local_def_names.setdefault(fpath, set()).add(name)

    for fpath in all_files:  # scan full project, not just filtered
        tree = parse_file(fpath)
        if not tree:
            continue

        # Cross-file imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    resolved = resolve_relative_import(
                        fpath, root, node.level, node.module
                    )
                else:
                    resolved = node.module
                if not resolved:
                    continue
                for a in node.names:
                    if a.name != "*":
                        referenced.add(f"{resolved}:{a.name}")

        # Intra-file Name references to local definitions
        modules = _file_modules(fpath, py_roots)
        defs_here = local_def_names.get(fpath, set())
        if defs_here and modules:
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in defs_here:
                    for m in modules:
                        referenced.add(f"{m}:{node.id}")

        # __all__ exports
        for exported in all_exports.get(fpath, set()):
            for m in _file_modules(fpath, py_roots):
                referenced.add(f"{m}:{exported}")

    # Collect unreferenced, deduplicating by physical location
    seen: set[tuple[Path, int]] = set()
    results: list[UnusedResult] = []
    for key, (fpath, lineno, name) in sorted(
        defs.items(), key=lambda x: (str(x[1][0]), x[1][1])
    ):
        loc = (fpath, lineno)
        if loc in seen:
            continue
        variants = [k for k, v in defs.items() if v == (fpath, lineno, name)]
        if any(k in referenced for k in variants):
            continue
        seen.add(loc)
        results.append(UnusedResult(
            filepath=str(fpath.relative_to(root)),
            line=lineno,
            kind="dead_code",
            name=name,
            detail=_snippet(fpath, lineno),
        ))
    return results


# ── unused parameters ─────────────────────────────────────────────────────────

def find_unused_params(
    all_files: list[Path], root: Path
) -> list[UnusedResult]:
    results: list[UnusedResult] = []

    for fpath in all_files:
        tree = parse_file(fpath)
        if not tree:
            continue

        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue

            args = node.args
            params = []
            all_args = args.args + args.posonlyargs + args.kwonlyargs
            for arg in all_args:
                if arg.arg in ("self", "cls") or arg.arg.startswith("_"):
                    continue
                params.append((arg.arg, arg.lineno))

            if not params:
                continue

            used = _names_in_stmts(node.body)
            fn_snippet = _snippet(fpath, node.lineno)

            for param_name, param_line in params:
                if param_name not in used:
                    results.append(UnusedResult(
                        filepath=str(fpath.relative_to(root)),
                        line=param_line,
                        kind="unused_param",
                        name=param_name,
                        detail=f"in {node.name}() — {fn_snippet}",
                    ))

    return results


# ── unused imports ────────────────────────────────────────────────────────────

def find_unused_imports(
    all_files: list[Path], root: Path
) -> list[UnusedResult]:
    results: list[UnusedResult] = []

    for fpath in all_files:
        tree = parse_file(fpath)
        if not tree:
            continue

        # Collect names imported inside TYPE_CHECKING blocks
        type_checking_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            is_tc = (
                isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
            ) or (
                isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"
            )
            if not is_tc:
                continue
            for child in ast.walk(node):
                if isinstance(child, (ast.ImportFrom, ast.Import)):
                    for a in child.names:
                        type_checking_names.add(a.asname or a.name)

        imports: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    local = a.asname or a.name
                    if local not in type_checking_names:
                        imports.append(
                            (local, node.lineno, _snippet(fpath, node.lineno))
                        )
            elif isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name
                    if local not in type_checking_names:
                        imports.append(
                            (local, node.lineno, _snippet(fpath, node.lineno))
                        )

        if not imports:
            continue

        import_lines = {ln for (_, ln, _) in imports}
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.lineno not in import_lines:
                used.add(node.id)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value.split("[")[0].strip())

        for local_name, lineno, snippet in imports:
            if local_name not in used:
                results.append(UnusedResult(
                    filepath=str(fpath.relative_to(root)),
                    line=lineno,
                    kind="unused_import",
                    name=local_name,
                    detail=snippet,
                ))

    return results


# ── output ────────────────────────────────────────────────────────────────────

def print_results(results: list[UnusedResult], header: str) -> None:
    if not results:
        print(f"{header}: none found.")
        return
    print(f"\n{header} ({len(results)})")
    for r in results:
        loc = f"{r.filepath}:{r.line}"
        print(f"  {loc:<55}  {r.name:<25}  {r.detail[:55]}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find unused Python symbols."
    )
    parser.add_argument(
        "path", nargs="?",
        help="Limit to a specific file or directory",
    )
    parser.add_argument(
        "--dead-code", action="store_true",
        help="Find unused top-level functions/classes",
    )
    parser.add_argument(
        "--params", action="store_true",
        help="Find unused function parameters",
    )
    parser.add_argument(
        "--imports", action="store_true",
        help="Find unused imports within files",
    )
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    any_flag = args.dead_code or args.params or args.imports
    run_dead = args.dead_code or not any_flag
    run_params = args.params or not any_flag
    run_imports = args.imports or not any_flag

    start = Path(args.project_root) if args.project_root else Path.cwd()
    root = (
        Path(args.project_root) if args.project_root
        else find_project_root(start)
    )

    if args.path:
        target = Path(args.path)
        all_files = [target] if target.is_file() else collect_python_files(target)
    else:
        all_files = collect_python_files(root)

    results: list[UnusedResult] = []
    if run_dead:
        results += find_dead_code(all_files, root)
    if run_params:
        results += find_unused_params(all_files, root)
    if run_imports:
        results += find_unused_imports(all_files, root)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return

    dead = [r for r in results if r.kind == "dead_code"]
    params = [r for r in results if r.kind == "unused_param"]
    imports_ = [r for r in results if r.kind == "unused_import"]

    if run_dead:
        print_results(dead, "DEAD CODE (no cross-module references)")
    if run_params:
        print_results(params, "UNUSED PARAMETERS")
    if run_imports:
        print_results(imports_, "UNUSED IMPORTS")

    total = len(results)
    print(f"\n{total} issue{'s' if total != 1 else ''} found.")


if __name__ == "__main__":
    main()
